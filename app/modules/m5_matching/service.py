"""匹配服务：求解、落库、查询。

匹配结果只从当前关账版本读取；求解本身由 M8 按账户推进，未确认账户不进已发布账区。
"""
from __future__ import annotations

from decimal import Decimal

from ...errors import DomainError
from ..m8_close import service as close_service
from ..m8_close import view as close_view


def payload(store, run: dict | None) -> dict:
    if not run:
        return {"run_id": None, "count": 0, "tiers": {}, "groups": [],
                "fee_alloc_total": "0.00", "fee_actual_total": "0.00",
                "revision_id": None, "completion": close_view.completion(None)}
    revision = close_service.current(store, run)
    groups = store.revision_matches(revision["revision_id"]) if revision else []
    tiers = close_view.tier_payload(groups)
    fee_alloc = sum((Decimal(group["fee_alloc"]) for group in groups), Decimal("0.00"))
    fee_actual = sum((Decimal(group["fee_actual"]) for group in groups), Decimal("0.00"))
    return {
        "run_id": run["run_id"],
        "revision_id": revision["revision_id"] if revision else None,
        "count": len(groups),
        "tiers": tiers,
        "groups": groups,
        "fee_alloc_total": format(fee_alloc, "f"),
        "fee_actual_total": format(fee_actual, "f"),
        "completion": close_view.completion(revision),
    }


def run_matching(store, run: dict | None, settings) -> dict:
    """推进未确认账户；已确认账户不重算。"""
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    states = store.account_state(run["run_id"])
    if not states:
        raise DomainError("E_NO_ACCOUNTS", "还没有采信任何账户", 409)
    close_service.advance(settings, store, run)
    return payload(store, store.get_run(run["run_id"]))