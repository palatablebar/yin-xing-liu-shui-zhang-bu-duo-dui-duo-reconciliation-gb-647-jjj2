"""SQLite 存储：批次、流水行、候选、匹配。

约定：
- 金额与手续费一律存 Decimal 字符串，不落 float；
- 一次运行的所有行在同一个事务里写入，失败整体回滚；
- 同一 run_id 重跑先删后插，行数、合计与首跑一致；
- 大整数编号（流水号、凭证号）按字符串保形，不过 int()。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    bank_sha256 TEXT NOT NULL DEFAULT '',
    ledger_sha256 TEXT NOT NULL DEFAULT '',
    bank_file TEXT NOT NULL DEFAULT '',
    ledger_file TEXT NOT NULL DEFAULT '',
    bank_bytes INTEGER NOT NULL DEFAULT 0,
    ledger_bytes INTEGER NOT NULL DEFAULT 0,
    bank_raw INTEGER NOT NULL DEFAULT 0,
    ledger_raw INTEGER NOT NULL DEFAULT 0,
    bank_valid INTEGER NOT NULL DEFAULT 0,
    ledger_valid INTEGER NOT NULL DEFAULT 0,
    bank_quar INTEGER NOT NULL DEFAULT 0,
    ledger_quar INTEGER NOT NULL DEFAULT 0,
    segments INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'empty',
    input_states_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS records (
    run_id TEXT NOT NULL,
    side TEXT NOT NULL,
    seq INTEGER NOT NULL,
    row_no INTEGER NOT NULL,
    txn_no TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL DEFAULT '',
    direction INTEGER NOT NULL DEFAULT 0,
    amount TEXT NOT NULL DEFAULT '',
    fee TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'valid',
    reason TEXT NOT NULL DEFAULT '',
    flags_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '[]',
    raw_amount TEXT NOT NULL DEFAULT '',
    raw_date TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id, side, status);

-- 按账户求解状态：账户是完成与发布的最小单位。done 为已确认，不可覆盖。
CREATE TABLE IF NOT EXISTS account_solves (
    run_id TEXT NOT NULL,
    account TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result_sha TEXT NOT NULL DEFAULT '',
    nodes INTEGER NOT NULL DEFAULT 0,
    groups INTEGER NOT NULL DEFAULT 0,
    candidates INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    computed_at TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, account)
);
CREATE INDEX IF NOT EXISTS idx_account_solves_run ON account_solves(run_id, status);
-- 已确认的账户匹配行：账户一次算完就整体写入，之后只读，重跑不得覆盖。
CREATE TABLE IF NOT EXISTS account_matches (
    run_id TEXT NOT NULL,
    account TEXT NOT NULL,
    match_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    bank_sum TEXT NOT NULL,
    ledger_sum TEXT NOT NULL,
    diff TEXT NOT NULL,
    day_gap INTEGER NOT NULL DEFAULT 0,
    fee_alloc TEXT NOT NULL DEFAULT '0',
    fee_actual TEXT NOT NULL DEFAULT '0',
    bank_signed TEXT NOT NULL DEFAULT '0',
    ledger_signed TEXT NOT NULL DEFAULT '0',
    PRIMARY KEY (run_id, account, match_id)
);
CREATE INDEX IF NOT EXISTS idx_account_matches_run ON account_matches(run_id, account);

-- 关账版本：一次发布对应一份不可变快照，只包含已完成账户，绝不包含未算完账户。
CREATE TABLE IF NOT EXISTS close_revisions (
    revision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'partial',
    accounts_total INTEGER NOT NULL DEFAULT 0,
    accounts_done INTEGER NOT NULL DEFAULT 0,
    accounts_json TEXT NOT NULL DEFAULT '[]',
    pending_json TEXT NOT NULL DEFAULT '[]',
    totals_json TEXT NOT NULL DEFAULT '{}',
    counts_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_revisions_run ON close_revisions(run_id, seq);
CREATE TABLE IF NOT EXISTS close_matches (
    revision_id TEXT NOT NULL,
    account TEXT NOT NULL,
    match_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    bank_sum TEXT NOT NULL,
    ledger_sum TEXT NOT NULL,
    diff TEXT NOT NULL,
    day_gap INTEGER NOT NULL DEFAULT 0,
    fee_alloc TEXT NOT NULL DEFAULT '0',
    fee_actual TEXT NOT NULL DEFAULT '0',
    bank_signed TEXT NOT NULL DEFAULT '0',
    ledger_signed TEXT NOT NULL DEFAULT '0'
);
CREATE INDEX IF NOT EXISTS idx_close_matches_rev ON close_matches(revision_id);
-- 当前发布指针：页面与导出只认这一行，切换版本是一次原子写入。
CREATE TABLE IF NOT EXISTS run_publications (
    run_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

RUN_COLUMNS = (
    "run_id", "created_at", "rule_version", "params_json", "bank_sha256", "ledger_sha256",
    "bank_file", "ledger_file", "bank_bytes", "ledger_bytes", "bank_raw", "ledger_raw",
    "bank_valid", "ledger_valid", "bank_quar", "ledger_quar", "segments", "status",
    "input_states_json", "duration_ms",
)


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ---------- 批次 ----------
    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["params"] = json.loads(data.pop("params_json") or "{}")
        data["input_states"] = json.loads(data.pop("input_states_json") or "{}")
        return data

    def latest_run(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return self._run_row(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def save_run(self, run: dict, records: list[dict]) -> None:
        """写入批次元数据与本次采集的记录。

        候选与匹配组不在这里写：它们按账户由 M8 在账户确认时写入独立表，
        避免「先删后插」把已确认结果清掉。
        """
        params_json = json.dumps(run.get("params", {}), ensure_ascii=False, sort_keys=True)
        states_json = json.dumps(run.get("input_states", {}), ensure_ascii=False)
        with self._conn() as conn:
            run_id = run["run_id"]
            conn.execute("DELETE FROM records WHERE run_id = ?", (run_id,))
            conn.execute(
                """INSERT OR REPLACE INTO runs (
                    run_id, created_at, rule_version, params_json, bank_sha256, ledger_sha256,
                    bank_file, ledger_file, bank_bytes, ledger_bytes, bank_raw, ledger_raw,
                    bank_valid, ledger_valid, bank_quar, ledger_quar, segments, status,
                    input_states_json, duration_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, run["created_at"], run["rule_version"], params_json,
                    run.get("bank_sha256", ""), run.get("ledger_sha256", ""),
                    run.get("bank_file", ""), run.get("ledger_file", ""),
                    run.get("bank_bytes", 0), run.get("ledger_bytes", 0),
                    run.get("bank_raw", 0), run.get("ledger_raw", 0),
                    run.get("bank_valid", 0), run.get("ledger_valid", 0),
                    run.get("bank_quar", 0), run.get("ledger_quar", 0),
                    run.get("segments", 0), run.get("status", "empty"),
                    states_json, run.get("duration_ms", 0),
                ),
            )
            conn.executemany(
                """INSERT INTO records (run_id, side, seq, row_no, txn_no, account, posted_at,
                    direction, amount, fee, memo, counterparty, status, reason, flags_json,
                    raw_json, raw_amount, raw_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, r["side"], r["seq"], r["row_no"], r["txn_no"], r.get("account", ""),
                        r.get("posted_at", ""), r.get("direction", 0), str(r.get("amount", "")),
                        str(r.get("fee", "")), r.get("memo", ""), r.get("counterparty", ""),
                        r.get("status", "valid"), r.get("reason", ""),
                        json.dumps(r.get("flags", []), ensure_ascii=False),
                        json.dumps(r.get("raw", []), ensure_ascii=False),
                        str(r.get("raw_amount", "")), str(r.get("raw_date", "")),
                    )
                    for r in records
                ],
            )

    # ---------- 行 ----------
    def records(self, run_id: str, side: str | None = None,
                status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM records WHERE run_id = ?"
        args: list = [run_id]
        if side:
            sql += " AND side = ?"
            args.append(side)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY side, row_no, seq"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["flags"] = json.loads(item.pop("flags_json") or "[]")
            item["raw"] = json.loads(item.pop("raw_json") or "[]")
            out.append(item)
        return out

    def quarantine(self, run_id: str) -> list[dict]:
        return self.records(run_id, status="quarantine")

    def account_state(self, run_id: str) -> dict[str, dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM account_solves WHERE run_id = ?", (run_id,)
            ).fetchall()
        return {row["account"]: dict(row) for row in rows}

    def account_status(self, run_id: str, account: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM account_solves WHERE run_id = ? AND account = ?",
                (run_id, account),
            ).fetchone()
        return row["status"] if row else None

    def reset_pending_accounts(self, run_id: str) -> int:
        """把未确认的账户重置为 pending（中断后的重跑只动这些）。"""
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE account_solves SET status = 'pending', reason = '' "
                "WHERE run_id = ? AND status != 'done'",
                (run_id,),
            )
            return cursor.rowcount

    def save_account_result(self, run_id: str, account: str, status: str,
                            groups: list[dict], *, result_sha: str, nodes: int,
                            candidates: int, reason: str = "", computed_at: str = "",
                            reset: bool = False, candidate_rows: list[dict] | None = None) -> bool:
        """在单个事务里写入一个账户的匹配行、候选与求解状态。

        done 账户只允许写入同一 result_sha 的结果；结果不同则拒绝，已确认记录不会被覆盖。
        `reset=True` 用于 pending/timeout 账户重算前清掉自己的旧行。
        """
        candidates_json = json.dumps(
            [
                {
                    "cand_id": row["cand_id"], "tier": row["tier"],
                    "bank_ids": row["bank_ids"], "ledger_ids": row["ledger_ids"],
                    "bank_sum": str(row["bank_sum"]), "ledger_sum": str(row["ledger_sum"]),
                    "diff": str(row["diff"]), "day_gap": row["day_gap"],
                    "account": row.get("account", account),
                }
                for row in (candidate_rows or [])
            ],
            ensure_ascii=False,
        ) if status == "done" else "[]"
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT status, result_sha FROM account_solves WHERE run_id = ? AND account = ?",
                (run_id, account),
            ).fetchone()
            if existing and existing["status"] == "done":
                if status != "done" or existing["result_sha"] != result_sha:
                    return False
            if status == "done" or reset:
                conn.execute(
                    "DELETE FROM account_matches WHERE run_id = ? AND account = ?",
                    (run_id, account),
                )
            if status == "done":
                conn.executemany(
                    """INSERT INTO account_matches (run_id, account, match_id, tier, bank_ids,
                        ledger_ids, bank_sum, ledger_sum, diff, day_gap, fee_alloc, fee_actual,
                        bank_signed, ledger_signed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            run_id, account, group["match_id"], group["tier"],
                            json.dumps(group["bank_ids"], ensure_ascii=False),
                            json.dumps(group["ledger_ids"], ensure_ascii=False),
                            str(group["bank_sum"]), str(group["ledger_sum"]),
                            str(group["diff"]), group["day_gap"],
                            str(group.get("fee_alloc", "0")),
                            str(group.get("fee_actual", "0")),
                            str(group.get("bank_signed", "0")),
                            str(group.get("ledger_signed", "0")),
                        )
                        for group in groups
                    ],
                )
            conn.execute(
                """INSERT INTO account_solves (run_id, account, status, result_sha, nodes,
                    groups, candidates, reason, computed_at, candidates_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id, account) DO UPDATE SET
                    status = excluded.status,
                    result_sha = excluded.result_sha,
                    nodes = excluded.nodes,
                    groups = excluded.groups,
                    candidates = excluded.candidates,
                    reason = excluded.reason,
                    computed_at = excluded.computed_at,
                    candidates_json = excluded.candidates_json""",
                (run_id, account, status, result_sha, nodes, len(groups),
                 candidates, reason, computed_at, candidates_json),
            )
            return True

    def account_matches(self, run_id: str, account: str | None = None) -> list[dict]:
        sql = "SELECT * FROM account_matches WHERE run_id = ?"
        args: list = [run_id]
        if account is not None:
            sql += " AND account = ?"
            args.append(account)
        sql += " ORDER BY account, tier, match_id"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            out.append(item)
        return out

    def account_candidates(self, run_id: str, accounts: list[str] | None = None) -> list[dict]:
        """已确认账户的候选，从求解时落下的快照读取，不再重复枚举子集。"""
        sql = "SELECT account, candidates_json FROM account_solves WHERE run_id = ? AND status = 'done'"
        args: list = [run_id]
        if accounts is not None:
            if not accounts:
                return []
            sql += f" AND account IN ({','.join('?' for _ in accounts)})"
            args.extend(accounts)
        sql += " ORDER BY account"
        out: list[dict] = []
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        for row in rows:
            out.extend(json.loads(row["candidates_json"] or "[]"))
        return out

    # ---------- 关账版本 ----------
    def next_revision_seq(self, run_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM close_revisions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["seq"]) + 1

    def save_revision(self, revision: dict, groups: list[dict]) -> None:
        """写入一份关账快照并原子切换发布指针。

        快照只包含已确认账户；写入与指针切换在同一事务，页面与导出不会看到半份结果。
        """
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO close_revisions (revision_id, run_id, seq, created_at,
                    rule_version, status, accounts_total, accounts_done, accounts_json,
                    pending_json, totals_json, counts_json, duration_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    revision["revision_id"], revision["run_id"], revision["seq"],
                    revision["created_at"], revision["rule_version"], revision["status"],
                    revision["accounts_total"], revision["accounts_done"],
                    json.dumps(revision["accounts"], ensure_ascii=False),
                    json.dumps(revision["pending"], ensure_ascii=False),
                    json.dumps(revision["totals"], ensure_ascii=False, sort_keys=True),
                    json.dumps(revision["counts"], ensure_ascii=False, sort_keys=True),
                    revision.get("duration_ms", 0),
                ),
            )
            conn.executemany(
                """INSERT INTO close_matches (revision_id, account, match_id, tier, bank_ids,
                    ledger_ids, bank_sum, ledger_sum, diff, day_gap, fee_alloc, fee_actual,
                    bank_signed, ledger_signed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        revision["revision_id"], group["account"], group["match_id"],
                        group["tier"],
                        json.dumps(group["bank_ids"], ensure_ascii=False),
                        json.dumps(group["ledger_ids"], ensure_ascii=False),
                        str(group["bank_sum"]), str(group["ledger_sum"]), str(group["diff"]),
                        group["day_gap"], str(group.get("fee_alloc", "0")),
                        str(group.get("fee_actual", "0")),
                        str(group.get("bank_signed", "0")),
                        str(group.get("ledger_signed", "0")),
                    )
                    for group in groups
                ],
            )
            conn.execute(
                """INSERT INTO run_publications (run_id, revision_id, updated_at)
                VALUES (?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    revision_id = excluded.revision_id,
                    updated_at = excluded.updated_at""",
                (revision["run_id"], revision["revision_id"], revision["created_at"]),
            )

    def published_revision_id(self, run_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT revision_id FROM run_publications WHERE run_id = ?", (run_id,)
            ).fetchone()
        return row["revision_id"] if row else None

    def get_revision(self, revision_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM close_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["accounts"] = json.loads(data.pop("accounts_json") or "[]")
        data["pending"] = json.loads(data.pop("pending_json") or "[]")
        data["totals"] = json.loads(data.pop("totals_json") or "{}")
        data["counts"] = json.loads(data.pop("counts_json") or "{}")
        return data

    def revision_matches(self, revision_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM close_matches WHERE revision_id = ? ORDER BY tier, match_id",
                (revision_id,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            out.append(item)
        return out

    def list_revisions(self, run_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT revision_id, run_id, seq, created_at, rule_version, status, "
                "accounts_total, accounts_done, duration_ms FROM close_revisions "
                "WHERE run_id = ? ORDER BY seq DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
