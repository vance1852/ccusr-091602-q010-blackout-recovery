"""恢复裁决服务：面向无人值守恢复的一体化入口。

职责编排：证据接入 → 裁决 → 计划 → （人工覆盖/双人确认）→ 执行 → 复核，
并提供正式/演练命名空间隔离与正式推进视图（阻塞链、物料账差、
命令去重结果、各单元最终处置）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .adjudication import adjudicate_unit
from .commands import CommandGateway
from .evidence import (
    SOURCE_BOOT_REPORT,
    SOURCE_COMMAND_LOG,
    SOURCE_INSPECTION,
    SOURCE_MATERIAL_SCAN,
    SOURCE_SNAPSHOT,
    EvidenceRepository,
)
from .executor import CrashHook, RecoveryExecutor
from .models import (
    DRILL_PREFIX,
    EXECUTED_STATES,
    OFFICIAL_NAMESPACE,
    ActionState,
    CommandResult,
    Decision,
    DecisionRecord,
    DomainError,
    DuplicateApprovalError,
    EvidencePackage,
    ImmutableRecordError,
    ReviewConclusion,
)
from .planner import RecoveryPlanner, action_id_for, kind_for
from .store import NAMESPACED_TABLES, Store
from .topology import TopologyRepository

#: 命令日志条目中不参与去重哈希的元数据字段（command_type 单独成列）。
_LOG_META_FIELDS = {"business_key", "issued_at", "status", "command_type"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecoveryService:
    def __init__(self, db_path: str = ":memory:", clock=None) -> None:
        self._clock = clock or _utc_now
        self.store = Store(db_path)
        self.topology = TopologyRepository(self.store, self._clock)
        self.evidence = EvidenceRepository(self.store, self._clock)
        self.gateway = CommandGateway(self.store, self._clock)
        self.planner = RecoveryPlanner(self.store, self.topology, self._clock)
        self.executor = RecoveryExecutor(
            self.store, self.gateway, self.planner, self.topology, self._clock
        )

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------ 拓扑
    def register_unit(
        self,
        incident_id: str,
        unit_id: str,
        category: str,
        requires: list[str] | tuple[str, ...] = (),
        profile: dict[str, Any] | None = None,
        namespace: str = OFFICIAL_NAMESPACE,
    ) -> None:
        self.topology.register_unit(
            namespace, incident_id, unit_id, category, requires, profile
        )

    # ------------------------------------------------------------------ 证据
    def record_snapshot(self, incident_id: str, unit_id: str, payload: dict,
                        namespace: str = OFFICIAL_NAMESPACE) -> list[dict]:
        return self._record(SOURCE_SNAPSHOT, incident_id, unit_id, payload, namespace)

    def record_boot_report(self, incident_id: str, unit_id: str, payload: dict,
                           namespace: str = OFFICIAL_NAMESPACE) -> list[dict]:
        return self._record(SOURCE_BOOT_REPORT, incident_id, unit_id, payload, namespace)

    def record_material_scan(self, incident_id: str, unit_id: str, payload: dict,
                             namespace: str = OFFICIAL_NAMESPACE) -> list[dict]:
        return self._record(SOURCE_MATERIAL_SCAN, incident_id, unit_id, payload, namespace)

    def record_inspection(self, incident_id: str, unit_id: str, payload: dict,
                          namespace: str = OFFICIAL_NAMESPACE) -> list[dict]:
        return self._record(SOURCE_INSPECTION, incident_id, unit_id, payload, namespace)

    def record_command_log(self, incident_id: str, unit_id: str, entries: list[dict],
                           namespace: str = OFFICIAL_NAMESPACE) -> list[dict]:
        """导入命令日志（含缓存的完成回执），并登记到命令去重账。"""
        reviews: list[dict] = []
        for entry in entries:
            reviews.extend(self._record(SOURCE_COMMAND_LOG, incident_id, unit_id, entry, namespace))
            payload = {k: v for k, v in entry.items() if k not in _LOG_META_FIELDS}
            self.gateway.register_historical(
                namespace,
                incident_id,
                entry["business_key"],
                unit_id,
                entry.get("command_type", "unknown"),
                payload,
                entry.get("issued_at", ""),
            )
        return reviews

    def _record(self, source: str, incident_id: str, unit_id: str,
                payload: dict, namespace: str) -> list[dict]:
        self.evidence.record(namespace, incident_id, unit_id, source, payload)
        return self._after_evidence(incident_id, namespace, unit_id)

    def evidence_package(self, incident_id: str, unit_id: str,
                         namespace: str = OFFICIAL_NAMESPACE) -> EvidencePackage:
        unit = self.topology.get(namespace, incident_id, unit_id)
        if unit is None:
            raise DomainError(f"单元未登记: {unit_id}")
        return self.evidence.package(namespace, incident_id, unit_id, unit["category"])

    # ------------------------------------------------------------------ 裁决
    def adjudicate(self, incident_id: str,
                   namespace: str = OFFICIAL_NAMESPACE) -> dict[str, DecisionRecord]:
        """对全部已登记单元裁决，返回 unit_id → DecisionRecord。"""
        results: dict[str, DecisionRecord] = {}
        for unit in self.topology.units(namespace, incident_id):
            pkg = self.evidence.package(
                namespace, incident_id, unit["unit_id"], unit["category"]
            )
            record = adjudicate_unit(pkg, unit["profile"])
            self._store_decision(namespace, incident_id, record)
            results[unit["unit_id"]] = record
        return results

    def _store_decision(self, namespace: str, incident_id: str,
                        record: DecisionRecord) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO decisions (namespace, incident_id, unit_id, decision, checkpoint, "
                "rationale, material_account, missing_evidence, resume_point, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    namespace,
                    incident_id,
                    record.unit_id,
                    record.decision.value,
                    json.dumps(record.last_trusted_checkpoint.to_dict(), ensure_ascii=False)
                    if record.last_trusted_checkpoint
                    else None,
                    json.dumps(record.rationale, ensure_ascii=False),
                    json.dumps(record.material_account, ensure_ascii=False),
                    json.dumps(record.missing_evidence, ensure_ascii=False),
                    json.dumps(record.resume_point, ensure_ascii=False)
                    if record.resume_point
                    else None,
                    self._clock(),
                ),
            )

    # ------------------------------------------------------------------ 计划
    def create_plan(self, incident_id: str,
                    namespace: str = OFFICIAL_NAMESPACE) -> list[dict[str, Any]]:
        return self.planner.build_plan(namespace, incident_id)

    def actions(self, incident_id: str,
                namespace: str = OFFICIAL_NAMESPACE) -> list[dict[str, Any]]:
        return self.planner.actions(namespace, incident_id)

    # ---------------------------------------------------------- 覆盖与确认
    def override_decision(self, incident_id: str, unit_id: str, manual_decision: str,
                          namespace: str = OFFICIAL_NAMESPACE) -> dict[str, Any]:
        """人工选择处置方式；与自动建议不一致时要求双人确认。"""
        Decision(manual_decision)  # 校验合法
        action_id = action_id_for(incident_id, unit_id)
        action = self.planner.get_action(namespace, incident_id, action_id)
        if action is None:
            raise DomainError(f"动作不存在: {action_id}，请先创建计划")
        if action["state"] in EXECUTED_STATES:
            raise ImmutableRecordError(f"动作 {action_id} 已执行，不可覆盖")
        unit = self.topology.get(namespace, incident_id, unit_id)
        auto = action["decision"]
        if manual_decision == auto:
            manual, requires_dual = None, 0
        else:
            manual, requires_dual = manual_decision, 1
        kind = kind_for(unit["category"], manual or auto)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE actions SET manual_choice=?, requires_dual=?, kind=?, updated_at=? "
                "WHERE namespace=? AND incident_id=? AND action_id=?",
                (manual, requires_dual, kind, self._clock(), namespace, incident_id, action_id),
            )
        self.planner.refresh_states(namespace, incident_id)
        return self.planner.get_action(namespace, incident_id, action_id)

    def confirm_action(self, incident_id: str, action_id: str, approver: str,
                       namespace: str = OFFICIAL_NAMESPACE) -> int:
        """双人确认：两名不同确认人确认后动作才可执行。返回当前确认人数。"""
        action = self.planner.get_action(namespace, incident_id, action_id)
        if action is None:
            raise DomainError(f"动作不存在: {action_id}")
        if action["state"] in EXECUTED_STATES:
            raise DomainError(f"动作 {action_id} 已执行，无需确认")
        try:
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT INTO approvals (namespace, incident_id, action_id, approver, approved_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (namespace, incident_id, action_id, approver, self._clock()),
                )
        except sqlite3.IntegrityError:
            raise DuplicateApprovalError(f"确认人 {approver} 已确认过 {action_id}") from None
        return self.planner.approvals_count(namespace, incident_id, action_id)

    # ------------------------------------------------------------------ 执行
    def execute_ready(self, incident_id: str, namespace: str = OFFICIAL_NAMESPACE,
                      crash_hook: CrashHook | None = None) -> dict[str, Any]:
        return self.executor.execute_ready(namespace, incident_id, crash_hook)

    def execute_action(self, incident_id: str, action_id: str,
                       namespace: str = OFFICIAL_NAMESPACE,
                       crash_hook: CrashHook | None = None) -> dict[str, Any]:
        return self.executor.execute_action(namespace, incident_id, action_id, crash_hook)

    def recover(self, incident_id: str,
                namespace: str = OFFICIAL_NAMESPACE) -> dict[str, Any]:
        """进程崩溃后的续跑入口：先幂等重放在途动作，再继续推进。"""
        recovered = self.executor.recover_inflight(namespace, incident_id)
        summary = self.executor.execute_ready(namespace, incident_id)
        summary["recovered_inflight"] = recovered
        return summary

    # ------------------------------------------------------------------ 命令
    def dispatch_command(self, incident_id: str, unit_id: str, business_key: str,
                         command_type: str, payload: dict, issued_at: str,
                         namespace: str = OFFICIAL_NAMESPACE) -> str:
        """外部命令入口（含网络恢复后到达的旧命令）：去重 + 依赖闸口。"""
        unit = self.topology.get(namespace, incident_id, unit_id)
        if unit is None:
            raise DomainError(f"单元未登记: {unit_id}")
        deps_met = all(
            (self.planner.get_action(namespace, incident_id, action_id_for(incident_id, dep)) or {})
            .get("state")
            == ActionState.COMPLETED.value
            for dep in unit["requires"]
        )
        return self.gateway.dispatch(
            namespace, incident_id, business_key, unit_id, command_type,
            payload, issued_at, dependencies_met=deps_met,
        )

    # ------------------------------------------------------------------ 删除
    def delete_action(self, incident_id: str, action_id: str,
                      namespace: str = OFFICIAL_NAMESPACE) -> bool:
        """执行过的恢复动作不可删除；未执行的动作允许从计划中移除。"""
        action = self.planner.get_action(namespace, incident_id, action_id)
        if action is None:
            return False
        if action["state"] in EXECUTED_STATES:
            raise ImmutableRecordError(f"动作 {action_id} 已执行，不可删除")
        with self.store.transaction() as conn:
            conn.execute(
                "DELETE FROM approvals WHERE namespace=? AND incident_id=? AND action_id=?",
                (namespace, incident_id, action_id),
            )
            conn.execute(
                "DELETE FROM actions WHERE namespace=? AND incident_id=? AND action_id=?",
                (namespace, incident_id, action_id),
            )
        return True

    # ------------------------------------------------------------------ 演练
    def start_drill(self, incident_id: str) -> str:
        """基于正式证据开启一次演练，返回独立的演练命名空间。"""
        now = self._clock()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT next_seq FROM drill_registry WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            seq = row["next_seq"] if row else 1
            conn.execute(
                "INSERT INTO drill_registry (incident_id, next_seq) VALUES (?, ?) "
                "ON CONFLICT (incident_id) DO UPDATE SET next_seq = excluded.next_seq",
                (incident_id, seq + 1),
            )
            namespace = f"{DRILL_PREFIX}{incident_id}:{seq}"
            conn.execute(
                "INSERT INTO units (namespace, incident_id, unit_id, category, requires, profile, created_at) "
                "SELECT ?, incident_id, unit_id, category, requires, profile, ? "
                "FROM units WHERE namespace=? AND incident_id=?",
                (namespace, now, OFFICIAL_NAMESPACE, incident_id),
            )
            conn.execute(
                "INSERT INTO evidence (namespace, incident_id, unit_id, source, payload, recorded_at) "
                "SELECT ?, incident_id, unit_id, source, payload, recorded_at "
                "FROM evidence WHERE namespace=? AND incident_id=?",
                (namespace, OFFICIAL_NAMESPACE, incident_id),
            )
            conn.execute(
                "INSERT INTO commands (namespace, incident_id, business_key, unit_id, command_type, "
                "payload_hash, payload, issued_at, applied_at) "
                "SELECT ?, incident_id, business_key, unit_id, command_type, payload_hash, payload, "
                "issued_at, applied_at FROM commands WHERE namespace=? AND incident_id=?",
                (namespace, OFFICIAL_NAMESPACE, incident_id),
            )
        return namespace

    def reset_drill(self, drill_namespace: str) -> int:
        """清空一次演练的全部记录（仅演练命名空间可重置）。"""
        if not drill_namespace.startswith(DRILL_PREFIX):
            raise DomainError("仅演练命名空间可重置，正式记录不可清除")
        deleted = 0
        with self.store.transaction() as conn:
            for table in NAMESPACED_TABLES:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE namespace=?", (drill_namespace,)
                )
                deleted += cur.rowcount
        return deleted

    # ------------------------------------------------------------------ 视图
    def dashboard(self, incident_id: str,
                  namespace: str = OFFICIAL_NAMESPACE) -> dict[str, Any]:
        """正式推进视图：阻塞链、物料账差、命令去重结果、各单元最终处置。"""
        actions = self.planner.actions(namespace, incident_id)
        decisions = self._latest_decisions(namespace, incident_id)
        attempts = self.gateway.attempts(namespace, incident_id)
        counts = {r.value: 0 for r in CommandResult}
        for attempt in attempts:
            counts[attempt["result"]] = counts.get(attempt["result"], 0) + 1
        reviews = self._reviews(namespace, incident_id)
        dispositions: dict[str, Any] = {}
        for action in actions:
            dec = decisions.get(action["unit_id"], {})
            dispositions[action["unit_id"]] = {
                "decision": action["decision"],
                "manual_choice": action["manual_choice"],
                "action_state": action["state"],
                "kind": action["kind"],
                "last_trusted_checkpoint": action["checkpoint"],
                "rationale": dec.get("rationale", []),
                "reviews": reviews.get(action["action_id"], []),
            }
        return {
            "incident_id": incident_id,
            "namespace": namespace,
            "blocking_chains": self.planner.blocking_chains(namespace, incident_id),
            "material_discrepancies": {
                unit_id: dec["material_account"]
                for unit_id, dec in decisions.items()
                if dec.get("material_account", {}).get("deltas")
            },
            "command_results": {
                "counts": counts,
                "rejected": [
                    {
                        "business_key": a["business_key"],
                        "unit_id": a["unit_id"],
                        "command_type": a["command_type"],
                        "result": a["result"],
                        "detail": a["detail"],
                    }
                    for a in attempts
                    if a["result"] != CommandResult.ACCEPTED.value
                ],
            },
            "unit_dispositions": dispositions,
            "material_ledger": self.material_ledger(incident_id, namespace),
        }

    def material_ledger(self, incident_id: str,
                        namespace: str = OFFICIAL_NAMESPACE) -> dict[str, Any]:
        """恢复期间的投料账：feed_material 入账，scrap 出账。"""
        ledger: dict[str, dict[str, dict[str, float]]] = {}
        for effect in self.gateway.effects(namespace, incident_id):
            payload = json.loads(effect["payload"])
            materials = payload.get("materials", {})
            entry = ledger.setdefault(effect["unit_id"], {})
            for material, qty in materials.items():
                account = entry.setdefault(material, {"fed": 0, "scrapped": 0, "net": 0})
                if effect["effect_type"] == "feed_material":
                    account["fed"] += qty
                elif effect["effect_type"] == "scrap":
                    account["scrapped"] += qty
                account["net"] = account["fed"] - account["scrapped"]
        return ledger

    # ------------------------------------------------------------------ 复核
    def _after_evidence(self, incident_id: str, namespace: str,
                        unit_id: str) -> list[dict[str, Any]]:
        """新证据到达后的处理：已执行动作只生成复核结论，未执行动作随新裁决更新。"""
        action_id = action_id_for(incident_id, unit_id)
        action = self.planner.get_action(namespace, incident_id, action_id)
        unit = self.topology.get(namespace, incident_id, unit_id)
        if action is None or unit is None:
            return []
        pkg = self.evidence.package(namespace, incident_id, unit_id, unit["category"])
        record = adjudicate_unit(pkg, unit["profile"])
        self._store_decision(namespace, incident_id, record)

        if action["state"] in EXECUTED_STATES:
            executed_disposition = action["manual_choice"] or action["decision"]
            if record.decision.value == executed_disposition:
                conclusion = ReviewConclusion.CONFIRMED.value
            else:
                conclusion = ReviewConclusion.CONTRADICTED.value
            detail = json.dumps(
                {
                    "basis_disposition": executed_disposition,
                    "new_decision": record.decision.value,
                    "rationale": record.rationale,
                },
                ensure_ascii=False,
            )
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT INTO reviews (namespace, incident_id, action_id, conclusion, detail, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (namespace, incident_id, action_id, conclusion, detail, self._clock()),
                )
                if conclusion == ReviewConclusion.CONTRADICTED.value:
                    conn.execute(
                        "UPDATE actions SET state=?, updated_at=? "
                        "WHERE namespace=? AND incident_id=? AND action_id=?",
                        (ActionState.REVIEW_REQUIRED.value, self._clock(),
                         namespace, incident_id, action_id),
                    )
            return [{"action_id": action_id, "conclusion": conclusion}]

        # 未执行：按最新裁决更新待定动作（人工覆盖仍然有效）。
        manual = action["manual_choice"]
        requires_dual = 1 if (manual and manual != record.decision.value) else 0
        effective = manual or record.decision.value
        kind = kind_for(unit["category"], effective)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE actions SET decision=?, checkpoint=?, resume_point=?, kind=?, "
                "requires_dual=?, updated_at=? "
                "WHERE namespace=? AND incident_id=? AND action_id=?",
                (
                    record.decision.value,
                    json.dumps(record.last_trusted_checkpoint.to_dict(), ensure_ascii=False)
                    if record.last_trusted_checkpoint
                    else None,
                    json.dumps(record.resume_point, ensure_ascii=False)
                    if record.resume_point
                    else None,
                    kind,
                    requires_dual,
                    self._clock(),
                    namespace,
                    incident_id,
                    action_id,
                ),
            )
        self.planner.refresh_states(namespace, incident_id)
        return []

    # ------------------------------------------------------------------ 内部
    def _latest_decisions(self, namespace: str, incident_id: str) -> dict[str, dict[str, Any]]:
        rows = self.store.query(
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
                "rationale": json.loads(row["rationale"]),
                "material_account": json.loads(row["material_account"]),
                "missing_evidence": json.loads(row["missing_evidence"]),
                "resume_point": json.loads(row["resume_point"]) if row["resume_point"] else None,
            }
        return result

    def _reviews(self, namespace: str, incident_id: str) -> dict[str, list[dict[str, Any]]]:
        rows = self.store.query(
            "SELECT * FROM reviews WHERE namespace=? AND incident_id=? ORDER BY id",
            (namespace, incident_id),
        )
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(row["action_id"], []).append(
                {
                    "conclusion": row["conclusion"],
                    "detail": json.loads(row["detail"]),
                    "created_at": row["created_at"],
                }
            )
        return result
