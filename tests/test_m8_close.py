"""分批关账：账户完成标记、发布隔离、幂等与中断恢复。

覆盖用户提出的四条硬要求：
1. 按账户完成、分批发布，而不是等整批算完；
2. 页面与导出看到同一份带完成标记的关账结果；
3. 超时或中断后保留完整旧版；
4. 重跑不覆盖已确认记录，也不丢失记录。
"""
from decimal import Decimal

import pytest

from app import config
from app.modules.m4_candidates import candidate
from app.modules.m8_close import account as account_state


def import_file(client, side, name, path, data=None):
    payload = {"side": side}
    if data:
        payload.update(data)
    with path.open("rb") as handle:
        return client.post(
            "/api/statements/import",
            data=payload,
            files={"file": (name, handle, "text/csv")},
        )


def write_pair(tmp_path, bank_rows, ledger_rows, accounts=None):
    """生成两侧 CSV，每行 (业务号, 账户, 日期, 方向, 金额)。"""
    head_bank = "流水号,账户,发生时间,借贷标志,金额,手续费,对方户名,摘要\n"
    head_ledger = "凭证号,账户,记账日期,方向,金额,摘要\n"
    bank = tmp_path / "bank.csv"
    ledger = tmp_path / "ledger.csv"
    bank.write_text(head_bank + "".join(
        f"{txn},{acct},{day},借,{amt},0.00,供应商,分批\n"
        for txn, acct, day, _d, amt in bank_rows
    ), encoding="utf-8-sig")
    ledger.write_text(head_ledger + "".join(
        f"{txn},{acct},{day},借,{amt},分批\n"
        for txn, acct, day, _d, amt in ledger_rows
    ), encoding="utf-8")
    return bank, ledger


def ingest(client, bank, ledger, solve=True, max_accounts=None, rounds=1):
    """采信两侧文件；`solve=False` 时只登记账户，返回后由 /api/close/advance 分批求解。"""
    data = {"solve": "true" if solve else "false"}
    if max_accounts is not None:
        data["max_accounts"] = str(max_accounts)
    data["rounds"] = str(rounds)
    assert import_file(client, "bank", "bank.csv", bank, data).status_code == 200
    response = import_file(client, "ledger", "ledger.csv", ledger, data)
    assert response.status_code == 200
    return response.json()


def load(client, bank, ledger, **kwargs):
    return ingest(client, bank, ledger, **kwargs)


def test_accounts_are_the_completion_unit(client, demo):
    status = client.get("/api/close/status").json()
    # 只有隔离行的未登记账户不是完成单位，它的行只留在隔离区。
    assert status["accounts_total"] == 3
    assert status["counts"]["done"] == 3
    assert status["completion"]["complete"] is True


def test_demo_publishes_closed_revision(client, demo):
    status = client.get("/api/close/status").json()
    assert status["revision_id"] is not None
    revision = client.get(f"/api/close/revisions/{status['revision_id']}").json()
    assert revision["status"] == "closed"
    assert revision["published"] is True
    assert revision["accounts_done"] == revision["accounts_total"]
    assert revision["pending"] == []


def test_timeout_account_never_enters_published_zone(client, settings, tmp_path):
    """把预算压到极低，让大账户超时；小账户仍应发布，大账户绝不入账。"""
    bank_rows = [
        ("S1", "6222-0012", "2026-03-02", -1, "100.00"),
        ("S2", "6222-0012", "2026-03-02", -1, "200.00"),
    ]
    ledger_rows = [
        ("VS1", "6222-0012", "2026-03-02", -1, "100.00"),
        ("VS2", "6222-0012", "2026-03-02", -1, "200.00"),
    ]
    for index in range(30):
        amount = f"{100 + index}.{index % 100:02d}"
        bank_rows.append((f"B{index:03d}", "6222-0034", "2026-03-05", -1, amount))
        ledger_rows.append((f"VB{index:03d}", "6222-0034", "2026-03-05", -1, amount))
    bank, ledger = write_pair(tmp_path, bank_rows, ledger_rows)

    tight = config.Settings(
        db_path=settings.db_path, sample_dir=settings.sample_dir, solve_budget=1
    )
    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app(tight)) as tight_client:
        body = ingest(tight_client, bank, ledger)
        assert body["accounts_done"] == 0
        assert body["completed"] == "partial"

        status = tight_client.get("/api/close/status").json()
        assert status["counts"]["done"] == 0
        assert status["counts"]["timeout"] == status["accounts_total"]

        # 未确认时没有可发布账区，接口与导出都不给出一份假结果。
        assert tight_client.get("/api/matches").json()["count"] == 0
        response = tight_client.get("/api/export/report.csv")
        assert response.status_code == 404
        assert response.json()["error"] == "E_NOT_PUBLISHED"


