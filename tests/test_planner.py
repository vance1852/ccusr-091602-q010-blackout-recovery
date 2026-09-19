"""恢复计划测试：依赖顺序、越级禁止与阻塞链。"""

import unittest

from app.models import ActionState, TopologyError

from tests.support import build_blackout_scenario, new_service


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()
        self.incident = build_blackout_scenario(self.svc)
        self.svc.adjudicate(self.incident)
        self.actions = {a["unit_id"]: a for a in self.svc.create_plan(self.incident)}

    def tearDown(self):
        self.svc.close()

    def test_action_kinds_follow_decisions(self):
        self.assertEqual(self.actions["U1"]["kind"], "restore_utility")
        self.assertEqual(self.actions["V1"]["kind"], "restore_conveyor")
        self.assertEqual(self.actions["P1"]["kind"], "resume_processing")
        self.assertEqual(self.actions["P2"]["kind"], "scrap_unit")
        self.assertEqual(self.actions["P3"]["kind"], "inspect_unit")
        self.assertEqual(self.actions["P4"]["kind"], "collect_evidence")

    def test_initial_readiness_respects_dependencies(self):
        self.assertEqual(self.actions["U1"]["state"], ActionState.READY.value)
        for unit in ("V1", "P1", "P2", "P3", "P4"):
            self.assertEqual(self.actions[unit]["state"], ActionState.BLOCKED.value, unit)

    def test_no_skipping_levels(self):
        # 公用工程未完成时，输送与加工都不能就绪。
        self.svc.execute_action(self.incident, self.actions["U1"]["action_id"])
        actions = {a["unit_id"]: a for a in self.svc.actions(self.incident)}
        self.assertEqual(actions["V1"]["state"], ActionState.READY.value)
        self.assertEqual(actions["P1"]["state"], ActionState.BLOCKED.value)
        with self.assertRaises(Exception):
            self.svc.execute_action(self.incident, actions["P1"]["action_id"])

    def test_full_execution_order(self):
        summary = self.svc.execute_ready(self.incident)
        executed = summary["executed"]
        # 依赖序：U1 → V1 → 加工单元。
        self.assertLess(executed.index("INC-1:U1:recovery"), executed.index("INC-1:V1:recovery"))
        self.assertLess(executed.index("INC-1:V1:recovery"), executed.index("INC-1:P1:recovery"))
        # P4 证据不足，保持阻塞。
        self.assertIn("INC-1:P4:recovery", summary["blocked"])

    def test_blocking_chains_before_execution(self):
        chains = self.svc.planner.blocking_chains("official", self.incident)
        p1_chains = chains["chains"]["INC-1:P1:recovery"]
        self.assertEqual(p1_chains, [["INC-1:U1:recovery", "INC-1:V1:recovery", "INC-1:P1:recovery"]])
        self.assertEqual(chains["reasons"]["INC-1:U1:recovery"], "pending_execution")
        self.assertEqual(chains["reasons"]["INC-1:V1:recovery"], "dependency_unmet")

    def test_topology_rejects_level_skipping_dependency(self):
        self.svc.register_unit(self.incident, "PX", "processing", requires=["U2-missing"])
        with self.assertRaises(TopologyError):
            self.svc.create_plan(self.incident)

    def test_topology_rejects_same_level_dependency(self):
        svc = new_service()
        svc.register_unit("INC-2", "A", "processing")
        svc.register_unit("INC-2", "B", "processing", requires=["A"])
        with self.assertRaises(TopologyError):
            svc.create_plan("INC-2")
        svc.close()

    def test_plan_is_idempotent(self):
        first = {a["action_id"]: a["state"] for a in self.svc.create_plan(self.incident)}
        second = {a["action_id"]: a["state"] for a in self.svc.create_plan(self.incident)}
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
