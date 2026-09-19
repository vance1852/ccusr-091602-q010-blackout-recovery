"""领域契约一致性：代码枚举与 domain_contract.json 对齐。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from app import models

CONTRACT = json.loads(
    (Path(__file__).resolve().parent.parent / "domain_contract.json")
    .read_text(encoding="utf-8")
)


class ContractConformanceTest(unittest.TestCase):
    def test_decisions_match_contract(self):
        self.assertEqual(sorted(models.DECISIONS), sorted(CONTRACT["decisions"]))

    def test_action_states_match_contract(self):
        self.assertEqual(sorted(models.ACTION_STATES),
                         sorted(CONTRACT["action_states"]))

    def test_command_results_match_contract(self):
        self.assertEqual(sorted(models.COMMAND_RESULTS),
                         sorted(CONTRACT["command_results"]))


if __name__ == "__main__":
    unittest.main()