def test_partial_release_contains_only_done_accounts(client, settings, tmp_path):
    """一批只算一个账户：已发布快照只含完成账户，页面与导出一致。"""
    bank_rows = [
        ("S1", "6222-0012", "2026-03-02", -1, "100.00"),
        ("S2", "6222-0012", "2026-03-02", -1, "200.00"),
        ("S3", "6222-0034", "2026-03-03", -1, "300.00"),
    ]
    ledger_rows = [
        ("VS1", "6222-0012", "2026-03-02", -1, "100.00"),
        ("VS2", "6222-0012", "2026-03-02", -1, "200.00"),
        ("VS3", "6222-0034", "2026-03-03", -1, "300.00"),
    ]
    bank, ledger = write_pair(tmp_path, bank_rows, ledger_rows)

    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app(settings)) as tight_client:
        # 采信后只推进一个账户，模拟第一批只完成一部分。
        ingest(tight_client, bank, ledger, solve=False)
        first = tight_client.post("/api/close/advance", json={"max_accounts": 1}).json()
        assert first["accounts_done"] == 1
        done = first["computed"]
        assert len(done) == 1

        matches = tight_client.get("/api/matches").json()
        assert matches["count"] == sum(
            1 for group in matches["groups"] if group["account"] == done[0]
        )
        assert matches["count"] == 2
        assert {group["account"] for group in matches["groups"]} == {done[0]}

        # 未完成账户被显式列出，且不出现在匹配组里。
        assert matches["completion"]["complete"] is False
        pending = {item["account"] for item in matches["completion"]["pending"]}
        assert pending == {"6222-0012", "6222-0034"} - set(done)

        # 页面与导出共用同一 revision 与同一完成标记。
        report = tight_client.get("/api/export/report.csv").text
        manifest = tight_client.get("/api/export/manifest.json").json()
        assert matches["revision_id"] == manifest["revision_id"]
        assert manifest["completion"]["label"] == matches["completion"]["label"]
        assert matches["revision_id"] in report
        page = tight_client.get("/matches").text
        assert matches["completion"]["label"] in page
        export_page = tight_client.get("/export").text
        assert matches["completion"]["label"] in export_page


def test_timeout_account_can_be_retried_after_budget_increase(settings, tmp_path):
    """预算不足 → 账户延后；提高预算后重跑即可补上，旧版始终可读。"""
    from fastapi.testclient import TestClient
    from app.main import create_app

    bank_rows = [("S1", "6222-0012", "2026-03-02", -1, "100.00"),
                 ("S2", "6222-0012", "2026-03-02", -1, "500.00")]
    ledger_rows = [("VS1", "6222-0012", "2026-03-02", -1, "100.00"),
                   ("VS2", "6222-0012", "2026-03-02", -1, "500.00")]
    for index in range(40):
        amount = f"{5000 + index}.{index % 100:02d}"
        bank_rows.append((f"B{index:03d}", "6222-0034", "2026-03-05", -1, amount))
        ledger_rows.append((f"VB{index:03d}", "6222-0034", "2026-03-05", -1, "9000.00"))
    bank, ledger = write_pair(tmp_path, bank_rows, ledger_rows)

    tight = config.Settings(
        db_path=settings.db_path, sample_dir=settings.sample_dir, solve_budget=1
    )
    with TestClient(create_app(tight)) as tight_client:
        ingest(tight_client, bank, ledger)
        status = tight_client.get("/api/close/status").json()
        assert status["counts"]["done"] == 0
        assert status["counts"]["timeout"] == 2

    roomy = config.Settings(
        db_path=settings.db_path, sample_dir=settings.sample_dir,
        solve_budget=100_000_000,
    )
    with TestClient(create_app(roomy)) as roomy_client:
        # 重跑只重算未确认账户，已确认的（这里没有）不受影响。
        result = roomy_client.post("/api/close/advance").json()
        assert result["accounts_done"] == 2
        status = roomy_client.get("/api/close/status").json()
        assert status["counts"]["timeout"] == 0
        assert status["completion"]["complete"] is True
        assert roomy_client.get("/api/matches").json()["count"] >= 1


