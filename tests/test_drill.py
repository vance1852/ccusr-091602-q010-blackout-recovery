"""演练模式测试：反复演练不污染正式记录。"""

import unittest

from app.models import DomainError

from tests.support import build_blackout_scenario, new_service


class DrillTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()
        self.incident = build_blackout_scenario(self.svc)
        self.svc.adjudicate(self.incident)
        self.svc.create_plan(self.incident)

    def tearDown(self):
        self.svc.close()

    def test_drill_runs_full_flow_without_touching_official(self):
        drill = self.svc.start_drill(self.incident)
        self.assertTrue(drill.startswith("drill:INC-1:"))

        decisions = self.svc.adjudicate(self.incident, namespace=drill)
        self.assertEqual(decisions["P2"].decision.value, "scrap")
        self.svc.create_plan(self.incident, namespace=drill)
        summary = self.svc.execute_ready(self.incident, namespace=drill)
        self.assertIn("INC-1:P1:recovery", summary["executed"])

        # 演练命名空间产生了投料账……
        drill_ledger = self.svc.material_ledger(self.incident, namespace=drill)
        self.assertEqual(drill_ledger["P1"]["M2"]["fed"], 5)
        # ……但正式命名空间毫无变化。
        self.assertEqual(self.svc.material_ledger(self.incident), {})
        official_actions = self.svc.actions(self.incident)
        self.assertTrue(all(a["state"] != "completed" for a in official_actions))

    def test_drill_can_be_reset_and_repeated(self):
        drill = self.svc.start_drill(self.incident)
        self.svc.adjudicate(self.incident, namespace=drill)
        self.svc.create_plan(self.incident, namespace=drill)
        self.svc.execute_ready(self.incident, namespace=drill)

        deleted = self.svc.reset_drill(drill)
        self.assertGreater(deleted, 0)
        self.assertEqual(self.svc.actions(self.incident, namespace=drill), [])

        # 同一事故可再次演练，正式记录始终不受影响。
        drill2 = self.svc.start_drill(self.incident)
        self.assertNotEqual(drill, drill2)
        self.svc.adjudicate(self.incident, namespace=drill2)
        self.svc.create_plan(self.incident, namespace=drill2)
        summary = self.svc.execute_ready(self.incident, namespace=drill2)
        self.assertIn("INC-1:P1:recovery", summary["executed"])
        self.assertEqual(self.svc.material_ledger(self.incident), {})

    def test_drill_evidence_injection_does_not_leak(self):
        drill = self.svc.start_drill(self.incident)
        # 演练中假设 P4 证据补齐。
        self.svc.record_boot_report(
            self.incident,
            "P4",
            {"controller_state": "standby", "physical_damage": False,
             "outage_duration_s": 45, "reported_at": "2026-09-19T08:20:00+00:00"},
            namespace=drill,
        )
        decisions = self.svc.adjudicate(self.incident, namespace=drill)
        self.assertIn("material_scan", decisions["P4"].missing_evidence)
        # 正式侧 P4 仍只有快照。
        official_pkg = self.svc.evidence_package(self.incident, "P4")
        self.assertIsNone(official_pkg.boot_report)

    def test_official_namespace_cannot_be_reset(self):
        with self.assertRaises(DomainError):
            self.svc.reset_drill("official")


if __name__ == "__main__":
    unittest.main()
