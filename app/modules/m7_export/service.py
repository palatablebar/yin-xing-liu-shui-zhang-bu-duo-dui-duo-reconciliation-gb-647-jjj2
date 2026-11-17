"""导出服务：报告 CSV 与运行清单 JSON。

导出与页面共用同一份已发布关账版本：同一 revision_id、同一批匹配组、同一份合计。
未确认账户不进入导出；导出的完成标记与页面顶部的标记逐字相同。
"""
from __future__ import annotations

from ...errors import DomainError
from ..m8_close import view as close_view
from . import csv_out, manifest_out


def _published(store, run: dict, settings) -> tuple[dict, list[dict], dict]:
    current = close_view.revision(store, run)
    if not current:
        raise DomainError("E_NOT_PUBLISHED", "还没有已发布的关账结果", 404)
    groups = store.revision_matches(current["revision_id"])
    reconciliation = close_view.reconciliation(store, run, settings)
    return current, groups, reconciliation


def _scoped_records(store, run: dict, current: dict) -> list[dict]:
    """导出只带已确认账户的记录；待算账户的行不属于已发布账区。"""
    accounts = set(current["accounts"])
    return [
        record for record in store.records(run["run_id"])
        if str(record.get("account") or "") in accounts
    ]


def report(store, run: dict | None, settings) -> str:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    current, groups, reconciliation = _published(store, run, settings)
    completion = close_view.completion(current)
    return csv_out.report_text(
        _scoped_records(store, run, current), groups, reconciliation, completion,
        quarantine_records=store.records(run["run_id"]),
    )


def manifest(store, run: dict | None, settings) -> str:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    current, _groups, reconciliation = _published(store, run, settings)
    completion = close_view.completion(current)
    return manifest_out.manifest_text(run, reconciliation, completion)


def summary(store, run: dict | None, settings) -> dict:
    if not run:
        return {"run_id": None, "counts": {}, "totals": {}, "completion": None}
    current = close_view.revision(store, run)
    if not current:
        return {
            "run_id": run["run_id"],
            "revision_id": None,
            "counts": {},
            "totals": {},
            "completion": close_view.completion(None),
        }
    result = close_view.reconciliation(store, run, settings)
    return {
        "run_id": run["run_id"],
        "revision_id": current["revision_id"],
        "counts": result["counts"],
        "totals": {key: format(value, "f") for key, value in result["totals"].items()},
        "completion": close_view.completion(current),
    }