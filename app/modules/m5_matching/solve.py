"""匹配求解：候选排序 → 不重叠挑选 → 组内合计与手续费分摊。

求解按账户隔离：账户是完成、确认与发布的最小单位。

- 不同账户的行永远不组成同一匹配组；跨账户的金额巧合不是对账依据。
- 手续费分摊在**账户内**完成：某账户的 `fee_alloc` 只取决于本账户的组与手续费，
  后续账户算完不会改变已发布账户的每一分钱。全局合计仍满足分摊守恒。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from ...config import dec
from ..m4_candidates import window
from . import fees, subset


def _match_id(bank_ids: list[str], ledger_ids: list[str]) -> str:
    key = hashlib.sha1(
        f"{','.join(bank_ids)}|{','.join(ledger_ids)}".encode("utf-8")
    ).hexdigest()[:10]
    return f"m-{key}"


def _index(records: list[dict], side: str) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for record in records:
        if record["side"] != side or record["status"] != "valid":
            continue
        table[record["txn_no"]] = {
            "txn_no": record["txn_no"],
            "account": str(record.get("account") or ""),
            "amount": dec(record["amount"]),
            "fee": dec(record["fee"]),
            "direction": int(record["direction"]),
            "posted_at": window.parse_instant(record["posted_at"]),
        }
    return table


def candidate_account(candidate: dict, bank: dict[str, dict],
                      ledger: dict[str, dict]) -> str:
    """候选的账户：优先取银行侧成员，其次账簿侧；两侧必须一致。"""
    accounts = {bank[key]["account"] for key in candidate["bank_ids"] if key in bank}
    accounts |= {ledger[key]["account"] for key in candidate["ledger_ids"] if key in ledger}
    if len(accounts) > 1:
        raise ValueError("候选跨越了多个账户")
    return next(iter(accounts), "")


def _groups_for(chosen: list[dict], bank: dict[str, dict], ledger: dict[str, dict],
                settings) -> list[dict]:
    groups: list[dict] = []
    for candidate in chosen:
        bank_rows = [bank[key] for key in candidate["bank_ids"] if key in bank]
        ledger_rows = [ledger[key] for key in candidate["ledger_ids"] if key in ledger]
        if len(bank_rows) != len(candidate["bank_ids"]) \
                or len(ledger_rows) != len(candidate["ledger_ids"]):
            continue
        bank_sum = sum((row["amount"] for row in bank_rows), Decimal("0.00"))
        ledger_sum = sum((row["amount"] for row in ledger_rows), Decimal("0.00"))
        bank_signed = sum((row["amount"] * row["direction"] for row in bank_rows), Decimal("0.00"))
        ledger_signed = sum((row["amount"] * row["direction"] for row in ledger_rows), Decimal("0.00"))
        groups.append(
            {
                "match_id": _match_id(candidate["bank_ids"], candidate["ledger_ids"]),
                "account": candidate_account(candidate, bank, ledger),
                "tier": int(candidate["tier"]),
                "bank_ids": list(candidate["bank_ids"]),
                "ledger_ids": list(candidate["ledger_ids"]),
                "bank_sum": bank_sum,
                "ledger_sum": ledger_sum,
                "diff": abs(bank_sum - ledger_sum).quantize(settings.fee_quantum),
                "day_gap": int(candidate["day_gap"]),
                "bank_signed": bank_signed,
                "ledger_signed": ledger_signed,
                "fee_actual": sum((row["fee"] for row in bank_rows), Decimal("0.00")),
            }
        )
    return groups


def allocate_fees(groups: list[dict], settings) -> None:
    """账户内按银行签向金额比例分摊实际手续费，尾差用最大余额法。"""
    if not groups:
        return
    fee_total = sum((group["fee_actual"] for group in groups), Decimal("0.00"))
    allocations = fees.allocate(
        fee_total,
        [abs(group["bank_signed"]) for group in groups],
        settings.fee_quantum,
        [group["match_id"] for group in groups],
    )
    for group, allocated in zip(groups, allocations):
        group["fee_alloc"] = allocated


def solve_scoped(candidates: list[dict], records: list[dict], settings,
                 account: str | None = None) -> list[dict]:
    """在给定候选集合内求解；`account` 给定时只保留该账户的候选与行。"""
    bank = _index(records, "bank")
    ledger = _index(records, "ledger")
    if account is not None:
        bank = {key: row for key, row in bank.items() if row["account"] == account}
        ledger = {key: row for key, row in ledger.items() if row["account"] == account}
        scoped = []
        for candidate in candidates:
            if candidate_account(candidate, bank, ledger) == account:
                scoped.append(candidate)
        candidates = scoped
    ordered = sorted(candidates, key=subset.priority_key)
    chosen = subset.choose_disjoint(ordered)
    groups = _groups_for(chosen, bank, ledger, settings)

    # 账户内分摊，保证已发布账户的金额不随后续账户变化。
    by_account: dict[str, list[dict]] = {}
    for group in groups:
        by_account.setdefault(group["account"], []).append(group)
    for account_groups in by_account.values():
        account_groups.sort(key=lambda group: (group["tier"], group["match_id"]))
        allocate_fees(account_groups, settings)

    groups.sort(key=lambda group: (group["tier"], group["match_id"]))
    return groups


def solve(candidates: list[dict], records: list[dict], settings,
          account: str | None = None) -> list[dict]:
    return solve_scoped(candidates, records, settings, account)