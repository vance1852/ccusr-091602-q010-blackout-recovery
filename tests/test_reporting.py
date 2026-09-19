"""正式推进视图：阻塞链、物料账差、命令去重结果、各单元最终处置。"""
from __future__ import annotations

from helpers import PlantCase


class ReportingTest(PlantCase):
    def test_blocking_chains_visible_before_execution(self):
        self.adjudicated_plant()
        report = self.svc.progression_report()
        chain = report["blocking_chains"]["act-resume_operation-U-RESUME"]
        self.assertEqual(
            [c["action_id"] for c in chain],
            ["act-restore_utility-UT-POWER", "act-restore_conveying-LINE-1"],
        )
        # 证据不足的 hold 动作阻塞原因是 evidence。
        hold_chain = report["blocking_chains"]["act-hold-U-UNKNOWN"]
        self.assertEqual(hold_chain[-1]["blocked_reason"], "evidence")

    def test_material_account_shows_diffs(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        account = self.svc.progression_report()["material_account"]

        # U-INSPECT：扫描 3 vs 预期 5，账差 -2。
        self.assertEqual(account["U-INSPECT"]["scan_diff"], -2.0)
        # U-FEED：投料前账实皆 0，恢复期间补投 5。
        self.assertEqual(account["U-FEED"]["moved"], 5.0)
        self.assertEqual(account["U-FEED"]["current_diff"], 5.0)
        # U-SCRAP：报废出账 -5。
        self.assertEqual(account["U-SCRAP"]["moved"], -5.0)
        self.assertEqual(account["U-SCRAP"]["current_diff"], -5.0)
        # U-RESUME：账实相符且无移动。
        self.assertEqual(account["U-RESUME"]["current_diff"], 0.0)
        # U-UNKNOWN：无快照（预期 None）但扫描可见。
        self.assertIsNone(account["U-UNKNOWN"]["expected"])
        self.assertEqual(account["U-UNKNOWN"]["scanned"], 5.0)

    def test_final_dispositions(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        units = self.svc.progression_report()["units"]
        self.assertEqual(units["U-RESUME"]["disposition"], "resumed")
        self.assertEqual(units["U-RECEIPT"]["disposition"], "receipt_uploaded")
        self.assertEqual(units["U-SCRAP"]["disposition"], "scrapped")
        self.assertEqual(units["U-INSPECT"]["disposition"], "inspected")
        self.assertEqual(units["U-UNKNOWN"]["disposition"],
                         "held_insufficient_evidence")
        # 每个单元的最终处置都带检查点解释。
        self.assertEqual(units["U-RESUME"]["checkpoint"], "process")
        self.assertIn("最后可信检查点", units["U-RESUME"]["rationale"])

    def test_command_dedup_results_in_report(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        self.svc.submit_command("cmd-late", "FEED", "U-RESUME", {"qty": 5.0})
        self.svc.submit_command("cmd-late", "FEED", "U-RESUME", {"qty": 5.0})
        counts = self.svc.progression_report()["commands"]["counts"]
        self.assertGreaterEqual(counts["accepted"], 1)
        self.assertEqual(counts["duplicate"], 1)

    def test_review_required_shows_under_review(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        self.svc.ingest_material_scan("U-RESUME", 1.0,
                                      scanned_at="2026-09-19T04:00:00")
        report = self.svc.progression_report()
        self.assertEqual(report["units"]["U-RESUME"]["disposition"],
                         "under_review")
        self.assertEqual(len(report["reviews"]), 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
