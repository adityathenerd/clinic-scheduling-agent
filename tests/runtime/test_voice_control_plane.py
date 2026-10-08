from __future__ import annotations

import asyncio
import threading
import time
import unittest

from appointment_harness.fixtures import load_fixture
from appointment_harness.models import (
    AppointmentStatus,
    AppointmentConfirmationCall,
    ConfirmationCallStatus,
)
from appointment_harness.service import AppointmentHarness
from clinic_agent.agent.prompts import PromptContext
from clinic_agent.call_mechanics.models import (
    CallDescriptor,
    CallDirection,
    OperationKind,
    ToolIntent,
    ToolStatus,
)
from clinic_agent.call_mechanics import InMemoryEventSink
from clinic_agent.control_plane.state_machine import ConversationStateMachine, WorkflowState
from clinic_agent.control_plane.state_machine import (
    ConfirmationDecision,
    FollowUpDecision,
    IdentityDecision,
)
from clinic_agent.runtime.voice_control_plane import (
    VoiceAgentControlPlane,
    tool_intent_from_live_call,
)


def context() -> PromptContext:
    return PromptContext(
        assistant_name="Mira",
        clinic_name="Aster Demo Clinic",
        approved_urgent_message="Please contact emergency services now.",
        constitution_version="test-v1",
        constitution_hash="abc123",
        clinic_policy_version="test-policy",
    )


class FakeIdentityClassifier:
    def __init__(
        self,
        decision: IdentityDecision,
        confirmation_decision: ConfirmationDecision = ConfirmationDecision.CONFIRMED,
        follow_up_decision: FollowUpDecision = FollowUpDecision.UNCLEAR,
    ) -> None:
        self.decision = decision
        self.confirmation_decision = confirmation_decision
        self.follow_up_decision = follow_up_decision
        self.calls: list[tuple[str, str]] = []
        self.confirmation_calls: list[tuple[str, str]] = []

    async def classify(
        self, *, transcript: str, expected_patient_name: str
    ) -> IdentityDecision:
        self.calls.append((transcript, expected_patient_name))
        return self.decision

    async def classify_confirmation(
        self, *, transcript: str, exact_proposal: str
    ) -> ConfirmationDecision:
        self.confirmation_calls.append((transcript, exact_proposal))
        return self.confirmation_decision

    async def classify_follow_up(
        self, *, transcript: str, offered_help: str
    ) -> FollowUpDecision:
        self.calls.append((transcript, offered_help))
        return self.follow_up_decision


class VoiceControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        store, clock = load_fixture()
        self.harness = AppointmentHarness(store, clock)
        self.state = ConversationStateMachine(patient_display_name="Asha Rao")
        self.events = InMemoryEventSink()
        self.subject = VoiceAgentControlPlane(
            self.harness,
            session_id="voice-session",
            patient_id="patient-001",
            prompt_context=context(),
            state_machine=self.state,
            event_sink=self.events,
        )

    async def test_bootstrap_exposes_tools_but_keeps_application_guard_authoritative(self) -> None:
        config = await self.subject.bootstrap(
            CallDescriptor("CA123", CallDirection.INBOUND)
        )
        self.assertGreater(len(config.tools), 0)
        exposed_tool_names = {str(tool["name"]) for tool in config.tools}
        self.assertNotIn("interpret_identity_response", exposed_tool_names)
        self.assertIn("list_appointments", exposed_tool_names)
        self.assertIn("application_directive", config.instructions)
        self.assertNotIn("current_state:", config.instructions)
        self.assertIn("intentionally not copied", config.instructions)
        self.assertEqual(WorkflowState.AWAITING_INTENT, self.state.state)

    async def test_outbound_bootstrap_uses_direction_and_natural_identity_once(self) -> None:
        config = await self.subject.bootstrap(
            CallDescriptor(
                "CA-outbound",
                CallDirection.OUTBOUND,
                destination="synthetic-recipient",
            )
        )

        self.assertIn("I'm calling about a scheduling matter", config.initial_commentary or "")
        self.assertNotIn("How can I help", config.initial_commentary or "")
        self.assertEqual(WorkflowState.AWAITING_NAME_CONFIRMATION, self.state.state)
        self.assertEqual(
            "Thank you. I'm checking your identity confirmation now.",
            self.subject.application_gate_progress_message,
        )

        await self.subject.observe_patient_transcript_delta(
            "Yes, this is Asha Rao speaking"
        )
        interpreted = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-identity",
                "interpret_identity_response",
                OperationKind.READ,
                {"decision": "affirmed"},
            )
        )
        result = await self.subject.handle_tool_intent(
            ToolIntent("op-list", "list_appointments", OperationKind.READ)
        )

        self.assertEqual(ToolStatus.SUCCEEDED, interpreted.status)
        self.assertTrue(interpreted.payload["result"]["immutable"])
        self.assertEqual(ToolStatus.SUCCEEDED, result.status)
        self.assertEqual(WorkflowState.ACTIVE, self.state.state)
        self.assertIsNone(self.subject.application_gate_progress_message)
        accepted = [
            transition
            for transition in self.state.transitions
            if transition.reason == "name_confirmation_accepted"
        ]
        self.assertEqual(1, len(accepted))

    async def test_confirmation_outbound_bootstrap_sets_post_identity_notification_intent(self) -> None:
        now = self.harness.clock.now()
        self.harness.store.appointments["appointment-existing"].status = (
            AppointmentStatus.CONFIRMED
        )
        self.harness.store.confirmation_calls["confirmation-call-0001"] = (
            AppointmentConfirmationCall(
                notification_id="confirmation-call-0001",
                appointment_id="appointment-existing",
                patient_id="patient-001",
                status=ConfirmationCallStatus.PLACED,
                idempotency_key="appointment-confirmed:appointment-existing",
                created_at=now,
                updated_at=now,
            )
        )

        config = await self.subject.bootstrap(
            CallDescriptor(
                "CA-notification",
                CallDirection.OUTBOUND,
                destination="synthetic-recipient",
                context_reference="confirmation-call-0001",
            )
        )

        self.assertIn("scheduling matter", config.initial_commentary or "")
        self.assertNotIn("appointment-existing", config.initial_commentary or "")
        self.assertIn("clinic-approval booking confirmation", self.state.pending_intent or "")
        self.assertIn("appointment-existing", self.state.pending_intent or "")
        self.assertIn("clinic approval is complete", self.state.pending_intent or "")
        self.assertIn("never say that internal ID aloud", self.state.pending_intent or "")

        self.subject.identity_classifier = FakeIdentityClassifier(
            IdentityDecision.AFFIRMED
        )
        await self.subject.observe_patient_transcript_delta("Yes, this is Asha Rao.")
        directive = await self.subject.complete_patient_turn()

        self.assertIsNotNone(directive)
        self.assertTrue((directive or "").startswith("say_exactly:"))
        self.assertIn("Dr. N. Mehta", directive or "")
        self.assertIn("October 8, 2026 at 9:00 AM", directive or "")
        self.assertIn("is confirmed for", directive or "")
        self.assertIn("9:00 AM IST", directive or "")
        self.assertIn("2care Clinic, Koramangala, Bengaluru", directive or "")
        self.assertNotIn("UTC", directive or "")
        self.assertNotIn("Maya Chen", directive or "")
        self.assertEqual(WorkflowState.AWAITING_FOLLOW_UP_DECISION, self.state.state)
        self.assertIn(
            "notification.context_resolved",
            [event["event_type"] for event in self.events.events],
        )

    async def test_completed_identity_turn_is_semantically_applied_without_delegation(self) -> None:
        classifier = FakeIdentityClassifier(IdentityDecision.AFFIRMED)
        self.subject.identity_classifier = classifier
        await self.subject.bootstrap(
            CallDescriptor(
                "CA-app-owned-turn",
                CallDirection.OUTBOUND,
                destination="synthetic-recipient",
            )
        )
        await self.subject.observe_patient_transcript_delta(
            "Yep, you have reached the person you were trying to reach."
        )

        directive = await self.subject.complete_patient_turn()

        self.assertEqual(WorkflowState.ACTIVE, self.state.state)
        self.assertIsNotNone(self.state.identity_confirmation_key)
        self.assertEqual(
            [
                (
                    "Yep, you have reached the person you were trying to reach.",
                    "Asha Rao",
                )
            ],
            classifier.calls,
        )
        self.assertIn("confirmed", (directive or "").casefold())
        self.assertTrue((directive or "").startswith("say_exactly:"))
        self.assertIn("scheduling callback", directive or "")
        event_types = [event["event_type"] for event in self.events.events]
        self.assertIn("identity.completed_turn_classified", event_types)
        self.assertIn("identity.continuation_prepared", event_types)
        processed = [
            event
            for event in self.events.events
            if event["event_type"] == "patient.turn_processed"
        ][-1]
        self.assertEqual("say_exactly", processed["directive_kind"])
        self.assertEqual(64, len(processed["directive_digest"]))

    async def test_inbound_intent_turn_is_application_owned_and_asks_identity(self) -> None:
        await self.subject.bootstrap(CallDescriptor("CA-inbound", CallDirection.INBOUND))
        await self.subject.observe_patient_transcript_delta(
            "I need to reschedule my appointment"
        )

        directive = await self.subject.complete_patient_turn()

        self.assertEqual(WorkflowState.AWAITING_NAME_CONFIRMATION, self.state.state)
        self.assertTrue((directive or "").startswith("say_exactly:"))
        self.assertIn("am I speaking with Asha Rao", directive or "")

    async def test_late_identity_tool_replays_immutable_application_decision(self) -> None:
        self.subject.identity_classifier = FakeIdentityClassifier(
            IdentityDecision.AFFIRMED
        )
        await self.subject.bootstrap(
            CallDescriptor(
                "CA-app-owned-replay",
                CallDirection.OUTBOUND,
                destination="synthetic-recipient",
            )
        )
        await self.subject.observe_patient_transcript_delta("Yes, speaking")
        await self.subject.complete_patient_turn()

        replay = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-late-identity",
                "interpret_identity_response",
                OperationKind.READ,
                {"decision": "denied"},
            )
        )

        self.assertEqual(ToolStatus.SUCCEEDED, replay.status)
        self.assertTrue(replay.payload["result"]["replayed"])
        self.assertTrue(replay.payload["result"]["immutable"])
        self.assertEqual(WorkflowState.ACTIVE, self.state.state)

    async def test_partial_transcript_is_not_applied_until_delegation_requests_work(self) -> None:
        await self.subject.observe_patient_transcript_delta("I need to ")
        await self.subject.observe_patient_transcript_delta("book an appointment")
        self.assertEqual(WorkflowState.AWAITING_INTENT, self.state.state)

        result = await self.subject.handle_tool_intent(
            ToolIntent("op-1", "check_eligibility", OperationKind.READ, {"appointment_type": "dermatology-followup"})
        )

        self.assertEqual(ToolStatus.FAILED, result.status)
        self.assertEqual(WorkflowState.AWAITING_NAME_CONFIRMATION, self.state.state)
        self.assertIn("Asha Rao", result.payload["application_directive"])
        self.assertEqual("tools_locked_by_workflow_state", result.payload["result"]["reason"])

    async def test_confirmation_turn_unlocks_identity_before_tool_execution(self) -> None:
        await self.subject.observe_patient_transcript_delta("Book an appointment")
        await self.subject.handle_tool_intent(
            ToolIntent("op-1", "check_eligibility", OperationKind.READ, {"appointment_type": "dermatology-followup"})
        )
        await self.subject.observe_patient_transcript_delta("Yes, this is Asha Rao")

        identity = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-identity",
                "interpret_identity_response",
                OperationKind.READ,
                {"decision": "affirmed"},
            )
        )

        result = await self.subject.handle_tool_intent(
            ToolIntent("op-2", "check_eligibility", OperationKind.READ, {"appointment_type": "dermatology-followup"})
        )

        self.assertEqual(ToolStatus.SUCCEEDED, identity.status)
        self.assertEqual(ToolStatus.SUCCEEDED, result.status)
        self.assertEqual("ok", result.payload["result"]["status"])
        self.assertEqual(WorkflowState.ACTIVE, self.state.state)

    async def test_urgent_turn_never_unlocks_tools(self) -> None:
        directive = await self.subject.observe_patient_transcript_delta(
            "I have chest pain"
        )
        result = await self.subject.handle_tool_intent(
            ToolIntent("op-1", "list_appointments", OperationKind.READ)
        )
        self.assertEqual(ToolStatus.FAILED, result.status)
        self.assertEqual(WorkflowState.URGENT_HANDOFF, self.state.state)
        self.assertIn("emergency services", directive or "")
        transitions = [
            event
            for event in self.events.events
            if event["event_type"] == "workflow.transition"
        ]
        self.assertEqual("urgent_handoff", transitions[-1]["current"])

    async def test_human_request_preempts_without_waiting_for_tool_call(self) -> None:
        directive = await self.subject.observe_patient_transcript_delta(
            "I want the front desk"
        )
        self.assertIn("front-desk request was accepted", directive or "")
        self.assertEqual(WorkflowState.HUMAN_HANDOFF_COMPLETED, self.state.state)
        self.assertIsNotNone(self.subject.last_observation)
        self.assertIsNotNone(self.subject.last_observation.handoff_reference)

    async def test_reconcile_does_not_retry_write(self) -> None:
        result = await self.subject.reconcile("op-write")
        self.assertEqual(ToolStatus.UNKNOWN, result.status)
        self.assertEqual("reconciliation_required", result.payload["status"])

    async def test_completed_follow_up_decline_is_semantically_classified_and_locked(self) -> None:
        classifier = FakeIdentityClassifier(
            IdentityDecision.AFFIRMED,
            follow_up_decision=FollowUpDecision.DECLINED,
        )
        self.subject.follow_up_classifier = classifier
        self.state.state = WorkflowState.AWAITING_FOLLOW_UP_DECISION

        await self.subject.observe_patient_transcript_delta("No thanks, goodbye")
        directive = await self.subject.complete_patient_turn()

        self.assertEqual(WorkflowState.COMPLETED, self.state.state)
        self.assertFalse(self.state.tools_allowed)
        self.assertEqual("say_exactly: Thank you. Take care.", directive)
        processed = [
            event
            for event in self.events.events
            if event["event_type"] == "patient.turn_processed"
        ][-1]
        self.assertEqual("say_exactly", processed["directive_kind"])
        self.assertTrue(
            any(
                event["event_type"] == "follow_up.completed_turn_classified"
                for event in self.events.events
            )
        )

    async def test_completed_follow_up_new_request_unlocks_only_after_semantic_decision(self) -> None:
        classifier = FakeIdentityClassifier(
            IdentityDecision.AFFIRMED,
            follow_up_decision=FollowUpDecision.NEW_REQUEST,
        )
        self.subject.follow_up_classifier = classifier
        self.state.state = WorkflowState.AWAITING_FOLLOW_UP_DECISION

        await self.subject.observe_patient_transcript_delta(
            "Please book a new appointment next Friday"
        )
        directive = await self.subject.complete_patient_turn()

        self.assertEqual(WorkflowState.ACTIVE, self.state.state)
        self.assertTrue(self.state.tools_allowed)
        self.assertIn("another supported appointment", directive or "")

    async def test_follow_up_clinic_question_is_not_discarded_after_decline_words(self) -> None:
        classifier = FakeIdentityClassifier(
            IdentityDecision.AFFIRMED,
            follow_up_decision=FollowUpDecision.NEW_REQUEST,
        )
        self.subject.follow_up_classifier = classifier
        self.state.state = WorkflowState.AWAITING_FOLLOW_UP_DECISION
        transcript = (
            "No, that's all. Do I have prerequisites for this appointment? "
            "Is parking available at the clinic?"
        )

        await self.subject.observe_patient_transcript_delta(transcript)
        directive = await self.subject.complete_patient_turn()

        self.assertEqual(WorkflowState.ACTIVE, self.state.state)
        self.assertTrue(self.state.tools_allowed)
        self.assertIn(transcript, directive or "")

        faq = await self.subject.handle_tool_intent(
            ToolIntent(
                "faq-after-reschedule",
                "search_clinic_faqs",
                OperationKind.READ,
                {"query": "parking at the clinic"},
            )
        )
        self.assertEqual(ToolStatus.SUCCEEDED, faq.status)
        self.assertTrue(faq.payload["result"]["matches"])
        self.assertIn(
            "parking",
            str(faq.payload["result"]["patient_facing_summary"]).casefold(),
        )

    async def test_completed_call_can_reopen_for_explicit_faq(self) -> None:
        classifier = FakeIdentityClassifier(
            IdentityDecision.AFFIRMED,
            follow_up_decision=FollowUpDecision.NEW_REQUEST,
        )
        self.subject.follow_up_classifier = classifier
        self.state.state = WorkflowState.COMPLETED

        await self.subject.observe_patient_transcript_delta(
            "Before I go, is there parking at the clinic?"
        )

        self.assertTrue(self.subject.application_owns_completed_turn)
        directive = await self.subject.complete_patient_turn()

        self.assertEqual(WorkflowState.ACTIVE, self.state.state)
        self.assertTrue(self.state.tools_allowed)
        self.assertIn("parking", (directive or "").casefold())

    async def test_active_information_call_closes_semantically_without_requiring_a_change(self) -> None:
        classifier = FakeIdentityClassifier(
            IdentityDecision.AFFIRMED,
            follow_up_decision=FollowUpDecision.DECLINED,
        )
        self.subject.follow_up_classifier = classifier
        self.state.state = WorkflowState.ACTIVE

        await self.subject.observe_patient_transcript_delta(
            "That's all. Thank you so much"
        )

        self.assertTrue(self.subject.application_owns_completed_turn)
        directive = await self.subject.complete_patient_turn()

        self.assertEqual(WorkflowState.COMPLETED, self.state.state)
        self.assertEqual(
            "say_exactly: Thank you for calling. Take care.", directive
        )
        self.assertTrue(
            any(
                event["event_type"] == "workflow.transition"
                and event.get("reason") == "patient_ended_call"
                for event in self.events.events
            )
        )
        classified = [
            event
            for event in self.events.events
            if event["event_type"] == "call_closure.completed_turn_classified"
        ][-1]
        self.assertEqual("declined", classified["decision"])
        self.assertTrue(classified["terminal"])

    async def test_active_closure_candidate_with_new_request_does_not_close(self) -> None:
        classifier = FakeIdentityClassifier(
            IdentityDecision.AFFIRMED,
            follow_up_decision=FollowUpDecision.NEW_REQUEST,
        )
        self.subject.follow_up_classifier = classifier
        self.state.state = WorkflowState.ACTIVE

        await self.subject.observe_patient_transcript_delta(
            "That's all for that appointment; please reschedule my other one"
        )
        directive = await self.subject.complete_patient_turn()

        self.assertEqual(WorkflowState.ACTIVE, self.state.state)
        self.assertIn("did not clearly end the call", directive or "")

    async def test_active_non_closing_turn_stays_observational(self) -> None:
        self.state.state = WorkflowState.ACTIVE

        await self.subject.observe_patient_transcript_delta(
            "What time is my appointment?"
        )

        self.assertFalse(self.subject.application_owns_completed_turn)
        self.assertIsNone(await self.subject.complete_patient_turn())
        self.assertEqual(WorkflowState.ACTIVE, self.state.state)

    async def test_parallel_provider_requests_are_serialized_at_control_plane(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()

        def execute(name: str, arguments: object) -> dict[str, str]:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return {"status": "ok"}

        self.subject.runtime.execute = execute  # type: ignore[method-assign]
        await asyncio.gather(
            self.subject.handle_tool_intent(
                ToolIntent("op-1", "list_appointments", OperationKind.READ)
            ),
            self.subject.handle_tool_intent(
                ToolIntent("op-2", "list_appointments", OperationKind.READ)
            ),
        )
        self.assertEqual(1, peak)

    async def test_voice_booking_happy_path_reaches_verified_completion(self) -> None:
        self.subject.confirmation_classifier = FakeIdentityClassifier(
            IdentityDecision.AFFIRMED
        )
        await self.subject.observe_patient_transcript_delta(
            "I want to book a dermatology follow-up on Friday afternoon"
        )
        locked = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-identity",
                "check_eligibility",
                OperationKind.READ,
                {"appointment_type": "dermatology-followup"},
            )
        )
        self.assertEqual(ToolStatus.FAILED, locked.status)
        self.assertIn("Asha Rao", locked.payload["application_directive"])

        await self.subject.observe_patient_transcript_delta(
            "Yes, this is Asha Rao"
        )
        identity = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-identity-confirm",
                "interpret_identity_response",
                OperationKind.READ,
                {"decision": "affirmed"},
            )
        )
        self.assertEqual(ToolStatus.SUCCEEDED, identity.status)
        eligibility = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-eligibility",
                "check_eligibility",
                OperationKind.READ,
                {"appointment_type": "dermatology-followup"},
            )
        )
        self.assertEqual("ok", eligibility.payload["result"]["status"])

        availability = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-slots",
                "search_slots",
                OperationKind.READ,
                {
                    "appointment_type": "dermatology-followup",
                    "date_from": "2026-10-09",
                    "date_to": "2026-10-09",
                },
            )
        )
        self.assertEqual("ok", availability.payload["result"]["status"])

        proposal = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-proposal",
                "create_appointment",
                OperationKind.WRITE,
                {"slot_id": "slot-1630"},
            )
        )
        self.assertEqual(
            "confirmation_required", proposal.payload["result"]["status"]
        )
        self.assertEqual(WorkflowState.CONFIRMATION_PENDING, self.state.state)

        await self.subject.observe_patient_transcript_delta("Yes, confirm")
        committed = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-commit",
                "create_appointment",
                OperationKind.WRITE,
                {"slot_id": "slot-1630"},
            )
        )
        self.assertEqual("proposed", committed.payload["result"]["status"])
        self.assertEqual(WorkflowState.AWAITING_FOLLOW_UP_DECISION, self.state.state)
        event_types = [event["event_type"] for event in self.events.events]
        self.assertIn("tool.decision", event_types)
        self.assertIn("workflow.transition", event_types)

    async def test_observed_reschedule_confirmation_commits_once_without_loop(self) -> None:
        classifier = FakeIdentityClassifier(IdentityDecision.AFFIRMED)
        self.subject.identity_classifier = classifier
        self.subject.confirmation_classifier = classifier
        await self.subject.bootstrap(
            CallDescriptor(
                "CA-reschedule-confirmation",
                CallDirection.OUTBOUND,
                destination="synthetic-recipient",
            )
        )
        await self.subject.observe_patient_transcript_delta("Yes, this is Asha Rao")
        await self.subject.complete_patient_turn()
        await self.subject.handle_tool_intent(
            ToolIntent(
                "op-slots-reschedule",
                "search_slots",
                OperationKind.READ,
                {
                    "appointment_type": "dermatology-followup",
                    "date_from": "2026-10-12",
                    "date_to": "2026-10-12",
                    "provider_id": "provider-mehta",
                    "location_id": "downtown",
                },
            )
        )
        arguments = {
            "appointment_id": "appointment-existing",
            "replacement_slot_id": "slot-mehta-20261012-1130",
        }
        proposal = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-reschedule-proposal",
                "edit_appointment",
                OperationKind.WRITE,
                arguments,
                state_revision=5,
            )
        )
        self.assertEqual("confirmation_required", proposal.payload["result"]["status"])
        self.assertIn(
            "confirm this rescheduling request",
            proposal.payload["application_directive"],
        )
        self.assertIn("IST", proposal.payload["application_directive"])
        self.assertIn(
            "2care Clinic, Koramangala, Bengaluru",
            proposal.payload["application_directive"],
        )
        await self.subject.arm_pending_confirmation(after_turn_epoch=5)

        observed = "And yes, confirm. Yes, I am confirming that proposal."
        await self.subject.observe_patient_transcript_delta(observed)
        directive = await self.subject.complete_patient_turn(turn_epoch=6)
        version_after_commit = self.harness.store.appointments[
            "appointment-existing"
        ].version
        delayed_duplicate = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-delayed-duplicate",
                "edit_appointment",
                OperationKind.WRITE,
                arguments,
            )
        )

        self.assertTrue((directive or "").startswith("say_exactly:"))
        self.assertIn("is held for clinic review", directive or "")
        self.assertIn("remains in place until approval", directive or "")
        self.assertEqual(ToolStatus.SUCCEEDED, delayed_duplicate.status)
        self.assertTrue(delayed_duplicate.payload["replayed"])
        self.assertEqual(WorkflowState.AWAITING_FOLLOW_UP_DECISION, self.state.state)
        self.assertEqual(
            version_after_commit,
            self.harness.store.appointments["appointment-existing"].version,
        )
        self.assertEqual(
            "slot-existing",
            self.harness.store.appointments["appointment-existing"].slot_id,
        )
        self.assertEqual(
            "slot-mehta-20261012-1130",
            self.harness.store.appointments[
                "appointment-existing"
            ].pending_replacement_slot_id,
        )
        self.assertEqual(1, len(classifier.confirmation_calls))
        event_types = [event["event_type"] for event in self.events.events]
        self.assertIn("confirmation.mutation_executed", event_types)

    async def test_trailing_fragment_from_proposal_turn_cannot_become_confirmation(self) -> None:
        self.subject.identity_classifier = FakeIdentityClassifier(
            IdentityDecision.AFFIRMED
        )
        await self.subject.bootstrap(
            CallDescriptor(
                "CA-proposal-turn-race",
                CallDirection.OUTBOUND,
                destination="synthetic-recipient",
            )
        )
        await self.subject.observe_patient_transcript_delta("Yes, this is Asha Rao")
        await self.subject.complete_patient_turn(turn_epoch=1)
        await self.subject.handle_tool_intent(
            ToolIntent(
                "op-slots-race",
                "search_slots",
                OperationKind.READ,
                {
                    "appointment_type": "dermatology-followup",
                    "date_from": "2026-10-12",
                    "date_to": "2026-10-12",
                    "provider_id": "provider-mehta",
                    "location_id": "downtown",
                },
            )
        )
        await self.subject.observe_patient_transcript_delta(
            "Move it to Wednesday at 11 AM"
        )
        proposal = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-reschedule-race",
                "edit_appointment",
                OperationKind.WRITE,
                {
                    "appointment_id": "appointment-existing",
                    "replacement_slot_id": "slot-mehta-20261012-1130",
                },
                state_revision=7,
            )
        )
        self.assertEqual("confirmation_required", proposal.payload["result"]["status"])
        await self.subject.arm_pending_confirmation(after_turn_epoch=7)

        await self.subject.observe_patient_transcript_delta(" slot")
        directive = await self.subject.complete_patient_turn(turn_epoch=7)

        self.assertEqual(WorkflowState.ACTIVE, self.state.state)
        self.assertIsNone(self.subject.runtime.pending)
        self.assertIn("Move it to Wednesday at 11 AM slot", directive or "")
        self.assertFalse(
            any(
                event["event_type"] == "confirmation.semantic_interpreted"
                for event in self.events.events
            )
        )
        continuation = [
            event
            for event in self.events.events
            if event["event_type"] == "confirmation.pre_delivery_continuation"
        ][-1]
        self.assertEqual(7, continuation["origin_turn_epoch"])
        self.assertEqual(7, continuation["continuation_turn_epoch"])

    async def test_empty_reschedule_search_returns_spoken_recovery_directive(self) -> None:
        await self.subject.observe_patient_transcript_delta(
            "I need to reschedule my appointment"
        )
        await self.subject.handle_tool_intent(
            ToolIntent("op-locked", "list_appointments", OperationKind.READ)
        )
        await self.subject.observe_patient_transcript_delta(
            "Yes, this is Asha Rao speaking"
        )
        identity = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-identity",
                "interpret_identity_response",
                OperationKind.READ,
                {"decision": "affirmed"},
            )
        )
        self.assertEqual(ToolStatus.SUCCEEDED, identity.status)

        result = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-no-slots",
                "search_slots",
                OperationKind.READ,
                {
                    "appointment_type": "dermatology-followup",
                    "date_from": "2026-10-07",
                    "date_to": "2026-10-07",
                    "time_window": "after 2 PM",
                },
            )
        )

        self.assertEqual(ToolStatus.SUCCEEDED, result.status)
        self.assertEqual([], result.payload["result"]["slots"])
        self.assertIn("couldn't find", result.payload["application_directive"])
        decision = [
            event
            for event in self.events.events
            if event["event_type"] == "tool.decision"
        ][-1]
        self.assertEqual(0, decision["result_count"])

    async def test_live_identity_requires_semantic_tool_not_phrase_allowlist(self) -> None:
        await self.subject.bootstrap(
            CallDescriptor(
                "CA-semantic",
                CallDirection.OUTBOUND,
                destination="synthetic-recipient",
            )
        )
        await self.subject.observe_patient_transcript_delta(
            "Yep, you have reached the person you were trying to reach."
        )

        still_locked = await self.subject.handle_tool_intent(
            ToolIntent("op-premature", "list_appointments", OperationKind.READ)
        )

        self.assertEqual(ToolStatus.FAILED, still_locked.status)
        self.assertEqual(WorkflowState.AWAITING_NAME_CONFIRMATION, self.state.state)
        self.assertIsNone(self.state.identity_confirmation_key)

    async def test_public_faq_can_answer_before_identity_without_unlocking_record(self) -> None:
        await self.subject.bootstrap(
            CallDescriptor(
                "CA-public-faq",
                CallDirection.OUTBOUND,
                destination="synthetic-recipient",
            )
        )
        await self.subject.observe_patient_transcript_delta(
            "Before that, where can I park at the clinic?"
        )

        result = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-faq",
                "search_clinic_faqs",
                OperationKind.READ,
                {"query": "Where can I park?", "category": "location"},
            )
        )

        self.assertEqual(ToolStatus.SUCCEEDED, result.status)
        self.assertEqual(
            "parking-and-transport",
            result.payload["result"]["matches"][0]["faq_id"],
        )
        self.assertIn("Asha Rao", result.payload["application_directive"])
        self.assertEqual(WorkflowState.AWAITING_NAME_CONFIRMATION, self.state.state)
        self.assertEqual({}, self.harness.store.authorized_sessions)

    async def test_semantic_identity_lock_is_application_minted_and_immutable(self) -> None:
        await self.subject.bootstrap(
            CallDescriptor(
                "CA-lock",
                CallDirection.OUTBOUND,
                destination="synthetic-recipient",
            )
        )
        await self.subject.observe_patient_transcript_delta(
            "Yep, you have reached the person you were trying to reach."
        )
        first = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-identity-first",
                "interpret_identity_response",
                OperationKind.READ,
                {"decision": "affirmed"},
            )
        )
        key = first.payload["result"]["identity_confirmation_key"]

        await self.subject.observe_patient_transcript_delta("No, change that answer")
        second = await self.subject.handle_tool_intent(
            ToolIntent(
                "op-identity-second",
                "interpret_identity_response",
                OperationKind.READ,
                {"decision": "denied"},
            )
        )

        self.assertTrue(str(key).startswith("identity_"))
        self.assertEqual(ToolStatus.FAILED, second.status)
        self.assertEqual(
            "identity_confirmation_is_immutable_or_not_requested",
            second.payload["result"]["reason"],
        )
        self.assertEqual(key, self.state.identity_confirmation_key)
        self.assertEqual(WorkflowState.ACTIVE, self.state.state)


class ToolIntentFactoryTests(unittest.TestCase):
    def test_maps_write_tool_and_validates_json_object(self) -> None:
        intent = tool_intent_from_live_call(
            operation_id="call-1",
            tool_name="create_appointment",
            arguments_json='{"slot_id":"slot-1"}',
            delegation_id="delegation-1",
        )
        self.assertEqual(OperationKind.WRITE, intent.operation_kind)
        self.assertEqual("slot-1", intent.arguments["slot_id"])

    def test_rejects_unknown_tool(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown tool"):
            tool_intent_from_live_call(
                operation_id="call-1",
                tool_name="delete_database",
                arguments_json="{}",
                delegation_id=None,
            )


if __name__ == "__main__":
    unittest.main()
