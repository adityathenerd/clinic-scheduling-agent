from __future__ import annotations

import unittest

from appointment_harness.fixtures import load_fixture
from appointment_harness.service import AppointmentHarness
from clinic_agent.control_plane.state_machine import (
    ConversationStateMachine,
    FollowUpDecision,
    WorkflowState,
)
from clinic_agent.runtime.text_session import GuardedToolRuntime


class ConversationStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.subject = ConversationStateMachine(patient_display_name="Asha Rao")

    def test_greeting_precedes_intent_and_identity(self) -> None:
        greeting = self.subject.greeting(
            assistant_name="Mira", clinic_name="Aster Demo Clinic"
        )

        self.assertIn("Hello", greeting)
        self.assertIn("scheduling assistant", greeting)
        self.assertEqual(WorkflowState.AWAITING_INTENT, self.subject.state)

    def test_outbound_opening_states_purpose_and_requests_identity_once(self) -> None:
        opening = self.subject.begin_outbound_call(
            assistant_name="Mira", clinic_name="Aster Demo Clinic"
        )

        self.assertIn("I'm calling about a scheduling matter", opening)
        self.assertNotIn("How can I help", opening)
        self.assertIn("Asha Rao", opening)
        self.assertEqual(WorkflowState.AWAITING_NAME_CONFIRMATION, self.subject.state)
        self.assertEqual(
            "outbound_scheduling_call_started", self.subject.transitions[-1].reason
        )

    def test_intent_is_captured_before_name_confirmation(self) -> None:
        calls: list[bool] = []

        directive = self.subject.accept_patient_turn(
            "I need to reschedule my appointment",
            authorize_name_confirmation=lambda confirmed: calls.append(confirmed) or True,
        )

        self.assertEqual(
            WorkflowState.AWAITING_NAME_CONFIRMATION, self.subject.state
        )
        self.assertIn("rescheduling", directive.reply or "")
        self.assertIn("Asha Rao", directive.reply or "")
        self.assertEqual([], calls)

    def test_affirmative_name_confirmation_unlocks_active_work(self) -> None:
        self.subject.accept_patient_turn(
            "I need to book an appointment",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        directive = self.subject.accept_patient_turn(
            "Yes, this is Asha Rao",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        self.assertEqual(WorkflowState.ACTIVE, self.subject.state)
        self.assertIn("book an appointment", directive.model_input or "")
        self.assertIn(
            "patient_name_confirmation", self.subject.prompt_context()
        )

    def test_affirmative_sentence_with_exact_patient_name_is_accepted(self) -> None:
        self.subject.accept_patient_turn(
            "I need to reschedule my appointment",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        directive = self.subject.accept_patient_turn(
            "Yes, I am Asha Rao.",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        self.assertEqual(WorkflowState.ACTIVE, self.subject.state)
        self.assertIsNotNone(directive.model_input)

    def test_observed_natural_identity_phrases_are_accepted_first_time(self) -> None:
        for phrase in (
            "Yes, this is Asha Rao speaking",
            "Yes, it is",
        ):
            with self.subTest(phrase=phrase):
                subject = ConversationStateMachine(patient_display_name="Asha Rao")
                subject.begin_outbound_call(
                    assistant_name="Mira", clinic_name="Aster Demo Clinic"
                )

                directive = subject.accept_patient_turn(
                    phrase,
                    authorize_name_confirmation=lambda confirmed: confirmed,
                )

                self.assertEqual(WorkflowState.ACTIVE, subject.state)
                self.assertIsNotNone(directive.model_input)

    def test_unclear_identity_answer_does_not_unlock_tools(self) -> None:
        self.subject.accept_patient_turn(
            "I need to cancel",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        directive = self.subject.accept_patient_turn(
            "Maybe",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        self.assertEqual(
            WorkflowState.AWAITING_NAME_CONFIRMATION, self.subject.state
        )
        self.assertIn("yes or no", directive.reply or "")

    def test_denied_identity_requests_proxy_details_without_record_access(self) -> None:
        self.subject.accept_patient_turn(
            "Show appointments",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        directive = self.subject.accept_patient_turn(
            "No, I am not Asha",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        self.assertEqual(WorkflowState.AWAITING_PROXY_DETAILS, self.subject.state)
        self.assertIn("authorized caregiver", directive.reply or "")

    def test_stored_proxy_authority_unlocks_active_work(self) -> None:
        self.subject.accept_patient_turn(
            "I need to reschedule",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )
        self.subject.accept_patient_turn(
            "No, I am not Asha",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        directive = self.subject.accept_patient_turn(
            "Rohan Rao, husband",
            authorize_name_confirmation=lambda confirmed: confirmed,
            authorize_proxy=lambda name, relationship: (
                name == "Rohan Rao" and relationship == "spouse"
            ),
        )

        self.assertEqual(WorkflowState.ACTIVE, self.subject.state)
        self.assertIn("authorized spouse", directive.model_input or "")
        self.assertIn("authorized_proxy", self.subject.prompt_context())

    def test_explicit_human_request_creates_handoff_directive(self) -> None:
        directive = self.subject.accept_patient_turn(
            "I want the front desk",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        self.assertEqual(WorkflowState.HUMAN_HANDOFF_PENDING, self.subject.state)
        self.assertEqual("caller_requested_human", directive.handoff_reason)

    def test_urgent_signal_preempts_intent_and_identity(self) -> None:
        calls: list[bool] = []

        directive = self.subject.accept_patient_turn(
            "I need an appointment because I have chest pain and cannot breathe",
            authorize_name_confirmation=lambda confirmed: calls.append(confirmed) or True,
        )

        self.assertEqual(WorkflowState.URGENT_HANDOFF, self.subject.state)
        self.assertIn("emergency services", directive.reply or "")
        self.assertEqual([], calls)

    def test_completed_task_asks_before_starting_follow_on_request(self) -> None:
        self.subject.state = WorkflowState.CONFIRMATION_PENDING
        self.subject.authority_mode = "patient_name_confirmation"
        self.subject.mutation_verified()

        directive = self.subject.accept_patient_turn(
            "Now I need to book a new appointment",
            authorize_name_confirmation=lambda confirmed: confirmed,
        )

        self.assertEqual(WorkflowState.AWAITING_FOLLOW_UP_DECISION, self.subject.state)
        self.assertIn("Would you like any other", directive.reply or "")
        self.assertFalse(self.subject.tools_allowed)
        self.assertEqual(
            "follow_up_offer_presented", self.subject.transitions[-1].reason
        )

    def test_follow_up_decline_is_terminal_and_does_not_unlock_tools(self) -> None:
        self.subject.state = WorkflowState.AWAITING_FOLLOW_UP_DECISION

        directive = self.subject.accept_patient_turn(
            "No thanks, goodbye",
            authorize_name_confirmation=lambda confirmed: confirmed,
            follow_up_decision=FollowUpDecision.DECLINED,
            require_semantic_follow_up=True,
        )

        self.assertEqual(WorkflowState.COMPLETED, self.subject.state)
        self.assertFalse(self.subject.tools_allowed)
        self.assertIn("No further scheduling action", directive.reply or "")

    def test_explicit_new_request_unlocks_next_task_without_reverification(self) -> None:
        self.subject.state = WorkflowState.AWAITING_FOLLOW_UP_DECISION
        self.subject.authority_mode = "patient_name_confirmation"

        directive = self.subject.accept_patient_turn(
            "Please book a new appointment next Friday",
            authorize_name_confirmation=lambda confirmed: confirmed,
            follow_up_decision=FollowUpDecision.NEW_REQUEST,
            require_semantic_follow_up=True,
        )

        self.assertEqual(WorkflowState.ACTIVE, self.subject.state)
        self.assertTrue(self.subject.tools_allowed)
        self.assertIn("another supported appointment", directive.model_input or "")

    def test_trailing_clinic_question_overrides_earlier_decline(self) -> None:
        self.subject.state = WorkflowState.AWAITING_FOLLOW_UP_DECISION
        self.subject.authority_mode = "patient_name_confirmation"
        transcript = (
            "No, that's all. Do I have prerequisites for this appointment? "
            "Is parking available at the clinic?"
        )

        directive = self.subject.accept_patient_turn(
            transcript,
            authorize_name_confirmation=lambda confirmed: confirmed,
            follow_up_decision=FollowUpDecision.NEW_REQUEST,
            require_semantic_follow_up=True,
        )

        self.assertEqual(WorkflowState.ACTIVE, self.subject.state)
        self.assertTrue(self.subject.tools_allowed)
        self.assertIn(transcript, directive.model_input or "")

    def test_explicit_request_reopens_completed_call(self) -> None:
        self.subject.state = WorkflowState.COMPLETED

        directive = self.subject.accept_patient_turn(
            "Before I go, is parking available at the clinic?",
            authorize_name_confirmation=lambda confirmed: confirmed,
            follow_up_decision=FollowUpDecision.NEW_REQUEST,
            require_semantic_follow_up=True,
        )

        self.assertEqual(WorkflowState.ACTIVE, self.subject.state)
        self.assertTrue(self.subject.tools_allowed)
        self.assertEqual(
            "follow_up_reopened_after_close",
            self.subject.transitions[-1].reason,
        )
        self.assertIn("parking", (directive.model_input or "").casefold())

    def test_unclear_follow_up_answer_reprompts_without_unlocking(self) -> None:
        self.subject.state = WorkflowState.AWAITING_FOLLOW_UP_DECISION

        directive = self.subject.accept_patient_turn(
            "Maybe later",
            authorize_name_confirmation=lambda confirmed: confirmed,
            follow_up_decision=FollowUpDecision.UNCLEAR,
            require_semantic_follow_up=True,
        )

        self.assertEqual(WorkflowState.AWAITING_FOLLOW_UP_DECISION, self.subject.state)
        self.assertFalse(self.subject.tools_allowed)
        self.assertIn("Please say yes or no", directive.reply or "")


class StateBoundToolTests(unittest.TestCase):
    def test_scheduler_tools_are_locked_before_name_confirmation(self) -> None:
        store, clock = load_fixture()
        harness = AppointmentHarness(store, clock)
        state = ConversationStateMachine(patient_display_name="Asha Rao")
        runtime = GuardedToolRuntime(
            harness,
            session_id="locked-session",
            patient_id="patient-001",
            state_machine=state,
        )

        result = runtime.execute("list_appointments", {})

        self.assertEqual("rejected", result["status"])
        self.assertEqual("tools_locked_by_workflow_state", result["reason"])
        self.assertEqual({}, harness.store.authorized_sessions)

    def test_name_confirmation_authorizes_harness_and_unlocks_reads(self) -> None:
        store, clock = load_fixture()
        harness = AppointmentHarness(store, clock)
        state = ConversationStateMachine(patient_display_name="Asha Rao")
        runtime = GuardedToolRuntime(
            harness,
            session_id="active-session",
            patient_id="patient-001",
            state_machine=state,
        )
        authorize = lambda confirmed: harness.confirm_identity_by_name(
            session_id="active-session",
            patient_id="patient-001",
            confirmed=confirmed,
        )
        state.accept_patient_turn(
            "Show my appointments", authorize_name_confirmation=authorize
        )
        state.accept_patient_turn("yes", authorize_name_confirmation=authorize)

        result = runtime.execute("list_appointments", {})

        self.assertEqual("ok", result["status"])
        self.assertEqual(WorkflowState.ACTIVE, state.state)


if __name__ == "__main__":
    unittest.main()
