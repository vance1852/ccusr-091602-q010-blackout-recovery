"""端到端集成：整厂短时断电后的无人值守恢复全流程。"""

import os
import tempfile
import unittest

from app.models import ActionState, CommandResult, Decision

from tests.support import SimulatedCrash, build_blackout_scenario, new_service


class EndToEndRecoveryTest(unittest.TestCase):
    def test_full_blackout_recovery_story(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "plant.db")
            svc = new_service(db_path)
            incident = build_blackout_scenario(svc)

            # 1. 裁决：矛盾现场被裁成四类结论，各自带最后可信检查点。
            decisions = svc.adjudicate(incident)
            self.assertEqual(decisions["P1"].decision, Decision.AUTO_RESUME)
            self.assertEqual(decisions["P1"].last_trusted_checkpoint.source, "command_log")
            self.assertEqual(decisions["P2"].decision, Decision.SCRAP)
            self.assertEqual(decisions["P3"].decision, Decision.INSPECTION_REQUIRED)
            self.assertEqual(decisions["P4"].decision, Decision.INSUFFICIENT_EVIDENCE)

            # 2. 演练：正式推进前先跑一遍，不污染正式记录。
            drill = svc.start_drill(incident)
            svc.adjudicate(incident, namespace=drill)
            svc.create_plan(incident, namespace=drill)
            svc.execute_ready(incident, namespace=drill)
            svc.reset_drill(drill)
            self.assertEqual(svc.material_ledger(incident), {})

            # 3. 正式计划：人工把 P3 从“检查”改为“报废”，触发双人确认。
            svc.create_plan(incident)
            svc.override_decision(incident, "P3", "scrap")
            svc.confirm_action(incident, "INC-1:P3:recovery", "alice")
            svc.confirm_action(incident, "INC-1:P3:recovery", "bob")

            # 4. 执行中进程崩溃（P1 投料落账后），重启续跑不多投料。
            def crash_on_feed(spec, _result):
                if spec.command_type == "feed_material":
                    raise SimulatedCrash(spec.key)

            with self.assertRaises(SimulatedCrash):
                svc.execute_ready(incident, crash_hook=crash_on_feed)
            svc.close()

            svc = new_service(db_path)
            svc.recover(incident)
            self.assertEqual(svc.material_ledger(incident)["P1"]["M2"]["fed"], 5)

            # 5. 网络恢复后旧命令到达：同键重投被去重。
            result = svc.dispatch_command(
                incident, "P1", "INC-1:P1:OP1:feed", "feed_material",
                {"operation": "OP1", "materials": {"M1": 10}},
                issued_at="2026-09-19T09:00:00+00:00",
            )
            self.assertEqual(result, CommandResult.DUPLICATE.value)

            # 6. P4 证据补齐后自动解锁并执行。
            svc.record_boot_report(
                incident, "P4",
                {"controller_state": "standby", "physical_damage": False,
                 "outage_duration_s": 45, "reported_at": "2026-09-19T08:20:00+00:00"},
            )
            svc.record_material_scan(
                incident, "P4", {"materials": {"M1": 10}, "scanned_at": "2026-09-19T08:21:00+00:00"}
            )
            summary = svc.execute_ready(incident)
            self.assertIn("INC-1:P4:recovery", summary["executed"])

            # 7. 正式推进视图：全部单元有最终处置，无遗留阻塞。
            dashboard = svc.dashboard(incident)
            self.assertEqual(dashboard["blocking_chains"]["chains"], {})
            dispositions = dashboard["unit_dispositions"]
            for unit in ("U1", "V1", "P1", "P2", "P3", "P4"):
                self.assertEqual(
                    dispositions[unit]["action_state"],
                    ActionState.COMPLETED.value,
                    unit,
                )
            self.assertEqual(dispositions["P3"]["manual_choice"], "scrap")
            # 两条 duplicate：崩溃重放的 OP2 投料 + 网络恢复后重投的 OP1 旧命令。
            rejected = dashboard["command_results"]["rejected"]
            duplicate_keys = {r["business_key"] for r in rejected if r["result"] == "duplicate"}
            self.assertEqual(
                duplicate_keys, {"INC-1:P1:OP1:feed", "INC-1:P1:OP2:feed"}
            )
            svc.close()


if __name__ == "__main__":
    unittest.main()
