"""证据包组装：四类证据汇聚与跨源矛盾标注。"""
from __future__ import annotations

from helpers import PlantCase, T0


class EvidencePackageTest(PlantCase):
    def test_package_assembles_four_sources(self):
        self.snapshot("U-RESUME", "process")
        self.boot("DEV-1")
        self.scan("U-RESUME", 5.0)
        self.svc.ingest_command_log("cmd-feed-1", "U-RESUME", "FEED",
                                    {"qty": 5.0}, issued_at=T0, status="acked")

        pkg = self.svc.evidence_package("U-RESUME")

        self.assertEqual(pkg.unit_id, "U-RESUME")
        self.assertEqual(pkg.snapshot["checkpoint_id"], "process")
        self.assertEqual(pkg.boot_report["boot_state"], "standby")
        self.assertEqual(pkg.scan["scanned_qty"], 5.0)
        self.assertEqual(len(pkg.commands), 1)
        self.assertEqual(pkg.commands[0]["business_key"], "cmd-feed-1")
        self.assertEqual(pkg.route,
                         ["enqueue", "feed", "process", "inspect", "complete"])

    def test_contradictions_flagged(self):
        # MES 认为仍在执行 + 控制器已待机 + 回执缓存未上传 + 账差。
        self.snapshot("U-INSPECT", "process")
        self.boot("DEV-1")
        self.scan("U-INSPECT", 3.0)
        self.svc.ingest_command_log("cmd-receipt-9", "U-INSPECT", "RECEIPT",
                                    issued_at=T0, status="cached")

        pkg = self.svc.evidence_package("U-INSPECT")

        self.assertTrue(pkg.flags["mes_executing"])
        self.assertTrue(pkg.flags["controller_standby"])
        self.assertTrue(pkg.flags["receipt_pending"])
        self.assertEqual(pkg.flags["scan_diff"], -2.0)
        text = "\n".join(pkg.contradictions)
        self.assertIn("MES 快照显示工序仍在执行", text)
        self.assertIn("完成回执缓存", text)
        self.assertIn("物料账差 -2", text)

    def test_mid_operation_conflict_flagged(self):
        self.snapshot("U-RESUME", "complete", state="completed")
        self.boot("DEV-1", mid_operation=True)
        self.scan("U-RESUME", 5.0)

        pkg = self.svc.evidence_package("U-RESUME")

        self.assertTrue(pkg.flags["mid_operation_conflict"])
        self.assertTrue(any("工序中段" in c for c in pkg.contradictions))

    def test_missing_sources_are_none(self):
        pkg = self.svc.evidence_package("U-UNKNOWN")
        self.assertIsNone(pkg.snapshot)
        self.assertIsNone(pkg.boot_report)
        self.assertIsNone(pkg.scan)
        self.assertEqual(pkg.commands, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
