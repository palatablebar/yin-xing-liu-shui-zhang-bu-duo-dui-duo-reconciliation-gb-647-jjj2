"""按账户编排求解与分批发布。

一轮 `advance` 做四件事，顺序固定：

1. 找出现在可进入发布的账户：本批已确认 + 库里已确认的账户；
2. 只对 `pending` / `timeout` / `failed` 的账户计算，每算完一个账户立即落库并标记；
3. 已确认账户的结果按指纹比对，结果不同直接拒绝覆盖，绝不静默改账；
4. 若已完成账户集合比上次发布有变化，写一份新的关账快照并原子切换发布指针。

中断安全：每个账户的结果与状态在同一个事务里写入，进程被杀最多丢掉「正在算的那一个
账户」，已确认的账户与已发布快照都保持完整；重跑只重算非 done 账户。
"""
from __future__ import annotations

import time

from ...manifest import utc_now_iso
from ..m4_candidates import candidate as candidate_mod
from ..m5_matching import solve as solve_mod
from . import account as account_state
from . import snapshot

DEFAULT_ROUNDS = 1


class ConfirmConflict(Exception):
    """已确认账户被算出不同结果；拒绝覆盖，等待人工处理。"""

    def __init__(self, account: str) -> None:
        super().__init__(f"账户 {account} 已确认，重算结果不同，拒绝覆盖")
        self.account = account


def _records_for(records: list[dict], account: str) -> tuple[list[dict], list[dict]]:
    bank = [
        record for record in records
        if record["side"] == "bank" and record["status"] == "valid"
        and str(record.get("account") or "") == account
    ]
    ledger = [
        record for record in records
        if record["side"] == "ledger" and record["status"] == "valid"
        and str(record.get("account") or "") == account
    ]
    return bank, ledger


def solve_account(settings, records: list[dict], account: str) -> dict:
    """求解单个账户，返回候选、匹配组、节点数与结果指纹；预算耗尽抛 BudgetExceeded。"""
    bank_records, ledger_records = _records_for(records, account)
    if not bank_records and not ledger_records:
        return {"candidates": [], "groups": [], "nodes": 0,
                "sha": snapshot.result_sha([], account)}
    budget = candidate_mod.Budget(settings.solve_budget)
    bank = candidate_mod.valid_rows(bank_records, "bank")
    ledger = candidate_mod.valid_rows(ledger_records, "ledger")
    candidates = candidate_mod.build_for_account(bank, ledger, settings, budget) \
        if bank and ledger else []
    groups = solve_mod.solve_scoped(candidates, bank_records + ledger_records, settings,
                                    account) if candidates else []
    return {
        "candidates": candidates,
        "groups": groups,
        "nodes": budget.used,
        "sha": snapshot.result_sha(groups, account),
    }


def _store_result(store, run_id: str, account: str, outcome: dict) -> bool:
    """写入账户结果；已确认账户被拒绝覆盖时抛 ConfirmConflict。"""
    ok = store.save_account_result(
        run_id, account, account_state.DONE, outcome["groups"],
        result_sha=outcome["sha"], nodes=outcome["nodes"],
        candidates=len(outcome["candidates"]), computed_at=utc_now_iso(),
        candidate_rows=outcome["candidates"],
    )
    if not ok:
        raise ConfirmConflict(account)
    return ok


