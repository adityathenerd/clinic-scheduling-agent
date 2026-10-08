from __future__ import annotations

from dataclasses import replace
import unittest

from appointment_harness.fixtures import load_fixture
from appointment_harness.service import AppointmentHarness
from clinic_agent.runtime.text_session import GuardedToolRuntime
from clinic_agent.runtime.text_session import PendingMutation, ResponsesTextSession
from clinic_agent.control_plane.state_machine import (
    ConfirmationDecision,
    ConversationStateMachine,
    WorkflowState,
)


SESSION = "runtime-test"
PATIENT = "patient-001"


def runtime() -> GuardedToolRuntime:
    store, clock = load_fixture()
    harness = AppointmentHarness(store, clock)
    assert harness.verify_identity(
        session_id=SESSION,
        patient_id=PATIENT,
        date_of_birth="1990-04-12",
        postal_code="411001",
    )
    return GuardedToolRuntime(harness, session_id=SESSION, patient_id=PATIENT)


def runtime_with_active_state() -> tuple[GuardedToolRuntime, ConversationStateMachine]:
    subject = runtime()
    state = ConversationStateMachine(patient_display_name="Asha Rao")
    state.state = WorkflowState.ACTIVE
    subject.state_machine = state
    return subject, state


class GuardedToolRuntimeTests(unittest.TestCase):
    def test_appointment_reads_return_complete_patient_facing_context(self) -> None:
        subject = runtime()

        result = subject.execute(
            "get_appointment", {"appointment_id": "appointment-existing"}
        )

        self.assertEqual("ok", result["status"])
        appointment = result["appointment"]
        self.assertEqual("dermatology-followup", appointment["appointment_type_id"])
        self.assertEqual("provider-mehta", appointment["provider_id"])
        self.assertEqual("downtown", appointment["location_id"])
        details = appointment["patient_facing_details"]
        self.assertEqual("Dermatology follow-up", details["appointment_type"])
        self.assertEqual("Dr. N. Mehta", details["provider"])
        self.assertEqual("Dermatology", details["specialty"])
        self.assertEqual(
            "2care Clinic, Koramangala, Bengaluru",
            details["location"],
        )
        self.assertTrue(details["date_time"].endswith("IST"))
        self.assertNotIn("UTC", details["date_time"])
        self.assertEqual(30, details["duration_minutes"])
        self.assertIn("arrive 15 minutes early", details["arrival_guidance"])
        self.assertEqual("receding hairline follow-up", details["visit_reason"])
        self.assertEqual(
            "previously prescribed minoxidil",
            details["prior_treatment_context"],
        )
        self.assertEqual([], details["prerequisites"])
        self.assertEqual("not_required", details["prerequisite_status"])
        summary = appointment["patient_facing_summary"]
        for expected in (
            "Dermatology follow-up",
            "Dr. N. Mehta",
            "Thursday, October 8, 2026",
            "9:00 AM",
            "2care Clinic, Koramangala, Bengaluru",
            "30 minutes",
            "arrive 15 minutes early",
            "reason on file is receding hairline follow-up",
            "record notes previously prescribed minoxidil",
        ):
            self.assertIn(expected, summary)
        self.assertNotIn("referral", summary.casefold())
        self.assertNotIn("prerequisite", summary.casefold())
        self.assertNotIn("dose", summary.casefold())
        self.assertNotIn("effective", summary.casefold())

    def test_appointment_read_supplies_canonical_ids_for_availability_search(self) -> None:
        subject = runtime()

        appointment = subject.execute(
            "get_appointment", {"appointment_id": "appointment-existing"}
        )["appointment"]
        result = subject.execute(
            "search_slots",
            {
                "appointment_type": appointment["appointment_type_id"],
                "date_from": "2026-10-09",
                "date_to": "2026-10-10",
                "provider_id": appointment["provider_id"],
                "location_id": appointment["location_id"],
            },
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual(3, len(result["slots"]))
        self.assertEqual(
            {"slot-1530", "slot-1630", "slot-1100"},
            {slot["slot_id"] for slot in result["slots"]},
        )

    def test_real_prerequisite_is_included_when_policy_defines_one(self) -> None:
        subject = runtime()
        appointment_type = subject.harness.store.appointment_types[
            "dermatology-followup"
        ]
        subject.harness.store.appointment_types["dermatology-followup"] = replace(
            appointment_type,
            prerequisites=("bring_prior_test_results",),
        )

        result = subject.execute(
            "get_appointment", {"appointment_id": "appointment-existing"}
        )

        details = result["appointment"]["patient_facing_details"]
        self.assertEqual(["Bring prior test results"], details["prerequisites"])
        self.assertEqual(
            "requirements_listed_not_verified", details["prerequisite_status"]
        )
        self.assertIn(
            "Bring prior test results",
            result["appointment"]["patient_facing_summary"],
        )

    def test_eligibility_without_prerequisites_is_supported_without_caveat(self) -> None:
        subject = runtime()

        result = subject.execute(
            "check_eligibility", {"appointment_type": "dermatology-followup"}
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual("supported", result["administrative_status"])
        self.assertEqual([], result["prerequisites"])

    def test_empty_slot_search_returns_explicit_recovery_prompt(self) -> None:
        subject = runtime()

        result = subject.execute(
            "search_slots",
            {
                "appointment_type": "dermatology-followup",
                "date_from": "2026-10-07",
                "date_to": "2026-10-07",
                "time_window": "after 2 PM",
            },
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual([], result["slots"])
        self.assertIn("couldn't find", result["patient_facing_summary"])
        self.assertIn("2026-10-07", result["patient_facing_summary"])
        self.assertIn("after 2 PM", result["patient_facing_summary"])

    def test_calendar_search_returns_multiple_dates_and_provider_details(self) -> None:
        subject = runtime()

        result = subject.execute(
            "search_slots",
            {
                "appointment_type": "dermatology-followup",
                "date_from": "2026-10-12",
                "date_to": "2026-10-13",
                "provider_id": "provider-mehta",
            },
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual(4, len(result["slots"]))
        self.assertEqual(
            ["Monday, October 12, 2026", "Tuesday, October 13, 2026"],
            sorted(
                {
                    str(slot["date_time"]).split(" at ", 1)[0]
                    for slot in result["slots"]
                }
            ),
        )

    def test_slot_search_normalizes_legacy_display_location_to_canonical_id(self) -> None:
        subject = runtime()

        result = subject.execute(
            "search_slots",
            {
                "appointment_type": "dermatology-followup",
                "date_from": "2026-10-12",
                "date_to": "2026-10-18",
                "provider_id": "provider-mehta",
                "location": "Downtown clinic",
            },
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual(8, len(result["slots"]))

    def test_slot_search_accepts_canonical_location_id(self) -> None:
        subject = runtime()

        result = subject.execute(
            "search_slots",
            {
                "appointment_type": "dermatology-followup",
                "date_from": "2026-10-12",
                "date_to": "2026-10-18",
                "provider_id": "provider-mehta",
                "location_id": "downtown",
            },
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual(8, len(result["slots"]))
        self.assertTrue(
            all(slot["provider"] == "Dr. N. Mehta" for slot in result["slots"])
        )

    def test_create_requires_a_prior_exact_proposal_and_confirmation(self) -> None:
        subject, state = runtime_with_active_state()
        subject.begin_patient_turn("Show me Thursday afternoon")
        subject.execute(
            "search_slots",
            {
                "appointment_type": "dermatology-followup",
                "date_from": "2026-10-09",
                "date_to": "2026-10-09",
            },
        )
        first = subject.execute("create_appointment", {"slot_id": "slot-1530"})

        self.assertEqual("confirmation_required", first["status"])
        self.assertNotIn("appointment-0001", subject.harness.store.appointments)

        subject.begin_patient_turn(
            "yes, confirm",
            confirmation_decision=ConfirmationDecision.CONFIRMED,
            require_semantic_confirmation=True,
        )
        second = subject.execute("create_appointment", {"slot_id": "slot-1530"})

        self.assertEqual("proposed", second["status"])
        self.assertEqual("proposal_created", second["operation"])
        self.assertEqual(WorkflowState.AWAITING_FOLLOW_UP_DECISION, state.state)
        self.assertIn(
            "Would you like any other scheduling or clinic information help?",
            second["patient_facing_summary"],
        )

    def test_correction_invalidates_pending_proposal(self) -> None:
        subject = runtime()
        subject.begin_patient_turn("Cancel my appointment")
        subject.execute(
            "delete_appointment", {"appointment_id": "appointment-existing"}
        )

        subject.begin_patient_turn("Actually, don't cancel it")

        self.assertIsNone(subject.pending)
        appointment = subject.harness.store.appointments["appointment-existing"]
        self.assertEqual("scheduled", appointment.status.value)

    def test_semantic_confirmation_accepts_observed_natural_voice_answer(self) -> None:
        subject, state = runtime_with_active_state()
        subject.execute(
            "search_slots",
            {
                "appointment_type": "dermatology-followup",
                "date_from": "2026-10-12",
                "date_to": "2026-10-12",
                "provider_id": "provider-mehta",
                "location_id": "downtown",
            },
        )
        arguments = {
            "appointment_id": "appointment-existing",
            "replacement_slot_id": "slot-mehta-20261012-1130",
        }
        proposed = subject.execute("edit_appointment", arguments)
        self.assertEqual("confirmation_required", proposed["status"])

        subject.begin_patient_turn(
            "And yes, confirm. Yes, I am confirming that proposal.",
            confirmation_decision=ConfirmationDecision.CONFIRMED,
            require_semantic_confirmation=True,
        )
        result = subject.execute("edit_appointment", arguments)

        self.assertEqual("proposed", result["status"])
        self.assertIn("held for clinic review", result["patient_facing_summary"])
        self.assertIn("remains in place until approval", result["patient_facing_summary"])
        self.assertIn(
            "Would you like any other scheduling or clinic information help?",
            result["patient_facing_summary"],
        )
        self.assertEqual(WorkflowState.AWAITING_FOLLOW_UP_DECISION, state.state)
        self.assertEqual(
            "slot-existing",
            subject.harness.store.appointments["appointment-existing"].slot_id,
        )
        self.assertEqual(
            "slot-mehta-20261012-1130",
            subject.harness.store.appointments[
                "appointment-existing"
            ].pending_replacement_slot_id,
        )

    def test_new_reschedule_proposal_discloses_replacement_of_existing_hold(self) -> None:
        subject = runtime()
        subject.execute(
            "search_slots",
            {
                "appointment_type": "dermatology-followup",
                "date_from": "2026-10-09",
                "date_to": "2026-10-09",
                "provider_id": "provider-mehta",
                "location_id": "downtown",
            },
        )
        first_arguments = {
            "appointment_id": "appointment-existing",
            "replacement_slot_id": "slot-1630",
        }
        subject.execute("edit_appointment", first_arguments)
        subject.begin_patient_turn(
            "yes, confirm",
            confirmation_decision=ConfirmationDecision.CONFIRMED,
            require_semantic_confirmation=True,
        )
        first = subject.execute("edit_appointment", first_arguments)
        self.assertEqual("proposed", first["status"])

        subject.execute(
            "search_slots",
            {
                "appointment_type": "dermatology-followup",
                "date_from": "2026-10-12",
                "date_to": "2026-10-12",
                "provider_id": "provider-mehta",
                "location_id": "downtown",
            },
        )
        second = subject.execute(
            "edit_appointment",
            {
                "appointment_id": "appointment-existing",
                "replacement_slot_id": "slot-mehta-20261012-1130",
            },
        )

        self.assertEqual("confirmation_required", second["status"])
        self.assertIn(
            "replace your pending rescheduling request for Friday, October 9, 2026 at 4:30 PM IST",
            second["exact_proposal"],
        )

    def test_unclear_semantic_confirmation_preserves_exact_proposal(self) -> None:
        subject = runtime()
        subject.execute(
            "delete_appointment", {"appointment_id": "appointment-existing"}
        )
        pending = subject.pending

        subject.begin_patient_turn(
            "I suppose so",
            confirmation_decision=ConfirmationDecision.UNCLEAR,
            require_semantic_confirmation=True,
        )

        self.assertIs(pending, subject.pending)
        self.assertFalse(subject.confirmation_authorized_for_turn)


class ConfirmationRenderingTests(unittest.TestCase):
    def test_confirmation_message_is_application_rendered_from_exact_proposal(self) -> None:
        pending = PendingMutation(
            tool_name="delete_appointment",
            arguments={"appointment_id": "appointment-existing"},
            proposal_id="proposal-0001",
            confirmation_token="server-secret",
            summary=(
                "Cancel Dermatology follow-up with Dr. N. Mehta at downtown on "
                "October 8, 2026 at 9:00 AM (+0530)"
            ),
            guard_context={"expected_appointment_version": 1},
        )

        message = ResponsesTextSession._confirmation_message(pending)

        self.assertIn("Dermatology follow-up", message)
        self.assertIn("Dr. N. Mehta", message)
        self.assertIn("downtown", message)
        self.assertIn("October 8, 2026 at 9:00 AM", message)
        self.assertIn("Please say yes or no", message)
        self.assertNotIn("server-secret", message)

    def test_cancellation_is_verified_and_creates_follow_up(self) -> None:
        subject = runtime()
        subject.begin_patient_turn("Cancel my appointment")
        subject.execute(
            "delete_appointment", {"appointment_id": "appointment-existing"}
        )
        subject.begin_patient_turn("yes, confirm")

        result = subject.execute(
            "delete_appointment", {"appointment_id": "appointment-existing"}
        )

        self.assertEqual("verified", result["status"])
        self.assertEqual("cancelled", result["operation"])
        self.assertEqual(
            "accepted", result["front_desk_follow_up"]["outcome"]
        )

    def test_ambiguous_assent_does_not_authorize_write(self) -> None:
        subject = runtime()
        subject.begin_patient_turn("Cancel my appointment")
        subject.execute(
            "delete_appointment", {"appointment_id": "appointment-existing"}
        )

        subject.begin_patient_turn("yes, but move it instead")
        result = subject.execute(
            "delete_appointment", {"appointment_id": "appointment-existing"}
        )

        self.assertEqual("confirmation_required", result["status"])
        appointment = subject.harness.store.appointments["appointment-existing"]
        self.assertEqual("scheduled", appointment.status.value)

    def test_confirmation_is_bound_to_proposed_appointment_version(self) -> None:
        subject = runtime()
        subject.begin_patient_turn("Cancel my appointment")
        subject.execute(
            "delete_appointment", {"appointment_id": "appointment-existing"}
        )
        subject.harness.store.appointments["appointment-existing"].version += 1

        subject.begin_patient_turn("yes, confirm")
        result = subject.execute(
            "delete_appointment", {"appointment_id": "appointment-existing"}
        )

        self.assertEqual("conflict", result["status"])
        appointment = subject.harness.store.appointments["appointment-existing"]
        self.assertEqual("scheduled", appointment.status.value)


if __name__ == "__main__":
    unittest.main()
