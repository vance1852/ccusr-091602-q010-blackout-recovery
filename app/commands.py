"""命令去重网关：同一业务键的命令只生效一次。

- accepted：新业务键且依赖就绪，命令与效果在同一事务中原子落账；
- duplicate：同键同载荷，直接忽略（网络恢复后重投的旧命令）；
- stale：同键不同载荷，或同单元同类命令已有更新版本生效；
- dependency_blocked：前置依赖未就绪，拒绝执行但不占用业务键，
  依赖补齐后同一键可重新提交。

命令行与效果行在同一事务提交：进程在任意时刻崩溃，
都不会出现“效果已发生但去重账未记”的中间态。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import CommandResult
from .store import Store

#: 命令类型 → 效果类型（物料账只认 feed_material / scrap）。
EFFECT_TYPE = {
    "feed_material": "feed_material",
    "scrap_unit": "scrap",
}


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class CommandGateway:
    def __init__(self, store: Store, clock) -> None:
        self._store = store
        self._clock = clock

    # ------------------------------------------------------------------ 分派
    def dispatch(
        self,
        namespace: str,
        incident_id: str,
        business_key: str,
        unit_id: str,
        command_type: str,
        payload: dict[str, Any],
        issued_at: str,
        dependencies_met: bool = True,
    ) -> str:
        """分派一条命令，返回 CommandResult 取值。"""
        payload_hash = _hash(payload)
        now = self._clock()
        with self._store.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM commands WHERE namespace=? AND incident_id=? AND business_key=?",
                (namespace, incident_id, business_key),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] == payload_hash:
                    result, detail = CommandResult.DUPLICATE.value, "同一业务键相同载荷，忽略"
                else:
                    result, detail = CommandResult.STALE.value, "同一业务键载荷冲突，按陈旧命令拒绝"
            else:
                newer = conn.execute(
                    "SELECT 1 FROM commands WHERE namespace=? AND incident_id=? AND unit_id=? "
                    "AND command_type=? AND issued_at > ? LIMIT 1",
                    (namespace, incident_id, unit_id, command_type, issued_at),
                ).fetchone()
                if newer is not None:
                    result, detail = CommandResult.STALE.value, "同单元同类命令已有更新版本生效，旧命令拒绝"
                elif not dependencies_met:
                    # 不占用业务键：依赖补齐后允许原键重试。
                    result, detail = CommandResult.DEPENDENCY_BLOCKED.value, "前置依赖未就绪，命令暂缓"
                else:
                    conn.execute(
                        "INSERT INTO commands (namespace, incident_id, business_key, unit_id, "
                        "command_type, payload_hash, payload, issued_at, applied_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            namespace,
                            incident_id,
                            business_key,
                            unit_id,
                            command_type,
                            payload_hash,
                            _canonical(payload),
                            issued_at,
                            now,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO effects (namespace, incident_id, business_key, effect_type, "
                        "unit_id, payload, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            namespace,
                            incident_id,
                            business_key,
                            EFFECT_TYPE.get(command_type, "state_change"),
                            unit_id,
                            _canonical(payload),
                            now,
                        ),
                    )
                    result, detail = CommandResult.ACCEPTED.value, ""
            conn.execute(
                "INSERT INTO command_attempts (namespace, incident_id, business_key, unit_id, "
                "command_type, result, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (namespace, incident_id, business_key, unit_id, command_type, result, detail, now),
            )
        return result

    # -------------------------------------------------------------- 历史导入
    def register_historical(
        self,
        namespace: str,
        incident_id: str,
        business_key: str,
        unit_id: str,
        command_type: str,
        payload: dict[str, Any],
        issued_at: str,
    ) -> str:
        """把断电前命令日志导入去重账（不重复产生物料效果）。"""
        payload_hash = _hash(payload)
        now = self._clock()
        with self._store.transaction() as conn:
            existing = conn.execute(
                "SELECT payload_hash FROM commands WHERE namespace=? AND incident_id=? AND business_key=?",
                (namespace, incident_id, business_key),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO commands (namespace, incident_id, business_key, unit_id, "
                    "command_type, payload_hash, payload, issued_at, applied_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        namespace,
                        incident_id,
                        business_key,
                        unit_id,
                        command_type,
                        payload_hash,
                        _canonical(payload),
                        issued_at,
                        now,
                    ),
                )
                result, detail = CommandResult.ACCEPTED.value, "historical_import"
            elif existing["payload_hash"] == payload_hash:
                result, detail = CommandResult.DUPLICATE.value, "historical_replay"
            else:
                result, detail = CommandResult.STALE.value, "historical_conflict"
            conn.execute(
                "INSERT INTO command_attempts (namespace, incident_id, business_key, unit_id, "
                "command_type, result, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (namespace, incident_id, business_key, unit_id, command_type, result, detail, now),
            )
        return result

    # ------------------------------------------------------------------ 查询
    def attempts(self, namespace: str, incident_id: str) -> list[dict[str, Any]]:
        return self._store.query(
            "SELECT * FROM command_attempts WHERE namespace=? AND incident_id=? ORDER BY id",
            (namespace, incident_id),
        )

    def commands(self, namespace: str, incident_id: str) -> list[dict[str, Any]]:
        return self._store.query(
            "SELECT * FROM commands WHERE namespace=? AND incident_id=? ORDER BY business_key",
            (namespace, incident_id),
        )

    def effects(self, namespace: str, incident_id: str) -> list[dict[str, Any]]:
        return self._store.query(
            "SELECT * FROM effects WHERE namespace=? AND incident_id=? ORDER BY business_key",
            (namespace, incident_id),
        )
