"""Credential-free scripted call through the real scheduling safety boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from appointment_harness.fixtures import load_fixture
from appointment_harness.service import AppointmentHarness
from clinic_agent.clinic_profile import CLINIC_NAME
from clinic_agent.control_plane.state_machine import (
    ConversationStateMachine,
    FollowUpDecision,
    WorkflowState,
)
from clinic_agent.runtime.text_session import GuardedToolRuntime


@dataclass(frozen=True, slots=True)
class MockCallResult:
    transcript: tuple[tuple[str, str], ...]
    workflow_state: WorkflowState
    appointment_id: str
    appointment_status: str
    appointment_slot_id: str
    transition_count: int
    tool_trace: tuple[dict[str, Any], ...]
    audit_event_types: tuple[str, ...]


def run_mock_booking_call() -> MockCallResult:
    """Propose one fixture slot using the same guards used by the model adapter."""

    store, clock = load_fixture()
    harness = AppointmentHarness(store, clock)
    session_id = "mock-call-session"
    patient_id = "patient-001"
    state = ConversationStateMachine(patient_display_name="Asha Rao")
    runtime = GuardedToolRuntime(
        harness,
        session_id=session_id,
        patient_id=patient_id,
        state_machine=state,
    )
    transcript: list[tuple[str, str]] = []

    def say(speaker: str, text: str) -> None:
        transcript.append((speaker, text))

    def authorize_name(confirmed: bool) -> bool:
        return harness.confirm_identity_by_name(
            session_id=session_id,
            patient_id=patient_id,
            confirmed=confirmed,
        )

    say(
        "Mira",
        state.greeting(assistant_name="Mira", clinic_name=CLINIC_NAME),
    )
    intent = "I'd like to book a dermatology follow-up on Friday afternoon."
    say("Patient", intent)
    directive = state.accept_patient_turn(
        intent,
        authorize_name_confirmation=authorize_name,
    )
    if directive.reply is None:
        raise RuntimeError("mock intent did not produce identity prompt")
    say("Mira", directive.reply)

    identity = "Yes, this is Asha Rao."
    say("Patient", identity)
    directive = state.accept_patient_turn(
        identity,
        authorize_name_confirmation=authorize_name,
    )
    if directive.model_input is None or state.state is not WorkflowState.ACTIVE:
        raise RuntimeError("mock identity did not unlock the active workflow")
    runtime.begin_patient_turn(identity)
    say("Mira", "Thanks, Asha. I'll check current Friday afternoon availability.")

    eligibility = runtime.execute(
        "check_eligibility",
        {"appointment_type": "dermatology-followup"},
    )
    if eligibility.get("status") != "ok":
        raise RuntimeError("mock eligibility check failed")
    availability = runtime.execute(
        "search_slots",
        {
            "appointment_type": "dermatology-followup",
            "date_from": "2026-10-09",
            "date_to": "2026-10-09",
            "location": "downtown",
        },
    )
    if availability.get("status") != "ok":
        raise RuntimeError("mock availability read failed")
    say(
        "Mira",
        "I found 3:30 PM and 4:30 PM with Dr. N. Mehta downtown. Which works better?",
    )

    selection = "3:30 PM, please."
    say("Patient", selection)
    state.accept_patient_turn(
        selection,
        authorize_name_confirmation=authorize_name,
    )
    runtime.begin_patient_turn(selection)
    proposal = runtime.execute("create_appointment", {"slot_id": "slot-1530"})
    if proposal.get("status") != "confirmation_required":
        raise RuntimeError("mock write did not stop for exact confirmation")
    proposal_text = str(proposal["exact_proposal"])
    say(
        "Mira",
        f"Here is the exact appointment request: {proposal_text}. "
        "Do you explicitly confirm that I should submit this request for clinic approval?",
    )

    confirmation = "Yes, confirm."
    say("Patient", confirmation)
    state.accept_patient_turn(
        confirmation,
        authorize_name_confirmation=authorize_name,
    )
    runtime.begin_patient_turn(confirmation)
    result = runtime.execute("create_appointment", {"slot_id": "slot-1530"})
    if result.get("status") != "proposed":
        raise RuntimeError(f"mock booking request was not proposed: {result}")
    appointment = result["appointment"]
    say(
        "Mira",
        str(result["patient_facing_summary"]),
    )
    closing = "No, that's all. Thank you."
    say("Patient", closing)
    closing_directive = state.accept_patient_turn(
        closing,
        authorize_name_confirmation=authorize_name,
        follow_up_decision=FollowUpDecision.DECLINED,
        require_semantic_follow_up=True,
    )
    if closing_directive.reply is None or state.state is not WorkflowState.COMPLETED:
        raise RuntimeError("mock follow-up decline did not close the workflow")
    say("Mira", "Thank you. Take care.")

    return MockCallResult(
        transcript=tuple(transcript),
        workflow_state=state.state,
        appointment_id=str(appointment["appointment_id"]),
        appointment_status=str(appointment["status"]),
        appointment_slot_id=str(appointment["slot_id"]),
        transition_count=len(state.transitions),
        tool_trace=tuple(runtime.trace),
        audit_event_types=tuple(event.event_type for event in store.audit_log),
    )


def render_mock_call(result: MockCallResult) -> str:
    lines = ["Mock call (credential-free application boundary)", ""]
    lines.extend(f"{speaker}: {text}" for speaker, text in result.transcript)
    lines.extend(
        [
            "",
            "Verified proposal outcome",
            f"workflow_state={result.workflow_state.value}",
            f"appointment_id={result.appointment_id}",
            f"appointment_status={result.appointment_status}",
            f"appointment_slot_id={result.appointment_slot_id}",
            f"transitions={result.transition_count}",
            "audit=" + ",".join(result.audit_event_types),
        ]
    )
    return "\n".join(lines)
