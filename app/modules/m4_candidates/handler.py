"""候选入口：查看已发布账户的候选、推进未确认账户。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request

from ...errors import DomainError
from . import service

router = APIRouter(prefix="/api/candidates", tags=["候选"])


@router.get("")
def get_candidates(request: Request):
    store = request.app.state.store
    return service.payload(store, store.latest_run(), request.app.state.settings)


@router.post("")
def post_candidates(request: Request, body: dict | None = Body(default=None)):
    store = request.app.state.store
    settings = request.app.state.settings
    accounts = (body or {}).get("accounts")
    if accounts is not None:
        if not isinstance(accounts, list) or not all(isinstance(a, str) for a in accounts):
            raise DomainError("E_ACCOUNTS", "accounts 必须是账户名数组", 400)
        return service.refresh_accounts(store, store.latest_run(), settings, accounts)
    return service.refresh(store, store.latest_run(), settings)