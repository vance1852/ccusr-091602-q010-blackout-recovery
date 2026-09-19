"""恢复计划：依赖层级、阻塞链与不得越级。"""
from __future__ import annotations

from app.models import BlockedError
from helpers import PlantCase


class PlanningTest(PlantCase):
    def test_plan_levels_and_dependencies(self):
        _, actions = self.adjudicated_plant()
        by_id = {a["action_id"]: a for a in actions}

        util = by_id["act-restore_utility-UT-POWER"]
        line = by_id["act-restore_conveying-LINE-1"]
        resume = by_id["act-resume_operation-U-RESUME"]

        self.assertEqual(util["level"], 0)
        self.assertEqual(util["state"], "ready")
        self.assertEqual(line["level"], 1)
        self.assertEqual(line["state"], "blocked")
        self.assertEqual(line["depends"], ["act-restore_utility-UT-POWER"])
        self.assertEqual(resume["level"], 2)
        self.assertEqual(resume["state"], "blocked")
        self.assertIn("act-restore_utility-UT-POWER", resume["depends"])
        self.assertIn("act-restore_conveying-LINE-1", resume["depends"])

    def test_decision_to_verb_mapping(self):
        _, actions = self.adjudicated_plant()
        verbs = {a["unit_id"]: a["verb"] for a in actions if a["level"] == 2}
        self.assertEqual(verbs["U-RESUME"], "resume_operation")
        self.assertEqual(verbs["U-RECEIPT"], "upload_receipt")
        self.assertEqual(verbs["U-SCRAP"], "scrap")
        self.assertEqual(verbs["U-INSPECT"], "inspect")
        self.assertEqual(verbs["U-UNKNOWN"], "hold")

    def test_hold_action_blocked_by_evidence(self):
        _, actions = self.adjudicated_plant()
        hold = next(a for a in actions if a["verb"] == "hold")
        self.assertEqual(hold["state"], "blocked")
        self.assertEqual(hold["blocked_reason"], "evidence")

    def test_blocking_chain_is_transitive(self):
        self.adjudicated_plant()
        chain = self.svc.blocking_chain("act-resume_operation-U-RESUME")
        ids = [c["action_id"] for c in chain]
        self.assertEqual(
            ids,
            ["act-restore_utility-UT-POWER", "act-restore_conveying-LINE-1"],
        )

    def test_cannot_skip_levels(self):
        """公用工程/输送未就绪时，加工单元不得越级执行。"""
        self.adjudicated_plant()
        with self.assertRaises(BlockedError) as ctx:
            self.svc.execute_action("act-resume_operation-U-RESUME")
        self.assertEqual(ctx.exception.action_id,
                         "act-resume_operation-U-RESUME")
        self.assertEqual(len(ctx.exception.chain), 2)

    def test_execute_ready_runs_in_dependency_order(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        done = [
            e["payload"]["action_id"]
            for e in self.svc.events(type_="execution_completed")
        ]
        self.assertLess(done.index("act-restore_utility-UT-POWER"),
                        done.index("act-restore_conveying-LINE-1"))
        self.assertLess(done.index("act-restore_conveying-LINE-1"),
                        done.index("act-resume_operation-U-RESUME"))

    def test_downstream_unblocks_after_upstream_completes(self):
        self.adjudicated_plant()
        self.svc.execute_action("act-restore_utility-UT-POWER")
        self.svc.execute_action("act-restore_conveying-LINE-1")
        from app.planning import refresh_states
        refresh_states(self.svc.store, "official", self.svc.clock())
        action = next(a for a in self.svc.list_actions()
                      if a["action_id"] == "act-resume_operation-U-RESUME")
        self.assertEqual(action["state"], "ready")

    def test_plan_rebuild_keeps_executed_actions(self):
        self.adjudicated_plant()
        self.svc.execute_ready()
        before = {a["action_id"]: a["state"] for a in self.svc.list_actions()}
        self.svc.build_plan()
        after = {a["action_id"]: a["state"] for a in self.svc.list_actions()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    import unittest

    unittest.main()
