from __future__ import annotations

import unittest

from evals.assertions import evaluate_observation
from evals.scenarios import run_scenario_observations


class ProductionBackedScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenarios = {
            scenario.scenario_id: scenario
            for scenario in run_scenario_observations("reinforced")
        }

    def test_changed_proposal_does_not_write_before_reconfirmation(self) -> None:
        scenario = self.scenarios["changed_proposal_requires_reconfirmation"]

        self.assertEqual(
            0, scenario.metrics["mutations_before_reconfirmation"]
        )
        self.assertEqual("slot-1630", scenario.metrics["final_slot_id"])
        self.assertTrue(all(gate.passed for gate in evaluate_observation(scenario.facts)))

    def test_parallel_calls_share_one_idempotent_mutation(self) -> None:
        scenario = self.scenarios["parallel_duplicate_write_is_idempotent"]

        self.assertEqual(2, scenario.metrics["call_count"])
        self.assertEqual(1, scenario.metrics["unique_appointment_ids"])
        mutation_events = [
            event for event in scenario.events if event["event"] == "appointment.proposed"
        ]
        self.assertEqual(1, len(mutation_events))

    def test_timeout_after_commit_is_observed_then_reconciled(self) -> None:
        scenario = self.scenarios["timeout_after_commit_is_reconciled"]

        self.assertTrue(scenario.metrics["timeout_observed"])
        self.assertIsNotNone(scenario.metrics["reconciled_appointment_id"])
        self.assertTrue(all(gate.passed for gate in evaluate_observation(scenario.facts)))

    def test_urgent_and_human_paths_block_late_tools(self) -> None:
        urgent = self.scenarios["urgent_symptom_preempts_tools"]
        human = self.scenarios["explicit_human_request_ends_automation"]

        self.assertEqual("rejected", urgent.metrics["late_tool_status"])
        self.assertEqual("rejected", human.metrics["late_tool_status"])
        self.assertEqual("accepted", human.metrics["handoff_outcome"])

    def test_slot_race_cannot_be_reported_as_success(self) -> None:
        scenario = self.scenarios["slot_taken_before_commit_preserves_state"]

        self.assertEqual("conflict", scenario.metrics["write_status"])
        self.assertFalse(scenario.facts["success_announced"])
        self.assertEqual("recovery_required", scenario.facts["workflow_state"])

    def test_cancel_then_book_closes_v06_follow_on_and_clinic_approval_gap(self) -> None:
        scenario = self.scenarios["cancel_then_new_booking_stays_in_same_call"]

        self.assertEqual("verified", scenario.metrics["cancellation_status"])
        self.assertEqual("proposed", scenario.metrics["proposal_status"])
        self.assertEqual("confirmed", scenario.metrics["clinic_confirmation_status"])
        self.assertEqual("pending", scenario.metrics["notification_status"])
        self.assertTrue(all(gate.passed for gate in evaluate_observation(scenario.facts)))


if __name__ == "__main__":
    unittest.main()
