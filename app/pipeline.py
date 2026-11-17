"""平台编排：采信 → 质量 → 按账户求解 → 关账发布。

模块之间只通过本文件衔接，模块内部不互相 import 规则函数；同一组输入得到同一 run_id。

求解按账户分区：账户是完成、确认与发布的最小单位。整批一次求解在账户流水暴涨时
会超时，因此 `run_demo` 只负责把输入采信进来并逐账户推进；发布由 M8 把关，
未确认账户绝不进已发布账区。
"""
from __future__ import annotations

import time

from .manifest import make_run_id, sha256_hex, utc_now_iso
from .modules.m1_ingest import service as m1_service
from .modules.m2_contract import service as m2_service
from .modules.m3_quality import quarantine as quarantine_mod
from .modules.m3_quality import rules as quality_rules
from .modules.m4_candidates import candidate as candidate_mod
from .modules.m8_close import account as account_state
from .modules.m8_close import service as close_service

DEFAULT_BANK = "bank_statement_dirty.csv"
DEFAULT_LEDGER = "ledger_book_dirty.csv"


def _parse(settings, side: str, filename: str, raw: bytes) -> tuple[dict, list[dict]]:
    aliases = m2_service.alias_index(side)
    parsed = m1_service.read_and_map(settings, side, filename, raw, aliases)
    records = []
    if not parsed["state"]:
        records = quality_rules.build_records(side, parsed["records"], settings)
    return parsed, records


def _side_meta(parsed: dict) -> dict:
    return {
        "file": parsed["filename"],
        "sha": parsed["sha256"] if "sha256" in parsed else "",
        "bytes": parsed["bytes"],
        "raw": parsed["raw_rows"],
        "state": parsed["state"] or "OK",
        "segments": parsed["segments"],
    }


def _counts(records: list[dict]) -> dict:
    bank = [record for record in records if record["side"] == "bank"]
    ledger = [record for record in records if record["side"] == "ledger"]
    return {
        "bank_raw": len(bank),
        "ledger_raw": len(ledger),
        "bank_valid": sum(1 for record in bank if record["status"] == "valid"),
        "ledger_valid": sum(1 for record in ledger if record["status"] == "valid"),
        "bank_quar": sum(1 for record in bank if record["status"] == "quarantine"),
        "ledger_quar": sum(1 for record in ledger if record["status"] == "quarantine"),
    }


def _run(settings, bank_meta: dict, ledger_meta: dict, records: list[dict],
         status: str, duration_ms: int = 0) -> dict:
    counts = _counts(records)
    params = settings.params()
    return {
        "run_id": make_run_id(bank_meta["sha"], ledger_meta["sha"],
                              settings.rule_version, params),
        "created_at": utc_now_iso(),
        "rule_version": settings.rule_version,
        "params": params,
        "bank_sha256": bank_meta["sha"],
        "ledger_sha256": ledger_meta["sha"],
        "bank_file": bank_meta["file"],
        "ledger_file": ledger_meta["file"],
        "bank_bytes": bank_meta["bytes"],
        "ledger_bytes": ledger_meta["bytes"],
        "bank_raw": counts["bank_raw"],
        "ledger_raw": counts["ledger_raw"],
        "bank_valid": counts["bank_valid"],
        "ledger_valid": counts["ledger_valid"],
        "bank_quar": counts["bank_quar"],
        "ledger_quar": counts["ledger_quar"],
        "segments": bank_meta["segments"] + ledger_meta["segments"],
        "status": status,
        "input_states": {"bank": bank_meta["state"], "ledger": ledger_meta["state"]},
        "duration_ms": duration_ms,
    }


def _summary(settings, store, run: dict) -> dict:
    """摘要只由当前已发布快照拼装，页面、导出与接口看到同一份结果。"""
    revision = close_service.current(store, run)
    states = store.account_state(run["run_id"])
    records = store.records(run["run_id"])
    if revision is None:
        counts = {}
        totals = {}
        groups = 0
        pending = [
            {"account": name, "status": state["status"],
             "label": account_state.label(state["status"]),
             "reason": state.get("reason", "")}
            for name, state in sorted(states.items())
            if state["status"] != account_state.DONE
        ]
        revision_id = None
        completed = "partial"
    else:
        counts = revision["counts"]
        totals = revision["totals"]
        groups = len(store.revision_matches(revision["revision_id"]))
        pending = revision["pending"]
        revision_id = revision["revision_id"]
        completed = revision["status"]
    return {
        "run_id": run["run_id"],
        "revision_id": revision_id,
        "completed": completed,
        "status": run["status"],
        "created_at": run["created_at"],
        "counts": counts,
        "totals": totals,
        "matches": groups,
        "candidates": sum(
            state["candidates"] for state in states.values()
            if state["status"] == account_state.DONE
        ),
        "accounts_total": len(states),
        "accounts_done": sum(1 for state in states.values()
                             if state["status"] == account_state.DONE),
        "pending": pending,
        "quarantine_distribution": quarantine_mod.distribution(records),
        "input_states": run["input_states"],
        "bank_sha256": run["bank_sha256"],
        "ledger_sha256": run["ledger_sha256"],
    }


