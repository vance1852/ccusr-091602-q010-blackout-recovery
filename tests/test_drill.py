"""演练命名空间：针对一次停电反复演练而不污染正式记录。"""
from __future__ import annotations

from helpers import PlantCase


class DrillTest(PlantCase):
    def test_drill_copies_evidence_but_not_execution_records(self):
        self.adjudicated_plant()
        self.svc.execute_ready()

        drill = self.svc.create_drill()

        # 证据已复制。
        pkg = self.svc.evidence_package("U-RESUME", ns=drill)
        self.assertEqual(pkg.snapshot["checkpoint_id"], "process")
        self.assertEqual(pkg.boot_report["boot_state"], "standby")
        # 执行记录未复制：演练从零开始。
        self.assertEqual(self.svc.list_actions(ns=drill), [])
        self.assertEqual(
            self.svc.store.query(
                "SELECT * FROM material_movements WHERE ns=?", (drill,)), [])
        self.assertEqual(
            self.svc.store.query(
                "SELECT * FROM decisions WHERE ns=?", (drill,)), [])

    def test_drill_execution_does_not_pollute_official(self):
        self.adjudicated_plant()
        drill = self.svc.create_drill()

        # 在演练中完整跑一遍恢复。
        self.svc.adjudicate(ns=drill)
        self.svc.build_plan(ns=drill)
        self.svc.execute_ready(ns=drill)
        drill_actions = {a["action_id"]: a["state"]
                         for a in self.svc.list_actions(ns=drill)}
        self.assertEqual(drill_actions["act-resume_operation-U-RESUME"],
                         "completed")

        # 正式命名空间毫无变化。
        self.assertEqual(self.svc.list_actions(ns="official") and
                         {a["action_id"]: a["state"]
                          for a in self.svc.list_actions(ns="official")}
                         ["act-resume_operation-U-RESUME"], "blocked")
        self.assertEqual(
            self.svc.store.query(
                "SELECT * FROM material_movements WHERE ns='official'"), [])
        self.assertEqual(
            self.svc.store.query(
                "SELECT * FROM commands WHERE ns='official'"), [])

    def test_repeated_drills_are_independent(self):
        self.adjudicated_plant()
        drill_a = self.svc.create_drill()
        drill_b = self.svc.create_drill()
        self.assertNotEqual(drill_a, drill_b)

        # 演练 A 中改判并执行；演练 B 与正式记录不受影响。
        self.svc.adjudicate(ns=drill_a)
        self.svc.build_plan(ns=drill_a)
        result = self.svc.override_decision("U-INSPECT", "scrap",
                                            user="manager-a", ns=drill_a)
        self.svc.confirm_override(result["approval_id"], user="supervisor-b",
                                  ns=drill_a)
        self.assertEqual(self.svc.get_decision("U-INSPECT", ns=drill_a).decision,
                         "scrap")

        self.svc.adjudicate(ns=drill_b)
        self.assertEqual(self.svc.get_decision("U-INSPECT", ns=drill_b).decision,
                         "inspection_required")
        self.assertEqual(self.svc.get_decision("U-INSPECT").decision,
                         "inspection_required")

    def test_drill_crash_recovery_is_isolated(self):
        self.adjudicated_plant()
        drill = self.svc.create_drill()
        self.svc.adjudicate(ns=drill)
        self.svc.build_plan(ns=drill)
        self.svc.execute_action("act-restore_utility-UT-POWER", ns=drill)
        self.svc.execute_action("act-restore_conveying-LINE-1", ns=drill)
        from app.models import SimulatedCrash
        with self.assertRaises(SimulatedCrash):
            self.svc.execute_action("act-resume_operation-U-FEED",
                                    crash_after="effect", ns=drill)
        self.svc.resume_execution(ns=drill)
        feeds = self.svc.store.query(
            "SELECT * FROM material_movements WHERE ns=? AND reason='feed'",
            (drill,))
        self.assertEqual(len(feeds), 1)
        # 正式记录依旧干净。
        self.assertEqual(
            self.svc.store.query(
                "SELECT * FROM material_movements WHERE ns='official'"), [])


if __name__ == "__main__":
    import unittest

    unittest.main()
