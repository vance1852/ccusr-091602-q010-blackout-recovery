"""端到端：从矛盾现场到正式推进的完整叙事。

整厂短时断电后：MES 认为工序仍在执行、控制器重启回到待机、完成回执
缓存未上传。系统汇成证据包 -> 裁决 -> 计划 -> 去重 -> 双人确认 ->
 崩溃续跑 -> 复核 -> 演练 -> 正式推进视图。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from app import RecoveryService
from app.models import SimulatedCrash
from helpers import OUTAGE_AT, T0, T1, make_clock


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.unlink(self.db + suffix)

    def _new_service(self, start=1_000):
        return RecoveryService(self.db, clock=make_clock(start))

    def _register_plant(self, svc):
        svc.open_outage("OUT-BLACKOUT-1", occurred_at=OUTAGE_AT)
        svc.register_unit("UT-POWER", "utility")
        svc.register_unit("LINE-1", "conveying", utility_id="UT-POWER")
        svc.define_route("DEV-1", "OP-1",
                         ["enqueue", "feed", "process", "inspect", "complete"])
        svc.register_unit("U-A", "processing", device_id="DEV-1",
                          line_id="LINE-1", utility_id="UT-POWER",
                          recipe_qty=5.0)
        svc.register_unit("U-B", "processing", device_id="DEV-1",
                          line_id="LINE-1", utility_id="UT-POWER",
                          recipe_qty=5.0)

    def test_full_recovery_story(self):
        svc = self._new_service()
        self._register_plant(svc)

        # ---- 矛盾现场：U-A 中断于投料前；U-B 已完成但回执未上传 ----
        svc.ingest_snapshot("U-A", "DEV-1", "OP-1", "enqueue",
                            "in_progress", 0.0, recorded_at=T0)
        svc.ingest_snapshot("U-B", "DEV-1", "OP-1", "complete",
                            "completed", 5.0, recorded_at=T0)
        svc.ingest_boot_report("DEV-1", "standby", restarted_at=T1)
        svc.ingest_material_scan("U-A", 0.0, scanned_at=T1)
        svc.ingest_material_scan("U-B", 5.0, scanned_at=T1)
        svc.ingest_command_log("cmd-receipt-U-B", "U-B", "RECEIPT",
                               issued_at=T0, status="cached")
        svc.ingest_command_log("cmd-feed-U-A", "U-A", "FEED", {"qty": 5.0},
                               issued_at=T0, status="issued")

        pkg = svc.evidence_package("U-B")
        self.assertTrue(pkg.flags["receipt_pending"])

        # ---- 裁决：检查点解释 ----
        decisions = svc.adjudicate()
        self.assertEqual(decisions["U-A"].decision, "auto_resume")
        self.assertEqual(decisions["U-A"].checkpoint.checkpoint_id, "enqueue")
        self.assertEqual(decisions["U-B"].decision, "auto_resume")
        self.assertEqual(decisions["U-B"].checkpoint.checkpoint_id, "complete")

        # ---- 网络恢复后旧命令到达：不得重复生效 ----
        late = svc.submit_command("cmd-feed-U-A", "FEED", "U-A", {"qty": 5.0},
                                  issued_at=T0)
        self.assertEqual(late["result"], "stale")

        # ---- 计划与越级禁止 ----
        svc.build_plan()
        from app.models import BlockedError
        with self.assertRaises(BlockedError):
            svc.execute_action("act-resume_operation-U-A")

        # ---- 人工改判不一致：双人确认 ----
        svc2 = self._new_service(2_000)  # 另一终端的经理
        svc2.close()  # 仅演示可跨进程访问；继续使用 svc
        pending = svc.override_decision("U-B", "inspection_required",
                                        user="manager-a")
        self.assertEqual(pending["status"], "pending")
        confirmed = svc.confirm_override(pending["approval_id"],
                                         user="supervisor-b")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(svc.get_decision("U-B").decision,
                         "inspection_required")

        # ---- 执行中崩溃：投料效果已发生、结果未落账 ----
        svc.execute_action("act-restore_utility-UT-POWER")
        svc.execute_action("act-restore_conveying-LINE-1")
        with self.assertRaises(SimulatedCrash):
            svc.execute_action("act-resume_operation-U-A",
                               crash_after="effect")
        svc.close()

        # ---- 新进程续跑：不多投一次料 ----
        svc = self._new_service(5_000)
        svc.resume_execution()
        svc.execute_ready()
        feeds = svc.store.query(
            "SELECT * FROM material_movements"
            " WHERE ns='official' AND reason='feed'")
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]["unit_id"], "U-A")

        # ---- 事后证据：只生成复核结论 ----
        svc.ingest_material_scan("U-A", 3.0, scanned_at="2026-09-19T04:00:00")
        action = svc.store.one(
            "SELECT state FROM actions"
            " WHERE ns='official' AND action_id='act-resume_operation-U-A'")
        self.assertEqual(action["state"], "review_required")
        from app.models import ImmutableError
        with self.assertRaises(ImmutableError):
            svc.delete_action("act-resume_operation-U-A")

        # ---- 演练不污染正式记录 ----
        drill = svc.create_drill()
        svc.adjudicate(ns=drill)
        svc.build_plan(ns=drill)
        svc.execute_ready(ns=drill)
        official_moves = svc.store.query(
            "SELECT * FROM material_movements WHERE ns='official'")
        self.assertEqual(len(official_moves), 1)  # 仍只有 U-A 那一笔

        # ---- 正式推进视图 ----
        report = svc.progression_report()
        self.assertEqual(report["outage_id"], "OUT-BLACKOUT-1")
        self.assertEqual(report["units"]["U-A"]["disposition"], "under_review")
        self.assertEqual(report["units"]["U-B"]["disposition"], "inspected")
        self.assertEqual(report["material_account"]["U-A"]["moved"], 5.0)
        self.assertTrue(any(
            d["result"] == "stale" and d["business_key"] == "cmd-feed-U-A"
            for d in report["commands"]["details"]))
        self.assertTrue(report["reviews"])
        svc.close()


if __name__ == "__main__":
    unittest.main()
