"""人工改判与双人确认。"""
from __future__ import annotations

from app.models import ApprovalError, DomainError
from helpers import PlantCase


class ApprovalTest(PlantCase):
    def test_consistent_choice_applies_immediately(self):
        self.adjudicated_plant()
        result = self.svc.override_decision("U-INSPECT", "inspection_required",
                                            user="manager-a")
        self.assertEqual(result["status"], "applied")
        dec = self.svc.get_decision("U-INSPECT")
        self.assertEqual(dec.source, "manual_consistent")

    def test_divergent_choice_requires_dual_confirmation(self):
        self.adjudicated_plant()
        result = self.svc.override_decision("U-INSPECT", "scrap",
                                            user="manager-a")
        self.assertEqual(result["status"], "pending")
        approval_id = result["approval_id"]
        # 未确认前裁决不变。
        self.assertEqual(self.svc.get_decision("U-INSPECT").decision,
                         "inspection_required")

        # 同一人不能充当第二人。
        with self.assertRaises(ApprovalError):
            self.svc.confirm_override(approval_id, user="manager-a")
        self.assertEqual(self.svc.get_decision("U-INSPECT").decision,
                         "inspection_required")

        # 第二人确认后生效。
        done = self.svc.confirm_override(approval_id, user="supervisor-b")
        self.assertEqual(done["status"], "confirmed")
        dec = self.svc.get_decision("U-INSPECT")
        self.assertEqual(dec.decision, "scrap")
        self.assertEqual(dec.source, "manual_dual_confirmed")

    def test_confirmed_override_rebuilds_plan(self):
        self.adjudicated_plant()
        result = self.svc.override_decision("U-INSPECT", "scrap",
                                            user="manager-a")
        self.svc.confirm_override(result["approval_id"], user="supervisor-b")
        verbs = {a["unit_id"]: a["verb"]
                 for a in self.svc.list_actions() if a["level"] == 2}
        self.assertEqual(verbs["U-INSPECT"], "scrap")
        # 旧的 inspect 动作（未开始）已被替换。
        ids = [a["action_id"] for a in self.svc.list_actions()]
        self.assertNotIn("act-inspect-U-INSPECT", ids)
        self.assertIn("act-scrap-U-INSPECT", ids)

    def test_cannot_override_executed_unit(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        with self.assertRaises(DomainError):
            self.svc.override_decision("U-RESUME", "scrap", user="manager-a")

    def test_cannot_confirm_twice(self):
        self.adjudicated_plant()
        result = self.svc.override_decision("U-INSPECT", "scrap",
                                            user="manager-a")
        self.svc.confirm_override(result["approval_id"], user="supervisor-b")
        with self.assertRaises(ApprovalError):
            self.svc.confirm_override(result["approval_id"], user="supervisor-c")

    def test_illegal_decision_rejected(self):
        self.adjudicated_plant()
        with self.assertRaises(DomainError):
            self.svc.override_decision("U-INSPECT", "ignore_it",
                                       user="manager-a")


if __name__ == "__main__":
    import unittest

    unittest.main()
