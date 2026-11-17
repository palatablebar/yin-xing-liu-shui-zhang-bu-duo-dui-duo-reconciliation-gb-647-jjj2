"""历史：批次清单、关账版本与运行清单。

批次历史同时给每批的关账版本链，方便审计追「哪一版发布了哪些账户」。
"""
from __future__ import annotations

from ...manifest import build_manifest
from ..m8_close import service as close_service


def batches(store, limit: int = 20) -> list[dict]:
    runs = store.list_runs(limit)
    out = []
    for run in runs:
        revision = close_service.current(store, run)
        out.append({
            "run_id": run["run_id"],
            "created_at": run["created_at"],
            "status": run["status"],
            "rule_version": run["rule_version"],
            "bank_valid": run["bank_valid"],
            "ledger_valid": run["ledger_valid"],
            "bank_quarantine": run["bank_quar"],
            "ledger_quarantine": run["ledger_quar"],
            "revision_id": revision["revision_id"] if revision else None,
            "revision_status": revision["status"] if revision else "none",
            "accounts_done": revision["accounts_done"] if revision else 0,
            "accounts_total": revision["accounts_total"] if revision else 0,
            "revisions": store.list_revisions(run["run_id"], 10),
            "manifest": build_manifest(run),
        })
    return out


def manifest(store, run: dict, totals: dict | None = None) -> dict:
    payload = build_manifest(run)
    revision = close_service.current(store, run)
    if revision:
        payload["revision"] = {
            "revision_id": revision["revision_id"],
            "seq": revision["seq"],
            "status": revision["status"],
            "accounts_done": revision["accounts_done"],
            "accounts_total": revision["accounts_total"],
            "accounts": revision["accounts"],
            "pending": revision["pending"],
        }
    if totals:
        payload["totals"] = {key: format(value, "f") if hasattr(value, "as_tuple") else value
                             for key, value in totals.items()}
    return payload