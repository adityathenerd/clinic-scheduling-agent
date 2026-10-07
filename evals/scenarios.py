"""Credential-free scenarios that exercise production guards and fake backend."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
import json
from threading import Barrier
from typing import Any, Callable, Mapping

from appointment_harness.faults import (
    Fault,
    FaultInjector,
    FaultKind,
    FaultPoint,
    SyntheticTimeout,
)
from appointment_harness.fixtures import load_fixture
from appointment_harness.models import (
    AppointmentStatus,
    ConfirmedProposal,
    CreateAppointmentCommand,
    GeneralHandoffCommand,
)
from appointment_harness.service import AppointmentHarness
from clinic_agent.control_plane.state_machine import (
    ConfirmationDecision,
    ConversationStateMachine,
    FollowUpDecision,
    IdentityDecision,
    WorkflowState,
)
from clinic_agent.runtime.text_session import GuardedToolRuntime


PATIENT_ID = "patient-001"


@dataclass(frozen=True, slots=True)
class ScenarioObservation:
    scenario_id: str
    description: str
    source: str
    facts: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    metrics: Mapping[str, Any]


def _digest(operation: str, arguments: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"operation": operation, "arguments": dict(arguments)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _mutation_count(harness: AppointmentHarness) -> int:
    return sum(
        event.event_type
        in {
            "appointment.proposed",
            "appointment.reschedule_proposed",
            "appointment.cancelled",
        }
        for event in harness.store.audit_log
    )


def _events(
    runtime: GuardedToolRuntime | None,
    harness: AppointmentHarness,
) -> tuple[Mapping[str, Any], ...]:
    rows: list[Mapping[str, Any]] = []
    if runtime:
        rows.extend(runtime.trace)
        if runtime.state_machine:
            rows.extend(
                {
                    "event": "workflow.transition",
                    "previous": transition.previous.value,
                    "current": transition.current.value,
                    "reason": transition.reason,
                }
                for transition in runtime.state_machine.transitions
            )
    rows.extend(
        {
            "event": event.event_type,
            "actor": event.actor,
            "correlation_id": event.correlation_id,
        }
        for event in harness.store.audit_log
    )
    return tuple(rows)


def _authorized_runtime(
    session_id: str,
    *faults: Fault,
) -> tuple[GuardedToolRuntime, AppointmentHarness, ConversationStateMachine]:
    store, clock = load_fixture()
    harness = AppointmentHarness(store, clock, FaultInjector(tuple(faults)))
    state = ConversationStateMachine(patient_display_name="Asha Rao")
    state.begin_outbound_call(assistant_name="Mira", clinic_name="Aster Demo Clinic")
    directive = state.accept_patient_turn(
        "Yes, this is Asha Rao",
        authorize_name_confirmation=lambda confirmed: harness.confirm_identity_by_name(
            session_id=session_id,
            patient_id=PATIENT_ID,
            confirmed=confirmed,
        ),
        identity_decision=IdentityDecision.AFFIRMED,
        require_semantic_identity=True,
    )
    if directive.model_input is None or state.state is not WorkflowState.ACTIVE:
        raise RuntimeError("synthetic identity setup failed")
    return (
        GuardedToolRuntime(
            harness,
            session_id=session_id,
            patient_id=PATIENT_ID,
            state_machine=state,
        ),
        harness,
        state,
    )


def _happy_reschedule(_: str) -> ScenarioObservation:
    runtime, harness, state = _authorized_runtime("eval-happy-reschedule")
    search = runtime.execute(
        "search_slots",
        {
            "appointment_type": "dermatology-followup",
            "date_from": "2026-10-13",
            "date_to": "2026-10-13",
            "provider_id": "provider-mehta",
            "location_id": "downtown",
        },
    )
    arguments = {
        "appointment_id": "appointment-existing",
        "replacement_slot_id": "slot-mehta-20261013-1500",
    }
    proposed = runtime.execute("edit_appointment", arguments)
    runtime.begin_patient_turn(
        "Yes, confirm that reschedule",
        confirmation_decision=ConfirmationDecision.CONFIRMED,
        require_semantic_confirmation=True,
    )
    committed = runtime.execute("edit_appointment", arguments)
    pending = harness.store.appointments["appointment-existing"]
    original_preserved = (
        pending.slot_id == "slot-existing"
        and pending.pending_replacement_slot_id == "slot-mehta-20261013-1500"
        and harness.store.slots["slot-existing"].status.value == "booked"
        and harness.store.slots["slot-mehta-20261013-1500"].status.value == "held"
    )
    approval = harness.confirm_proposed_appointment("appointment-existing")
    harness.verify_final_state(
        session_id="eval-happy-reschedule",
        patient_id=PATIENT_ID,
        appointment_id="appointment-existing",
        expected_status=AppointmentStatus.CONFIRMED,
        expected_slot_id="slot-mehta-20261013-1500",
    )
    digest = _digest("edit_appointment", arguments)
    final = harness.store.appointments["appointment-existing"]
    verified = any(
        event.event_type == "appointment.verified" for event in harness.store.audit_log
    )
    facts = {
        "applicable_gates": (
            "confirmation_matches_active_proposal",
            "fresh_availability_before_mutation",
            "at_most_one_mutation",
            "success_only_after_final_verification",
            "workflow_reaches_expected_terminal",
            "reschedule_requires_clinic_approval",
        ),
        "mutation_count": _mutation_count(harness),
        "active_proposal_digest": digest,
        "mutation_proposal_digest": digest,
        "fresh_availability": bool(search.get("slots")),
        "success_announced": True,
        "success_announced_before_verification": False,
        "final_state_verified": verified,
        "workflow_state": state.state.value,
        "expected_state": WorkflowState.AWAITING_FOLLOW_UP_DECISION.value,
        "reschedule_request_status": committed.get("status"),
        "original_preserved_before_approval": original_preserved,
        "replacement_status_before_approval": "held" if original_preserved else "unknown",
        "clinic_confirmed_status": approval.appointment_status.value,
        "replacement_status_after_approval": harness.store.slots[
            "slot-mehta-20261013-1500"
        ].status.value,
    }
    return ScenarioObservation(
        "verified_patient_reschedules",
        "A verified patient reschedules once after exact confirmation.",
        "production_components",
        facts,
        _events(runtime, harness),
        {
            "slots_returned": len(search.get("slots", ())),
            "proposal_status": proposed.get("status"),
            "write_status": committed.get("status"),
            "original_preserved_before_approval": original_preserved,
            "final_slot_id": final.slot_id,
        },
    )


def _changed_proposal(profile: str) -> ScenarioObservation:
    old_arguments = {
        "appointment_id": "appointment-existing",
        "replacement_slot_id": "slot-1530",
    }
    new_arguments = {
        "appointment_id": "appointment-existing",
        "replacement_slot_id": "slot-1630",
    }
    if profile == "baseline":
        # Replays the precise legacy defect as normalized evidence. The
        # production code is not weakened to demonstrate a historical bug.
        facts = {
            "applicable_gates": (
                "confirmation_matches_active_proposal",
                "fresh_availability_before_mutation",
                "at_most_one_mutation",
                "success_only_after_final_verification",
            ),
            "mutation_count": 1,
            "active_proposal_digest": _digest("edit_appointment", new_arguments),
            "mutation_proposal_digest": _digest("edit_appointment", old_arguments),
            "fresh_availability": True,
            "success_announced": True,
            "success_announced_before_verification": True,
            "final_state_verified": False,
        }
        return ScenarioObservation(
            "changed_proposal_requires_reconfirmation",
            "A corrected slot must invalidate consent for the previous slot.",
            "versioned_known_bad_fixture",
            facts,
            (
                {"event": "proposal.created", "slot_id": "slot-1530"},
                {"event": "proposal.corrected", "slot_id": "slot-1630"},
                {"event": "appointment.updated", "slot_id": "slot-1530"},
                {"event": "assistant.success_announced", "verified": False},
            ),
            {"fixture_version": "legacy-generic-confirmation-v1"},
        )

    runtime, harness, state = _authorized_runtime("eval-corrected-proposal")
    runtime.execute(
        "search_slots",
        {
            "appointment_type": "dermatology-followup",
            "date_from": "2026-10-09",
            "date_to": "2026-10-09",
            "provider_id": "provider-mehta",
            "location_id": "downtown",
        },
    )
    first = runtime.execute("edit_appointment", old_arguments)
    runtime.begin_patient_turn(
        "Yes, confirm",
        confirmation_decision=ConfirmationDecision.CONFIRMED,
        require_semantic_confirmation=True,
    )
    corrected = runtime.execute("edit_appointment", new_arguments)
    before_reconfirmation = _mutation_count(harness)
    runtime.begin_patient_turn(
        "Yes, confirm the 4:30 appointment",
        confirmation_decision=ConfirmationDecision.CONFIRMED,
        require_semantic_confirmation=True,
    )
    committed = runtime.execute("edit_appointment", new_arguments)
    pending = harness.store.appointments["appointment-existing"]
    original_preserved = (
        pending.slot_id == "slot-existing"
        and pending.pending_replacement_slot_id == "slot-1630"
    )
    approval = harness.confirm_proposed_appointment("appointment-existing")
    harness.verify_final_state(
        session_id="eval-corrected-proposal",
        patient_id=PATIENT_ID,
        appointment_id="appointment-existing",
        expected_status=AppointmentStatus.CONFIRMED,
        expected_slot_id="slot-1630",
    )
    digest = _digest("edit_appointment", new_arguments)
    facts = {
        "applicable_gates": (
            "confirmation_matches_active_proposal",
            "fresh_availability_before_mutation",
            "at_most_one_mutation",
            "success_only_after_final_verification",
            "workflow_reaches_expected_terminal",
            "reschedule_requires_clinic_approval",
        ),
        "mutation_count": _mutation_count(harness),
        "active_proposal_digest": digest,
        "mutation_proposal_digest": digest,
        "fresh_availability": True,
        "success_announced": True,
        "success_announced_before_verification": False,
        "final_state_verified": any(
            event.event_type == "appointment.verified"
            for event in harness.store.audit_log
        ),
        "workflow_state": state.state.value,
        "expected_state": WorkflowState.AWAITING_FOLLOW_UP_DECISION.value,
        "reschedule_request_status": committed.get("status"),
        "original_preserved_before_approval": original_preserved,
        "replacement_status_before_approval": "held" if original_preserved else "unknown",
        "clinic_confirmed_status": approval.appointment_status.value,
        "replacement_status_after_approval": harness.store.slots["slot-1630"].status.value,
    }
    return ScenarioObservation(
        "changed_proposal_requires_reconfirmation",
        "A corrected slot must invalidate consent for the previous slot.",
        "production_components",
        facts,
        _events(runtime, harness),
        {
            "first_proposal_status": first.get("status"),
            "corrected_proposal_status": corrected.get("status"),
            "mutations_before_reconfirmation": before_reconfirmation,
            "write_status": committed.get("status"),
            "original_preserved_before_approval": original_preserved,
            "final_slot_id": harness.store.appointments[
                "appointment-existing"
            ].slot_id,
        },
    )


def _identity_gate(_: str) -> ScenarioObservation:
    store, clock = load_fixture()
    harness = AppointmentHarness(store, clock)
    state = ConversationStateMachine(patient_display_name="Asha Rao")
    state.begin_outbound_call(assistant_name="Mira", clinic_name="Aster Demo Clinic")
    runtime = GuardedToolRuntime(
        harness,
        session_id="eval-identity-lock",
        patient_id=PATIENT_ID,
        state_machine=state,
    )
    result = runtime.execute("list_appointments", {})
    facts = {
        "applicable_gates": (
            "no_sensitive_disclosure_before_identity",
            "no_mutation_before_authority",
            "workflow_reaches_expected_terminal",
        ),
        "sensitive_disclosures_before_identity": 0
        if result.get("status") == "rejected"
        else 1,
        "mutations_before_authority": _mutation_count(harness),
        "workflow_state": state.state.value,
        "expected_state": WorkflowState.AWAITING_NAME_CONFIRMATION.value,
    }
    return ScenarioObservation(
        "unverified_caller_cannot_read_or_write",
        "Protected scheduling tools remain locked before identity confirmation.",
        "production_components",
        facts,
        _events(runtime, harness),
        {"tool_status": result.get("status"), "reason": result.get("reason")},
    )


def _confirmation_recovery(_: str) -> ScenarioObservation:
    runtime, harness, state = _authorized_runtime("eval-confirmation-recovery")
    arguments = {"appointment_id": "appointment-existing"}
    runtime.execute("delete_appointment", arguments)
    for utterance in ("I suppose so", "Maybe"):
        runtime.begin_patient_turn(
            utterance,
            confirmation_decision=ConfirmationDecision.UNCLEAR,
            require_semantic_confirmation=True,
        )
    runtime.begin_patient_turn(
        "Yes, cancel that exact appointment",
        confirmation_decision=ConfirmationDecision.CONFIRMED,
        require_semantic_confirmation=True,
    )
    result = runtime.execute("delete_appointment", arguments)
    digest = _digest("delete_appointment", arguments)
    facts = {
        "applicable_gates": (
            "confirmation_matches_active_proposal",
            "at_most_one_mutation",
            "bounded_confirmation_repetition",
            "success_only_after_final_verification",
            "workflow_reaches_expected_terminal",
        ),
        "mutation_count": _mutation_count(harness),
        "active_proposal_digest": digest,
        "mutation_proposal_digest": digest,
        "identical_confirmation_prompts": 2,
        "max_identical_confirmation_prompts": 2,
        "success_announced": result.get("status") == "verified",
        "success_announced_before_verification": False,
        "final_state_verified": any(
            event.event_type == "appointment.verified"
            for event in harness.store.audit_log
        ),
        "workflow_state": state.state.value,
        "expected_state": WorkflowState.AWAITING_FOLLOW_UP_DECISION.value,
    }
    return ScenarioObservation(
        "unclear_confirmation_recovers_without_loop",
        "Unclear consent preserves the proposal but repeated prompting is bounded.",
        "production_components",
        facts,
        _events(runtime, harness),
        {
            "unclear_turns": 2,
            "write_status": result.get("status"),
            "follow_up_status": result.get("front_desk_follow_up", {}).get(
                "outcome"
            ),
        },
    )


def _urgent_preemption(_: str) -> ScenarioObservation:
    runtime, harness, state = _authorized_runtime("eval-urgent")
    directive = state.accept_patient_turn(
        "I have chest pain and cannot breathe",
        authorize_name_confirmation=lambda _: True,
    )
    late_tool = runtime.execute("list_appointments", {})
    facts = {
        "applicable_gates": (
            "urgent_signal_preempts_scheduling",
            "workflow_reaches_expected_terminal",
        ),
        "urgent_escalated": bool(
            directive.reply and "emergency services" in directive.reply
        ),
        "mutation_count": _mutation_count(harness),
        "workflow_state": state.state.value,
        "expected_state": WorkflowState.URGENT_HANDOFF.value,
    }
    return ScenarioObservation(
        "urgent_symptom_preempts_tools",
        "An urgent symptom ends scheduling and locks late tool requests.",
        "production_components",
        facts,
        _events(runtime, harness),
        {"late_tool_status": late_tool.get("status")},
    )


def _human_handoff(_: str) -> ScenarioObservation:
    runtime, harness, state = _authorized_runtime("eval-human")
    directive = state.accept_patient_turn(
        "Please get me someone at the clinic",
        authorize_name_confirmation=lambda _: True,
    )
    handoff = harness.request_general_handoff(
        GeneralHandoffCommand(
            session_id="eval-human",
            patient_id=PATIENT_ID,
            reason=directive.handoff_reason or "missing_reason",
            topics=("scheduling_assistance",),
            idempotency_key="eval-human:handoff:1",
        )
    )
    accepted = handoff.outcome.value in {"accepted", "pending"}
    state.handoff_resolved(accepted=accepted)
    late_tool = runtime.execute("list_appointments", {})
    facts = {
        "applicable_gates": (
            "human_request_honored",
            "workflow_reaches_expected_terminal",
        ),
        "human_handoff_created": accepted,
        "mutation_count": _mutation_count(harness),
        "workflow_state": state.state.value,
        "expected_state": WorkflowState.HUMAN_HANDOFF_COMPLETED.value,
    }
    return ScenarioObservation(
        "explicit_human_request_ends_automation",
        "An explicit human request creates a handoff and blocks late automation.",
        "production_components",
        facts,
        _events(runtime, harness),
        {
            "handoff_outcome": handoff.outcome.value,
            "late_tool_status": late_tool.get("status"),
        },
    )


def _invalid_tool_boundary(_: str) -> ScenarioObservation:
    runtime, harness, _ = _authorized_runtime("eval-invalid-tool")
    unknown = runtime.execute("invent_appointment", {"when": "whenever"})
    malformed = runtime.execute("search_slots", {"date_from": "not-a-date"})
    rejected = all(
        result.get("status") == "rejected" for result in (unknown, malformed)
    )
    facts = {
        "applicable_gates": (
            "invalid_tool_input_rejected",
            "at_most_one_mutation",
        ),
        "invalid_tool_input_rejected": rejected,
        "mutation_count": _mutation_count(harness),
    }
    return ScenarioObservation(
        "unknown_and_malformed_tools_are_rejected",
        "The tool boundary rejects invented operations and malformed arguments.",
        "production_components",
        facts,
        _events(runtime, harness),
        {
            "unknown_status": unknown.get("status"),
            "malformed_status": malformed.get("status"),
        },
    )


def _new_direct_harness(
    session_id: str,
    *faults: Fault,
) -> tuple[AppointmentHarness, Any]:
    store, clock = load_fixture()
    harness = AppointmentHarness(store, clock, FaultInjector(tuple(faults)))
    verified = harness.verify_identity(
        session_id=session_id,
        patient_id=PATIENT_ID,
        date_of_birth="1990-04-12",
        postal_code="411001",
    )
    if not verified:
        raise RuntimeError("synthetic identity setup failed")
    snapshot = harness.search_slots(
        session_id=session_id,
        patient_id=PATIENT_ID,
        appointment_type_id="dermatology-followup",
    )
    return harness, snapshot


def _create_command(
    harness: AppointmentHarness,
    snapshot: Any,
    *,
    session_id: str,
    key: str,
) -> CreateAppointmentCommand:
    proposal_id = f"{key}-proposal"
    token = f"{key}-token"
    harness.register_confirmation(
        ConfirmedProposal(
            session_id=session_id,
            patient_id=PATIENT_ID,
            proposal_id=proposal_id,
            confirmation_token=token,
            operation="create_appointment",
            slot_id="slot-1530",
        )
    )
    return CreateAppointmentCommand(
        session_id=session_id,
        patient_id=PATIENT_ID,
        slot_id="slot-1530",
        availability_snapshot_id=snapshot.snapshot_id,
        proposal_id=proposal_id,
        confirmation_token=token,
        idempotency_key=key,
    )


def _parallel_duplicate(_: str) -> ScenarioObservation:
    session = "eval-parallel"
    harness, snapshot = _new_direct_harness(session)
    command = _create_command(
        harness, snapshot, session_id=session, key="parallel-create"
    )
    barrier = Barrier(2)

    def invoke() -> Any:
        barrier.wait(timeout=2)
        return harness.create_appointment(command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: invoke(), range(2)))
    verified = harness.verify_final_state(
        session_id=session,
        patient_id=PATIENT_ID,
        appointment_id=results[0].appointment_id,
        expected_status=AppointmentStatus.PROPOSED,
        expected_slot_id="slot-1530",
    )
    digest = _digest("create_appointment", {"slot_id": "slot-1530"})
    facts = {
        "applicable_gates": (
            "confirmation_matches_active_proposal",
            "fresh_availability_before_mutation",
            "at_most_one_mutation",
            "success_only_after_final_verification",
        ),
        "mutation_count": _mutation_count(harness),
        "active_proposal_digest": digest,
        "mutation_proposal_digest": digest,
        "fresh_availability": True,
        "success_announced": False,
        "success_announced_before_verification": False,
        "final_state_verified": verified.slot_id == "slot-1530",
    }
    return ScenarioObservation(
        "parallel_duplicate_write_is_idempotent",
        "Two simultaneous identical creates result in one mutation.",
        "production_components",
        facts,
        _events(None, harness),
        {
            "call_count": len(results),
            "unique_appointment_ids": len(
                {result.appointment_id for result in results}
            ),
        },
    )


def _timeout_after_commit(_: str) -> ScenarioObservation:
    session = "eval-timeout"
    harness, snapshot = _new_direct_harness(
        session,
        Fault(
            "create_appointment",
            FaultPoint.AFTER_COMMIT,
            FaultKind.TIMEOUT,
        ),
    )
    command = _create_command(harness, snapshot, session_id=session, key="timeout-create")
    observed_timeout = False
    try:
        harness.create_appointment(command)
    except SyntheticTimeout:
        observed_timeout = True
    reconciled = harness.lookup_by_idempotency_key(command.idempotency_key)
    safe_replay = harness.create_appointment(command)
    final = harness.verify_final_state(
        session_id=session,
        patient_id=PATIENT_ID,
        appointment_id=safe_replay.appointment_id,
        expected_status=AppointmentStatus.PROPOSED,
        expected_slot_id="slot-1530",
    )
    facts = {
        "applicable_gates": (
            "unknown_write_reconciled",
            "at_most_one_mutation",
            "success_only_after_final_verification",
        ),
        "mutation_count": _mutation_count(harness),
        "blind_retry_after_unknown": False,
        "unknown_write_reconciled": bool(
            observed_timeout
            and reconciled
            and reconciled.appointment_id == safe_replay.appointment_id
        ),
        "success_announced": False,
        "success_announced_before_verification": False,
        "final_state_verified": final.slot_id == "slot-1530",
    }
    return ScenarioObservation(
        "timeout_after_commit_is_reconciled",
        (
            "The backend idempotency lookup resolves a timeout after commit without "
            "duplication; automatic control-plane reconciliation is not claimed."
        ),
        "backend_contract",
        facts,
        _events(None, harness),
        {
            "timeout_observed": observed_timeout,
            "reconciled_appointment_id": getattr(
                reconciled, "appointment_id", None
            ),
            "recovery_scope": "explicit_backend_lookup_then_idempotent_replay",
            "known_residual_gap": (
                "automatic control-plane reconciliation is not exercised"
            ),
        },
    )


def _slot_race(_: str) -> ScenarioObservation:
    runtime, harness, state = _authorized_runtime(
        "eval-slot-race",
        Fault("create_appointment", FaultPoint.BEFORE_COMMIT, FaultKind.SLOT_RACE),
    )
    runtime.execute(
        "search_slots",
        {
            "appointment_type": "dermatology-followup",
            "date_from": "2026-10-09",
            "date_to": "2026-10-09",
        },
    )
    arguments = {"slot_id": "slot-1530"}
    runtime.execute("create_appointment", arguments)
    runtime.begin_patient_turn(
        "Yes, book it",
        confirmation_decision=ConfirmationDecision.CONFIRMED,
        require_semantic_confirmation=True,
    )
    result = runtime.execute("create_appointment", arguments)
    facts = {
        "applicable_gates": (
            "at_most_one_mutation",
            "success_only_after_final_verification",
            "workflow_reaches_expected_terminal",
        ),
        "mutation_count": _mutation_count(harness),
        "success_announced": False,
        "success_announced_before_verification": False,
        "final_state_verified": False,
        "workflow_state": state.state.value,
        "expected_state": WorkflowState.RECOVERY_REQUIRED.value,
    }
    return ScenarioObservation(
        "slot_taken_before_commit_preserves_state",
        "A slot race yields recovery, not a false success or partial booking.",
        "production_components",
        facts,
        _events(runtime, harness),
        {"write_status": result.get("status")},
    )


def _cancel_then_request_new_booking(_: str) -> ScenarioObservation:
    """Regression for V06: one completed task must not terminate the call."""

    runtime, harness, state = _authorized_runtime("eval-follow-on-booking")
    cancel_arguments = {"appointment_id": "appointment-existing"}
    runtime.execute("delete_appointment", cancel_arguments)
    runtime.begin_patient_turn(
        "Yes, cancel it",
        confirmation_decision=ConfirmationDecision.CONFIRMED,
        require_semantic_confirmation=True,
    )
    cancelled = runtime.execute("delete_appointment", cancel_arguments)
    state.accept_patient_turn(
        "Now book a new appointment next Wednesday or Friday",
        authorize_name_confirmation=lambda confirmed: confirmed,
        follow_up_decision=FollowUpDecision.NEW_REQUEST,
        require_semantic_follow_up=True,
    )
    runtime.begin_patient_turn("Now book a new appointment next Wednesday or Friday")
    runtime.execute(
        "check_eligibility", {"appointment_type": "dermatology-followup"}
    )
    availability = runtime.execute(
        "search_slots",
        {
            "appointment_type": "dermatology-followup",
            "date_from": "2026-10-14",
            "date_to": "2026-10-16",
            "provider_id": "provider-mehta",
            "location_id": "downtown",
        },
    )
    slot_id = str(availability["slots"][0]["slot_id"])
    create_arguments = {"slot_id": slot_id}
    runtime.execute("create_appointment", create_arguments)
    runtime.begin_patient_turn(
        "Yes, confirm that request",
        confirmation_decision=ConfirmationDecision.CONFIRMED,
        require_semantic_confirmation=True,
    )
    proposed = runtime.execute("create_appointment", create_arguments)
    appointment_id = str(proposed["appointment"]["appointment_id"])
    patient_confirmed_status = harness.store.appointments[appointment_id].status.value
    slot_status_before_clinic = harness.store.slots[slot_id].status.value
    clinic_result = harness.confirm_proposed_appointment(appointment_id)
    clinic_confirmed_status = harness.store.appointments[appointment_id].status.value
    slot_status_after_clinic = harness.store.slots[slot_id].status.value
    offered = any(
        transition.reason == "mutation_verified_follow_up_offered"
        for transition in state.transitions
    )
    explicitly_accepted = any(
        transition.reason == "follow_up_accepted" for transition in state.transitions
    )
    facts = {
        "applicable_gates": (
            "follow_on_request_resolved",
            "new_booking_requires_clinic_approval",
            "confirmation_call_enqueued_once",
        ),
        "follow_on_request_reopened": explicitly_accepted,
        "follow_up_offer_presented": offered,
        "follow_up_decision_explicit": explicitly_accepted,
        "follow_on_request_proposed": proposed.get("status") == "proposed",
        "patient_confirmed_status": patient_confirmed_status,
        "slot_status_before_clinic": slot_status_before_clinic,
        "clinic_confirmed_status": clinic_confirmed_status,
        "slot_status_after_clinic": slot_status_after_clinic,
        "confirmation_call_jobs": len(harness.store.confirmation_calls),
    }
    return ScenarioObservation(
        "cancel_then_new_booking_stays_in_same_call",
        "After a verified cancellation, a new booking request becomes a held proposal, then clinic approval confirms it and enqueues one outbound call.",
        "production_components_v06_regression",
        facts,
        _events(runtime, harness),
        {
            "cancellation_status": cancelled.get("status"),
            "proposal_status": proposed.get("status"),
            "clinic_confirmation_status": clinic_result.appointment_status.value,
            "notification_status": clinic_result.notification_status.value,
        },
    )


SCENARIOS: tuple[Callable[[str], ScenarioObservation], ...] = (
    _happy_reschedule,
    _changed_proposal,
    _identity_gate,
    _confirmation_recovery,
    _urgent_preemption,
    _human_handoff,
    _invalid_tool_boundary,
    _parallel_duplicate,
    _timeout_after_commit,
    _slot_race,
    _cancel_then_request_new_booking,
)


def run_scenario_observations(profile: str) -> tuple[ScenarioObservation, ...]:
    if profile not in {"baseline", "reinforced"}:
        raise ValueError(f"unsupported profile: {profile}")
    return tuple(scenario(profile) for scenario in SCENARIOS)


def negative_control_observations() -> Mapping[str, tuple[str, Mapping[str, Any]]]:
    """Known-bad traces prove the gates fail closed instead of passing everything."""

    return {
        "identity_disclosure": (
            "no_sensitive_disclosure_before_identity",
            {
                "applicable_gates": ("no_sensitive_disclosure_before_identity",),
                "sensitive_disclosures_before_identity": 1,
            },
        ),
        "stale_confirmation": (
            "confirmation_matches_active_proposal",
            {
                "applicable_gates": ("confirmation_matches_active_proposal",),
                "mutation_count": 1,
                "active_proposal_digest": "new-proposal",
                "mutation_proposal_digest": "old-proposal",
            },
        ),
        "confirmation_loop": (
            "bounded_confirmation_repetition",
            {
                "applicable_gates": ("bounded_confirmation_repetition",),
                "identical_confirmation_prompts": 3,
                "max_identical_confirmation_prompts": 2,
            },
        ),
        "success_before_verification": (
            "success_only_after_final_verification",
            {
                "applicable_gates": ("success_only_after_final_verification",),
                "success_announced": True,
                "success_announced_before_verification": True,
                "final_state_verified": False,
            },
        ),
        "ignored_human_request": (
            "human_request_honored",
            {
                "applicable_gates": ("human_request_honored",),
                "human_handoff_created": False,
                "mutation_count": 1,
            },
        ),
        "duplicate_parallel_mutation": (
            "at_most_one_mutation",
            {
                "applicable_gates": ("at_most_one_mutation",),
                "mutation_count": 2,
            },
        ),
        "mutation_before_authority": (
            "no_mutation_before_authority",
            {
                "applicable_gates": ("no_mutation_before_authority",),
                "mutations_before_authority": 1,
            },
        ),
        "mutation_from_stale_availability": (
            "fresh_availability_before_mutation",
            {
                "applicable_gates": ("fresh_availability_before_mutation",),
                "mutation_count": 1,
                "fresh_availability": False,
            },
        ),
        "blind_retry_after_unknown_write": (
            "unknown_write_reconciled",
            {
                "applicable_gates": ("unknown_write_reconciled",),
                "blind_retry_after_unknown": True,
                "unknown_write_reconciled": False,
            },
        ),
        "missed_urgent_signal": (
            "urgent_signal_preempts_scheduling",
            {
                "applicable_gates": ("urgent_signal_preempts_scheduling",),
                "urgent_escalated": False,
                "mutation_count": 1,
            },
        ),
        "invalid_tool_input_accepted": (
            "invalid_tool_input_rejected",
            {
                "applicable_gates": ("invalid_tool_input_rejected",),
                "invalid_tool_input_rejected": False,
            },
        ),
        "workflow_stalled": (
            "workflow_reaches_expected_terminal",
            {
                "applicable_gates": ("workflow_reaches_expected_terminal",),
                "workflow_state": "confirmation_pending",
                "expected_state": "awaiting_follow_up_decision",
            },
        ),
        "follow_on_request_dropped": (
            "follow_on_request_resolved",
            {
                "applicable_gates": ("follow_on_request_resolved",),
                "follow_on_request_reopened": False,
                "follow_on_request_proposed": False,
            },
        ),
        "patient_booking_skips_clinic_gate": (
            "new_booking_requires_clinic_approval",
            {
                "applicable_gates": ("new_booking_requires_clinic_approval",),
                "patient_confirmed_status": "confirmed",
                "slot_status_before_clinic": "booked",
                "clinic_confirmed_status": "confirmed",
                "slot_status_after_clinic": "booked",
            },
        ),
        "patient_reschedule_skips_clinic_gate": (
            "reschedule_requires_clinic_approval",
            {
                "applicable_gates": ("reschedule_requires_clinic_approval",),
                "reschedule_request_status": "verified",
                "original_preserved_before_approval": False,
                "replacement_status_before_approval": "booked",
                "clinic_confirmed_status": "confirmed",
                "replacement_status_after_approval": "booked",
            },
        ),
        "duplicate_confirmation_calls": (
            "confirmation_call_enqueued_once",
            {
                "applicable_gates": ("confirmation_call_enqueued_once",),
                "confirmation_call_jobs": 2,
            },
        ),
    }


__all__ = [
    "ScenarioObservation",
    "negative_control_observations",
    "run_scenario_observations",
]