def _save_and_advance(settings, store, run: dict, records: list[dict],
                      accounts: list[str] | None = None, rounds: int = 1,
                      solve: bool = True, max_accounts: int | None = None) -> dict:
    """写入本次采集的记录，再逐账户推进。

    `save_run` 只重写本次输入的记录与批次行；账户确认状态与关账快照在独立表里，
    因此重跑不会覆盖已确认账户、也不会丢掉已发布结果。

    `solve=False` 只采信并登记账户，不求解：采信可以立即返回，求解交给
    `/api/close/advance` 分批推进。
    """
    store.save_run(run, records)
    if not solve:
        for name in candidate_mod.account_names(records):
            if store.account_status(run["run_id"], name) is None:
                store.save_account_result(run["run_id"], name,
                                          account_state.PENDING, [], result_sha="",
                                          nodes=0, candidates=0)
        return {
            "run_id": run["run_id"],
            "computed": [],
            "deferred": [],
            "conflicts": [],
            "accounts_done": 0,
            "accounts_total": len(store.account_state(run["run_id"])),
            "pending": [],
            "revision": None,
            "revision_id": store.published_revision_id(run["run_id"]),
            "duration_ms": 0,
            "solved": False,
        }
    store.reset_pending_accounts(run["run_id"])
    return close_service.advance(settings, store, run, accounts=accounts, rounds=rounds,
                                 max_accounts=max_accounts)


def run_demo(settings, store, bank_name: str = DEFAULT_BANK,
             ledger_name: str = DEFAULT_LEDGER, rounds: int = 1,
             solve: bool = True, max_accounts: int | None = None) -> dict:
    started = time.monotonic()
    bank_file, bank_raw = m1_service.resolve_sample(settings, bank_name)
    ledger_file, ledger_raw = m1_service.resolve_sample(settings, ledger_name)
    parsed_bank, bank_records = _parse(settings, "bank", bank_file, bank_raw)
    parsed_ledger, ledger_records = _parse(settings, "ledger", ledger_file, ledger_raw)
    parsed_bank["sha256"] = sha256_hex(bank_raw)
    parsed_ledger["sha256"] = sha256_hex(ledger_raw)
    records = bank_records + ledger_records
    duration = int((time.monotonic() - started) * 1000)
    run = _run(settings, _side_meta(parsed_bank), _side_meta(parsed_ledger),
               records, "matched", duration)
    stored = store.get_run(run["run_id"])
    if stored:
        # 同一输入重跑命中同一批次，保留首跑时间，避免批次历史里时间漂移。
        run["created_at"] = stored["created_at"]
    _save_and_advance(settings, store, run, records, rounds=rounds,
                      solve=solve, max_accounts=max_accounts)
    return _summary(settings, store, store.get_run(run["run_id"]))


def import_side(settings, store, side: str, filename: str, raw: bytes,
                rounds: int = 1, solve: bool = True,
                max_accounts: int | None = None) -> dict:
    started = time.monotonic()
    parsed, records = _parse(settings, side, filename, raw)
    parsed_bytes = len(raw)
    meta = _side_meta(parsed)
    meta["bytes"] = parsed_bytes
    meta["sha"] = sha256_hex(raw)

    previous = store.latest_run()
    other = "ledger" if side == "bank" else "bank"
    other_records: list[dict] = []
    other_meta = _empty_meta()
    if previous and previous["status"] in ("partial_bank", "partial_ledger") \
            and previous.get(f"{other}_sha256"):
        other_records = store.records(previous["run_id"], side=other)
        other_meta = {
            "file": previous.get(f"{other}_file", ""),
            "sha": previous.get(f"{other}_sha256", ""),
            "bytes": previous.get(f"{other}_bytes", 0),
            "raw": previous.get(f"{other}_raw", 0),
            "state": previous.get("input_states", {}).get(other, "OK"),
            "segments": previous.get("segments", 0),
        }

    if side == "bank":
        bank_meta, ledger_meta = meta, other_meta
    else:
        bank_meta, ledger_meta = other_meta, meta
    combined = other_records + records
    both = bool(bank_meta["sha"] and ledger_meta["sha"])
    status = "matched" if both else f"partial_{side}"
    duration = int((time.monotonic() - started) * 1000)
    run = _run(settings, bank_meta, ledger_meta, combined, status, duration)
    stored = store.get_run(run["run_id"])
    if stored:
        run["created_at"] = stored["created_at"]
    advance = _save_and_advance(settings, store, run, combined, rounds=rounds,
                                solve=solve, max_accounts=max_accounts)
    summary = _summary(settings, store, store.get_run(run["run_id"]))
    summary.update({
        "side": side,
        "state": parsed["state"],
        "encoding": parsed["encoding"],
        "bom": parsed["bom"],
        "segments": parsed["segments"],
        "raw_rows": parsed["raw_rows"],
        "valid": sum(1 for record in records if record["status"] == "valid"),
        "quarantine": sum(1 for record in records if record["status"] == "quarantine"),
        "advance": advance,
    })
    return summary


def _empty_meta() -> dict:
    return {"file": "", "sha": "", "bytes": 0, "raw": 0, "state": "", "segments": 0}