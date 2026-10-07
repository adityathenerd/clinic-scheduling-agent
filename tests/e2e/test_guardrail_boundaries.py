from __future__ import annotations

import asyncio
import unittest

from appointment_harness.faults import Fault, FaultInjector, FaultKind, FaultPoint
from appointment_harness.fixtures import load_fixture
from appointment_harness.models import AppointmentStatus, SlotStatus
from appointment_harness.service import AppointmentHarness
from clinic_agent.agent.prompts import PromptContext
from clinic_agent.call_mechanics import InMemoryEventSink
from clinic_agent.call_mechanics.models import (
    CallDescriptor,
    CallDirection,
    OperationKind,
    ToolIntent,
    ToolResult,
    ToolStatus,
)
from clinic_agent.control_plane.state_machine import (
    ConfirmationDecision,
    ConversationStateMachine,
    IdentityDecision,
    WorkflowState,
)
from clinic_agent.runtime.voice_control_plane import VoiceAgentControlPlane


PATIENT_ID = "patient-001"
EXISTING_APPOINTMENT = "appointment-existing"
REPLACEMENT_SLOT = "slot-mehta-20261012-1130"
BOOKING_SLOT = "slot-1630"


def prompt_context() -> PromptContext:
    return PromptContext(
        assistant_name="Mira",
        clinic_name="Aster Demo Clinic",
        approved_urgent_message="Please contact emergency services now.",
        constitution_version="e2e-test-v1",
        constitution_hash="e2e-test-hash",
        clinic_policy_version="e2e-test-policy",
    )


class ScriptedSemanticClassifier:
    """Deterministic substitute for Responses semantic classification."""

    def __init__(
        self,
        *,
        identity: IdentityDecision = IdentityDecision.AFFIRMED,
        confirmations: tuple[ConfirmationDecision, ...] = (),
    ) -> None:
        self.identity = identity
        self.confirmations = list(confirmations)
        self.identity_calls: list[tuple[str, str]] = []
        self.confirmation_calls: list[tuple[str, str]] = []

    async def classify(
        self, *, transcript: str, expected_patient_name: str
    ) -> IdentityDecision:
        self.identity_calls.append((transcript, expected_patient_name))
        return self.identity

    async def classify_confirmation(
        self, *, transcript: str, exact_proposal: str
    ) -> ConfirmationDecision:
        self.confirmation_calls.append((transcript, exact_proposal))
        if not self.confirmations:
            raise AssertionError("unexpected confirmation classification")
        return self.confirmations.pop(0)


class GuardrailJourney:
    """Small black-box driver around the production control-plane boundary."""

    def __init__(
        self,
        harness: AppointmentHarness,
        *,
        session_id: str,
        confirmations: tuple[ConfirmationDecision, ...] = (),
    ) -> None:
        self.harness = harness
        self.session_id = session_id
        self.state = ConversationStateMachine(patient_display_name="Asha Rao")
        self.events = InMemoryEventSink()
        self.classifier = ScriptedSemanticClassifier(confirmations=confirmations)
        self.control = VoiceAgentControlPlane(
            harness,
            session_id=session_id,
            patient_id=PATIENT_ID,
            prompt_context=prompt_context(),
            state_machine=self.state,
            event_sink=self.events,
            identity_classifier=self.classifier,
            confirmation_classifier=self.classifier,
        )
        self._operation = 0
        self._turn_epoch = 0

    async def bootstrap_outbound(self) -> None:
        await self.control.bootstrap(
            CallDescriptor(
                f"call-{self.session_id}",
                CallDirection.OUTBOUND,
                destination="synthetic-recipient",
            )
        )

    async def identify(self, words: str = "Yes, this is Asha Rao speaking") -> None:
        self._turn_epoch += 1
        await self.control.observe_patient_transcript_delta(words)
        directive = await self.control.complete_patient_turn(
            turn_epoch=self._turn_epoch
        )
        if self.state.state is not WorkflowState.ACTIVE:
            raise AssertionError(f"identity did not unlock the workflow: {directive!r}")

    async def intent(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        kind: OperationKind | None = None,
    ) -> ToolResult:
        self._operation += 1
        if kind is None:
            kind = (
                OperationKind.WRITE
                if name in {
                    "create_appointment",
                    "edit_appointment",
                    "delete_appointment",
                }
                else OperationKind.READ
            )
        result = await self.control.handle_tool_intent(
            ToolIntent(
                f"{self.session_id}-op-{self._operation}",
                name,
                kind,
                arguments or {},
                state_revision=self._turn_epoch,
            )
        )
        domain_result = result.payload.get("result")
        if (
            isinstance(domain_result, dict)
            and domain_result.get("status") == "confirmation_required"
        ):
            await self.control.arm_pending_confirmation(
                after_turn_epoch=self._turn_epoch
            )
        return result

    async def search_replacement(self) -> ToolResult:
        return await self.intent(
            "search_slots",
            {
                "appointment_type": "dermatology-followup",
                "date_from": "2026-10-12",
                "date_to": "2026-10-12",
                "provider_id": "provider-mehta",
                "location_id": "downtown",
            },
        )

    async def propose_reschedule(self) -> tuple[dict[str, object], ToolResult]:
        await self.search_replacement()
        arguments: dict[str, object] = {
            "appointment_id": EXISTING_APPOINTMENT,
            "replacement_slot_id": REPLACEMENT_SLOT,
        }
        return arguments, await self.intent("edit_appointment", arguments)

    async def confirm(self, words: str = "Yes, confirm that exact proposal") -> str:
        self._turn_epoch += 1
        await self.control.observe_patient_transcript_delta(words)
        return (
            await self.control.complete_patient_turn(turn_epoch=self._turn_epoch)
            or ""
        )