def test_partial_totals_exclude_pending_accounts(client, settings, tmp_path):
    """待算账户的行不进已发布合计，也不以未匹配形式出现。"""
    from fastapi.testclient import TestClient
    from app.main import create_app

    bank_rows = [("S1", "6222-0012", "2026-03-02", -1, "100.00"),
                 ("S2", "6222-0034", "2026-03-03", -1, "200.00")]
    ledger_rows = [("VS1", "6222-0012", "2026-03-02", -1, "100.00"),
                   ("VS2", "6222-0034", "2026-03-03", -1, "200.00")]
    bank, ledger = write_pair(tmp_path, bank_rows, ledger_rows)

    with TestClient(create_app(settings)) as main_client:
        ingest(main_client, bank, ledger, solve=False)
        first = main_client.post("/api/close/advance", json={"max_accounts": 1}).json()
        done_account = first["computed"][0]
        pending_account = "6222-0034" if done_account == "6222-0012" else "6222-0012"

        data = main_client.get("/api/differences").json()
        # 只统计已确认账户那 100.00，待算账户的 200.00 完全不在账区。
        assert Decimal(data["totals"]["bank_total"]) == Decimal("-100.00")
        assert Decimal(data["totals"]["ledger_total"]) == Decimal("-100.00")
        assert data["counts"]["bank_valid"] == 1
        assert data["counts"]["ledger_valid"] == 1
        assert data["counts"]["bank_unmatched_rows"] == 0

        # 页面与导出用同一份合计。
        summary = main_client.get("/api/export/summary").json()
        assert summary["totals"]["bank_total"] == "-100.00"
        manifest = main_client.get("/api/export/manifest.json").json()
        assert Decimal(manifest["totals"]["bank_total"]) == Decimal("-100.00")
        pending = {item["account"] for item in data["completion"]["pending"]}
        assert pending == {pending_account}


def test_quarantine_evidence_survives_partial_close(client, settings, tmp_path):
    """未登记账户只有隔离行：账户未确认，隔离证据仍应完整可见、可导出。"""
    from fastapi.testclient import TestClient
    from app.main import create_app

    head = "流水号,账户,发生时间,借贷标志,金额,手续费,对方户名,摘要\n"
    bank = tmp_path / "bank.csv"
    bank.write_text(head + (
        "S1,6222-0012,2026-03-02,借,100.00,0.00,x,y\n"
        "S2,6222-9999,2026-03-03,借,200.00,0.00,x,未登记账户\n"
        "S3,6222-0012,2026-03-02,借,N/A,0.00,x,空金额\n"
    ), encoding="utf-8-sig")
    ledger = tmp_path / "ledger.csv"
    ledger.write_text("凭证号,账户,记账日期,方向,金额,摘要\n"
                      "VS1,6222-0012,2026-03-02,借,100.00,x\n", encoding="utf-8")

    with TestClient(create_app(settings)) as main_client:
        ingest(main_client, bank, ledger, solve=False)
        main_client.post("/api/close/advance", json={"max_accounts": 1})

        data = main_client.get("/api/differences").json()
        assert data["counts"]["bank_quarantine"] == 2  # 未登记账户 + 空金额

        quarantine = main_client.get("/api/quarantine").json()
        assert quarantine["count"] == 2
        report = main_client.get("/api/export/report.csv").text
        assert "ACCOUNT_UNKNOWN" in report
        assert "AMOUNT_SENTINEL" in report


