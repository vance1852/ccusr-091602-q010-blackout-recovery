"""一体化裁决系统门面：RecoveryService。

用法概览：
    svc = RecoveryService("recovery.db")
    svc.open_outage("OUT-1", occurred_at="2026-09-19T02:00:00")
    svc.register_unit(...); svc.define_route(...)
    svc.ingest_snapshot(...); svc.ingest_boot_report(...)
    svc.ingest_material_scan(...); svc.ingest_command_log(...)
    svc.adjudicate()            # 四分类 + 最后可信检查点解释
    svc.build_plan()            # 公用工程 -> 输送 -> 加工
    svc.submit_command(...)     # 业务键去重
    svc.execute_ready()         # 崩溃安全执行
    svc.progression_report()    # 阻塞链/账差/去重/最终处置
    drill = svc.create_drill()  # 演练命名空间，不污染正式记录
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import adjudication, approvals, evidence, planning, review
from .commands import CommandGateway
from .execution import Executor
from .models import (
    OFFICIAL_NS,
    EvidencePackage,
    ImmutableError,
)
from .reporting import progression_report
from .store import Store


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecoveryService:
    def __init__(self, db_path: str = ":memory:",
                 clock: Optional[Callable[[], str]] = None):
        self.store = Store(db_path)
        self.clock = clock or _utcnow
        self.gateway = CommandGateway(self.store)
        self.executor = Executor(self.store, self.gateway, self.clock)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------
    # 停电事件与台账
    # ------------------------------------------------------------------
    def open_outage(self, outage_id: str, occurred_at: str,
                    ns: str = OFFICIAL_NS) -> None:
        now = self.clock()
        with self.store.tx():
            self.store.execute(
                "INSERT OR REPLACE INTO meta(ns, key, value) VALUES(?,?,?)",
                (ns, "outage_id", outage_id),
            )
            self.store.execute(
                "INSERT OR REPLACE INTO meta(ns, key, value) VALUES(?,?,?)",
                (ns, "outage_at", occurred_at),
            )
            self.store.emit(ns, "outage_opened",
                            {"outage_id": outage_id, "occurred_at": occurred_at},
                            now)

    def register_unit(self, unit_id: str, kind: str, device_id: str = "",
                      line_id: str = "", utility_id: str = "",
                      recipe_qty: float = 0.0,
                      exposure_sensitive: bool = False,
                      ns: str = OFFICIAL_NS) -> None:
        evidence.register_unit(
            self.store, ns, unit_id, kind,
            device_id or None, line_id or None, utility_id or None,
            recipe_qty, exposure_sensitive, at=self.clock(),
        )

    def define_route(self, device_id: str, operation_id: str,
                     checkpoints: list[str], ns: str = OFFICIAL_NS) -> None:
        evidence.define_route(self.store, ns, device_id, operation_id,
                              checkpoints, at=self.clock())

    # ------------------------------------------------------------------
    # 证据入库（入库后自动复核已执行动作）
    # ------------------------------------------------------------------
    def ingest_snapshot(self, unit_id: str, device_id: str, operation_id: str,
                        checkpoint_id: str, state: str, expected_qty: float,
                        recorded_at: str, meta: Optional[dict] = None,
                        ns: str = OFFICIAL_NS) -> str:
        eid = evidence.ingest_snapshot(
            self.store, ns, unit_id, device_id, operation_id, checkpoint_id,
            state, expected_qty, recorded_at, meta, at=self.clock())
        self._review_after_evidence(ns, unit_id)
        return eid

    def ingest_boot_report(self, device_id: str, boot_state: str,
                           faults: Optional[list[str]] = None,
                           excursion: bool = False,
                           mid_operation: bool = False,
                           restarted_at: str = "",
                           ns: str = OFFICIAL_NS) -> str:
        eid = evidence.ingest_boot_report(
            self.store, ns, device_id, boot_state, faults, excursion,
            mid_operation, restarted_at, at=self.clock())
        units = self.store.query(
            "SELECT unit_id FROM unit_registry WHERE ns=? AND device_id=?",
            (ns, device_id))
        self._review_after_evidence(ns, [u["unit_id"] for u in units])
        return eid

    def ingest_material_scan(self, unit_id: str, scanned_qty: float,
                             location: str = "", scanned_at: str = "",
                             ns: str = OFFICIAL_NS) -> str:
        eid = evidence.ingest_material_scan(
            self.store, ns, unit_id, scanned_qty, location, scanned_at,
            at=self.clock())
        self._review_after_evidence(ns, unit_id)
        return eid

    def ingest_command_log(self, business_key: str, unit_id: str, verb: str,
                           payload: Optional[dict] = None, issued_at: str = "",
                           status: str = "issued",
                           ns: str = OFFICIAL_NS) -> str:
        eid = evidence.ingest_command_log(
            self.store, ns, business_key, unit_id, verb, payload, issued_at,
            status, at=self.clock())
        self._review_after_evidence(ns, unit_id)
        return eid

    def _review_after_evidence(self, ns: str, unit_ids) -> None:
        if isinstance(unit_ids, str):
            unit_ids = [unit_ids]
        review.review_pass(self.store, ns, unit_ids, self.clock())

    # ------------------------------------------------------------------
    # 证据包与裁决
    # ------------------------------------------------------------------
    def evidence_package(self, unit_id: str,
                         ns: str = OFFICIAL_NS) -> EvidencePackage:
        return evidence.build_package(self.store, ns, unit_id)

    def adjudicate(self, ns: str = OFFICIAL_NS) -> dict[str, Any]:
        return adjudication.adjudicate_all(self.store, ns, self.clock())

    def get_decision(self, unit_id: str, ns: str = OFFICIAL_NS):
        return adjudication.get_decision(self.store, ns, unit_id)

    # ------------------------------------------------------------------
    # 人工改判（双人确认）
    # ------------------------------------------------------------------
    def override_decision(self, unit_id: str, manual_decision: str, user: str,
                          ns: str = OFFICIAL_NS) -> dict[str, Any]:
        return approvals.override_decision(
            self.store, ns, unit_id, manual_decision, user, self.clock())

    def confirm_override(self, approval_id: str, user: str,
                         ns: str = OFFICIAL_NS) -> dict[str, Any]:
        return approvals.confirm_override(
            self.store, ns, approval_id, user, self.clock())

    def list_approvals(self, ns: str = OFFICIAL_NS) -> list[dict[str, Any]]:
        return approvals.list_approvals(self.store, ns)

    # ------------------------------------------------------------------
    # 计划与执行
    # ------------------------------------------------------------------
    def build_plan(self, ns: str = OFFICIAL_NS) -> list[dict[str, Any]]:
        return planning.build_plan(self.store, ns, self.clock())

    def list_actions(self, ns: str = OFFICIAL_NS) -> list[dict[str, Any]]:
        return planning.list_actions(self.store, ns)

    def blocking_chain(self, action_id: str,
                       ns: str = OFFICIAL_NS) -> list[dict[str, Any]]:
        return planning.blocking_chain(self.store, ns, action_id)

    def execute_action(self, action_id: str, crash_after: Optional[str] = None,
                       ns: str = OFFICIAL_NS) -> dict[str, Any]:
        return self.executor.execute_action(ns, action_id, crash_after)

    def execute_ready(self, crash_after: Optional[str] = None,
                      ns: str = OFFICIAL_NS) -> list[dict[str, Any]]:
        return self.executor.execute_ready(ns, crash_after)

    def resume_execution(self, ns: str = OFFICIAL_NS) -> list[dict[str, Any]]:
        return self.executor.resume_execution(ns)

    def delete_action(self, action_id: str, ns: str = OFFICIAL_NS) -> None:
        """删除未开始的动作；执行过的动作不可删除。"""
        action = planning.get_action(self.store, ns, action_id)
        if action is None:
            raise KeyError(f"未知动作: {action_id}")
        if action["state"] in ("executing", "completed", "review_required"):
            raise ImmutableError(
                f"动作 {action_id} 已执行（{action['state']}），不可删除；"
                "后续证据只会生成复核结论"
            )
        with self.store.tx():
            self.store.execute(
                "DELETE FROM actions WHERE ns=? AND action_id=?",
                (ns, action_id))
            self.store.emit(ns, "action_deleted",
                            {"action_id": action_id}, self.clock())

    # ------------------------------------------------------------------
    # 命令网关
    # ------------------------------------------------------------------
    def submit_command(self, business_key: str, verb: str,
                       unit_id: Optional[str] = None,
                       payload: Optional[dict] = None,
                       issued_at: Optional[str] = None,
                       ns: str = OFFICIAL_NS) -> dict[str, Any]:
        outage = self.store.one(
            "SELECT value FROM meta WHERE ns=? AND key='outage_at'", (ns,))
        return self.gateway.submit(
            ns, business_key, unit_id, verb, payload,
            issued_at=issued_at or self.clock(),
            outage_at=outage["value"] if outage else "",
            now=self.clock())

    # ------------------------------------------------------------------
    # 演练
    # ------------------------------------------------------------------
    def create_drill(self, src_ns: str = OFFICIAL_NS) -> str:
        """建立演练命名空间：复制证据与台账，不复制任何执行记录。"""
        drill_ns = f"drill-{uuid.uuid4().hex[:8]}"
        self.store.copy_namespace(src_ns, drill_ns)
        with self.store.tx():
            self.store.emit(src_ns, "drill_created",
                            {"drill_ns": drill_ns, "src_ns": src_ns},
                            self.clock())
            self.store.emit(drill_ns, "drill_started",
                            {"src_ns": src_ns}, self.clock())
        return drill_ns

    # ------------------------------------------------------------------
    # 正式推进视图
    # ------------------------------------------------------------------
    def progression_report(self, ns: str = OFFICIAL_NS) -> dict[str, Any]:
        return progression_report(self.store, ns)

    def events(self, ns: str = OFFICIAL_NS,
               type_: Optional[str] = None) -> list[dict[str, Any]]:
        return self.store.events(ns, type_)