def harness_with(*faults: Fault) -> AppointmentHarness:
    store, clock = load_fixture()
    return AppointmentHarness(store, clock, FaultInjector(tuple(faults)))


class GuardrailBoundaryE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_lock_blocks_record_disclosure_but_public_faq_stays_available(
        self,
    ) -> None:
        harness = harness_with()
        journey = GuardrailJourney(harness, session_id="identity-lock")
        await journey.bootstrap_outbound()

        protected = await journey.intent("list_appointments")
        faq = await journey.intent(
            "search_clinic_faqs",
            {"query": "Where can I park?", "category": "location"},
        )

        self.assertEqual(ToolStatus.FAILED, protected.status)
        self.assertEqual(
            "tools_locked_by_workflow_state",
            protected.payload["result"]["reason"],
        )
        self.assertNotIn("appointments", protected.payload["result"])
        self.assertEqual(ToolStatus.SUCCEEDED, faq.status)
        self.assertEqual(
            "parking-and-transport",
            faq.payload["result"]["matches"][0]["faq_id"],
        )
        self.assertEqual({}, harness.store.authorized_sessions)
        self.assertIsNone(journey.state.identity_confirmation_key)

    async def test_verified_happy_path_binds_confirmation_to_exact_proposal_once(
        self,
    ) -> None:
        harness = harness_with()
        journey = GuardrailJourney(
            harness,
            session_id="happy-reschedule",
            confirmations=(ConfirmationDecision.CONFIRMED,),
        )
        await journey.bootstrap_outbound()
        await journey.identify()
        arguments, proposal = await journey.propose_reschedule()

        before = harness.store.appointments[EXISTING_APPOINTMENT]
        self.assertEqual("confirmation_required", proposal.payload["result"]["status"])
        self.assertEqual("slot-existing", before.slot_id)
        directive = await journey.confirm()
        version_after_commit = harness.store.appointments[EXISTING_APPOINTMENT].version
        delayed_duplicate = await journey.intent("edit_appointment", arguments)

        self.assertTrue(directive.startswith("say_exactly:"))
        self.assertIn("is held for clinic review", directive)
        self.assertIn("remains in place until approval", directive)
        self.assertEqual(ToolStatus.SUCCEEDED, delayed_duplicate.status)
        self.assertTrue(delayed_duplicate.payload["replayed"])
        self.assertEqual(
            WorkflowState.AWAITING_FOLLOW_UP_DECISION, journey.state.state
        )
        self.assertEqual(
            version_after_commit,
            harness.store.appointments[EXISTING_APPOINTMENT].version,
        )
        self.assertEqual(
            "slot-existing",
            harness.store.appointments[EXISTING_APPOINTMENT].slot_id,
        )
        self.assertEqual(
            REPLACEMENT_SLOT,
            harness.store.appointments[
                EXISTING_APPOINTMENT
            ].pending_replacement_slot_id,
        )
        self.assertIs(SlotStatus.BOOKED, harness.store.slots["slot-existing"].status)
        self.assertIs(SlotStatus.HELD, harness.store.slots[REPLACEMENT_SLOT].status)
        updates = [
            event
            for event in harness.store.audit_log
            if event.event_type == "appointment.reschedule_proposed"
        ]
        confirmations = [
            event
            for event in harness.store.audit_log
            if event.event_type == "confirmation.recorded"
        ]
        self.assertEqual(1, len(updates))
        self.assertEqual(1, len(confirmations))

    async def test_denial_and_correction_invalidate_proposal_without_mutation(self) -> None:
        for decision in (
            ConfirmationDecision.DENIED,
            ConfirmationDecision.CORRECTION,
        ):
            with self.subTest(decision=decision.value):
                harness = harness_with()
                journey = GuardrailJourney(
                    harness,
                    session_id=f"reject-{decision.value}",
                    confirmations=(decision,),
                )
                await journey.bootstrap_outbound()
                await journey.identify()
                _, proposal = await journey.propose_reschedule()
                proposal_id = proposal.payload["result"]["proposal_id"]

                words = (
                    "No, do not make that change"
                    if decision is ConfirmationDecision.DENIED
                    else "Actually, make it Tuesday afternoon instead"
                )
                await journey.confirm(words)

                self.assertEqual(WorkflowState.ACTIVE, journey.state.state)
                self.assertIsNone(journey.control.runtime.pending)
                self.assertEqual(
                    "slot-existing",
                    harness.store.appointments[EXISTING_APPOINTMENT].slot_id,
                )
                self.assertNotIn(
                    (journey.session_id, proposal_id),
                    harness.store.confirmed_proposals,
                )

    async def test_unclear_confirmation_preserves_proposal_but_cannot_write(self) -> None:
        harness = harness_with()
        journey = GuardrailJourney(
            harness,
            session_id="unclear",
            confirmations=(
                ConfirmationDecision.UNCLEAR,
                ConfirmationDecision.UNCLEAR,
            ),
        )
        await journey.bootstrap_outbound()
        await journey.identify()
        arguments, proposal = await journey.propose_reschedule()
        proposal_id = proposal.payload["result"]["proposal_id"]

        directive = await journey.confirm("I suppose that might be okay")
        blocked = await journey.intent("edit_appointment", arguments)

        self.assertIn("say yes or no", directive)
        self.assertEqual(ToolStatus.FAILED, blocked.status)
        self.assertEqual(
            "exact_proposal_not_confirmed", blocked.payload["result"]["reason"]
        )
        self.assertEqual(WorkflowState.CONFIRMATION_PENDING, journey.state.state)
        self.assertEqual(proposal_id, journey.control.runtime.pending.proposal_id)
        self.assertEqual(
            "slot-existing", harness.store.appointments[EXISTING_APPOINTMENT].slot_id
        )

    async def test_confirmed_arguments_cannot_be_substituted_at_commit(self) -> None:
        harness = harness_with()
        journey = GuardrailJourney(
            harness,
            session_id="argument-binding",
            confirmations=(ConfirmationDecision.CONFIRMED,),
        )
        await journey.bootstrap_outbound()
        await journey.identify()
        await journey.propose_reschedule()
        await journey.confirm()

        substituted = await journey.intent(
            "edit_appointment",
            {
                "appointment_id": EXISTING_APPOINTMENT,
                "replacement_slot_id": "slot-mehta-20261012-0900",
            },
        )

        self.assertEqual("rejected", substituted.payload["result"]["status"])
        self.assertEqual(
            "tools_locked_by_workflow_state",
            substituted.payload["result"]["reason"],
        )
        self.assertEqual(
            "slot-existing", harness.store.appointments[EXISTING_APPOINTMENT].slot_id
        )
        self.assertEqual(
            REPLACEMENT_SLOT,
            harness.store.appointments[
                EXISTING_APPOINTMENT
            ].pending_replacement_slot_id,
        )

    async def test_stale_slot_and_appointment_versions_fail_closed(self) -> None:
        mutations = (
            (
                "slot",
                lambda harness: setattr(
                    harness.store.slots[REPLACEMENT_SLOT],
                    "version",
                    harness.store.slots[REPLACEMENT_SLOT].version + 1,
                ),
            ),
            (
                "appointment",
                lambda harness: setattr(
                    harness.store.appointments[EXISTING_APPOINTMENT],
                    "version",
                    harness.store.appointments[EXISTING_APPOINTMENT].version + 1,
                ),
            ),
        )
        for label, make_stale in mutations:
            with self.subTest(stale=label):
                harness = harness_with()
                journey = GuardrailJourney(
                    harness,
                    session_id=f"stale-{label}",
                    confirmations=(ConfirmationDecision.CONFIRMED,),
                )
                await journey.bootstrap_outbound()
                await journey.identify()
                arguments, _ = await journey.propose_reschedule()
                make_stale(harness)
                await journey.confirm()

                result = await journey.intent("edit_appointment", arguments)

                self.assertEqual("conflict", result.payload["result"]["status"])
                self.assertEqual(WorkflowState.RECOVERY_REQUIRED, journey.state.state)
                self.assertEqual(
                    "slot-existing",
                    harness.store.appointments[EXISTING_APPOINTMENT].slot_id,
                )
                self.assertIs(
                    SlotStatus.BOOKED,
                    harness.store.slots["slot-existing"].status,
                )

    async def test_parallel_duplicate_commit_produces_exactly_one_mutation(self) -> None:
        harness = harness_with()
        journey = GuardrailJourney(
            harness,
            session_id="duplicate-write",
            confirmations=(ConfirmationDecision.CONFIRMED,),
        )
        await journey.bootstrap_outbound()
        await journey.identify()
        arguments, _ = await journey.propose_reschedule()
        await journey.confirm()

        first, second = await asyncio.gather(
            journey.intent("edit_appointment", arguments),
            journey.intent("edit_appointment", arguments),
        )

        self.assertEqual(ToolStatus.SUCCEEDED, first.status)
        self.assertEqual(ToolStatus.SUCCEEDED, second.status)
        self.assertTrue(first.payload["replayed"])
        self.assertTrue(second.payload["replayed"])
        self.assertEqual(
            "slot-existing",
            harness.store.appointments[EXISTING_APPOINTMENT].slot_id,
        )
        self.assertEqual(
            REPLACEMENT_SLOT,
            harness.store.appointments[
                EXISTING_APPOINTMENT
            ].pending_replacement_slot_id,
        )
        self.assertEqual(
            1,
            sum(
                event.event_type == "appointment.reschedule_proposed"
                for event in harness.store.audit_log
            ),
        )
        self.assertEqual(
            WorkflowState.AWAITING_FOLLOW_UP_DECISION, journey.state.state
        )

    async def test_commit_timeout_is_unknown_and_requires_authoritative_reconciliation(
        self,
    ) -> None:
        harness = harness_with(
            Fault("edit_appointment", FaultPoint.AFTER_COMMIT, FaultKind.TIMEOUT)
        )
        journey = GuardrailJourney(
            harness,
            session_id="commit-timeout",
            confirmations=(ConfirmationDecision.CONFIRMED,),
        )
        await journey.bootstrap_outbound()
        await journey.identify()
        arguments, _ = await journey.propose_reschedule()
        await journey.confirm()

        result = await journey.intent("edit_appointment", arguments)
        reconciliation = await journey.control.reconcile(result.operation_id)

        self.assertEqual(ToolStatus.UNKNOWN, result.status)
        self.assertEqual(ToolStatus.UNKNOWN, reconciliation.status)
        self.assertEqual(
            "reconciliation_required", reconciliation.payload["status"]
        )
        self.assertEqual(
            "slot-existing",
            harness.store.appointments[EXISTING_APPOINTMENT].slot_id,
        )
        self.assertEqual(
            REPLACEMENT_SLOT,
            harness.store.appointments[
                EXISTING_APPOINTMENT
            ].pending_replacement_slot_id,
        )
        self.assertEqual(
            1,
            sum(
                event.event_type == "appointment.reschedule_proposed"
                for event in harness.store.audit_log
            ),
        )
        self.assertEqual(1, len(harness.store.idempotency_records))

    async def test_urgent_and_human_preemption_never_cross_protected_boundary(self) -> None:
        for words, final_state in (
            ("I have chest pain", WorkflowState.URGENT_HANDOFF),
            ("I need a real person", WorkflowState.HUMAN_HANDOFF_COMPLETED),
        ):
            with self.subTest(words=words):
                harness = harness_with()
                journey = GuardrailJourney(
                    harness,
                    session_id=f"preempt-{final_state.value}",
                )
                await journey.bootstrap_outbound()

                directive = await journey.control.observe_patient_transcript_delta(words)
                protected = await journey.intent("list_appointments")

                self.assertIsNotNone(directive)
                self.assertEqual(final_state, journey.state.state)
                self.assertEqual(ToolStatus.FAILED, protected.status)
                self.assertEqual({}, harness.store.authorized_sessions)
                self.assertNotIn("appointments", protected.payload["result"])

    async def test_completed_workflow_allows_public_faq_but_not_more_record_access(
        self,
    ) -> None:
        harness = harness_with()
        journey = GuardrailJourney(
            harness,
            session_id="post-completion",
            confirmations=(ConfirmationDecision.CONFIRMED,),
        )
        await journey.bootstrap_outbound()
        await journey.identify()
        arguments, _ = await journey.propose_reschedule()
        await journey.confirm()
        committed = await journey.intent("edit_appointment", arguments)
        self.assertEqual("proposed", committed.payload["result"]["status"])

        faq = await journey.intent(
            "search_clinic_faqs",
            {"query": "Where can I park?", "category": "location"},
        )
        protected = await journey.intent("list_appointments")

        self.assertEqual(ToolStatus.SUCCEEDED, faq.status)
        self.assertEqual(ToolStatus.FAILED, protected.status)
        self.assertEqual(
            "tools_locked_by_workflow_state",
            protected.payload["result"]["reason"],
        )

    async def test_concurrent_calls_isolate_state_and_competing_slot_writes(self) -> None:
        harness = harness_with()
        first = GuardrailJourney(
            harness,
            session_id="parallel-a",
            confirmations=(ConfirmationDecision.CONFIRMED,),
        )
        second = GuardrailJourney(
            harness,
            session_id="parallel-b",
            confirmations=(ConfirmationDecision.CONFIRMED,),
        )
        await asyncio.gather(first.bootstrap_outbound(), second.bootstrap_outbound())
        await asyncio.gather(first.identify(), second.identify())

        async def prepare(journey: GuardrailJourney) -> dict[str, object]:
            await journey.intent(
                "search_slots",
                {
                    "appointment_type": "dermatology-followup",
                    "date_from": "2026-10-09",
                    "date_to": "2026-10-09",
                },
            )
            arguments: dict[str, object] = {"slot_id": BOOKING_SLOT}
            proposal = await journey.intent("create_appointment", arguments)
            self.assertEqual(
                "confirmation_required", proposal.payload["result"]["status"]
            )
            await journey.confirm()
            return arguments

        first_arguments, second_arguments = await asyncio.gather(
            prepare(first), prepare(second)
        )
        first_result, second_result = await asyncio.gather(
            first.intent("create_appointment", first_arguments),
            second.intent("create_appointment", second_arguments),
        )

        domain_statuses = {
            first_result.payload["result"]["status"],
            second_result.payload["result"]["status"],
        }
        self.assertEqual({"proposed", "conflict"}, domain_statuses)
        self.assertNotEqual(
            first.state.identity_confirmation_key,
            second.state.identity_confirmation_key,
        )
        self.assertEqual(
            {"parallel-a", "parallel-b"},
            set(harness.store.authorized_sessions),
        )
        self.assertIs(SlotStatus.HELD, harness.store.slots[BOOKING_SLOT].status)
        created = [
            appointment
            for appointment in harness.store.appointments.values()
            if appointment.slot_id == BOOKING_SLOT
            and appointment.status is AppointmentStatus.PROPOSED
        ]
        self.assertEqual(1, len(created))
        self.assertEqual(
            {
                WorkflowState.AWAITING_FOLLOW_UP_DECISION,
                WorkflowState.RECOVERY_REQUIRED,
            },
            {first.state.state, second.state.state},
        )


if __name__ == "__main__":
    unittest.main()