def test_historical_revision_export(client, settings, tmp_path):
    """可按任意历史版本导出；导出内容取该版本快照。"""
    from fastapi.testclient import TestClient
    from app.main import create_app

    bank_rows = [("S1", "6222-0012", "2026-03-02", -1, "100.00"),
                 ("S2", "6222-0034", "2026-03-03", -1, "200.00")]
    ledger_rows = [("VS1", "6222-0012", "2026-03-02", -1, "100.00"),
                   ("VS2", "6222-0034", "2026-03-03", -1, "200.00")]
    bank, ledger = write_pair(tmp_path, bank_rows, ledger_rows)

    with TestClient(create_app(settings)) as main_client:
        ingest(main_client, bank, ledger, solve=False)
        first = main_client.post("/api/close/advance", json={"max_accounts": 1}).json()
        first_rev = first["revision_id"]
        main_client.post("/api/close/advance")
        current = main_client.get("/api/close/revisions").json()["published"]
        assert current != first_rev

        historical = main_client.get(f"/api/close/revisions/{first_rev}/report.csv")
        assert historical.status_code == 200
        assert historical.headers["X-Revision-Id"] == first_rev
        assert first_rev in historical.text
        assert "分批" in historical.text
        # 历史版本只含当时确认的账户。
        body = main_client.get(f"/api/close/revisions/{first_rev}").json()
        assert body["accounts_done"] == 1
        assert len(body["accounts"]) == 1


def test_rerun_does_not_overwrite_confirmed(client, demo):
    """已确认账户的结果在重跑后逐字不变，且不丢记录。"""
    before = client.get("/api/matches").json()
    before_rows = client.get("/api/statements", params={"side": "bank"}).json()["count"]
    again = client.post("/api/demo/run").json()
    after = client.get("/api/matches").json()
    assert again["run_id"] == demo["run_id"]
    assert before["revision_id"] == after["revision_id"]
    assert [group["match_id"] for group in before["groups"]] == \
        [group["match_id"] for group in after["groups"]]
    assert [group["fee_alloc"] for group in before["groups"]] == \
        [group["fee_alloc"] for group in after["groups"]]
    assert client.get("/api/statements", params={"side": "bank"}).json()["count"] == before_rows


def test_confirmed_account_rejects_different_result(client, demo, monkeypatch):
    """显式重算已确认账户：结果不同必须拒绝覆盖并报冲突。"""
    from app.modules.m8_close import service as close_service

    store = client.app.state.store
    run = store.latest_run()
    account = "6222-0012"
    original = [
        group["match_id"]
        for group in store.account_matches(run["run_id"], account)
    ]
    assert original

    real = close_service.solve_account

    def tampered(settings, records, name):
        outcome = real(settings, records, name)
        if name == account:
            # 模拟换了规则/数据后算出不同结果。
            outcome = dict(outcome)
            outcome["sha"] = "0" * 64
        return outcome

    monkeypatch.setattr(close_service, "solve_account", tampered)
    result = close_service.advance(client.app.state.settings, store, run, accounts=[account])
    assert result["conflicts"]
    assert result["conflicts"][0]["account"] == account

    # 已确认记录原样保留。
    kept = [group["match_id"] for group in store.account_matches(run["run_id"], account)]
    assert kept == original


