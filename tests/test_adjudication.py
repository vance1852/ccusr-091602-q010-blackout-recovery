"""裁决规则测试：四分类结论与最后可信检查点解释。"""

import unittest

from app.adjudication import adjudicate_unit
from app.models import Decision, EvidencePackage, UnitCategory

from tests.support import build_blackout_scenario, new_service


def _pkg(**kwargs) -> EvidencePackage:
    kwargs.setdefault("unit_id", "PX")
    kwargs.setdefault("category", UnitCategory.PROCESSING)
    kwargs.setdefault("snapshot", None)
    kwargs.setdefault("boot_report", None)
    kwargs.setdefault("material_scan", None)
    kwargs.setdefault("command_logs", [])
    kwargs.setdefault("inspections", [])
    return EvidencePackage(**kwargs)


def _snapshot(**over):
    base = {
        "operation": "OP1",
        "step": "process",
        "state": "executing",
        "checkpoint_id": "PX-ck3",
        "checkpoint_seq": 3,
        "expected_materials": {"M1": 10},
        "captured_at": "2026-09-19T07:59:00+00:00",
    }
    base.update(over)
    return base


def _boot(**over):
    base = {
        "controller_state": "standby",
        "physical_damage": False,
        "outage_duration_s": 45,
        "reported_at": "2026-09-19T08:01:00+00:00",
    }
    base.update(over)
    return base


class ScenarioAdjudicationTest(unittest.TestCase):
    """整厂场景下各单元的裁决结论。"""

    def setUp(self):
        self.svc = new_service()
        self.incident = build_blackout_scenario(self.svc)
        self.decisions = self.svc.adjudicate(self.incident)

    def tearDown(self):
        self.svc.close()

    def test_infrastructure_auto_resume(self):
        for unit in ("U1", "V1"):
            record = self.decisions[unit]
            self.assertEqual(record.decision, Decision.AUTO_RESUME)
            self.assertEqual(record.last_trusted_checkpoint.source, "snapshot")

    def test_receipt_corroborated_by_material_account(self):
        record = self.decisions["P1"]
        self.assertEqual(record.decision, Decision.AUTO_RESUME)
        # 最后可信检查点来自缓存完成回执，而不是 MES 快照。
        checkpoint = record.last_trusted_checkpoint
        self.assertEqual(checkpoint.source, "command_log")
        self.assertEqual(checkpoint.seq, 6)
        self.assertEqual(record.resume_point, {"operation": "OP2", "step": "feed"})
        self.assertTrue(any("回执" in r for r in record.rationale))
        self.assertTrue(any("P1-ck5" not in r for r in record.rationale))

    def test_interruption_critical_scrap(self):
        record = self.decisions["P2"]
        self.assertEqual(record.decision, Decision.SCRAP)
        self.assertEqual(record.last_trusted_checkpoint.checkpoint_id, "P2-ck3")
        self.assertTrue(any("120" in r for r in record.rationale))

    def test_material_discrepancy_requires_inspection(self):
        record = self.decisions["P3"]
        self.assertEqual(record.decision, Decision.INSPECTION_REQUIRED)
        self.assertEqual(record.material_account["deltas"], {"M1": -2})
        self.assertEqual(record.last_trusted_checkpoint.checkpoint_id, "P3-ck2")

    def test_missing_evidence(self):
        record = self.decisions["P4"]
        self.assertEqual(record.decision, Decision.INSUFFICIENT_EVIDENCE)
        self.assertIn("boot_report", record.missing_evidence)
        self.assertIn("material_scan", record.missing_evidence)
        # 快照存在时仍给出已知检查点，便于人工定位。
        self.assertEqual(record.last_trusted_checkpoint.checkpoint_id, "P4-ck2")


