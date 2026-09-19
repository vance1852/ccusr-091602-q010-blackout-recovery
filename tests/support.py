"""测试共用的场景构造：整厂短时断电后的矛盾现场。

拓扑：U1(公用工程) → V1(输送) → P1..P4(加工单元)。
- P1：MES 认为 OP1 仍在执行，缓存完成回执显示已完成，物料账证实已消耗；
- P2：中断敏感工序，断电 120s 超过容忍 30s；
- P3：物料账差（账 10 实 8）；
- P4：只有快照，缺启动报告与物料扫描。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.service import RecoveryService


class FakeClock:
    """每次调用前进 1 秒的单调时钟，保证时间戳确定且递增。"""

    def __init__(self, start: str = "2026-09-19T08:10:00+00:00") -> None:
        self._t = datetime.fromisoformat(start)

    def __call__(self) -> str:
        value = self._t.isoformat()
        self._t += timedelta(seconds=1)
        return value


class SimulatedCrash(Exception):
    """故障注入：模拟进程在命令落账后崩溃。"""


def new_service(db_path: str = ":memory:") -> RecoveryService:
    return RecoveryService(db_path=db_path, clock=FakeClock())


def build_blackout_scenario(svc: RecoveryService, incident: str = "INC-1") -> str:
    svc.register_unit(incident, "U1", "utility")
    svc.register_unit(incident, "V1", "conveyor", requires=["U1"])
    svc.register_unit(
        incident,
        "P1",
        "processing",
        requires=["V1"],
        profile={
            "operations": {"OP1": {"feed": {"M1": 10}}, "OP2": {"feed": {"M2": 5}}},
            "operation_order": ["OP1", "OP2"],
        },
    )
    svc.register_unit(
        incident,
        "P2",
        "processing",
        requires=["V1"],
        profile={
            "operations": {
                "OP1": {
                    "feed": {"M1": 10},
                    "interruption_critical": True,
                    "max_interruption_s": 30,
                }
            },
            "operation_order": ["OP1"],
        },
    )
    svc.register_unit(
        incident,
        "P3",
        "processing",
        requires=["V1"],
        profile={"operations": {"OP1": {"feed": {"M1": 10}}}, "operation_order": ["OP1"]},
    )
    svc.register_unit(
        incident,
        "P4",
        "processing",
        requires=["V1"],
        profile={"operations": {"OP1": {"feed": {"M1": 10}}}, "operation_order": ["OP1"]},
    )

    for unit in ("U1", "V1"):
        svc.record_snapshot(
            incident,
            unit,
            {
                "state": "running",
                "checkpoint_id": f"{unit}-ck1",
                "checkpoint_seq": 1,
                "captured_at": "2026-09-19T07:59:00+00:00",
            },
        )
        svc.record_boot_report(
            incident,
            unit,
            {
                "controller_state": "standby",
                "physical_damage": False,
                "outage_duration_s": 45,
                "reported_at": "2026-09-19T08:01:00+00:00",
            },
        )

    # P1：快照说 OP1 执行中；缓存完成回执说已完成；扫描证实物料已消耗。
    svc.record_snapshot(
        incident,
        "P1",
        {
            "operation": "OP1",
            "step": "process",
            "state": "executing",
            "checkpoint_id": "P1-ck5",
            "checkpoint_seq": 5,
            "expected_materials": {"M1": 10},
            "captured_at": "2026-09-19T07:59:00+00:00",
        },
    )
    svc.record_boot_report(
        incident,
        "P1",
        {
            "controller_state": "standby",
            "physical_damage": False,
            "outage_duration_s": 45,
            "reported_at": "2026-09-19T08:01:00+00:00",
        },
    )
    svc.record_command_log(
        incident,
        "P1",
        [
            {
                "business_key": "INC-1:P1:OP1:feed",
                "command_type": "feed_material",
                "operation": "OP1",
                "materials": {"M1": 10},
                "issued_at": "2026-09-19T07:30:00+00:00",
                "status": "acked",
            },
            {
                "business_key": "INC-1:P1:OP1:complete",
                "command_type": "completion_receipt",
                "operation": "OP1",
                "checkpoint_seq": 6,
                "consumed": {"M1": 10},
                "completed_at": "2026-09-19T07:58:30+00:00",
                "issued_at": "2026-09-19T07:58:30+00:00",
                "status": "cached",
            },
        ],
    )
    svc.record_material_scan(
        incident, "P1", {"materials": {"M1": 0}, "scanned_at": "2026-09-19T08:02:00+00:00"}
    )

    # P2：中断敏感工序，断电超时。
    svc.record_snapshot(
        incident,
        "P2",
        {
            "operation": "OP1",
            "step": "process",
            "state": "executing",
            "checkpoint_id": "P2-ck3",
            "checkpoint_seq": 3,
            "expected_materials": {"M1": 10},
            "captured_at": "2026-09-19T07:59:00+00:00",
        },
    )
    svc.record_boot_report(
        incident,
        "P2",
        {
            "controller_state": "standby",
            "physical_damage": False,
            "outage_duration_s": 120,
            "reported_at": "2026-09-19T08:01:00+00:00",
        },
    )
    svc.record_material_scan(
        incident, "P2", {"materials": {"M1": 10}, "scanned_at": "2026-09-19T08:02:00+00:00"}
    )

    # P3：物料账差（账 10 实 8）。
    svc.record_snapshot(
        incident,
        "P3",
        {
            "operation": "OP1",
            "step": "feed",
            "state": "fed",
            "checkpoint_id": "P3-ck2",
            "checkpoint_seq": 2,
            "expected_materials": {"M1": 10},
            "captured_at": "2026-09-19T07:59:00+00:00",
        },
    )
    svc.record_boot_report(
        incident,
        "P3",
        {
            "controller_state": "standby",
            "physical_damage": False,
            "outage_duration_s": 45,
            "reported_at": "2026-09-19T08:01:00+00:00",
        },
    )
    svc.record_material_scan(
        incident, "P3", {"materials": {"M1": 8}, "scanned_at": "2026-09-19T08:02:00+00:00"}
    )

    # P4：只有快照。
    svc.record_snapshot(
        incident,
        "P4",
        {
            "operation": "OP1",
            "step": "feed",
            "state": "fed",
            "checkpoint_id": "P4-ck2",
            "checkpoint_seq": 2,
            "expected_materials": {"M1": 10},
            "captured_at": "2026-09-19T07:59:00+00:00",
        },
    )
    return incident
