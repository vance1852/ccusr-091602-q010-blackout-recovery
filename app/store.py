"""SQLite 持久层：命名空间隔离、事务、追加式审计事件。

所有业务表都带 ns 列：official 为正式记录，drill-<id> 为演练副本，
演练写入永远不会污染正式记录。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
  PRIMARY KEY(ns, key));
CREATE TABLE IF NOT EXISTS unit_registry(
  ns TEXT NOT NULL, unit_id TEXT NOT NULL, kind TEXT NOT NULL,
  device_id TEXT, line_id TEXT, utility_id TEXT,
  recipe_qty REAL DEFAULT 0, exposure_sensitive INTEGER DEFAULT 0,
  PRIMARY KEY(ns, unit_id));
CREATE TABLE IF NOT EXISTS routes(
  ns TEXT NOT NULL, device_id TEXT NOT NULL, operation_id TEXT NOT NULL,
  seq INTEGER NOT NULL, checkpoint_id TEXT NOT NULL,
  PRIMARY KEY(ns, device_id, operation_id, seq));
CREATE TABLE IF NOT EXISTS snapshots(
  ns TEXT NOT NULL, unit_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
  device_id TEXT, operation_id TEXT, checkpoint_id TEXT, state TEXT,
  expected_qty REAL, recorded_at TEXT, meta_json TEXT,
  PRIMARY KEY(ns, unit_id));
CREATE TABLE IF NOT EXISTS boot_reports(
  ns TEXT NOT NULL, device_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
  boot_state TEXT, faults_json TEXT, excursion INTEGER DEFAULT 0,
  mid_operation INTEGER DEFAULT 0, restarted_at TEXT,
  PRIMARY KEY(ns, device_id));
CREATE TABLE IF NOT EXISTS material_scans(
  ns TEXT NOT NULL, unit_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
  scanned_qty REAL, location TEXT, scanned_at TEXT,
  PRIMARY KEY(ns, unit_id));
CREATE TABLE IF NOT EXISTS command_logs(
  ns TEXT NOT NULL, business_key TEXT NOT NULL, evidence_id TEXT NOT NULL,
  unit_id TEXT, verb TEXT, payload_json TEXT, issued_at TEXT, status TEXT,
  PRIMARY KEY(ns, business_key));
CREATE TABLE IF NOT EXISTS decisions(
  ns TEXT NOT NULL, unit_id TEXT NOT NULL, decision TEXT NOT NULL,
  checkpoint_json TEXT, rationale TEXT, source TEXT, created_at TEXT,
  PRIMARY KEY(ns, unit_id));
CREATE TABLE IF NOT EXISTS approvals(
  ns TEXT NOT NULL, approval_id TEXT NOT NULL, unit_id TEXT NOT NULL,
  suggested TEXT, manual TEXT, requester TEXT, confirmations_json TEXT,
  status TEXT, created_at TEXT, resolved_at TEXT,
  PRIMARY KEY(ns, approval_id));
CREATE TABLE IF NOT EXISTS actions(
  ns TEXT NOT NULL, action_id TEXT NOT NULL, unit_id TEXT, level INTEGER,
  verb TEXT, state TEXT, blocked_reason TEXT, depends_json TEXT,
  decision_basis_json TEXT, created_at TEXT, executed_at TEXT,
  PRIMARY KEY(ns, action_id));
CREATE TABLE IF NOT EXISTS exec_journal(
  ns TEXT NOT NULL, action_id TEXT NOT NULL, phase TEXT NOT NULL,
  effect_json TEXT, updated_at TEXT,
  PRIMARY KEY(ns, action_id));
CREATE TABLE IF NOT EXISTS commands(
  ns TEXT NOT NULL, business_key TEXT NOT NULL, unit_id TEXT, verb TEXT,
  payload_json TEXT, issued_at TEXT, result TEXT, applied_at TEXT,
  PRIMARY KEY(ns, business_key));
CREATE TABLE IF NOT EXISTS command_attempts(
  ns TEXT NOT NULL, seq INTEGER PRIMARY KEY AUTOINCREMENT,
  business_key TEXT, unit_id TEXT, verb TEXT, result TEXT, detail TEXT,
  at TEXT);
CREATE TABLE IF NOT EXISTS material_movements(
  ns TEXT NOT NULL, effect_key TEXT NOT NULL, unit_id TEXT, delta REAL,
  reason TEXT, created_at TEXT,
  PRIMARY KEY(ns, effect_key));
CREATE TABLE IF NOT EXISTS reviews(
  ns TEXT NOT NULL, review_id TEXT NOT NULL, action_id TEXT, unit_id TEXT,
  fingerprint TEXT, conclusion TEXT, created_at TEXT,
  PRIMARY KEY(ns, review_id));
CREATE TABLE IF NOT EXISTS events(
  ns TEXT NOT NULL, seq INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT, payload_json TEXT, at TEXT);
"""

# 演练时从正式命名空间复制的证据类表（不含任何执行记录）。
DRILL_COPY_TABLES = (
    "meta",
    "unit_registry",
    "routes",
    "snapshots",
    "boot_reports",
    "material_scans",
    "command_logs",
)


class Store:
    """对 sqlite3 的薄封装：行转 dict、显式事务、审计事件。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # 关闭隐式事务：所有写事务都经 tx() 显式开启，
        # 事务外的单条语句自动提交。
        self.conn.isolation_level = None
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        """写事务：BEGIN IMMEDIATE 保证写串行化。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params)]

    def one(self, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def emit(self, ns: str, type_: str, payload: dict[str, Any], at: str) -> None:
        """追加审计事件（调用方须在事务内）。"""
        self.execute(
            "INSERT INTO events(ns, type, payload_json, at) VALUES(?,?,?,?)",
            (ns, type_, json.dumps(payload, ensure_ascii=False, sort_keys=True), at),
        )

    def events(self, ns: str, type_: Optional[str] = None) -> list[dict[str, Any]]:
        if type_ is None:
            rows = self.query("SELECT * FROM events WHERE ns=? ORDER BY seq", (ns,))
        else:
            rows = self.query(
                "SELECT * FROM events WHERE ns=? AND type=? ORDER BY seq",
                (ns, type_),
            )
        for r in rows:
            r["payload"] = json.loads(r.pop("payload_json") or "{}")
        return rows

    def copy_namespace(self, src_ns: str, dst_ns: str, tables=DRILL_COPY_TABLES) -> None:
        """把证据类表从 src 复制到 dst（用于建立演练命名空间）。"""
        with self.tx():
            for table in tables:
                cols = [
                    r["name"]
                    for r in self.conn.execute(f"PRAGMA table_info({table})")
                ]
                col_list = ",".join(cols)
                placeholders = ",".join("?" for _ in cols)
                rows = self.conn.execute(
                    f"SELECT {col_list} FROM {table} WHERE ns=?", (src_ns,)
                ).fetchall()
                for row in rows:
                    values = [dst_ns if c == "ns" else row[c] for c in cols]
                    self.conn.execute(
                        f"INSERT OR REPLACE INTO {table}({col_list}) "
                        f"VALUES({placeholders})",
                        values,
                    )


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text: Optional[str], default: Any = None) -> Any:
    if text is None:
        return default
    return json.loads(text)
