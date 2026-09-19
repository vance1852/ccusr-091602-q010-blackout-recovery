"""命令网关：业务键去重、旧命令不得重复生效。"""
from __future__ import annotations

from helpers import T0, PlantCase


class CommandGatewayTest(PlantCase):
    def _movements(self, unit_id):
        return self.svc.store.query(
            "SELECT * FROM material_movements WHERE ns='official'"
            " AND unit_id=?",
            (unit_id,),
        )

    def test_accepted_then_duplicate(self):
        self.adjudicated_plant()
        self.svc.execute_ready()  # 依赖就绪，加工动作不再阻塞
        first = self.svc.submit_command("cmd-x", "FEED", "U-RESUME",
                                        {"qty": 5.0})
        second = self.svc.submit_command("cmd-x", "FEED", "U-RESUME",
                                         {"qty": 5.0})
        self.assertEqual(first["result"], "accepted")
        self.assertEqual(second["result"], "duplicate")
        # 只生效一次：物料账上只有一笔移动。
        movements = [m for m in self._movements("U-RESUME")
                     if m["effect_key"] == "cmd-x"]
        self.assertEqual(len(movements), 1)

    def test_pre_outage_acked_command_is_duplicate_after_network_recovery(self):
        """停电前已 acked 的命令，网络恢复后再次到达不得重复生效。"""
        self.svc.ingest_command_log("cmd-feed-old", "U-RESUME", "FEED",
                                    {"qty": 5.0}, issued_at=T0, status="acked")
        self.adjudicated_plant()

        late = self.svc.submit_command("cmd-feed-old", "FEED", "U-RESUME",
                                       {"qty": 5.0}, issued_at=T0)

        self.assertEqual(late["result"], "duplicate")
        self.assertEqual(self._movements("U-RESUME"), [])

    def test_pre_outage_unconfirmed_command_is_stale(self):
        """停电前签发但从未确认的命令：恢复计划已接管，不得生效。"""
        self.svc.ingest_command_log("cmd-feed-maybe", "U-RESUME", "FEED",
                                    {"qty": 5.0}, issued_at=T0,
                                    status="issued")
        self.adjudicated_plant()

        late = self.svc.submit_command("cmd-feed-maybe", "FEED", "U-RESUME",
                                       {"qty": 5.0}, issued_at=T0)

        self.assertEqual(late["result"], "stale")
        self.assertEqual(self._movements("U-RESUME"), [])

    def test_old_timestamp_new_key_is_stale(self):
        self.adjudicated_plant()
        res = self.svc.submit_command("cmd-ancient", "FEED", "U-RESUME",
                                      {"qty": 5.0}, issued_at=T0)
        self.assertEqual(res["result"], "stale")
        self.assertEqual(self._movements("U-RESUME"), [])

    def test_dependency_blocked_then_accepted_after_unblock(self):
        self.adjudicated_plant()
        # 加工动作仍被公用工程/输送阻塞。
        blocked = self.svc.submit_command("cmd-y", "FEED", "U-RESUME",
                                          {"qty": 5.0})
        self.assertEqual(blocked["result"], "dependency_blocked")
        self.assertEqual(self._movements("U-RESUME"), [])

        # 依赖就绪后同一业务键可重发并生效（阻塞不消耗业务键）。
        self.svc.execute_ready()
        retry = self.svc.submit_command("cmd-y", "FEED", "U-RESUME",
                                        {"qty": 5.0})
        self.assertEqual(retry["result"], "accepted")
        self.assertEqual(len(self._movements("U-RESUME")), 1)

    def test_command_results_visible_in_report(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        self.svc.submit_command("cmd-a", "FEED", "U-RESUME", {"qty": 5.0})
        self.svc.submit_command("cmd-a", "FEED", "U-RESUME", {"qty": 5.0})
        self.svc.submit_command("cmd-b", "FEED", "U-RESUME", {"qty": 5.0},
                                issued_at=T0)
        report = self.svc.progression_report()
        mine = [d for d in report["commands"]["details"]
                if d["business_key"] in ("cmd-a", "cmd-b")]
        results = sorted(d["result"] for d in mine)
        self.assertEqual(results, ["accepted", "duplicate", "stale"])


if __name__ == "__main__":
    import unittest

    unittest.main()
