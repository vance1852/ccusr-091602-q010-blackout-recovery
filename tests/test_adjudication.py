"""裁决规则：四分类与最后可信检查点解释。"""
from __future__ import annotations

from helpers import PlantCase


class AdjudicationTest(PlantCase):
    def test_four_way_classification(self):
        decisions, _ = self.adjudicated_plant()
        self.assertEqual(decisions["U-RESUME"].decision, "auto_resume")
        self.assertEqual(decisions["U-FEED"].decision, "auto_resume")
        self.assertEqual(decisions["U-RECEIPT"].decision, "auto_resume")
        self.assertEqual(decisions["U-SCRAP"].decision, "scrap")
        self.assertEqual(decisions["U-INSPECT"].decision, "inspection_required")
        self.assertEqual(decisions["U-UNKNOWN"].decision,
                         "insufficient_evidence")

    def test_conclusion_explains_last_trusted_checkpoint(self):
        decisions, _ = self.adjudicated_plant()
        dec = decisions["U-RESUME"]
        self.assertIsNotNone(dec.checkpoint)
        self.assertEqual(dec.checkpoint.checkpoint_id, "process")
        self.assertEqual(dec.checkpoint.device_id, "DEV-1")
        self.assertEqual(dec.checkpoint.operation_id, "OP-1")
        # 解释中必须出现检查点与确认它的证据。
        self.assertIn("process", dec.rationale)
        self.assertIn("SNAP-", dec.rationale)
        self.assertIn("BOOT-", dec.rationale)
        self.assertTrue(dec.checkpoint.material_verified)

    def test_completed_unit_uses_complete_checkpoint(self):
        decisions, _ = self.adjudicated_plant()
        dec = decisions["U-RECEIPT"]
        self.assertEqual(dec.checkpoint.checkpoint_id, "complete")
        self.assertIn("回执", dec.rationale)

    def test_boot_fault_caps_checkpoint_one_step(self):
        self.snapshot("U-RESUME", "process")
        self.boot("DEV-1", faults=["SERVO_TIMEOUT"])
        self.scan("U-RESUME", 5.0)
        self.svc.adjudicate()

        dec = self.svc.get_decision("U-RESUME")

        self.assertEqual(dec.decision, "inspection_required")
        # process 前一个检查点是 feed。
        self.assertEqual(dec.checkpoint.checkpoint_id, "feed")
        self.assertIsNotNone(dec.checkpoint.capped_by)
        self.assertTrue(dec.checkpoint.capped_by.startswith("BOOT-"))
        self.assertIn("压低", dec.rationale)

    def test_mid_operation_conflict_caps_trust(self):
        self.snapshot("U-RESUME", "complete", state="completed")
        self.boot("DEV-1", mid_operation=True)
        self.scan("U-RESUME", 5.0)
        self.svc.adjudicate()

        dec = self.svc.get_decision("U-RESUME")

        self.assertEqual(dec.checkpoint.checkpoint_id, "inspect")

    def test_scrap_requires_scrap(self):
        self.snapshot("U-SCRAP", "process", device="DEV-2")
        self.boot("DEV-2", excursion=True)
        self.scan("U-SCRAP", 5.0)
        self.svc.adjudicate()

        dec = self.svc.get_decision("U-SCRAP")

        self.assertEqual(dec.decision, "scrap")
        self.assertIn("暴露超限", dec.rationale)

    def test_contamination_fault_scraps(self):
        self.snapshot("U-RESUME", "process")
        self.boot("DEV-1", faults=["CONTAMINATION"])
        self.scan("U-RESUME", 5.0)
        self.svc.adjudicate()
        self.assertEqual(self.svc.get_decision("U-RESUME").decision, "scrap")

    def test_scan_mismatch_requires_inspection(self):
        decisions, _ = self.adjudicated_plant()
        dec = decisions["U-INSPECT"]
        self.assertEqual(dec.decision, "inspection_required")
        self.assertFalse(dec.checkpoint.material_verified)
        self.assertIn("账差", dec.rationale)

    def test_missing_each_source_gives_insufficient_evidence(self):
        # 缺快照
        self.boot("DEV-1")
        self.scan("U-RESUME", 5.0)
        self.svc.adjudicate()
        dec = self.svc.get_decision("U-RESUME")
        self.assertEqual(dec.decision, "insufficient_evidence")
        self.assertIsNone(dec.checkpoint)
        self.assertIn("缺少停电前快照", dec.rationale)

        # 缺启动报告
        self.snapshot("U-FEED", "enqueue", qty=0.0)
        self.scan("U-FEED", 0.0)
        self.svc.store.execute(
            "DELETE FROM boot_reports WHERE ns='official'")
        self.svc.store.conn.commit()
        self.svc.adjudicate()
        dec = self.svc.get_decision("U-FEED")
        self.assertEqual(dec.decision, "insufficient_evidence")
        self.assertIn("缺少设备启动报告", dec.rationale)

        # 缺物料扫描
        self.boot("DEV-1")
        self.svc.store.execute(
            "DELETE FROM material_scans WHERE ns='official'")
        self.svc.store.conn.commit()
        self.svc.adjudicate()
        dec = self.svc.get_decision("U-FEED")
        self.assertEqual(dec.decision, "insufficient_evidence")
        self.assertIn("缺少物料扫描", dec.rationale)


if __name__ == "__main__":
    import unittest

    unittest.main()
