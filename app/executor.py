"""恢复执行器：按依赖顺序执行恢复动作，崩溃后可安全续跑。

崩溃安全的关键设计：
- 每条命令的业务键由（事故、单元、工序、检查点）确定性生成，重放结果不变；
- 命令与效果由去重网关在同一事务落账；
- 动作状态迁移（executing/completed）各自独立提交；
- 崩溃后 ``recover_inflight`` 对 executing 状态的动作做幂等重放：
  已落账的命令返回 duplicate，不会多投一次料。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .commands import CommandGateway
from .models import (
    CATEGORY_RANK,
    ActionState,
    ConfirmationRequiredError,
    DomainError,
    UnitCategory,
)
from .planner import RecoveryPlanner
from .store import Store
from .topology import TopologyRepository


@dataclass(frozen=True)
class CommandSpec:
    key: str
    command_type: str
    payload: dict[str, Any]


#: 故障注入钩子：在每条命令落账后回调，用于测试模拟进程崩溃。
CrashHook = Callable[[CommandSpec, str], None]


class RecoveryExecutor:
    def __init__(
        self,
        store: Store,
        gateway: CommandGateway,
        planner: RecoveryPlanner,
        topology: TopologyRepository,
        clock,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._planner = planner
        self._topology = topology
        self._clock = clock

    # ------------------------------------------------------------------ 执行
    def execute_ready(
        self,
        namespace: str,
        incident_id: str,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        """按依赖顺序执行所有就绪动作，直到没有可推进的动作为止。"""
        summary: dict[str, Any] = {
            "executed": [],
            "awaiting_confirmation": [],
            "blocked": [],
        }
        ranks = self._category_ranks(namespace, incident_id)
        while True:
            self._planner.refresh_states(namespace, incident_id)
            ready = [
                a
                for a in self._planner.actions(namespace, incident_id)
                if a["state"] == ActionState.READY.value
            ]
            if not ready:
                break
            ready.sort(key=lambda a: (ranks.get(a["unit_id"], 99), a["action_id"]))
            progressed = False
            for action in ready:
                if action["requires_dual"] and self._planner.approvals_count(
                    namespace, incident_id, action["action_id"]
                ) < 2:
                    if action["action_id"] not in summary["awaiting_confirmation"]:
                        summary["awaiting_confirmation"].append(action["action_id"])
                    continue
                self.execute_action(namespace, incident_id, action["action_id"], crash_hook)
                summary["executed"].append(action["action_id"])
                progressed = True
            if not progressed:
                break
        self._planner.refresh_states(namespace, incident_id)
        summary["blocked"] = [
            a["action_id"]
            for a in self._planner.actions(namespace, incident_id)
            if a["state"] == ActionState.BLOCKED.value
        ]
        return summary

    def execute_action(
        self,
        namespace: str,
        incident_id: str,
        action_id: str,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        """执行单个动作；对 executing 状态的动作调用等价于崩溃重放。"""
        action = self._planner.get_action(namespace, incident_id, action_id)
        if action is None:
            raise DomainError(f"动作不存在: {action_id}")
        if action["state"] == ActionState.COMPLETED.value:
            return {"action_id": action_id, "commands": [], "note": "already_completed"}
        if action["state"] not in (ActionState.READY.value, ActionState.EXECUTING.value):
            raise DomainError(f"动作未就绪，当前状态: {action['state']}")
        if action["requires_dual"] and self._planner.approvals_count(
            namespace, incident_id, action_id
        ) < 2:
            raise ConfirmationRequiredError(
                f"动作 {action_id} 的人工选择与自动建议不一致，需双人确认"
            )
        if action["kind"] == "collect_evidence":
            raise DomainError(f"动作 {action_id} 证据不足，不可执行")

        self._set_state(namespace, incident_id, action_id, ActionState.EXECUTING.value)
        results = []
        for spec in self.command_specs(namespace, incident_id, action):
            result = self._gateway.dispatch(
                namespace,
                incident_id,
                spec.key,
                action["unit_id"],
                spec.command_type,
                spec.payload,
                issued_at=self._clock(),
                dependencies_met=True,  # 就绪性已由计划层保证
            )
            results.append({"key": spec.key, "result": result})
            if crash_hook is not None:
                crash_hook(spec, result)
        self._set_state(namespace, incident_id, action_id, ActionState.COMPLETED.value)
        # 让依赖本动作的下游动作立即进入就绪评估。
        self._planner.refresh_states(namespace, incident_id)
        return {"action_id": action_id, "commands": results}

    def recover_inflight(self, namespace: str, incident_id: str) -> list[str]:
        """崩溃恢复：对 executing 状态的动作做幂等重放。"""
        inflight = [
            a["action_id"]
            for a in self._planner.actions(namespace, incident_id)
            if a["state"] == ActionState.EXECUTING.value
        ]
        for action_id in inflight:
            self.execute_action(namespace, incident_id, action_id)
        return inflight

    # ------------------------------------------------------------------ 命令
    def command_specs(
        self, namespace: str, incident_id: str, action: dict[str, Any]
    ) -> list[CommandSpec]:
        """由动作类型与续作点确定性地生成命令序列（业务键稳定）。"""
        unit = self._topology.get(namespace, incident_id, action["unit_id"])
        profile = (unit or {}).get("profile", {})
        category = (unit or {}).get("category", "")
        incident = action["incident_id"]
        uid = action["unit_id"]
        kind = action["kind"]
        resume = action["resume_point"] or {}
        checkpoint = action["checkpoint"] or {}
        seq = checkpoint.get("seq", 0)

        if kind in ("restore_utility", "restore_conveyor"):
            return [
                CommandSpec(
                    f"{incident}:{uid}:restore",
                    "start_unit",
                    {"mode": "restore", "category": category},
                )
            ]
        if kind == "resume_processing":
            operation = resume.get("operation")
            step = resume.get("step")
            if step == "done" or operation is None:
                return [CommandSpec(f"{incident}:{uid}:complete", "complete_unit", {})]
            if step == "feed":
                feed = (profile.get("operations") or {}).get(operation, {}).get("feed", {})
                return [
                    CommandSpec(
                        f"{incident}:{uid}:{operation}:feed",
                        "feed_material",
                        {"operation": operation, "materials": feed},
                    ),
                    CommandSpec(
                        f"{incident}:{uid}:{operation}:start",
                        "start_operation",
                        {"operation": operation},
                    ),
                ]
            return [
                CommandSpec(
                    f"{incident}:{uid}:{operation}:resume:{seq}",
                    "resume_operation",
                    {"operation": operation, "from_checkpoint": seq},
                )
            ]
        if kind == "inspect_unit":
            return [
                CommandSpec(
                    f"{incident}:{uid}:inspect:{seq}",
                    "inspect_unit",
                    {"operation": resume.get("operation")},
                )
            ]
        if kind == "scrap_unit":
            materials = self._station_materials(namespace, incident_id, uid)
            return [
                CommandSpec(
                    f"{incident}:{uid}:scrap:{seq}",
                    "scrap_unit",
                    {"materials": materials},
                )
            ]
        return []  # collect_evidence：无命令，等待证据补齐

    # ------------------------------------------------------------------ 内部
    def _station_materials(self, namespace: str, incident_id: str, unit_id: str) -> dict:
        row = self._store.query_one(
            "SELECT payload FROM evidence WHERE namespace=? AND incident_id=? AND unit_id=? "
            "AND source='material_scan' ORDER BY id DESC LIMIT 1",
            (namespace, incident_id, unit_id),
        )
        if row:
            return json.loads(row["payload"]).get("materials", {})
        row = self._store.query_one(
            "SELECT payload FROM evidence WHERE namespace=? AND incident_id=? AND unit_id=? "
            "AND source='snapshot' ORDER BY id DESC LIMIT 1",
            (namespace, incident_id, unit_id),
        )
        if row:
            return json.loads(row["payload"]).get("expected_materials", {})
        return {}

    def _category_ranks(self, namespace: str, incident_id: str) -> dict[str, int]:
        return {
            u["unit_id"]: CATEGORY_RANK[UnitCategory(u["category"])]
            for u in self._topology.units(namespace, incident_id)
        }

    def _set_state(self, namespace: str, incident_id: str, action_id: str, state: str) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE actions SET state=?, updated_at=? "
                "WHERE namespace=? AND incident_id=? AND action_id=?",
                (state, self._clock(), namespace, incident_id, action_id),
            )
