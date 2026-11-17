"""候选服务：按账户生成、落库、查询。

候选不再整批一次算完：候选按账户产生，账户算完即确认。这里只暴露当前已发布
快照覆盖到的账户候选，避免把未确认账户的半份候选混进已发布账区。
"""
from __future__ import annotations

from decimal import Decimal

from ...errors import DomainError
from ..m8_close import service as close_service
from . import candidate


def build(records: list[dict], settings) -> list[dict]:
    return candidate.build(records, settings)


def payload(store, run: dict | None, settings=None) -> dict:
    if not run:
        return {"run_id": None, "count": 0, "tiers": {}, "candidates": [],
                "revision_id": None, "accounts": []}
    revision = close_service.current(store, run)
    done = list(revision["accounts"]) if revision else []
    items = store.account_candidates(run["run_id"], done) if done else []
    tiers: dict[str, int] = {}
    for item in items:
        key = str(item["tier"])
        tiers[key] = tiers.get(key, 0) + 1
    return {
        "run_id": run["run_id"],
        "revision_id": revision["revision_id"] if revision else None,
        "count": len(items),
        "tiers": tiers,
        "candidates": [_serialize(item) for item in items],
        "accounts": sorted(done),
    }


def _serialize(item: dict) -> dict:
    """金额与差额对外一律十进制字符串，避免页面/接口出现浮点尾数。"""
    data = dict(item)
    for key in ("bank_sum", "ledger_sum", "diff"):
        value = data.get(key)
        if value is not None:
            data[key] = format(Decimal(str(value)), "f")
    return data


def refresh(store, run: dict | None, settings) -> dict:
    """重新推进未确认账户；已确认账户不重算、不覆盖。"""
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    close_service.advance(settings, store, run)
    return payload(store, store.get_run(run["run_id"]), settings)


def refresh_accounts(store, run: dict | None, settings, accounts: list[str]) -> dict:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    close_service.advance(settings, store, run, accounts=accounts)
    return payload(store, store.get_run(run["run_id"]), settings)