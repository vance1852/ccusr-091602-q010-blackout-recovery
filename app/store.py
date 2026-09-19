"""SQLite 持久化层。

所有多步写入都通过 ``transaction()`` 完成，保证进程崩溃时要么全部提交、
要么全部回滚——这是“进程再次崩溃后继续执行也不多投一次料”的底层支撑。
正式记录与演练记录通过 ``namespace`` 列隔离。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
  namespace   TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  unit_id     TEXT NOT NULL,
  category    TEXT NOT NULL,
  requires    TEXT NOT NULL DEFAULT '[]',
  profile     TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL,
  PRIMARY KEY (namespace, incident_id, unit_id)
);

CREATE TABLE IF NOT EXISTS evidence (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace   TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  unit_id     TEXT NOT NULL,
  source      TEXT NOT NULL,
  payload     TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_unit
  ON evidence (namespace, incident_id, unit_id, source);

CREATE TABLE IF NOT EXISTS decisions (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace        TEXT NOT NULL,
  incident_id      TEXT NOT NULL,
  unit_id          TEXT NOT NULL,
  decision         TEXT NOT NULL,
  checkpoint       TEXT,
  rationale        TEXT NOT NULL,
  material_account TEXT NOT NULL,
  missing_evidence TEXT NOT NULL,
  resume_point     TEXT,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_unit
  ON decisions (namespace, incident_id, unit_id);

CREATE TABLE IF NOT EXISTS actions (
  namespace      TEXT NOT NULL,
  incident_id    TEXT NOT NULL,
  action_id      TEXT NOT NULL,
  unit_id        TEXT NOT NULL,
  kind           TEXT NOT NULL,
  state          TEXT NOT NULL,
  depends_on     TEXT NOT NULL DEFAULT '[]',
  decision       TEXT NOT NULL,
  manual_choice  TEXT,
  requires_dual  INTEGER NOT NULL DEFAULT 0,
  checkpoint     TEXT,
  resume_point   TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (namespace, incident_id, action_id)
);

CREATE TABLE IF NOT EXISTS approvals (
  namespace   TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  action_id   TEXT NOT NULL,
  approver    TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  PRIMARY KEY (namespace, incident_id, action_id, approver)
);

CREATE TABLE IF NOT EXISTS commands (
  namespace    TEXT NOT NULL,
  incident_id  TEXT NOT NULL,
  business_key TEXT NOT NULL,
  unit_id      TEXT NOT NULL,
  command_type TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload      TEXT NOT NULL,
  issued_at    TEXT NOT NULL,
  applied_at   TEXT NOT NULL,
  PRIMARY KEY (namespace, incident_id, business_key)
);

CREATE TABLE IF NOT EXISTS command_attempts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace    TEXT NOT NULL,
  incident_id  TEXT NOT NULL,
  business_key TEXT NOT NULL,
  unit_id      TEXT NOT NULL,
  command_type TEXT NOT NULL,
  result       TEXT NOT NULL,
  detail       TEXT NOT NULL DEFAULT '',
  at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_incident
  ON command_attempts (namespace, incident_id);

CREATE TABLE IF NOT EXISTS effects (
  namespace    TEXT NOT NULL,
  incident_id  TEXT NOT NULL,
  business_key TEXT NOT NULL,
  effect_type  TEXT NOT NULL,
  unit_id      TEXT NOT NULL,
  payload      TEXT NOT NULL,
  applied_at   TEXT NOT NULL,
  PRIMARY KEY (namespace, incident_id, business_key)
);

CREATE TABLE IF NOT EXISTS reviews (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace   TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  action_id   TEXT NOT NULL,
  conclusion  TEXT NOT NULL,
  detail      TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drill_registry (
  incident_id TEXT PRIMARY KEY,
  next_seq    INTEGER NOT NULL
);
"""

#: 所有命名空间隔离的表（演练重置时逐表清理）。
NAMESPACED_TABLES = (
    "units",
    "evidence",
    "decisions",
    "actions",
    "approvals",
    "commands",
    "command_attempts",
    "effects",
    "reviews",
)


class Store:
    """对 SQLite 连接的薄封装：事务 + 行转字典。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(sql, params)]

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        self._conn.close()