def advance(settings, store, run: dict, *, accounts: list[str] | None = None,
            rounds: int = DEFAULT_ROUNDS, max_accounts: int | None = None,
            republish: bool = True) -> dict:
    """推进账户求解；返回本轮结果摘要。

    `max_accounts` 限制本次调用最多算完几个账户，用来把一次请求压在可接受的时长内：
    财务先采信（瞬时返回），再分批调用本接口，每批完成即发布一版。
    """
    started = time.monotonic()
    run_id = run["run_id"]
    records = store.records(run_id)
    states = store.account_state(run_id)

    names = accounts if accounts is not None else candidate_mod.account_names(records)
    for name in names:
        if name not in states:
            store.save_account_result(run_id, name, account_state.PENDING, [],
                                      result_sha="", nodes=0, candidates=0)
    states = store.account_state(run_id)

    # 只有两侧都已采信才可能对账：单侧导入时账户保持 pending，
    # 不能把「没有账簿可比」当成「账户已算完」确认下来。
    both_sides = bool(run.get("bank_sha256") and run.get("ledger_sha256"))
    if not both_sides:
        assembled = _assemble(settings, store, run_id, records)
        return {
            "run_id": run_id,
            "computed": [],
            "deferred": [{"account": name, "status": account_state.PENDING,
                          "reason": "E_WAITING_OTHER_SIDE"}
                         for name in names],
            "conflicts": [],
            "accounts_done": assembled["accounts_done"],
            "accounts_total": len(states),
            "pending": assembled["pending"],
            "revision": None,
            "revision_id": store.published_revision_id(run_id),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "waiting_other_side": True,
        }

    computed: list[str] = []
    deferred: list[dict] = []
    conflicts: list[dict] = []
    remaining_budget = max_accounts if max_accounts is not None else None
    # 同一次调用里已判延后/失败的账户不再重试：预算是确定性的，重试不会改变结果。
    settled: set[str] = set()

    for _round in range(max(1, rounds)):
        progressed = False
        for name in names:
            if remaining_budget is not None and remaining_budget <= 0:
                break
            if name in settled:
                continue
            state = store.account_state(run_id).get(name, {})
            if state.get("status") == account_state.DONE:
                # 已确认账户默认不重算；显式点名时只做一致性核对，绝不覆盖。
                if accounts is None:
                    continue
            try:
                outcome = solve_account(settings, records, name)
            except candidate_mod.BudgetExceeded as exc:
                store.save_account_result(
                    run_id, name, account_state.TIMEOUT, [], result_sha="",
                    nodes=settings.solve_budget, candidates=0,
                    reason=str(exc), computed_at=utc_now_iso(), reset=True,
                )
                deferred.append({"account": name, "status": account_state.TIMEOUT,
                                 "reason": str(exc)})
                settled.add(name)
                progressed = True
                continue
            except Exception as exc:  # 单个账户失败不拖垮整批
                store.save_account_result(
                    run_id, name, account_state.FAILED, [], result_sha="",
                    nodes=0, candidates=0, reason=str(exc),
                    computed_at=utc_now_iso(), reset=True,
                )
                deferred.append({"account": name, "status": account_state.FAILED,
                                 "reason": str(exc)})
                settled.add(name)
                progressed = True
                continue
            try:
                _store_result(store, run_id, name, outcome)
            except ConfirmConflict as exc:
                conflicts.append({"account": name, "reason": str(exc)})
                settled.add(name)
                continue
            if state.get("status") != account_state.DONE:
                computed.append(name)
                progressed = True
                if remaining_budget is not None:
                    remaining_budget -= 1
        if not progressed:
            break

    states = store.account_state(run_id)
    done = sorted(name for name, state in states.items()
                  if state["status"] == account_state.DONE)
    assembled = _assemble(settings, store, run_id, records)
    revision = None
    if republish and _should_publish(store, run_id, assembled):
        revision = publish(settings, store, run, assembled)

    duration = int((time.monotonic() - started) * 1000)
    return {
        "run_id": run_id,
        "computed": computed,
        "deferred": deferred,
        "conflicts": conflicts,
        "accounts_done": len(done),
        "accounts_total": len(states),
        "pending": assembled["pending"],
        "revision": revision,
        "revision_id": revision["revision_id"] if revision else
        store.published_revision_id(run_id),
        "duration_ms": duration,
    }


def _assemble(settings, store, run_id: str, records: list[dict]) -> dict:
    states = store.account_state(run_id)
    done = sorted(name for name, state in states.items()
                  if state["status"] == account_state.DONE)
    account_groups = {name: store.account_matches(run_id, name) for name in done}
    return snapshot.assemble(settings, records, states, account_groups)


def _should_publish(store, run_id: str, assembled: dict) -> bool:
    published_id = store.published_revision_id(run_id)
    if not published_id:
        # 只在确实有关账内容时发布第一版；全 pending 的空壳不占版本号。
        return assembled["accounts_done"] > 0
    published = store.get_revision(published_id)
    if not published:
        return True
    return published["accounts"] != assembled["accounts"] or \
        published["status"] != assembled["status"]


def publish(settings, store, run: dict, assembled: dict) -> dict:
    """写一份新的关账快照并切换发布指针。"""
    revision_id = f"rev-{run['run_id'][4:]}-{store.next_revision_seq(run['run_id']):03d}"
    revision = {
        "revision_id": revision_id,
        "run_id": run["run_id"],
        "seq": store.next_revision_seq(run["run_id"]),
        "created_at": utc_now_iso(),
        "rule_version": run["rule_version"],
        "status": assembled["status"],
        "accounts_total": assembled["accounts_total"],
        "accounts_done": assembled["accounts_done"],
        "accounts": assembled["accounts"],
        "pending": assembled["pending"],
        "totals": assembled["totals"],
        "counts": assembled["counts"],
        "duration_ms": run.get("duration_ms", 0),
    }
    store.save_revision(revision, assembled["groups"])
    return revision


def current(store, run: dict | None) -> dict | None:
    """当前已发布快照；没有则返回 None。"""
    if not run:
        return None
    revision_id = store.published_revision_id(run["run_id"])
    if not revision_id:
        return None
    return store.get_revision(revision_id)


def is_complete(store, run: dict | None) -> bool:
    revision = current(store, run)
    return bool(revision and revision["status"] == "closed")