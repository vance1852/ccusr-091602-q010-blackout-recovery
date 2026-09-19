"""不可变性：执行过的动作不可删除，后续证据只生成复核结论。"""
from __future__ import annotations

from app.models import ImmutableError
from helpers import PlantCase


class ImmutabilityTest(PlantCase):
    def test_executed_action_cannot_be_deleted(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        with self.assertRaises(ImmutableError):
            self.svc.delete_action("act-resume_operation-U-RESUME")
        # 记录仍在。
        ids = [a["action_id"] for a in self.svc.list_actions()]
        self.assertIn("act-resume_operation-U-RESUME", ids)

    def test_unstarted_action_can_be_deleted(self):
        self.adjudicated_plant()
        self.svc.delete_action("act-restore_utility-UT-POWER")
        ids = [a["action_id"] for a in self.svc.list_actions()]
        self.assertNotIn("act-restore_utility-UT-POWER", ids)

    def test_late_contradicting_evidence_creates_review_only(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        basis_before = self.svc.store.one(
            "SELECT decision_basis_json, executed_at FROM actions"
            " WHERE ns='official'"
            " AND action_id='act-resume_operation-U-RESUME'")

        # 事后到达的证据：物料账差（原裁决为 auto_resume，新裁决应为检查）。
        self.svc.ingest_material_scan("U-RESUME", 2.0, scanned_at="2026-09-19T04:00:00")

        action = self.svc.store.one(
            "SELECT * FROM actions WHERE ns='official'"
            " AND action_id='act-resume_operation-U-RESUME'")
        # 动作被标记为待复核，但执行记录（依据、执行时间）原样保留。
        self.assertEqual(action["state"], "review_required")
        self.assertEqual(action["decision_basis_json"],
                         basis_before["decision_basis_json"])
        self.assertEqual(action["executed_at"], basis_before["executed_at"])

        reviews = self.svc.store.query(
            "SELECT * FROM reviews WHERE ns='official' AND unit_id='U-RESUME'")
        self.assertEqual(len(reviews), 1)
        self.assertIn("复核不一致", reviews[0]["conclusion"])
        self.assertIn("inspection_required", reviews[0]["conclusion"])

    def test_late_consistent_evidence_records_passing_review(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        # 与依据一致的新证据（账实仍相符）。
        self.svc.ingest_material_scan("U-RESUME", 5.0, scanned_at="2026-09-19T04:00:00")

        action = self.svc.store.one(
            "SELECT state FROM actions WHERE ns='official'"
            " AND action_id='act-resume_operation-U-RESUME'")
        self.assertEqual(action["state"], "completed")
        reviews = self.svc.store.query(
            "SELECT * FROM reviews WHERE ns='official' AND unit_id='U-RESUME'")
        self.assertEqual(len(reviews), 1)
        self.assertIn("复核一致", reviews[0]["conclusion"])

    def test_review_not_duplicated_for_same_conclusion(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        self.svc.ingest_material_scan("U-RESUME", 2.0, scanned_at="2026-09-19T04:00:00")
        self.svc.ingest_material_scan("U-RESUME", 2.0, scanned_at="2026-09-19T04:05:00")
        reviews = self.svc.store.query(
            "SELECT * FROM reviews WHERE ns='official' AND unit_id='U-RESUME'")
        self.assertEqual(len(reviews), 1)

    def test_review_required_action_still_cannot_be_deleted(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        self.svc.ingest_material_scan("U-RESUME", 2.0, scanned_at="2026-09-19T04:00:00")
        with self.assertRaises(ImmutableError):
            self.svc.delete_action("act-resume_operation-U-RESUME")


if __name__ == "__main__":
    import unittest

    unittest.main()
