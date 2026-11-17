"""差异服务：对账结论、批次历史、单批详情。

对账结论只从当前关账版本读取：未确认账户不进已发布账区，其行也不会被并入
已确认账户的匹配组。
"""
from __future__ import annotations

from ...errors import DomainError
from ..m8_close import view as close_view
from . import differences, history


def payload(store, run: dict | None, settings) -> dict:
    if not run:
        return {
            "run_id": None,
            "revision_id": None,
            "totals": {},
            "counts": {},
            "identities": [],
            "differences": [],
            "tier_names": differences.TIER_NAMES,
            "completion": close_view.completion(None),
        }
    result = close_view.reconciliation(store, run, settings)
    if result is None:
        return {
            "run_id": run["run_id"],
            "revision_id": None,
            "totals": {},
            "counts": {},
            "identities": [],
            "differences": [],
            "tier_names": differences.TIER_NAMES,
            "completion": close_view.completion(None),
        }
    result["run_id"] = run["run_id"]
    result["revision_id"] = result["revision"]["revision_id"]
    result["completion"] = close_view.completion(result["revision"])
    return result


def batches_payload(store) -> dict:
    batch_rows = history.batches(store)
    return {"count": len(batch_rows), "batches": batch_rows}


def batch_detail(store, run_id: str, settings) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise DomainError("E_NO_RUN", "批次不存在", 404)
    result = payload(store, run, settings)
    result["manifest"] = history.manifest(store, run, result.get("totals"))
    return result