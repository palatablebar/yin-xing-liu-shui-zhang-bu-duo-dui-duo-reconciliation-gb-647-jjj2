"""质量服务：隔离区查询、原因分布与 CSV 导出。

隔离行属于采信证据，与是否关账无关：即使账户还没算完，它的隔离行也应该能看到。
但「已发布账区」的完成标记与发布版本一致，页面据此区分哪些账户已入账。
"""
from __future__ import annotations

from ...store import Store
from ..m8_close import service as close_service
from ..m8_close import view as close_view
from . import quarantine


def payload(store: Store, run: dict | None) -> dict:
    if not run:
        return {"run_id": None, "count": 0, "distribution": [], "rows": [],
                "completion": close_view.completion(None)}
    records = store.quarantine(run["run_id"])
    revision = close_service.current(store, run)
    return {
        "run_id": run["run_id"],
        "revision_id": revision["revision_id"] if revision else None,
        "count": len(records),
        "distribution": quarantine.distribution(store.records(run["run_id"])),
        "rows": records,
        "completion": close_view.completion(revision),
    }


def csv_text(store: Store, run: dict | None) -> str:
    if not run:
        return quarantine.csv_text([])
    return quarantine.csv_text(store.records(run["run_id"]))