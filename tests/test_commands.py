"""命令去重网关测试：业务键去重、陈旧拒绝与依赖闸口。"""

import unittest

from app.models import CommandResult

from tests.support import build_blackout_scenario, new_service


class CommandGatewayTest(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()
        self.incident = build_blackout_scenario(self.svc)
        self.svc.adjudicate(self.incident)
        self.svc.create_plan(self.incident)

    def tearDown(self):
        self.svc.close()

    def test_duplicate_same_key_same_payload(self):
        # 网络恢复后 MES 重投断电前的投料命令：不得重复生效。
        result = self.svc.dispatch_command(
            self.incident,
            "P1",
            "INC-1:P1:OP1:feed",
            "feed_material",
            {"operation": "OP1", "materials": {"M1": 10}},
            issued_at="2026-09-19T09:00:00+00:00",
        )
        self.assertEqual(result, CommandResult.DUPLICATE.value)
        # 物料账没有任何新增投料。
        self.assertEqual(self.svc.material_ledger(self.incident), {})

    def test_stale_same_key_conflicting_payload(self):
        result = self.svc.dispatch_command(
            self.incident,
            "P1",
            "INC-1:P1:OP1:feed",
            "feed_material",
            {"operation": "OP1", "materials": {"M1": 12}},
            issued_at="2026-09-19T09:00:00+00:00",
        )
        self.assertEqual(result, CommandResult.STALE.value)

    def test_stale_old_timestamp_after_newer_applied(self):
        self.svc.execute_ready(self.incident)
        # P1 已在恢复中应用了 OP2 投料；迟到的旧投料命令按陈旧拒绝。
        result = self.svc.dispatch_command(
            self.incident,
            "P1",
            "INC-1:P1:OP1:refeed",
            "feed_material",
            {"operation": "OP1", "materials": {"M1": 10}},
            issued_at="2026-09-19T07:00:00+00:00",
        )
        self.assertEqual(result, CommandResult.STALE.value)

    def test_dependency_blocked_does_not_claim_key(self):
        # 公用工程/输送未就绪时，加工单元命令被暂缓。
        result = self.svc.dispatch_command(
            self.incident,
            "P1",
            "INC-1:P1:OPX:feed",
            "feed_material",
            {"operation": "OPX", "materials": {"M3": 1}},
            issued_at="2026-09-19T09:00:00+00:00",
        )
        self.assertEqual(result, CommandResult.DEPENDENCY_BLOCKED.value)
        # 键未被占用：依赖补齐后同一键可被接受。
        self.svc.execute_ready(self.incident)
        retry = self.svc.dispatch_command(
            self.incident,
            "P1",
            "INC-1:P1:OPX:feed",
            "feed_material",
            {"operation": "OPX", "materials": {"M3": 1}},
            issued_at="2026-09-19T23:59:59+00:00",
        )
        self.assertEqual(retry, CommandResult.ACCEPTED.value)

    def test_accepted_command_applies_effect_atomically(self):
        self.svc.execute_ready(self.incident)
        ledger = self.svc.material_ledger(self.incident)
        self.assertEqual(ledger["P1"]["M2"], {"fed": 5, "scrapped": 0, "net": 5})

    def test_attempts_are_auditable(self):
        self.svc.dispatch_command(
            self.incident,
            "P1",
            "INC-1:P1:OP1:feed",
            "feed_material",
            {"operation": "OP1", "materials": {"M1": 10}},
            issued_at="2026-09-19T09:00:00+00:00",
        )
        attempts = self.svc.gateway.attempts("official", self.incident)
        duplicates = [a for a in attempts if a["result"] == "duplicate"]
        self.assertTrue(any(a["business_key"] == "INC-1:P1:OP1:feed" for a in duplicates))


if __name__ == "__main__":
    unittest.main()
