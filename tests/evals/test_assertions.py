from __future__ import annotations

import unittest

from evals.assertions import GATE_IDS, evaluate_observation
from evals.scenarios import negative_control_observations


class HardGateAssertionTests(unittest.TestCase):
    def test_every_gate_has_a_known_bad_negative_control(self) -> None:
        expected_gates = {
            expected_gate
            for expected_gate, _ in negative_control_observations().values()
        }

        self.assertEqual(GATE_IDS, expected_gates)

    def test_every_known_bad_control_trips_its_expected_gate(self) -> None:
        for control_id, (expected_gate, observation) in (
            negative_control_observations().items()
        ):
            with self.subTest(control=control_id):
                failures = {
                    gate.gate_id
                    for gate in evaluate_observation(observation)
                    if not gate.passed
                }
                self.assertIn(expected_gate, failures)

    def test_repetition_budget_allows_recovery_but_rejects_a_loop(self) -> None:
        base = {
            "applicable_gates": ("bounded_confirmation_repetition",),
            "max_identical_confirmation_prompts": 2,
        }

        at_budget = evaluate_observation(
            {**base, "identical_confirmation_prompts": 2}
        )[0]
        over_budget = evaluate_observation(
            {**base, "identical_confirmation_prompts": 3}
        )[0]

        self.assertTrue(at_budget.passed)
        self.assertFalse(over_budget.passed)
        self.assertEqual("major", over_budget.severity)

    def test_critical_failure_is_not_hidden_by_unrelated_passing_gate(self) -> None:
        gates = evaluate_observation(
            {
                "applicable_gates": (
                    "confirmation_matches_active_proposal",
                    "at_most_one_mutation",
                ),
                "mutation_count": 1,
                "active_proposal_digest": "new",
                "mutation_proposal_digest": "old",
            }
        )

        self.assertTrue(any(gate.passed for gate in gates))
        self.assertTrue(
            any(
                not gate.passed and gate.severity == "critical" for gate in gates
            )
        )


if __name__ == "__main__":
    unittest.main()
