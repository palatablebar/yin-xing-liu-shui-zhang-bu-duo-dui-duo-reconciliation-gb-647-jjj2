"""按账户的候选生成：一对一、一对多（账簿子集和）、多对一（银行子集和）。

- 候选只在同一账户内产生：跨账户的金额巧合不构成对账依据；
- 金额差 ≤ 容差、日期差 ≤ 窗口、方向相同；
- 子集最多 4 条，按金额降序枚举，超过目标 + 容差的分支不再递归；
- 手续费不参与匹配金额，只随银行行进入成本统计。

求解按层级推进：先用一对一占住行，再只在剩余行上枚举一对多、多对一。
这与「先生成全部候选再按层级贪心择一」等价——被一对一占住的行本来就不会再进
高层级组合——但把子集枚举的规模从「全账户」降到「一对一之后的残差」。

搜索工作量用**节点数**计量而不是墙钟：同一输入、同一预算得到同一结果。节点预算在
`account.py` 里按账户执行，耗尽即把该账户整体判为 timeout，绝不落半份候选。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from ...config import dec
from . import window

QUANT = Decimal("0.01")


class BudgetExceeded(Exception):
    """账户级搜索预算耗尽；调用方据此把账户整体延后。"""


class Budget:
    """确定性节点预算。每次展开一个组合或对比一对行都计 1 个节点。"""

    def __init__(self, limit: int) -> None:
        self.limit = int(limit)
        self.used = 0

    def spend(self, nodes: int = 1) -> None:
        self.used += nodes
        if self.used > self.limit:
            raise BudgetExceeded(f"搜索节点超过预算 {self.limit}")

    @property
    def exhausted(self) -> bool:
        return self.used > self.limit


def as_row(record: dict) -> dict:
    return {
        "side": record["side"],
        "txn_no": record["txn_no"],
        "account": record["account"],
        "posted_at": window.parse_instant(record["posted_at"]),
        "direction": int(record["direction"]),
        "amount": dec(record["amount"]),
        "fee": dec(record["fee"]),
    }


def valid_rows(records: list[dict], side: str) -> list[dict]:
    return [as_row(record) for record in records
            if record["status"] == "valid" and record["side"] == side]


def local_days(rows: list[dict], zone) -> list[int]:
    """每行的本地日序数；窗口比较退化为整数差，不再重复做时区换算。"""
    return [window.local_day(row["posted_at"], zone).toordinal() for row in rows]


def bounded_subsets(rows: list[dict], target: Decimal, tolerance: Decimal,
                    max_items: int, budget: Budget | None = None) -> list[list[dict]]:
    ordered = sorted(rows, key=lambda row: (-row["amount"], row["txn_no"]))
    results: list[list[dict]] = []
    total_rows = len(ordered)

    # 只有金额非负时，后缀和才能作为「再加多少都够不到目标」的剪枝依据。
    non_negative = all(row["amount"] >= 0 for row in ordered)
    suffix = [Decimal("0.00")] * (total_rows + 1)
    if non_negative:
        for index in range(total_rows - 1, -1, -1):
            suffix[index] = suffix[index + 1] + ordered[index]["amount"]

    def walk(start: int, chosen: list[dict], total: Decimal) -> None:
        if budget is not None:
            budget.spend()
        if len(chosen) >= 2 and abs(total - target) <= tolerance:
            results.append(list(chosen))
        if len(chosen) >= max_items or start >= total_rows:
            return
        if non_negative and total + suffix[start] < target - tolerance:
            return
        for index in range(start, total_rows):
            if budget is not None:
                budget.spend()
            row = ordered[index]
            next_total = total + row["amount"]
            if non_negative and next_total > target + tolerance:
                continue
            chosen.append(row)
            walk(index + 1, chosen, next_total)
            chosen.pop()

    walk(0, [], Decimal("0.00"))
    return results


def _day_gap(anchor: datetime, rows: list[dict], zone) -> int:
    return max((window.day_gap(anchor, row["posted_at"], zone) for row in rows), default=0)


def _candidate(tier: int, banks: list[dict], ledgers: list[dict], zone) -> dict:
    bank_ids = sorted(row["txn_no"] for row in banks)
    ledger_ids = sorted(row["txn_no"] for row in ledgers)
    bank_sum = sum((row["amount"] for row in banks), Decimal("0.00"))
    ledger_sum = sum((row["amount"] for row in ledgers), Decimal("0.00"))
    diff = abs(bank_sum - ledger_sum).quantize(QUANT)
    if tier == 2:
        gap = _day_gap(banks[0]["posted_at"], ledgers, zone)
    else:
        gap = _day_gap(ledgers[0]["posted_at"], banks, zone) if tier == 3 \
            else _day_gap(banks[0]["posted_at"], ledgers, zone)
    key = hashlib.sha1(
        f"{tier}|{','.join(bank_ids)}|{','.join(ledger_ids)}".encode("utf-8")
    ).hexdigest()[:10]
    return {
        "cand_id": f"c{tier}-{key}",
        "tier": tier,
        "bank_ids": bank_ids,
        "ledger_ids": ledger_ids,
        "bank_sum": bank_sum,
        "ledger_sum": ledger_sum,
        "diff": diff,
        "day_gap": gap,
        "account": banks[0]["account"] if banks else "",
    }


def _tier1(bank, bank_days, ledger, ledger_days, settings, budget: Budget) -> list[dict]:
    zone = window.tzinfo(settings.tz_name)
    tolerance = settings.tolerance
    window_days = settings.window_days
    cands: list[dict] = []
    # 按本地日分桶，只比较窗口内的桶，避免全笛卡尔积。
    buckets: dict[int, list[tuple[dict, int]]] = {}
    for row, day in zip(ledger, ledger_days):
        buckets.setdefault(day, []).append((row, day))
    for bank_row, bank_day in zip(bank, bank_days):
        for delta in range(-window_days, window_days + 1):
            for ledger_row, _day in buckets.get(bank_day + delta, ()):
                if bank_row["direction"] != ledger_row["direction"]:
                    continue
                budget.spend()
                if abs(bank_row["amount"] - ledger_row["amount"]) <= tolerance:
                    cands.append(_candidate(1, [bank_row], [ledger_row], zone))
    return cands


def _tier_bank_many(bank, bank_days, ledger, ledger_days, settings, budget: Budget) -> list[dict]:
    """一对多：一笔银行流水对多张账簿凭证。"""
    zone = window.tzinfo(settings.tz_name)
    tolerance = settings.tolerance
    window_days = settings.window_days
    cands: list[dict] = []
    buckets: dict[int, list[dict]] = {}
    for row, day in zip(ledger, ledger_days):
        buckets.setdefault(day, []).append(row)
    for bank_row, bank_day in zip(bank, bank_days):
        pool = [
            row
            for delta in range(-window_days, window_days + 1)
            for row in buckets.get(bank_day + delta, ())
            if row["direction"] == bank_row["direction"]
        ]
        if len(pool) < 2:
            continue
        for combo in bounded_subsets(pool, bank_row["amount"], tolerance,
                                     settings.subset_max, budget):
            cands.append(_candidate(2, [bank_row], combo, zone))
    return cands


def _tier_ledger_many(bank, bank_days, ledger, ledger_days, settings, budget: Budget) -> list[dict]:
    """多对一：多笔银行流水对一张账簿凭证。"""
    zone = window.tzinfo(settings.tz_name)
    tolerance = settings.tolerance
    window_days = settings.window_days
    cands: list[dict] = []
    buckets: dict[int, list[dict]] = {}
    for row, day in zip(bank, bank_days):
        buckets.setdefault(day, []).append(row)
    for ledger_row, ledger_day in zip(ledger, ledger_days):
        pool = [
            row
            for delta in range(-window_days, window_days + 1)
            for row in buckets.get(ledger_day + delta, ())
            if row["direction"] == ledger_row["direction"]
        ]
        if len(pool) < 2:
            continue
        for combo in bounded_subsets(pool, ledger_row["amount"], tolerance,
                                     settings.subset_max, budget):
            cands.append(_candidate(3, combo, [ledger_row], zone))
    return cands


def sort_candidates(results: list[dict]) -> list[dict]:
    results.sort(
        key=lambda item: (
            item["tier"],
            item["diff"],
            item["day_gap"],
            ",".join(item["bank_ids"]),
            ",".join(item["ledger_ids"]),
        )
    )
    return results


def sort_all(results: list[dict]) -> list[dict]:
    """跨账户汇总后的稳定排序：账户优先，再按层级、差额、日期差与业务号。"""
    results.sort(
        key=lambda item: (
            item.get("account", ""),
            item["tier"],
            item["diff"],
            item["day_gap"],
            ",".join(item["bank_ids"]),
            ",".join(item["ledger_ids"]),
        )
    )
    return results


def build_for_account(bank: list[dict], ledger: list[dict], settings,
                      budget: Budget) -> list[dict]:
    """单账户三层候选：逐层推进，高层级只在低层级未占用的残差行上枚举。

    与「全量生成 + 按层级贪心择一」等价，因为被前一层级选中的行不会再进更高层级；
    但避免了对已被一对一占住的行做昂贵的子集枚举。
    """
    from ..m5_matching import subset

    zone = window.tzinfo(settings.tz_name)
    bank_days = local_days(bank, zone)
    ledger_days = local_days(ledger, zone)

    tier1 = sort_candidates(_tier1(bank, bank_days, ledger, ledger_days, settings, budget))
    chosen1 = subset.choose_disjoint(tier1)
    used_bank = {key for cand in chosen1 for key in cand["bank_ids"]}
    used_ledger = {key for cand in chosen1 for key in cand["ledger_ids"]}

    residual_bank = [(row, day) for row, day in zip(bank, bank_days)
                     if row["txn_no"] not in used_bank]
    residual_ledger = [(row, day) for row, day in zip(ledger, ledger_days)
                       if row["txn_no"] not in used_ledger]
    rb = [item[0] for item in residual_bank]
    rbd = [item[1] for item in residual_bank]
    rl = [item[0] for item in residual_ledger]
    rld = [item[1] for item in residual_ledger]

    tier2: list[dict] = []
    if rb and rl:
        tier2 = sort_candidates(_tier_bank_many(rb, rbd, rl, rld, settings, budget))
        for cand in subset.choose_disjoint(tier2):
            used_bank.update(cand["bank_ids"])
            used_ledger.update(cand["ledger_ids"])

    residual_bank = [(row, day) for row, day in zip(bank, bank_days)
                     if row["txn_no"] not in used_bank]
    residual_ledger = [(row, day) for row, day in zip(ledger, ledger_days)
                       if row["txn_no"] not in used_ledger]
    rb = [item[0] for item in residual_bank]
    rbd = [item[1] for item in residual_bank]
    rl = [item[0] for item in residual_ledger]
    rld = [item[1] for item in residual_ledger]

    tier3: list[dict] = []
    if rb and rl:
        tier3 = sort_candidates(_tier_ledger_many(rb, rbd, rl, rld, settings, budget))

    return sort_candidates(chosen1 + tier2 + tier3)


def build(records: list[dict], settings, budget: Budget | None = None) -> list[dict]:
    """兼容入口：按账户分区后逐账户生成，结果按账户名与大整数业务号稳定排序。"""
    bank = valid_rows(records, "bank")
    ledger = valid_rows(records, "ledger")
    budget = budget or Budget(settings.solve_budget)
    results: list[dict] = []
    for account in account_names(records):
        account_bank = [row for row in bank if row["account"] == account]
        account_ledger = [row for row in ledger if row["account"] == account]
        if not account_bank or not account_ledger:
            continue
        results.extend(build_for_account(account_bank, account_ledger, settings, budget))
    return sort_all(results)


def account_names(records: list[dict]) -> list[str]:
    """参与对账的账户：至少有一条有效行的账户。

    只有隔离行的账户（如未登记账户）不进完成清单——它们没有可对账的行，
    证据留在隔离区；把它们算进「未算完」会让关账永远差几个账户。
    """
    names = {
        str(record.get("account") or "")
        for record in records
        if record.get("status") == "valid"
    }
    return sorted(names)


def account_records(records: list[dict], account: str) -> list[dict]:
    return [record for record in records if str(record.get("account") or "") == account]