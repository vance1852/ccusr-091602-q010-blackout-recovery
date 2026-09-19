"""崩溃安全执行：进程再次崩溃后继续执行也不能多投一次料。"""
from __future__ import annotations

import os
import tempfile
import unittest

from app import RecoveryService
from app.models import SimulatedCrash
from helpers import OUTAGE_AT, PlantCase, make_clock


def feed_movements(svc, ns="official"):
    return svc.store.query(
        "SELECT * FROM material_movements WHERE ns=? AND reason='feed'",
        (ns,),
    )


class CrashRecoveryFileTest(unittest.TestCase):
    """用真实数据库文件模拟进程崩溃后用新实例继续执行。"""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)  # 让 Store 自己创建

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.unlink(self.db + suffix)

    def _boot_service(self, start=1_000):
        svc = RecoveryService(self.db, clock=make_clock(start))
        svc.open_outage("OUT-1", occurred_at=OUTAGE_AT)
        svc.register_unit("UT-POWER", "utility")
        svc.register_unit("LINE-1", "conveying", utility_id="UT-POWER")
        svc.define_route("DEV-1", "OP-1",
                         ["enqueue", "feed", "process", "inspect", "complete"])
        svc.register_unit("U-FEED", "processing", device_id="DEV-1",
                          line_id="LINE-1", utility_id="UT-POWER",
                          recipe_qty=5.0)
        svc.ingest_snapshot("U-FEED", "DEV-1", "OP-1", "enqueue",
                            "in_progress", 0.0, recorded_at="2026-09-19T01:55:00")
        svc.ingest_boot_report("DEV-1", "standby", restarted_at="2026-09-19T02:05:00")
        svc.ingest_material_scan("U-FEED", 0.0, scanned_at="2026-09-19T02:05:00")
        svc.adjudicate()
        svc.build_plan()
        return svc

    def test_crash_after_effect_then_resume_feeds_exactly_once(self):
        svc1 = self._boot_service()
        svc1.execute_action("act-restore_utility-UT-POWER")
        svc1.execute_action("act-restore_conveying-LINE-1")
        with self.assertRaises(SimulatedCrash):
            svc1.execute_action("act-resume_operation-U-FEED",
                                crash_after="effect")
        svc1.close()  # 模拟进程崩溃退出

        # 新进程继续执行。
        svc2 = RecoveryService(self.db, clock=make_clock(5_000))
        svc2.resume_execution()

        feeds = feed_movements(svc2)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]["delta"], 5.0)
        action = svc2.store.one(
            "SELECT state FROM actions"
            " WHERE ns='official' AND action_id='act-resume_operation-U-FEED'")
        self.assertEqual(action["state"], "completed")
        # 重放时网关把已生效的投料判为 duplicate，未重复生效。
        dups = [e for e in svc2.events(type_="command_result")
                if e["payload"]["result"] == "duplicate"
                and "feed" in e["payload"]["business_key"]]
        self.assertTrue(dups)
        svc2.close()

    def test_crash_after_intent_then_resume_feeds_exactly_once(self):
        svc1 = self._boot_service()
        svc1.execute_action("act-restore_utility-UT-POWER")
        svc1.execute_action("act-restore_conveying-LINE-1")
        with self.assertRaises(SimulatedCrash):
            svc1.execute_action("act-resume_operation-U-FEED",
                                crash_after="intent")
        svc1.close()

        svc2 = RecoveryService(self.db, clock=make_clock(5_000))
        svc2.resume_execution()

        self.assertEqual(len(feed_movements(svc2)), 1)
        svc2.close()

    def test_repeated_crashes_still_exactly_once(self):
        """连续两次崩溃后恢复，投料仍然只发生一次。"""
        svc1 = self._boot_service()
        svc1.execute_action("act-restore_utility-UT-POWER")
        svc1.execute_action("act-restore_conveying-LINE-1")
        with self.assertRaises(SimulatedCrash):
            svc1.execute_action("act-resume_operation-U-FEED",
                                crash_after="effect")
        svc1.close()

        svc2 = RecoveryService(self.db, clock=make_clock(5_000))
        # 恢复过程中再次崩溃（intent 已重放，outcome 未提交）。
        journal = svc2.store.one(
            "SELECT phase FROM exec_journal"
            " WHERE ns='official' AND action_id='act-resume_operation-U-FEED'")
        self.assertEqual(journal["phase"], "intent")
        svc2.close()

        svc3 = RecoveryService(self.db, clock=make_clock(9_000))
        svc3.resume_execution()
        svc3.resume_execution()  # 幂等：再恢复一次也无副作用
        self.assertEqual(len(feed_movements(svc3)), 1)
        svc3.close()


class ExecutionTest(PlantCase):
    def test_execute_ready_completes_all_executable_actions(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        states = {a["action_id"]: a["state"] for a in self.svc.list_actions()}
        self.assertEqual(states["act-restore_utility-UT-POWER"], "completed")
        self.assertEqual(states["act-restore_conveying-LINE-1"], "completed")
        self.assertEqual(states["act-resume_operation-U-RESUME"], "completed")
        self.assertEqual(states["act-upload_receipt-U-RECEIPT"], "completed")
        self.assertEqual(states["act-scrap-U-SCRAP"], "completed")
        self.assertEqual(states["act-inspect-U-INSPECT"], "completed")
        # 证据不足的 hold 动作保持阻塞，不会被执行。
        self.assertEqual(states["act-hold-U-UNKNOWN"], "blocked")

    def test_completed_action_is_idempotent(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        before = self.svc.store.query(
            "SELECT * FROM material_movements WHERE ns='official'")
        again = self.svc.execute_action("act-resume_operation-U-FEED")
        self.assertEqual(again["state"], "completed")
        after = self.svc.store.query(
            "SELECT * FROM material_movements WHERE ns='official'")
        self.assertEqual(before, after)

    def test_resume_feeds_only_units_before_feed_checkpoint(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        feeds = {m["unit_id"]: m["delta"] for m in feed_movements(self.svc)}
        # U-FEED 最后可信检查点 enqueue 早于 feed -> 补投料；
        # U-RESUME 检查点 process 在 feed 之后 -> 不投料。
        self.assertEqual(feeds, {"U-FEED": 5.0})

    def test_scrap_removes_material_from_account(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        moves = self.svc.store.query(
            "SELECT * FROM material_movements"
            " WHERE ns='official' AND unit_id='U-SCRAP'")
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["delta"], -5.0)
        self.assertEqual(moves[0]["reason"], "consume")


if __name__ == "__main__":
    unittest.main()
