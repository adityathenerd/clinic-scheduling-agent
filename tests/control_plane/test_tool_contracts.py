from __future__ import annotations

import unittest

from clinic_agent.control_plane.tool_contracts import (
    DELETE_APPOINTMENT_TOOL,
    EDIT_APPOINTMENT_TOOL,
    DeleteAppointmentCommand,
    EditAppointmentCommand,
    PostCancellationFollowUpCommand,
)


class ToolSchemaTests(unittest.TestCase):
    def test_edit_schema_exposes_only_domain_arguments(self) -> None:
        parameters = EDIT_APPOINTMENT_TOOL["parameters"]
        self.assertEqual(
            set(parameters["properties"]),
            {"appointment_id", "replacement_slot_id"},
        )
        self.assertFalse(parameters["additionalProperties"])

    def test_delete_schema_does_not_expose_guard_or_handoff_fields(self) -> None:
        parameters = DELETE_APPOINTMENT_TOOL["parameters"]
        self.assertEqual(set(parameters["properties"]), {"appointment_id"})
        self.assertFalse(parameters["additionalProperties"])


class InternalCommandTests(unittest.TestCase):
    def test_edit_requires_non_negative_current_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-negative"):
            EditAppointmentCommand(
                session_id="session-1",
                patient_id="patient-1",
                appointment_id="appointment-1",
                expected_appointment_version=-1,
                replacement_slot_id="slot-2",
                availability_snapshot_id="availability-1",
                proposal_id="proposal-1",
                confirmation_token="confirmation-1",
                idempotency_key="edit-1",
            )

    def test_delete_requires_server_owned_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "confirmation_token"):
            DeleteAppointmentCommand(
                session_id="session-1",
                patient_id="patient-1",
                appointment_id="appointment-1",
                expected_appointment_version=4,
                proposal_id="proposal-1",
                confirmation_token="",
                idempotency_key="delete-1",
            )

    def test_follow_up_reason_is_fixed_by_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed by policy"):
            PostCancellationFollowUpCommand(
                session_id="session-1",
                patient_id="patient-1",
                cancelled_appointment_id="appointment-1",
                follow_up_topics=("reschedule_options",),
                idempotency_key="follow-up-1",
                reason="model_selected_reason",
            )


if __name__ == "__main__":
    unittest.main()
