"""恢复计划：把裁决结论编成按依赖排序的恢复动作。

计划按“公用工程 → 输送 → 加工单元”的依赖拓扑排序：前置动作未完成时，
后续动作保持 blocked，不允许越级执行。已执行过的动作不可改写，
未执行的动作可随新证据重新裁决后更新。
"""

from __future__ import annotations

import json
from typing import Any

from .models import (
    CATEGORY_RANK,
    ActionState,
    Decision,
    EXECUTED_STATES,
    UnitCategory,
)
from .store import Store
from .topology import TopologyRepository

#: 动作 id 稳定生成：同一事故同一单元只有一条恢复动作。
def action_id_for(incident_id: str, unit_id: str) -> str:
    return f"{incident_id}:{unit_id}:recovery"


def kind_for(category: str, decision: str) -> str:
    if decision == Decision.INSUFFICIENT_EVIDENCE.value:
        return "collect_evidence"
    if decision == Decision.SCRAP.value:
        return "scrap_unit"
    if decision == Decision.INSPECTION_REQUIRED.value:
        return "inspect_unit"
    return {
        UnitCategory.UTILITY.value: "restore_utility",
        UnitCategory.CONVEYOR.value: "restore_conveyor",
        UnitCategory.PROCESSING.value: "resume_processing",
    }[category]


