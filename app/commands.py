"""命令网关：按业务键去重，旧命令网络恢复后到达也不得重复生效。

判定顺序：
1. 业务键已被网关 accepted，或在停电前命令日志中为 acked -> duplicate
2. 命令签发时间早于停电时刻（且从未生效）-> stale（恢复计划已接管）
3. 目标单元的恢复动作仍被依赖/证据阻塞 -> dependency_blocked（不消耗
   业务键，依赖就绪后重发可 accepted）
4. 否则 -> accepted，并在同一事务内应用效果（投料/消耗记物料账）
"""
from __future__ import annotations

from typing import Any, Optional

from .models import LEVEL_PROCESSING
from .store import Store, dumps

# 产生物料账移动的命令动词及其方向。
MATERIAL_DELTA = {"FEED": +1.0, "CONSUME": -1.0}


class CommandGateway:
    def __init__(self, store: Store):
        self.store = store

    def submit(
        self,
        ns: str,
        business_key: str,
        unit_id: Optional[str],
        verb: str,
        payload: Optional[dict] = None,
        issued_at: str = "",
        outage_at: str = "",
        now: str = "",
    ) -> dict[str, Any]:
        """提交命令，返回 {result, business_key, detail}；只有 accepted 生效。"""
        payload = payload or {}
        with self.store.tx():
            existing = self.store.one(
                "SELECT result FROM commands WHERE ns=? AND business_key=?",
                (ns, business_key),
            )
            if existing and existing["result"] == "accepted":
                return self._finish(ns, business_key, unit_id, verb, "duplicate",
                                    "业务键已生效，拒绝重复执行", now)
            acked_log = self.store.one(
                "SELECT status FROM command_logs"
                " WHERE ns=? AND business_key=? AND status='acked'",
                (ns, business_key),
            )
            if acked_log:
                return self._finish(ns, business_key, unit_id, verb, "duplicate",
                                    "停电前命令日志显示该业务键已确认生效", now)

            if outage_at and issued_at and issued_at < outage_at:
                self._record(ns, business_key, unit_id, verb, payload,
                             issued_at, "stale", None, now)
                return self._finish(ns, business_key, unit_id, verb, "stale",
                                    "命令签发于停电之前且从未生效，"
                                    "已由恢复计划接管，拒绝生效", now)

            if unit_id and self._unit_blocked(ns, unit_id):
                self._record(ns, business_key, unit_id, verb, payload,
                             issued_at, "dependency_blocked", None, now)
                return self._finish(ns, business_key, unit_id, verb,
                                    "dependency_blocked",
                                    "目标单元的恢复动作被依赖或证据阻塞，"
                                    "命令暂不生效（业务键未消耗，可重发）", now)

            self._apply(ns, business_key, unit_id, verb, payload, now)
            self._record(ns, business_key, unit_id, verb, payload, issued_at,
                         "accepted", now, now)
            return self._finish(ns, business_key, unit_id, verb, "accepted",
                                "命令已生效", now)

    def _unit_blocked(self, ns: str, unit_id: str) -> bool:
        row = self.store.one(
            "SELECT state FROM actions WHERE ns=? AND unit_id=? AND level=?"
            " ORDER BY created_at DESC LIMIT 1",
            (ns, unit_id, LEVEL_PROCESSING),
        )
        return bool(row and row["state"] == "blocked")

    def _apply(
        self,
        ns: str,
        business_key: str,
        unit_id: Optional[str],
        verb: str,
        payload: dict,
        now: str,
    ) -> None:
        """应用命令效果；物料移动以业务键为效果键，天然幂等。"""
        sign = MATERIAL_DELTA.get(verb)
        if sign is not None and unit_id:
            qty = float(payload.get("qty", 0.0)) * sign
            self.store.execute(
                "INSERT OR IGNORE INTO material_movements"
                "(ns, effect_key, unit_id, delta, reason, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (ns, business_key, unit_id, qty, verb.lower(), now),
            )

    def _record(
        self, ns, business_key, unit_id, verb, payload, issued_at,
        result, applied_at, now,
    ) -> None:
        self.store.execute(
            "INSERT OR REPLACE INTO commands"
            "(ns, business_key, unit_id, verb, payload_json, issued_at,"
            " result, applied_at) VALUES(?,?,?,?,?,?,?,?)",
            (ns, business_key, unit_id, verb, dumps(payload), issued_at,
             result, applied_at),
        )

    def _finish(self, ns, business_key, unit_id, verb, result, detail,
                now) -> dict[str, Any]:
        # 每次提交都留痕（含被拒绝的重复/过期命令），供去重结果审计。
        self.store.execute(
            "INSERT INTO command_attempts"
            "(ns, business_key, unit_id, verb, result, detail, at)"
            " VALUES(?,?,?,?,?,?,?)",
            (ns, business_key, unit_id, verb, result, detail, now),
        )
        self.store.emit(
            ns, "command_result",
            {"business_key": business_key, "result": result, "detail": detail},
            now,
        )
        return {"business_key": business_key, "result": result, "detail": detail}

    def results(self, ns: str) -> list[dict[str, Any]]:
        return self.store.query(
            "SELECT business_key, unit_id, verb, result, detail, at"
            " FROM command_attempts WHERE ns=? ORDER BY seq", (ns,)
        )
