"""测试共用的产线场景与确定性时钟。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import RecoveryService  # noqa: E402

OUTAGE_AT = "2026-09-19T02:00:00"
T0 = "2026-09-19T01:55:00"   # 停电前
T1 = "2026-09-19T02:05:00"   # 恢复后

ROUTE = ["enqueue", "feed", "process", "inspect", "complete"]


def make_clock(start: int = 1_000):
    """确定性时钟：每次调用 +1 秒，返回 ISO 时间串。"""
    state = {"n": start}

    def tick() -> str:
        state["n"] += 1
        return f"2026-09-19T03:{state['n'] // 60:02d}:{state['n'] % 60:02d}"

    return tick


class PlantCase(unittest.TestCase):
    """标准产线：公用工程 -> 输送线 -> 六个加工单元（覆盖四种裁决）。"""

    def setUp(self) -> None:
        self.svc = RecoveryService(":memory:", clock=make_clock())
        svc = self.svc
        svc.open_outage("OUT-1", occurred_at=OUTAGE_AT)
        svc.register_unit("UT-POWER", "utility")
        svc.register_unit("LINE-1", "conveying", utility_id="UT-POWER")
        svc.define_route("DEV-1", "OP-1", ROUTE)
        svc.define_route("DEV-2", "OP-1", ROUTE)
        # 五个加工单元挂在正常的 DEV-1 上。
        for uid in ("U-RESUME", "U-FEED", "U-RECEIPT", "U-INSPECT", "U-UNKNOWN"):
            svc.register_unit(uid, "processing", device_id="DEV-1",
                              line_id="LINE-1", utility_id="UT-POWER",
                              recipe_qty=5.0)
        # 报废单元挂在暴露超限的 DEV-2 上，且为敏感物料。
        svc.register_unit("U-SCRAP", "processing", device_id="DEV-2",
                          line_id="LINE-1", utility_id="UT-POWER",
                          recipe_qty=5.0, exposure_sensitive=True)

    def tearDown(self) -> None:
        self.svc.close()

    # -- 证据快捷方式 ----------------------------------------------------
    def snapshot(self, uid, checkpoint, state="in_progress", qty=5.0,
                 device="DEV-1"):
        return self.svc.ingest_snapshot(
            uid, device, "OP-1", checkpoint, state, qty, recorded_at=T0)

    def boot(self, device="DEV-1", faults=None, excursion=False,
             mid_operation=False, state="standby"):
        return self.svc.ingest_boot_report(
            device, state, faults or [], excursion, mid_operation,
            restarted_at=T1)

    def scan(self, uid, qty, location="LOC-1"):
        return self.svc.ingest_material_scan(uid, qty, location,
                                             scanned_at=T1)

    def fill_standard_evidence(self):
        """六单元标准证据：覆盖全部四种裁决。"""
        svc = self.svc
        # U-RESUME：工序中断于 process，账实相符 -> auto_resume
        self.snapshot("U-RESUME", "process")
        # U-FEED：中断于 enqueue（投料前）-> auto_resume，续作需补投料
        self.snapshot("U-FEED", "enqueue", qty=0.0)
        # U-RECEIPT：已完成，回执缓存未上传 -> auto_resume(upload_receipt)
        self.snapshot("U-RECEIPT", "complete", state="completed")
        svc.ingest_command_log("cmd-receipt-1", "U-RECEIPT", "RECEIPT",
                               issued_at=T0, status="cached")
        # U-SCRAP：暴露超限 + 敏感物料 -> scrap
        self.snapshot("U-SCRAP", "process", device="DEV-2")
        # U-INSPECT：账差 -> inspection_required
        self.snapshot("U-INSPECT", "process")
        # U-UNKNOWN：无快照 -> insufficient_evidence
        self.boot("DEV-1")
        self.boot("DEV-2", excursion=True)
        self.scan("U-RESUME", 5.0)
        self.scan("U-FEED", 0.0)
        self.scan("U-RECEIPT", 5.0)
        self.scan("U-SCRAP", 5.0)
        self.scan("U-INSPECT", 3.0)
        self.scan("U-UNKNOWN", 5.0)

    def adjudicated_plant(self):
        """证据 + 裁决 + 计划一步到位的场景。"""
        self.fill_standard_evidence()
        decisions = self.svc.adjudicate()
        actions = self.svc.build_plan()
        return decisions, actions
