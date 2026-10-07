from __future__ import annotations

import unittest

from clinic_agent.control_plane.state_machine import WorkflowState
from clinic_agent.runtime.mock_call import render_mock_call, run_mock_booking_call


class MockCallTests(unittest.TestCase):
    def test_mock_call_reaches_verified_terminal_booking(self) -> None:
        result = run_mock_booking_call()

        self.assertEqual(WorkflowState.COMPLETED, result.workflow_state)
        self.assertEqual("proposed", result.appointment_status)
        self.assertEqual("slot-1530", result.appointment_slot_id)
        self.assertIn("appointment.verified", result.audit_event_types)

    def test_mock_call_stops_for_confirmation_before_mutation(self) -> None:
        result = run_mock_booking_call()
        proposal_index = next(
            index
            for index, (_, text) in enumerate(result.transcript)
            if "exact appointment request" in text
        )
        confirmation_index = next(
            index
            for index, (speaker, text) in enumerate(result.transcript)
            if speaker == "Patient" and text == "Yes, confirm."
        )

        self.assertLess(proposal_index, confirmation_index)
        self.assertEqual(
            1,
            sum(event == "appointment.proposed" for event in result.audit_event_types),
        )

    def test_rendered_mock_call_contains_no_confirmation_token(self) -> None:
        rendered = render_mock_call(run_mock_booking_call())

        self.assertIn("Verified proposal outcome", rendered)
        self.assertNotIn("confirmation_token", rendered)
        self.assertNotIn("proposal-0001", rendered)


if __name__ == "__main__":
    unittest.main()