def test_interrupted_solve_keeps_previous_revision(client, settings, tmp_path, monkeypatch):
    """中途异常：旧版快照仍完整可读，已确认账户不受影响。"""
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.modules.m8_close import service as close_service

    bank_rows = [
        ("S1", "6222-0012", "2026-03-02", -1, "100.00"),
        ("S2", "6222-0012", "2026-03-02", -1, "200.00"),
        ("S3", "6222-0034", "2026-03-03", -1, "300.00"),
    ]
    ledger_rows = [
        ("VS1", "6222-0012", "2026-03-02", -1, "100.00"),
        ("VS2", "6222-0012", "2026-03-02", -1, "200.00"),
        ("VS3", "6222-0034", "2026-03-03", -1, "300.00"),
    ]
    bank, ledger = write_pair(tmp_path, bank_rows, ledger_rows)

    with TestClient(create_app(settings)) as main_client:
        # 采信后只推进一个账户，形成一版分批快照。
        ingest(main_client, bank, ledger, solve=False)
        main_client.post("/api/close/advance", json={"max_accounts": 1})
        store = main_client.app.state.store
        run = store.latest_run()
        revision_id = store.published_revision_id(run["run_id"])
        assert revision_id
        snapshot = store.get_revision(revision_id)
        published_accounts = snapshot["accounts"]
        assert len(published_accounts) == 1
        published_groups = len(store.revision_matches(revision_id))
        real_solve = close_service.solve_account

        # 模拟下一轮在第二个账户处崩溃。
        def boom(settings_, records_, name):
            if name == "6222-0034":
                raise RuntimeError("进程被杀")
            return real_solve(settings_, records_, name)

        monkeypatch.setattr(close_service, "solve_account", boom)
        close_service.advance(settings, store, run)

        # 旧版快照原样保留，仍可读、可导出。
        after = store.get_revision(revision_id)
        assert after is not None
        assert after["accounts"] == published_accounts
        assert len(store.revision_matches(revision_id)) == published_groups
        assert store.published_revision_id(run["run_id"]) == revision_id
        assert main_client.get("/api/close/revisions/%s/report.csv" % revision_id).status_code == 200


def test_second_round_closes_remaining_accounts(client, settings, tmp_path):
    """第一批只发布完成账户；第二批推进后关账完成，旧版仍可查。"""
    from fastapi.testclient import TestClient
    from app.main import create_app

    bank_rows = [
        ("S1", "6222-0012", "2026-03-02", -1, "100.00"),
        ("S2", "6222-0034", "2026-03-02", -1, "200.00"),
    ]
    ledger_rows = [
        ("VS1", "6222-0012", "2026-03-02", -1, "100.00"),
        ("VS2", "6222-0034", "2026-03-02", -1, "200.00"),
    ]
    bank, ledger = write_pair(tmp_path, bank_rows, ledger_rows)

    with TestClient(create_app(settings)) as main_client:
        ingest(main_client, bank, ledger, solve=False)
        first = main_client.post("/api/close/advance", json={"accounts": ["6222-0012"]}).json()
        assert first["accounts_done"] == 1
        first_rev = first["revision"]["revision_id"]
        first_snapshot = main_client.get(f"/api/close/revisions/{first_rev}").json()
        assert first_snapshot["status"] == "partial"
        assert first_snapshot["accounts"] == ["6222-0012"]

        # 分批推进时页面看到的是已发布的那一版，未完成账户不在账区。
        live = main_client.get("/api/matches").json()
        assert live["revision_id"] == first_rev
        assert {group["account"] for group in live["groups"]} == {"6222-0012"}

        second = main_client.post("/api/close/advance", json={"accounts": ["6222-0034"]}).json()
        assert second["accounts_done"] == 2
        second_rev = second["revision"]["revision_id"]
        assert second_rev != first_rev
        final = main_client.get(f"/api/close/revisions/{second_rev}").json()
        assert final["status"] == "closed"
        assert sorted(final["accounts"]) == ["6222-0012", "6222-0034"]

        # 第一版仍在历史里，且不再指向当前发布。
        history = main_client.get("/api/close/revisions").json()
        ids = [item["revision_id"] for item in history["revisions"]]
        assert first_rev in ids and second_rev in ids
        published = [item for item in history["revisions"] if item["published"]]
        assert [item["revision_id"] for item in published] == [second_rev]


