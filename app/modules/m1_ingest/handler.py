"""采信入口：导入一份银行流水或账簿凭证。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile

from ...errors import DomainError
from ...pipeline import import_side

router = APIRouter(prefix="/api/statements", tags=["采信"])


def _flag(value: str) -> bool:
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _positive(value: str, code: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(code, "必须是正整数", 400) from exc
    if number < 1:
        raise DomainError(code, "必须是正整数", 400)
    return number


@router.post("/import")
async def import_statement(
    request: Request,
    side: str = Form("bank"),
    sample: str = Form(""),
    solve: str = Form("true"),
    max_accounts: str = Form(""),
    rounds: str = Form("1"),
    file: UploadFile | None = File(None),
):
    if side not in ("bank", "ledger"):
        raise DomainError("E_SIDE", "side 只能是 bank 或 ledger", 400)
    settings = request.app.state.settings
    store = request.app.state.store
    if file is not None and file.filename:
        raw = await file.read()
        filename = file.filename
    elif sample:
        from .service import resolve_sample

        filename, raw = resolve_sample(settings, sample)
    else:
        raise DomainError("E_INPUT", "需要上传文件或指定样例", 400)
    max_count = _positive(max_accounts, "E_MAX_ACCOUNTS")
    round_count = _positive(rounds, "E_ROUNDS") or 1
    if round_count > 10:
        raise DomainError("E_ROUNDS", "rounds 必须在 1 到 10 之间", 400)
    return import_side(settings, store, side, filename, raw,
                       rounds=round_count, solve=_flag(solve), max_accounts=max_count)


@router.get("")
def list_statement_rows(request: Request, side: str = "bank", status: str = "valid"):
    if side not in ("bank", "ledger"):
        raise DomainError("E_SIDE", "side 只能是 bank 或 ledger", 400)
    store = request.app.state.store
    run = store.latest_run()
    if not run:
        return {"run_id": None, "rows": [], "count": 0}
    rows = store.records(run["run_id"], side=side, status=status)
    return {"run_id": run["run_id"], "count": len(rows), "rows": rows}