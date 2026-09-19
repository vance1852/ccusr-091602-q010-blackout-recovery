"""正式推进视图测试：阻塞链、物料账差、命令去重结果与最终处置。"""

import unittest

from app.models import ActionState

from tests.support import build_blackout_scenario, new_service


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()
        self.incident = build_blackout_scenario(self.svc)
        self.svc.adjudicate(self.incident)
        self.svc.create_plan(self.incident)

    def tearDown(self):
        self.svc.close()

    def test_dashboard_before_execution_shows_blocking_chains(self):
        dashboard = self.svc.dashboard(self.incident)
        chains = dashboard["blocking_chains"]["chains"]
        self.assertEqual(
            chains["INC-1:P1:recovery"],
            [["INC-1:U1:recovery", "INC-1:V1:recovery", "INC-1:P1:recovery"]],
        )
        reasons = dashboard["blocking_chains"]["reasons"]
        self.assertEqual(reasons["INC-1:P4:recovery"], "insufficient_evidence")

    def test_dashboard_after_execution(self):
        self.svc.execute_ready(self.incident)
        # 制造去重与拒绝样本：重投旧命令 + 载荷冲突命令。
        self.svc.dispatch_command(
            self.incident, "P1", "INC-1:P1:OP1:feed", "feed_material",
            {"operation": "OP1", "materials": {"M1": 10}},
            issued_at="2026-09-19T09:00:00+00:00",
        )
        self.svc.dispatch_command(
            self.incident, "P1", "INC-1:P1:OP1:feed", "feed_material",
            {"operation": "OP1", "materials": {"M1": 99}},
            issued_at="2026-09-19T09:01:00+00:00",
        )
        dashboard = self.svc.dashboard(self.incident)

        # 阻塞链：只剩证据不足的 P4。
        chains = dashboard["blocking_chains"]["chains"]
        self.assertEqual(list(chains), ["INC-1:P4:recovery"])
        self.assertEqual(
            dashboard["blocking_chains"]["reasons"]["INC-1:P4:recovery"],
            "insufficient_evidence",
        )

        # 物料账差：P3 账 10 实 8。
        discrepancies = dashboard["material_discrepancies"]
        self.assertEqual(discrepancies["P3"]["deltas"], {"M1": -2})
        self.assertEqual(discrepancies["P3"]["book"], {"M1": 10})
        self.assertEqual(discrepancies["P3"]["scanned"], {"M1": 8})

        # 命令去重结果。
        counts = dashboard["command_results"]["counts"]
        self.assertEqual(counts["duplicate"], 1)
        self.assertEqual(counts["stale"], 1)
        self.assertGreaterEqual(counts["accepted"], 6)
        rejected_keys = {r["business_key"] for r in dashboard["command_results"]["rejected"]}
        self.assertIn("INC-1:P1:OP1:feed", rejected_keys)

        # 各单元最终处置。
        dispositions = dashboard["unit_dispositions"]
        self.assertEqual(dispositions["P1"]["decision"], "auto_resume")
        self.assertEqual(dispositions["P1"]["action_state"], ActionState.COMPLETED.value)
        self.assertEqual(dispositions["P1"]["last_trusted_checkpoint"]["source"], "command_log")
        self.assertEqual(dispositions["P2"]["decision"], "scrap")
        self.assertEqual(dispositions["P3"]["decision"], "inspection_required")
        self.assertEqual(dispositions["P4"]["action_state"], ActionState.BLOCKED.value)

        # 投料账：P1 投 M2=5，P2 报废 M1=10。
        ledger = dashboard["material_ledger"]
        self.assertEqual(ledger["P1"]["M2"]["fed"], 5)
        self.assertEqual(ledger["P2"]["M1"]["scrapped"], 10)

    def test_dashboard_shows_reviews(self):
        self.svc.execute_ready(self.incident)
        self.svc.record_boot_report(
            self.incident, "P2",
            {"controller_state": "standby", "physical_damage": False,
             "outage_duration_s": 10, "reported_at": "2026-09-19T08:30:00+00:00"},
        )
        dashboard = self.svc.dashboard(self.incident)
        reviews = dashboard["unit_dispositions"]["P2"]["reviews"]
        self.assertEqual(reviews[0]["conclusion"], "contradicted")
        self.assertEqual(
            dashboard["unit_dispositions"]["P2"]["action_state"],
            ActionState.REVIEW_REQUIRED.value,
        )


if __name__ == "__main__":
    unittest.main()