def test_per_account_fee_sums_to_actual(client, demo):
    groups = client.get("/api/matches").json()["groups"]
    by_account: dict[str, list[dict]] = {}
    for group in groups:
        by_account.setdefault(group["account"], []).append(group)
    assert by_account
    for account, items in by_account.items():
        allocated = sum((Decimal(item["fee_alloc"]) for item in items), Decimal("0.00"))
        actual = sum((Decimal(item["fee_actual"]) for item in items), Decimal("0.00"))
        assert allocated == actual, account
    total = sum((Decimal(item["fee_alloc"]) for item in groups), Decimal("0.00"))
    assert total == Decimal("22.31")


def test_published_matches_are_account_pure(client, demo):
    """已发布匹配组绝不跨账户：两侧成员同属一个账户。"""
    records = {
        row["txn_no"]: row["account"]
        for row in client.get("/api/statements", params={"side": "bank"}).json()["rows"]
    }
    records.update({
        row["txn_no"]: row["account"]
        for row in client.get("/api/statements", params={"side": "ledger"}).json()["rows"]
    })
    for group in client.get("/api/matches").json()["groups"]:
        accounts = {records[key] for key in group["bank_ids"] + group["ledger_ids"]}
        assert len(accounts) == 1
        assert group["account"] == next(iter(accounts))


def test_large_account_solves_within_budget(settings, tmp_path):
    """300 行同向流水的账户在默认预算内算完，不再整批超时。"""
    from app.pipeline import _parse
    from app.modules.m8_close import service as close_service

    lines = ["流水号,账户,发生时间,借贷标志,金额,手续费,对方户名,摘要"]
    for index in range(300):
        amount = f"{100 + index // 3}.{index % 100:02d}"
        lines.append(f"B{index:05d},6222-0012,2026-03-02,借,{amount},0.00,供应商,大账户")
    bank_raw = ("\n".join(lines) + "\n").encode("utf-8")
    ledger_lines = ["凭证号,账户,记账日期,方向,金额,摘要"]
    for index in range(300):
        amount = f"{100 + index // 3}.{index % 100:02d}"
        ledger_lines.append(f"VB{index:05d},6222-0012,2026-03-02,借,{amount},大账户")
    ledger_raw = ("\n".join(ledger_lines) + "\n").encode("utf-8")

    _, bank = _parse(settings, "bank", "bank.csv", bank_raw)
    _, ledger = _parse(settings, "ledger", "ledger.csv", ledger_raw)
    outcome = close_service.solve_account(settings, bank + ledger, "6222-0012")
    assert outcome["nodes"] <= settings.solve_budget
    assert outcome["groups"]


def test_budget_is_deterministic(settings):
    """同一账户在相同预算下两次求解结果指纹相同。"""
    from app.pipeline import _parse
    from app.modules.m8_close import service as close_service

    rows = ["流水号,账户,发生时间,借贷标志,金额,手续费,对方户名,摘要"]
    for index in range(40):
        rows.append(f"B{index:03d},6222-0012,2026-03-02,借,{200 + index}.00,0.00,x,y")
    raw = ("\n".join(rows) + "\n").encode("utf-8")
    _, bank = _parse(settings, "bank", "bank.csv", raw)
    ledger_rows = ["凭证号,账户,记账日期,方向,金额,摘要"]
    for index in range(40):
        ledger_rows.append(f"V{index:03d},6222-0012,2026-03-02,借,{200 + index}.00,x")
    ledger_raw = ("\n".join(ledger_rows) + "\n").encode("utf-8")
    _, ledger = _parse(settings, "ledger", "ledger.csv", ledger_raw)
    first = close_service.solve_account(settings, bank + ledger, "6222-0012")
    second = close_service.solve_account(settings, list(reversed(bank + ledger)), "6222-0012")
    assert first["sha"] == second["sha"]
    assert first["nodes"] == second["nodes"]


def test_all_six_pages_show_same_completion(client, demo):
    for path in ("/", "/quarantine", "/candidates", "/matches", "/batches", "/export"):
        page = client.get(path).text
        assert demo["revision_id"] in page, path