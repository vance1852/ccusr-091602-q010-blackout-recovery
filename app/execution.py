"""崩溃安全的恢复动作执行器。

三阶段写前日志（同一业务键贯穿网关与物料账，天然幂等）：
1. intent  —— 事务内写入执行日志并把动作置为 executing
2. effect  —— 逐条效果经命令网关下发（网关按业务键去重）
3. outcome —— 事务内把日志与动作置为 completed

进程在任意阶段崩溃后，resume() 重放未完成动作：已生效的效果被网关
判重跳过，未生效的补发 —— 不会多投一次料。
"""
from __future__ import annotations

from typing import Any, Optional

from .commands import CommandGateway
from .models import (
    FEED_CHECKPOINT,
    BlockedError,
    SimulatedCrash,
)
from .planning import blocking_chain, get_action, list_actions, refresh_states
from .store import Store, dumps, loads


class Executor:
    def __init__(self, store: Store, gateway: CommandGateway, clock):
        self.store = store
        self.gateway = gateway
        self.clock = clock

    # ------------------------------------------------------------------
    # 效果编排
    # ------------------------------------------------------------------
    def _effects_for(self, action: dict[str, Any]) -> list[dict[str, Any]]:
        """根据动作动词与裁决依据生成效果列表（每条都有稳定业务键）。"""
        aid = action["action_id"]
        verb = action["verb"]
        unit_id = action["unit_id"]
        basis = action.get("decision_basis") or {}

        def cmd(v: str, payload: Optional[dict] = None, tag: Optional[str] = None):
            return {
                "business_key": f"exec:{aid}:{tag or v.lower()}",
                "unit_id": unit_id,
                "verb": v,
                "payload": payload or {},
            }

        if verb == "restore_utility":
            return [cmd("SET_UTILITY_READY")]
        if verb == "restore_conveying":
            return [cmd("SET_LINE_READY")]
        if verb == "upload_receipt":
            return [cmd("RECEIPT")]
        if verb == "inspect":
            return [cmd("INSPECT")]
        if verb == "scrap":
            qty = self._expected_qty(action["ns"], unit_id)
            return [
                cmd("SCRAP"),
                {
                    "business_key": f"exec:{aid}:scrap-material",
                    "unit_id": unit_id,
                    "verb": "CONSUME",
                    "payload": {"qty": qty},
                },
            ]
        if verb == "resume_operation":
            effects: list[dict[str, Any]] = []
            if self._needs_feed(action, basis):
                qty = self._recipe_qty(action["ns"], unit_id)
                effects.append(cmd("FEED", {"qty": qty}, tag="feed"))
            effects.append(cmd("RESUME"))
            return effects
        return []

    def _needs_feed(self, action: dict[str, Any], basis: dict[str, Any]) -> bool:
        """最后可信检查点早于投料检查点时，续作才需要补投料。"""
        from .evidence import get_route

        ns, unit_id = action["ns"], action["unit_id"]
        cp_id = basis.get("checkpoint_id")
        if not cp_id:
            return False
        unit = self.store.one(
            "SELECT device_id FROM unit_registry WHERE ns=? AND unit_id=?",
            (ns, unit_id),
        )
        snap = self.store.one(
            "SELECT operation_id FROM snapshots WHERE ns=? AND unit_id=?",
            (ns, unit_id),
        )
        route = get_route(
            self.store, ns,
            (unit or {}).get("device_id"), (snap or {}).get("operation_id"),
        )
        if FEED_CHECKPOINT not in route or cp_id not in route:
            return False
        return route.index(cp_id) < route.index(FEED_CHECKPOINT)

    def _recipe_qty(self, ns: str, unit_id: Optional[str]) -> float:
        row = self.store.one(
            "SELECT recipe_qty FROM unit_registry WHERE ns=? AND unit_id=?",
            (ns, unit_id),
        )
        return float(row["recipe_qty"]) if row else 0.0

    def _expected_qty(self, ns: str, unit_id: Optional[str]) -> float:
        row = self.store.one(
            "SELECT expected_qty FROM snapshots WHERE ns=? AND unit_id=?",
            (ns, unit_id),
        )
        return float(row["expected_qty"]) if row else 0.0

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def execute_action(
        self, ns: str, action_id: str, crash_after: Optional[str] = None
    ) -> dict[str, Any]:
        """执行单个动作；已完成动作幂等返回，被阻塞动作抛 BlockedError。"""
        refresh_states(self.store, ns, self.clock())
        action = get_action(self.store, ns, action_id)
        if action is None:
            raise KeyError(f"未知动作: {action_id}")
        if action["state"] == "completed":
            return {"action_id": action_id, "state": "completed",
                    "detail": "动作已完成，幂等跳过"}
        if action["state"] == "blocked":
            raise BlockedError(action_id, blocking_chain(self.store, ns, action_id))

        now = self.clock()
        effects = self._effects_for(action)

        # 阶段 1：intent（事务）
        with self.store.tx():
            self.store.execute(
                "INSERT OR IGNORE INTO exec_journal"
                "(ns, action_id, phase, effect_json, updated_at)"
                " VALUES(?,?,?,?,?)",
                (ns, action_id, "intent", dumps(effects), now),
            )
            self.store.execute(
                "UPDATE actions SET state='executing'"
                " WHERE ns=? AND action_id=? AND state!='completed'",
                (ns, action_id),
            )
            self.store.emit(ns, "execution_intent",
                            {"action_id": action_id}, now)
        if crash_after == "intent":
            raise SimulatedCrash(f"崩溃于 intent 之后: {action_id}")

        # 阶段 2：effect（经网关逐条下发，网关事务内判重）
        journal = self.store.one(
            "SELECT effect_json FROM exec_journal WHERE ns=? AND action_id=?",
            (ns, action_id),
        )
        effects = loads(journal["effect_json"], []) if journal else effects
        outage_at = self._outage_at(ns)
        for eff in effects:
            self.gateway.submit(
                ns,
                business_key=eff["business_key"],
                unit_id=eff["unit_id"],
                verb=eff["verb"],
                payload=eff["payload"],
                issued_at=now,
                outage_at=outage_at,
                now=now,
            )
        if crash_after == "effect":
            raise SimulatedCrash(f"崩溃于 effect 之后: {action_id}")

        # 阶段 3：outcome（事务）
        self._finalize(ns, action_id)
        return {"action_id": action_id, "state": "completed",
                "detail": "动作执行完成"}

    def _finalize(self, ns: str, action_id: str) -> None:
        now = self.clock()
        with self.store.tx():
            self.store.execute(
                "UPDATE exec_journal SET phase='completed', updated_at=?"
                " WHERE ns=? AND action_id=?",
                (now, ns, action_id),
            )
            self.store.execute(
                "UPDATE actions SET state='completed', executed_at=?,"
                " blocked_reason=NULL WHERE ns=? AND action_id=?",
                (now, ns, action_id),
            )
            self.store.emit(ns, "execution_completed",
                            {"action_id": action_id}, now)

    def execute_ready(
        self, ns: str, crash_after: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """按依赖层级顺序执行所有就绪动作（公用工程 -> 输送 -> 加工）。"""
        results: list[dict[str, Any]] = []
        while True:
            refresh_states(self.store, ns, self.clock())
            ready = [
                a for a in list_actions(self.store, ns) if a["state"] == "ready"
            ]
            if not ready:
                return results
            for action in ready:
                results.append(
                    self.execute_action(ns, action["action_id"], crash_after)
                )

    def resume_execution(self, ns: str) -> list[dict[str, Any]]:
        """崩溃恢复：重放未完成动作（效果幂等），随后继续执行就绪动作。"""
        results: list[dict[str, Any]] = []
        pending = self.store.query(
            "SELECT action_id FROM exec_journal"
            " WHERE ns=? AND phase='intent' ORDER BY rowid",
            (ns,),
        )
        for row in pending:
            action = get_action(self.store, ns, row["action_id"])
            if action is None or action["state"] == "completed":
                continue
            # 重放效果：网关按业务键判重，已生效的不会重复生效。
            journal = self.store.one(
                "SELECT effect_json FROM exec_journal"
                " WHERE ns=? AND action_id=?",
                (ns, row["action_id"]),
            )
            now = self.clock()
            outage_at = self._outage_at(ns)
            for eff in loads(journal["effect_json"], []):
                self.gateway.submit(
                    ns,
                    business_key=eff["business_key"],
                    unit_id=eff["unit_id"],
                    verb=eff["verb"],
                    payload=eff["payload"],
                    issued_at=now,
                    outage_at=outage_at,
                    now=now,
                )
            self._finalize(ns, row["action_id"])
            results.append({"action_id": row["action_id"], "state": "completed",
                            "detail": "崩溃后重放完成"})
        results.extend(self.execute_ready(ns))
        return results

    def _outage_at(self, ns: str) -> str:
        row = self.store.one(
            "SELECT value FROM meta WHERE ns=? AND key='outage_at'", (ns,)
        )
        return row["value"] if row else ""
