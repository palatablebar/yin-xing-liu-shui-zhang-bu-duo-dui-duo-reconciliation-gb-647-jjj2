"""关账快照：只由已确认账户拼装，结果确定、可重放。

一份快照 = 已确认账户的全部匹配组 + 只用这些账户的行算出的对账结论。
未算完账户的记录仍留在库里（不丢失），但它们既不进匹配组，也不进合计——
不会以「未匹配」形式混进已发布账区，而是在 `pending` 里显式列出。
"""
from __future__ import annotations

import hashlib
import json

from ..m6_diff import differences
from . import account as account_state

TOTAL_KEYS = (
    "bank_gross", "bank_fee", "bank_total", "bank_matched", "bank_unmatched",
    "ledger_total", "ledger_matched", "ledger_unmatched", "match_gap",
    "fee_alloc_total", "fee_actual_total",
)

COUNT_KEYS = (
    "bank_valid", "bank_matched_rows", "bank_unmatched_rows",
    "ledger_valid", "ledger_matched_rows", "ledger_unmatched_rows",
    "matched_groups", "matched_tier", "bank_quarantine", "ledger_quarantine",
)


def result_sha(groups: list[dict], account: str) -> str:
    """一个账户完整结果的指纹；用于确认后拒绝不同结果覆盖。"""
    payload = [
        {
            "match_id": group["match_id"],
            "tier": group["tier"],
            "bank_ids": group["bank_ids"],
            "ledger_ids": group["ledger_ids"],
            "bank_sum": str(group["bank_sum"]),
            "ledger_sum": str(group["ledger_sum"]),
            "diff": str(group["diff"]),
            "fee_actual": str(group["fee_actual"]),
            "fee_alloc": str(group["fee_alloc"]),
        }
        for group in sorted(groups, key=lambda item: (item["tier"], item["match_id"]))
    ]
    text = json.dumps({"account": account, "groups": payload}, ensure_ascii=False,
                      sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def totals_dict(reconciliation: dict) -> dict:
    return {key: format(reconciliation["totals"][key], "f") for key in TOTAL_KEYS}


def counts_dict(reconciliation: dict) -> dict:
    counts = reconciliation["counts"]
    return {key: counts[key] for key in COUNT_KEYS}


def assemble(settings, records: list[dict], states: dict[str, dict],
             account_groups: dict[str, list[dict]]) -> dict:
    """把已确认账户拼成一份可发布的关账结果。

    快照的范围**只有已确认账户**：`account_groups` 中非 `done` 账户的组不纳入，
    未确认账户的行也不参与合计。否则待算账户的金额会以「未匹配」形式漏进
    `bank_total`，等于把没算完的账户混进已发布账区。待算账户在 `pending` 里显式列出。
    """
    accounts = sorted(states)
    done = [name for name in accounts if states[name]["status"] == account_state.DONE]
    pending = [
        {
            "account": name,
            "status": states[name]["status"],
            "label": account_state.label(states[name]["status"]),
            "reason": states[name].get("reason", ""),
        }
        for name in accounts
        if states[name]["status"] != account_state.DONE
    ]

    done_set = set(done)
    groups: list[dict] = []
    for name in done:
        groups.extend(
            group for group in account_groups.get(name, [])
            if group["account"] in done_set
        )
    groups.sort(key=lambda group: (group["tier"], group["match_id"]))

    # 只把已确认账户的记录带进对账，待算账户的行不参与金额与匹配合计。
    scoped_records = [
        record for record in records
        if str(record.get("account") or "") in done_set
    ]
    reconciliation = differences.reconcile(scoped_records, groups, settings)
    # 隔离行是采信证据，不属于任何账户的账区；按全量记录统计，避免未登记账户的
    # 隔离证据因账户未确认而消失。
    all_view = differences.reconcile(records, groups, settings)
    counts = counts_dict(reconciliation)
    counts["bank_quarantine"] = all_view["counts"]["bank_quarantine"]
    counts["ledger_quarantine"] = all_view["counts"]["ledger_quarantine"]
    status = "closed" if not pending else "partial"
    return {
        "status": status,
        "accounts": done,
        "pending": pending,
        "accounts_total": len(accounts),
        "accounts_done": len(done),
        "groups": groups,
        "totals": totals_dict(reconciliation),
        "counts": counts,
        "reconciliation": reconciliation,
    }
