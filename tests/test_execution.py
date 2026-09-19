"""执行层测试：双人确认、不可删除、复核结论与崩溃安全续跑。"""

import os
import tempfile
import unittest

from app.models import (
    ActionState,
    ConfirmationRequiredError,
    DuplicateApprovalError,
    ImmutableRecordError,
    ReviewConclusion,
)

from tests.support import SimulatedCrash, build_blackout_scenario, new_service


class DualConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()
        self.incident = build_blackout_scenario(self.svc)
        self.svc.adjudicate(self.incident)
        self.svc.create_plan(self.incident)
        self.action_id = "INC-1:P3:recovery"

    def tearDown(self):
        self.svc.close()

    def test_override_consistent_with_suggestion_needs_no_confirmation(self):
        action = self.svc.override_decision(self.incident, "P3", "inspection_required")
        self.assertFalse(action["requires_dual"])
        self.assertIsNone(action["manual_choice"])

    def test_override_disagreeing_requires_two_distinct_approvers(self):
        action = self.svc.override_decision(self.incident, "P3", "scrap")
        self.assertTrue(action["requires_dual"])
        self.assertEqual(action["kind"], "scrap_unit")

        self.svc.execute_ready(self.incident)
        # 未获双人确认：其他动作执行完毕，P3 仍在等待。
        action = self.svc.planner.get_action("official", self.incident, self.action_id)
        self.assertNotEqual(action["state"], ActionState.COMPLETED.value)
        with self.assertRaises(ConfirmationRequiredError):
            self.svc.execute_action(self.incident, self.action_id)

        self.assertEqual(self.svc.confirm_action(self.incident, self.action_id, "alice"), 1)
        with self.assertRaises(DuplicateApprovalError):
            self.svc.confirm_action(self.incident, self.action_id, "alice")
        with self.assertRaises(ConfirmationRequiredError):
            self.svc.execute_action(self.incident, self.action_id)

        self.assertEqual(self.svc.confirm_action(self.incident, self.action_id, "bob"), 2)
        self.svc.execute_action(self.incident, self.action_id)
        action = self.svc.planner.get_action("official", self.incident, self.action_id)
        self.assertEqual(action["state"], ActionState.COMPLETED.value)
        # 人工选择被完整记录。
        self.assertEqual(action["manual_choice"], "scrap")
        self.assertEqual(action["decision"], "inspection_required")


class ImmutabilityTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()
        self.incident = build_blackout_scenario(self.svc)
        self.svc.adjudicate(self.incident)
        self.svc.create_plan(self.incident)

    def tearDown(self):
        self.svc.close()

    def test_executed_action_cannot_be_deleted(self):
        self.svc.execute_ready(self.incident)
        with self.assertRaises(ImmutableRecordError):
            self.svc.delete_action(self.incident, "INC-1:P2:recovery")

    def test_unexecuted_action_can_be_deleted(self):
        self.assertTrue(self.svc.delete_action(self.incident, "INC-1:P3:recovery"))

    def test_executed_action_cannot_be_overridden(self):
        self.svc.execute_ready(self.incident)
        with self.assertRaises(ImmutableRecordError):
            self.svc.override_decision(self.incident, "P2", "auto_resume")


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()
        self.incident = build_blackout_scenario(self.svc)
        self.svc.adjudicate(self.incident)
        self.svc.create_plan(self.incident)
        self.svc.execute_ready(self.incident)

    def tearDown(self):
        self.svc.close()

    def test_contradicting_late_evidence_marks_review_required(self):
        # 更正启动报告：实际断电仅 10s，未超容忍 —— 与已执行的报废矛盾。
        reviews = self.svc.record_boot_report(
            self.incident,
            "P2",
            {
                "controller_state": "standby",
                "physical_damage": False,
                "outage_duration_s": 10,
                "reported_at": "2026-09-19T08:30:00+00:00",
            },
        )
        self.assertEqual(reviews[0]["conclusion"], ReviewConclusion.CONTRADICTED.value)
        action = self.svc.planner.get_action("official", self.incident, "INC-1:P2:recovery")
        self.assertEqual(action["state"], ActionState.REVIEW_REQUIRED.value)
        # 已执行动作依然不可删除。
        with self.assertRaises(ImmutableRecordError):
            self.svc.delete_action(self.incident, "INC-1:P2:recovery")

    def test_consistent_late_evidence_confirms(self):
        reviews = self.svc.record_material_scan(
            self.incident, "P1", {"materials": {"M1": 0}, "scanned_at": "2026-09-19T08:40:00+00:00"}
        )
        self.assertEqual(reviews[0]["conclusion"], ReviewConclusion.CONFIRMED.value)
        action = self.svc.planner.get_action("official", self.incident, "INC-1:P1:recovery")
        self.assertEqual(action["state"], ActionState.COMPLETED.value)

    def test_late_evidence_updates_pending_action(self):
        # P4 证据补齐后，动作从 collect_evidence 变为可执行。
        self.svc.record_boot_report(
            self.incident,
            "P4",
            {
                "controller_state": "standby",
                "physical_damage": False,
                "outage_duration_s": 45,
                "reported_at": "2026-09-19T08:20:00+00:00",
            },
        )
        self.svc.record_material_scan(
            self.incident, "P4", {"materials": {"M1": 10}, "scanned_at": "2026-09-19T08:21:00+00:00"}
        )
        action = self.svc.planner.get_action("official", self.incident, "INC-1:P4:recovery")
        self.assertEqual(action["kind"], "resume_processing")
        summary = self.svc.execute_ready(self.incident)
        self.assertIn("INC-1:P4:recovery", summary["executed"])


class CrashSafetyTest(unittest.TestCase):
    """进程在投料命令落账后崩溃：重启续跑不得多投一次料。"""

    def test_crash_after_feed_does_not_double_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "recovery.db")
            svc = new_service(db_path)
            incident = build_blackout_scenario(svc)
            svc.adjudicate(incident)
            svc.create_plan(incident)

            def crash_on_feed(spec, _result):
                if spec.command_type == "feed_material":
                    raise SimulatedCrash(spec.key)

            with self.assertRaises(SimulatedCrash):
                svc.execute_ready(incident, crash_hook=crash_on_feed)
            svc.close()

            # 进程重启：新实例、同一数据库，继续执行。
            svc2 = new_service(db_path)
            summary = svc2.recover(incident)
            self.assertIn("INC-1:P1:recovery", summary["recovered_inflight"])

            ledger = svc2.material_ledger(incident)
            self.assertEqual(ledger["P1"]["M2"]["fed"], 5)  # 恰好投料一次
            effects = [
                e
                for e in svc2.gateway.effects("official", incident)
                if e["effect_type"] == "feed_material"
            ]
            self.assertEqual(len(effects), 1)

            # 重放的投料命令被去重网关识别为 duplicate。
            attempts = svc2.gateway.attempts("official", incident)
            self.assertTrue(
                any(
                    a["business_key"] == "INC-1:P1:OP2:feed" and a["result"] == "duplicate"
                    for a in attempts
                )
            )
            action = svc2.planner.get_action("official", incident, "INC-1:P1:recovery")
            self.assertEqual(action["state"], ActionState.COMPLETED.value)
            svc2.close()

    def test_repeated_recover_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "recovery.db")
            svc = new_service(db_path)
            incident = build_blackout_scenario(svc)
            svc.adjudicate(incident)
            svc.create_plan(incident)
            svc.execute_ready(incident)
            svc.close()

            svc2 = new_service(db_path)
            svc2.recover(incident)
            svc2.recover(incident)
            ledger = svc2.material_ledger(incident)
            self.assertEqual(ledger["P1"]["M2"]["fed"], 5)
            svc2.close()


if __name__ == "__main__":
    unittest.main()
