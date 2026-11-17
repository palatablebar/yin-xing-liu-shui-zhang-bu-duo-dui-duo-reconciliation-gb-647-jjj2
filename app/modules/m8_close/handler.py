"""关账入口：账户状态、分批推进、关账版本历史。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Body, Query, Request

from ...errors import DomainError
from . import account as account_state
from . import service, view

router = APIRouter(prefix="/api/close", tags=["关账"])


@router.get("/status")
def get_status(request: Request):
    store = request.app.state.store
    run = store.latest_run()
    if not run:
        return {"run_id": None, "accounts": [], "completion": view.completion(None)}
    states = store.account_state(run["run_id"])
    accounts = [
        {
            "account": name,
            "status": state["status"],
            "label": account_state.label(state["status"]),
            "reason": state.get("reason", ""),
            "groups": state["groups"],
            "nodes": state["nodes"],
            "computed_at": state.get("computed_at", ""),
            "releasable": account_state.is_releasable(state["status"]),
        }
        for name, state in sorted(states.items())
    ]
    current = service.current(store, run)
    counts = {
        "done": sum(1 for item in accounts if item["status"] == account_state.DONE),
        "pending": sum(1 for item in accounts
                       if item["status"] == account_state.PENDING),
        "timeout": sum(1 for item in accounts
                       if item["status"] == account_state.TIMEOUT),
        "failed": sum(1 for item in accounts
                      if item["status"] == account_state.FAILED),
    }
    return {
        "run_id": run["run_id"],
        "revision_id": current["revision_id"] if current else None,
        "completion": view.completion(current),
        "accounts": accounts,
        "accounts_total": len(accounts),
        "accounts_done": counts["done"],
        "counts": counts,
    }


@router.post("/advance")
def post_advance(request: Request, body: dict | None = Body(default=None)):
    """推进未确认账户。可指定 accounts 只算一部分，便于分批完成。"""
    store = request.app.state.store
    settings = request.app.state.settings
    run = store.latest_run()
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    payload = body or {}
    accounts = payload.get("accounts")
    if accounts is not None:
        if not isinstance(accounts, list) or not all(isinstance(a, str) for a in accounts):
            raise DomainError("E_ACCOUNTS", "accounts 必须是账户名数组", 400)
        known = store.account_state(run["run_id"])
        unknown = [name for name in accounts if name not in known]
        if unknown:
            raise DomainError("E_ACCOUNT_UNKNOWN",
                              f"批次里没有这些账户：{', '.join(sorted(unknown))}", 400)
    rounds = int(payload.get("rounds", 1) or 1)
    if rounds < 1 or rounds > 10:
        raise DomainError("E_ROUNDS", "rounds 必须在 1 到 10 之间", 400)
    max_accounts = payload.get("max_accounts")
    if max_accounts is not None:
        max_accounts = int(max_accounts)
        if max_accounts < 1:
            raise DomainError("E_MAX_ACCOUNTS", "max_accounts 必须 >= 1", 400)
    return service.advance(settings, store, run, accounts=accounts, rounds=rounds,
                           max_accounts=max_accounts)


@router.get("/revisions")
def get_revisions(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    store = request.app.state.store
    run = store.latest_run()
    if not run:
        return {"run_id": None, "count": 0, "revisions": []}
    revisions = store.list_revisions(run["run_id"], limit)
    current_id = store.published_revision_id(run["run_id"])
    for item in revisions:
        item["published"] = item["revision_id"] == current_id
    return {"run_id": run["run_id"], "count": len(revisions),
            "published": current_id, "revisions": revisions}


@router.get("/revisions/{revision_id}")
def get_revision(request: Request, revision_id: str):
    store = request.app.state.store
    revision = store.get_revision(revision_id)
    if not revision:
        raise DomainError("E_NO_REVISION", "关账版本不存在", 404)
    revision["groups"] = store.revision_matches(revision_id)
    revision["published"] = store.published_revision_id(revision["run_id"]) == revision_id
    return revision


@router.get("/revisions/{revision_id}/report.csv")
def get_revision_report(request: Request, revision_id: str):
    """按任意历史版本导出：内容取该版本快照，不重新求解。"""
    from fastapi.responses import Response

    from ..m6_diff import differences
    from ..m7_export import csv_out

    store = request.app.state.store
    settings = request.app.state.settings
    revision = store.get_revision(revision_id)
    if not revision:
        raise DomainError("E_NO_REVISION", "关账版本不存在", 404)
    run = store.get_run(revision["run_id"])
    groups = store.revision_matches(revision_id)
    accounts = set(revision["accounts"])
    scoped = [
        record for record in store.records(run["run_id"])
        if str(record.get("account") or "") in accounts
    ]
    reconciliation = differences.reconcile(scoped, groups, settings)
    reconciliation["totals"] = {
        key: Decimal(value) for key, value in revision["totals"].items()
    }
    reconciliation["counts"] = dict(revision["counts"])
    completion = {
        "revision_id": revision_id,
        "status": revision["status"],
        "label": f"已关账 {revision['accounts_done']}/{revision['accounts_total']} 账户"
        if revision["status"] == "closed"
        else f"分批关账 {revision['accounts_done']}/{revision['accounts_total']} 账户",
        "accounts_done": revision["accounts_done"],
        "accounts_total": revision["accounts_total"],
        "pending": revision["pending"],
        "complete": revision["status"] == "closed",
        "created_at": revision["created_at"],
    }
    text = csv_out.report_text(scoped, groups, reconciliation, completion,
                              quarantine_records=store.records(run["run_id"]))
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="report-{revision_id}.csv"',
            "X-Revision-Id": revision_id,
        },
    )