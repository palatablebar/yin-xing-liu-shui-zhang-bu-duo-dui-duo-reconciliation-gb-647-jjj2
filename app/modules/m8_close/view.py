"""已发布账区的只读视图：页面、接口与导出共用同一份快照。

任何读路径都先取 `current()` 的关账版本，再按版本里的匹配组与已存合计展示；
不在这里重新求解、也不从 `matches` 表临时拼一批结果。这样页面与导出看到的是
同一份带完成标记的关账结果，未确认账户不会混进来。
"""
from __future__ import annotations

from decimal import Decimal

from ...config import dec
from ..m6_diff import differences
from ..m5_matching.subset import TIER_NAMES
from . import service as close_service

ZERO = Decimal("0.00")


def revision(store, run: dict | None) -> dict | None:
    return close_service.current(store, run)


def matches(store, run: dict | None) -> list[dict]:
    """已发布匹配组；没有发布版本时为空，绝不回落到未确认结果。"""
    current = revision(store, run)
    if not current:
        return []
    return store.revision_matches(current["revision_id"])


def reconciliation(store, run: dict | None, settings) -> dict | None:
    """对账结论：范围只限已发布版本里的已确认账户。

    记录与匹配组都按 `revision.accounts` 过滤后再算，待算账户的行不会以
    「未匹配」形式漏进合计；合计与计数最后以快照为准逐字覆盖。
    """
    current = revision(store, run)
    if not current or not run:
        return None
    accounts = set(current["accounts"])
    records = [
        record for record in store.records(run["run_id"])
        if str(record.get("account") or "") in accounts
    ]
    groups = store.revision_matches(current["revision_id"])
    result = differences.reconcile(records, groups, settings)
    # 隔离行是采信证据，按全量记录统计，不随账户是否确认而消失。
    all_view = differences.reconcile(store.records(run["run_id"]), groups, settings)
    # 以快照为准覆盖合计与计数，保证页面/导出与关账版本逐字一致。
    result["totals"] = {key: dec(value) for key, value in current["totals"].items()}
    result["counts"] = dict(current["counts"])
    result["counts"]["bank_quarantine"] = all_view["counts"]["bank_quarantine"]
    result["counts"]["ledger_quarantine"] = all_view["counts"]["ledger_quarantine"]
    result["revision"] = current
    return result


def completion(current: dict | None) -> dict:
    """完成标记：页面与导出在顶部展示同一份。"""
    if not current:
        return {
            "revision_id": None,
            "status": "none",
            "label": "未关账",
            "accounts_done": 0,
            "accounts_total": 0,
            "pending": [],
            "complete": False,
        }
    done = current["accounts_done"]
    total = current["accounts_total"]
    if current["status"] == "closed":
        label = f"已关账 {done}/{total} 账户"
    else:
        label = f"分批关账 {done}/{total} 账户"
    return {
        "revision_id": current["revision_id"],
        "seq": current["seq"],
        "status": current["status"],
        "label": label,
        "accounts_done": done,
        "accounts_total": total,
        "pending": current["pending"],
        "created_at": current["created_at"],
        "complete": current["status"] == "closed",
    }


def fee_total(groups: list[dict]) -> Decimal:
    return sum((Decimal(group["fee_alloc"]) for group in groups), ZERO)


def tier_payload(groups: list[dict]) -> dict:
    tiers: dict[str, int] = {}
    for group in groups:
        key = str(group["tier"])
        tiers[key] = tiers.get(key, 0) + 1
    return tiers


def tier_names() -> dict:
    return TIER_NAMES