def action_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "namespace": row["namespace"],
        "incident_id": row["incident_id"],
        "action_id": row["action_id"],
        "unit_id": row["unit_id"],
        "kind": row["kind"],
        "state": row["state"],
        "depends_on": json.loads(row["depends_on"]),
        "decision": row["decision"],
        "manual_choice": row["manual_choice"],
        "requires_dual": bool(row["requires_dual"]),
        "checkpoint": json.loads(row["checkpoint"]) if row["checkpoint"] else None,
        "resume_point": json.loads(row["resume_point"]) if row["resume_point"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class RecoveryPlanner:
    def __init__(self, store: Store, topology: TopologyRepository, clock) -> None:
        self._store = store
        self._topology = topology
        self._clock = clock

    # ------------------------------------------------------------------ 计划
    def build_plan(self, namespace: str, incident_id: str) -> list[dict[str, Any]]:
        """按当前裁决结论生成/更新恢复计划（幂等，可重复调用）。"""
        self._topology.validate(namespace, incident_id)
        units = self._topology.units(namespace, incident_id)
        decisions = self._latest_decisions(namespace, incident_id)
        now = self._clock()
        with self._store.transaction() as conn:
            for unit in units:
                action_id = action_id_for(incident_id, unit["unit_id"])
                dec = decisions.get(unit["unit_id"], {})
                decision = dec.get("decision", Decision.INSUFFICIENT_EVIDENCE.value)
                checkpoint = dec.get("checkpoint")
                resume_point = dec.get("resume_point")
                depends_on = [action_id_for(incident_id, r) for r in unit["requires"]]
                existing = conn.execute(
                    "SELECT * FROM actions WHERE namespace=? AND incident_id=? AND action_id=?",
                    (namespace, incident_id, action_id),
                ).fetchone()
                if existing and existing["state"] in EXECUTED_STATES:
                    continue  # 已执行过的动作不可改写
                manual = existing["manual_choice"] if existing else None
                effective = manual or decision
                kind = kind_for(unit["category"], effective)
                if existing:
                    conn.execute(
                        """
                        UPDATE actions SET kind=?, decision=?, checkpoint=?, resume_point=?,
                               depends_on=?, updated_at=?
                        WHERE namespace=? AND incident_id=? AND action_id=?
                        """,
                        (
                            kind,
                            decision,
                            json.dumps(checkpoint, ensure_ascii=False) if checkpoint else None,
                            json.dumps(resume_point, ensure_ascii=False) if resume_point else None,
                            json.dumps(depends_on, ensure_ascii=False),
                            now,
                            namespace,
                            incident_id,
                            action_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO actions (namespace, incident_id, action_id, unit_id, kind,
                                             state, depends_on, decision, manual_choice,
                                             requires_dual, checkpoint, resume_point,
                                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?)
                        """,
                        (
                            namespace,
                            incident_id,
                            action_id,
                            unit["unit_id"],
                            kind,
                            ActionState.BLOCKED.value,
                            json.dumps(depends_on, ensure_ascii=False),
                            decision,
                            json.dumps(checkpoint, ensure_ascii=False) if checkpoint else None,
                            json.dumps(resume_point, ensure_ascii=False) if resume_point else None,
                            now,
                            now,
                        ),
                    )
            self._refresh_states_locked(conn, namespace, incident_id)
        return self.actions(namespace, incident_id)

    def refresh_states(self, namespace: str, incident_id: str) -> None:
        with self._store.transaction() as conn:
            self._refresh_states_locked(conn, namespace, incident_id)

    def _refresh_states_locked(self, conn, namespace: str, incident_id: str) -> None:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM actions WHERE namespace=? AND incident_id=?",
                (namespace, incident_id),
            )
        ]
        state = {r["action_id"]: r["state"] for r in rows}
        now = self._clock()
        for row in rows:
            if row["state"] in EXECUTED_STATES:
                continue
            effective = row["manual_choice"] or row["decision"]
            if effective == Decision.INSUFFICIENT_EVIDENCE.value:
                new_state = ActionState.BLOCKED.value  # 证据不足：保持阻塞
            else:
                deps = json.loads(row["depends_on"])
                ready = all(state.get(d) == ActionState.COMPLETED.value for d in deps)
                new_state = ActionState.READY.value if ready else ActionState.BLOCKED.value
            if new_state != row["state"]:
                conn.execute(
                    "UPDATE actions SET state=?, updated_at=? "
                    "WHERE namespace=? AND incident_id=? AND action_id=?",
                    (new_state, now, namespace, incident_id, row["action_id"]),
                )
                state[row["action_id"]] = new_state

    # ------------------------------------------------------------------ 查询
    def actions(self, namespace: str, incident_id: str) -> list[dict[str, Any]]:
        rows = self._store.query(
            "SELECT * FROM actions WHERE namespace=? AND incident_id=? ORDER BY action_id",
            (namespace, incident_id),
        )
        return [action_from_row(r) for r in rows]

    def get_action(
        self, namespace: str, incident_id: str, action_id: str
    ) -> dict[str, Any] | None:
        row = self._store.query_one(
            "SELECT * FROM actions WHERE namespace=? AND incident_id=? AND action_id=?",
            (namespace, incident_id, action_id),
        )
        return action_from_row(row) if row else None

    def approvals_count(self, namespace: str, incident_id: str, action_id: str) -> int:
        row = self._store.query_one(
            "SELECT COUNT(DISTINCT approver) AS n FROM approvals "
            "WHERE namespace=? AND incident_id=? AND action_id=?",
            (namespace, incident_id, action_id),
        )
        return int(row["n"]) if row else 0

    def blocking_chains(self, namespace: str, incident_id: str) -> dict[str, Any]:
        """阻塞链：每个未完成动作 ← 未就绪前置动作的依赖路径及根因。"""
        actions = self.actions(namespace, incident_id)
        by_id = {a["action_id"]: a for a in actions}
        state = {a["action_id"]: a["state"] for a in actions}

        def chains_for(action_id: str, seen: frozenset) -> list[list[str]]:
            action = by_id.get(action_id)
            if action is None or action_id in seen:
                return [[action_id]]
            unmet = [d for d in action["depends_on"] if state.get(d) != ActionState.COMPLETED.value]
            if not unmet:
                return [[action_id]]  # 自身即根阻塞（待执行/待确认/证据不足）
            chains: list[list[str]] = []
            for dep in unmet:
                for sub in chains_for(dep, seen | {action_id}):
                    chains.append(sub + [action_id])
            return chains

        chains: dict[str, list[list[str]]] = {}
        reasons: dict[str, str] = {}
        for action in actions:
            if action["state"] == ActionState.COMPLETED.value:
                continue
            chains[action["action_id"]] = chains_for(action["action_id"], frozenset())
            reasons[action["action_id"]] = self._block_reason(namespace, incident_id, action)
        return {"chains": chains, "reasons": reasons}

    def _block_reason(self, namespace: str, incident_id: str, action: dict[str, Any]) -> str:
        state = action["state"]
        if state == ActionState.EXECUTING.value:
            return "executing"
        if state == ActionState.REVIEW_REQUIRED.value:
            return "review_required"
        effective = action["manual_choice"] or action["decision"]
        if state == ActionState.READY.value:
            if action["requires_dual"] and self.approvals_count(
                namespace, incident_id, action["action_id"]
            ) < 2:
                return "awaiting_dual_confirmation"
            return "pending_execution"
        if effective == Decision.INSUFFICIENT_EVIDENCE.value:
            return "insufficient_evidence"
        return "dependency_unmet"

    # ----------------------------------------------------------------- 决策
    def _latest_decisions(self, namespace: str, incident_id: str) -> dict[str, dict[str, Any]]:
        rows = self._store.query(
            """
            SELECT d.* FROM decisions d
            JOIN (
              SELECT unit_id, MAX(id) AS mid FROM decisions
              WHERE namespace=? AND incident_id=? GROUP BY unit_id
            ) t ON d.id = t.mid
            """,
            (namespace, incident_id),
        )
        result = {}
        for row in rows:
            result[row["unit_id"]] = {
                "decision": row["decision"],
                "checkpoint": json.loads(row["checkpoint"]) if row["checkpoint"] else None,
                "resume_point": json.loads(row["resume_point"]) if row["resume_point"] else None,
            }
        return result