class RuleUnitTest(unittest.TestCase):
    """单规则行为。"""

    def test_missing_snapshot_is_insufficient(self):
        record = adjudicate_unit(_pkg(boot_report=_boot(), material_scan={"materials": {}}))
        self.assertEqual(record.decision, Decision.INSUFFICIENT_EVIDENCE)
        self.assertIsNone(record.last_trusted_checkpoint)

    def test_physical_damage_scraps_processing_unit(self):
        record = adjudicate_unit(
            _pkg(
                snapshot=_snapshot(),
                boot_report=_boot(physical_damage=True),
                material_scan={"materials": {"M1": 10}},
            )
        )
        self.assertEqual(record.decision, Decision.SCRAP)

    def test_contradicted_receipt_falls_back_to_snapshot(self):
        # 回执说完成，但物料未消耗：回执是幻影，回退快照检查点。
        record = adjudicate_unit(
            _pkg(
                snapshot=_snapshot(),
                boot_report=_boot(),
                material_scan={"materials": {"M1": 10}},
                command_logs=[
                    {
                        "business_key": "K1",
                        "command_type": "completion_receipt",
                        "operation": "OP1",
                        "checkpoint_seq": 4,
                        "consumed": {"M1": 10},
                        "issued_at": "2026-09-19T07:58:00+00:00",
                    }
                ],
            )
        )
        self.assertEqual(record.decision, Decision.AUTO_RESUME)
        self.assertEqual(record.last_trusted_checkpoint.source, "snapshot")
        self.assertEqual(record.last_trusted_checkpoint.seq, 3)
        self.assertTrue(any("丢弃回执" in r for r in record.rationale))

    def test_receipt_and_scan_both_mismatch_requires_inspection(self):
        record = adjudicate_unit(
            _pkg(
                snapshot=_snapshot(),
                boot_report=_boot(),
                material_scan={"materials": {"M1": 6}},
                command_logs=[
                    {
                        "business_key": "K1",
                        "command_type": "completion_receipt",
                        "operation": "OP1",
                        "checkpoint_seq": 4,
                        "consumed": {"M1": 10},
                        "issued_at": "2026-09-19T07:58:00+00:00",
                    }
                ],
            )
        )
        self.assertEqual(record.decision, Decision.INSPECTION_REQUIRED)
        self.assertEqual(record.material_account["deltas"], {"M1": -4})

    def test_quality_critical_interrupted_step_requires_inspection(self):
        record = adjudicate_unit(
            _pkg(
                snapshot=_snapshot(),
                boot_report=_boot(),
                material_scan={"materials": {"M1": 10}},
            ),
            {"operations": {"OP1": {"quality_critical": True}}},
        )
        self.assertEqual(record.decision, Decision.INSPECTION_REQUIRED)

    def test_retained_state_conflict_requires_inspection(self):
        record = adjudicate_unit(
            _pkg(
                snapshot=_snapshot(),
                boot_report=_boot(retained_operation="OP9"),
                material_scan={"materials": {"M1": 10}},
            )
        )
        self.assertEqual(record.decision, Decision.INSPECTION_REQUIRED)
        self.assertTrue(any("冲突" in r for r in record.rationale))

    def test_consistent_mid_operation_auto_resumes(self):
        record = adjudicate_unit(
            _pkg(
                snapshot=_snapshot(),
                boot_report=_boot(),
                material_scan={"materials": {"M1": 10}},
            )
        )
        self.assertEqual(record.decision, Decision.AUTO_RESUME)
        self.assertEqual(record.resume_point, {"operation": "OP1", "step": "process"})

    def test_completed_last_operation_marks_done(self):
        record = adjudicate_unit(
            _pkg(
                snapshot=_snapshot(state="completed"),
                boot_report=_boot(),
                material_scan={"materials": {"M1": 10}},
            ),
            {"operation_order": ["OP1"]},
        )
        self.assertEqual(record.decision, Decision.AUTO_RESUME)
        self.assertEqual(record.resume_point, {"operation": None, "step": "done"})

    def test_inspection_result_drives_disposition(self):
        base = dict(
            snapshot=_snapshot(),
            boot_report=_boot(),
            material_scan={"materials": {"M1": 8}},
        )
        passed = adjudicate_unit(_pkg(**base, inspections=[{"result": "pass"}]))
        self.assertEqual(passed.decision, Decision.AUTO_RESUME)
        failed = adjudicate_unit(_pkg(**base, inspections=[{"result": "fail"}]))
        self.assertEqual(failed.decision, Decision.SCRAP)

    def test_infrastructure_rules(self):
        healthy = adjudicate_unit(
            _pkg(
                category=UnitCategory.UTILITY,
                snapshot={"state": "running", "checkpoint_id": "U-1", "checkpoint_seq": 1},
                boot_report=_boot(),
            )
        )
        self.assertEqual(healthy.decision, Decision.AUTO_RESUME)

        errored = adjudicate_unit(
            _pkg(
                category=UnitCategory.CONVEYOR,
                snapshot={"state": "running", "checkpoint_id": "V-1", "checkpoint_seq": 1},
                boot_report=_boot(controller_state="error"),
            )
        )
        self.assertEqual(errored.decision, Decision.INSPECTION_REQUIRED)

        damaged = adjudicate_unit(
            _pkg(
                category=UnitCategory.UTILITY,
                snapshot={"state": "running", "checkpoint_id": "U-1", "checkpoint_seq": 1},
                boot_report=_boot(physical_damage=True),
            )
        )
        self.assertEqual(damaged.decision, Decision.INSPECTION_REQUIRED)

        no_boot = adjudicate_unit(
            _pkg(
                category=UnitCategory.UTILITY,
                snapshot={"state": "running", "checkpoint_id": "U-1", "checkpoint_seq": 1},
            )
        )
        self.assertEqual(no_boot.decision, Decision.INSUFFICIENT_EVIDENCE)


if __name__ == "__main__":
    unittest.main()
