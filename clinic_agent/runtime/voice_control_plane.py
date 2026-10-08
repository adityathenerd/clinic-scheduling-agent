"""Application-owned control plane used by the live voice call actor.

The Live model owns turn-taking and natural speech.  This component owns the
security-sensitive interpretation of completed caller turns and every domain
tool execution.  Transcript fragments are accumulated until the backend asks
for work; that delegation boundary is the deterministic turn boundary available
on GPT-Live's primary WebSocket.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
from typing import Any, Mapping

from appointment_harness.models import GeneralHandoffCommand
from appointment_harness.service import AppointmentHarness
from clinic_agent.agent.prompts import PromptContext, build_backend_prompt, build_live_prompt
from clinic_agent.agent.tool_catalog import (
    BACKEND_TOOL_CATALOG,
    IDENTITY_INTERPRETATION_TOOL_NAME,
)
from clinic_agent.call_mechanics.models import (
    CallDescriptor,
    CallDirection,
    OperationKind,
    ToolIntent,
    ToolResult,
    ToolStatus,
    VoiceSessionConfig,
)
from clinic_agent.call_mechanics.protocols import EventSink
from clinic_agent.control_plane.state_machine import (
    ConfirmationDecision,
    ConversationStateMachine,
    FollowUpDecision,
    IdentityDecision,
    POST_TASK_HELP_OFFER,
    WorkflowState,
)
from clinic_agent.runtime.text_session import GuardedToolRuntime, WRITE_TOOLS
from clinic_agent.runtime.identity_classifier import (
    ConfirmationTurnClassifier,
    FollowUpTurnClassifier,
    IdentityTurnClassifier,
)
from clinic_agent.runtime.speech_contract import spoken_contract_digest


_CALL_CLOSURE_CANDIDATE_PATTERNS = (
    re.compile(r"\b(?:that(?:'s| is)|this is) all\b", re.I),
    re.compile(r"\bnothing else\b", re.I),
    re.compile(r"\b(?:goodbye|bye)\b", re.I),
    re.compile(r"\bno\s*(?:,|\.)?\s*(?:thank you|thanks)\b", re.I),
    re.compile(r"\bi(?:'m| am) (?:all )?done\b", re.I),
)


def _looks_like_call_closure_candidate(transcript: str) -> bool:
    """Route likely closing turns to semantics; never authorize from text alone."""

    normalized = transcript.replace("’", "'").strip()
    return bool(normalized) and any(
        pattern.search(normalized) for pattern in _CALL_CLOSURE_CANDIDATE_PATTERNS
    )


@dataclass(frozen=True, slots=True)
class PatientTurnObservation:
    """Result of applying one accumulated caller turn to application state."""

    transcript: str
    directive: str | None
    handoff_reference: str | None = None


class VoiceAgentControlPlane:
    """Bridge GPT-Live delegations to the guarded deterministic scheduler."""

    def __init__(
        self,
        harness: AppointmentHarness,
        *,
        session_id: str,
        patient_id: str,
        prompt_context: PromptContext,
        state_machine: ConversationStateMachine,
        event_sink: EventSink | None = None,
        identity_classifier: IdentityTurnClassifier | None = None,
        confirmation_classifier: ConfirmationTurnClassifier | None = None,
        follow_up_classifier: FollowUpTurnClassifier | None = None,
        confirmation_classification_timeout: float = 10.0,
    ) -> None:
        self.harness = harness
        self.session_id = session_id
        self.patient_id = patient_id
        self.prompt_context = prompt_context
        self.state_machine = state_machine
        self.event_sink = event_sink
        self.identity_classifier = identity_classifier
        self.confirmation_classifier = confirmation_classifier
        self.follow_up_classifier = follow_up_classifier
        if confirmation_classification_timeout <= 0:
            raise ValueError("confirmation classification timeout must be positive")
        self._confirmation_classification_timeout = confirmation_classification_timeout
        self._caller_turn_epoch: int | None = None
        self.runtime = GuardedToolRuntime(
            harness,
            session_id=session_id,
            patient_id=patient_id,
            state_machine=state_machine,
        )
        self._transcript_fragments: list[str] = []
        self._transcript_lock = asyncio.Lock()
        self._tool_lock = asyncio.Lock()
        self._last_observation: PatientTurnObservation | None = None
        self._notification_reference: str | None = None
        self._notification_appointment_id: str | None = None
        self._confirmation_commit_receipt: dict[str, Any] | None = None
        self._confirmation_armed = True
        self._proposal_origin_turn_epoch: int | None = None
        self._proposal_origin_transcript: str | None = None
        self._call_direction: CallDirection | None = None

    @property
    def last_observation(self) -> PatientTurnObservation | None:
        return self._last_observation

    def begin_caller_turn(self, turn_epoch: int) -> None:
        """Revoke an in-flight snapshot as soon as the caller starts speaking."""

        self._caller_turn_epoch = turn_epoch

    @property
    def normal_conversation_active(self) -> bool:
        return self.state_machine.state is WorkflowState.ACTIVE

    @property
    def application_owns_completed_turn(self) -> bool:
        """Whether the next completed caller turn belongs to an application gate."""

        if self.state_machine.state in {
            WorkflowState.AWAITING_INTENT,
            WorkflowState.AWAITING_NAME_CONFIRMATION,
            WorkflowState.CONFIRMATION_PENDING,
            WorkflowState.AWAITING_FOLLOW_UP_DECISION,
            WorkflowState.COMPLETED,
        }:
            return True
        if self.state_machine.state is not WorkflowState.ACTIVE:
            return False
        # This is only a conservative routing hint: it suppresses an autonomous
        # Live reply while the semantic classifier decides.  It never authorizes
        # a state transition by itself.
        transcript = "".join(self._transcript_fragments).strip()
        return _looks_like_call_closure_candidate(transcript)

    @property
    def application_gate_progress_message(self) -> str | None:
        """Return a bounded caller-facing update for a potentially slow gate."""

        if self.state_machine.state is WorkflowState.AWAITING_NAME_CONFIRMATION:
            return "Thank you. I'm checking your identity confirmation now."
        if (
            self.state_machine.state is WorkflowState.CONFIRMATION_PENDING
            and self._confirmation_armed
        ):
            return "Thank you. I'm checking your confirmation now."
        return None

    async def bootstrap(self, call: CallDescriptor) -> VoiceSessionConfig:
        self._call_direction = call.direction
        transition_index = len(self.state_machine.transitions)
        if call.direction is CallDirection.OUTBOUND:
            pending_intent = None
            if call.context_reference:
                notification = self.harness.store.confirmation_calls.get(
                    call.context_reference
                )
                if notification is not None and notification.patient_id == self.patient_id:
                    self._notification_reference = notification.notification_id
                    self._notification_appointment_id = notification.appointment_id
                    notification_kind = (
                        "reschedule confirmation"
                        if notification.idempotency_key.startswith("reschedule-confirmed:")
                        else "booking confirmation"
                    )
                    pending_intent = (
                        f"This is a clinic-approval {notification_kind} call. After "
                        "identity is verified, use get_appointment for internal "
                        f"appointment_id {notification.appointment_id!r}; never say "
                        "that internal ID aloud. Tell the patient that clinic approval "
                        "is complete and read the full authoritative patient-facing "
                        "appointment details. Deliver this notification before asking "
                        "whether they need any other scheduling help."
                    )
            initial_commentary = self.state_machine.begin_outbound_call(
                assistant_name=self.prompt_context.assistant_name,
                clinic_name=self.prompt_context.clinic_name,
                pending_intent=pending_intent,
            )
        else:
            initial_commentary = self.state_machine.greeting(
                assistant_name=self.prompt_context.assistant_name,
                clinic_name=self.prompt_context.clinic_name,
            )
        if self._notification_reference is not None:
            await self._record(
                "notification.context_loaded",
                notification_reference=self._notification_reference,
                appointment_id=self._notification_appointment_id,
            )
        await self._record_new_transitions(transition_index)
        await self._record(
            "call.opening_selected",
            direction=call.direction.value,
            opening_state=self.state_machine.state.value,
        )
        return VoiceSessionConfig(
            instructions=(
                build_live_prompt(self.prompt_context)
                + "\n\n"
                + "Application workflow state is intentionally not copied into this "
                "startup prompt because it changes during the call. Treat the latest "
                "application instruction and every tool result as authoritative. "
                "They supersede older conversational assumptions. When a tool result "
                "includes application_directive, follow it before continuing. Do not "
                "paraphrase text marked say_exactly. Once the application says identity "
                "is confirmed, never describe it as pending and never ask for it again."
            ),
            backend_instructions=build_backend_prompt(self.prompt_context),
            initial_commentary=initial_commentary,
            # Identity is classified by the application-owned completed-turn
            # classifier, not by the scheduling backend. Exposing the same tool
            # here let later, unrelated turns trigger redundant interpretation.
            # Keep the handler's immutable replay guard for unexpected provider
            # events, but apply least privilege to the model-visible catalog.
            tools=tuple(
                schema
                for name, schema in BACKEND_TOOL_CATALOG.items()
                if name != IDENTITY_INTERPRETATION_TOOL_NAME
            ),
            context_reference=self.session_id,
        )

    async def observe_patient_transcript_delta(self, delta: str) -> str | None:
        """Accumulate transcript text without treating a partial as a full turn."""

        if not delta:
            return None
        async with self._transcript_lock:
            self._transcript_fragments.append(delta)
            accumulated = "".join(self._transcript_fragments).strip()
        if self.state_machine.requires_immediate_preemption(accumulated):
            observation = await self._flush_patient_turn()
            return observation.directive if observation else None
        return None

    async def complete_patient_turn(
        self, *, turn_epoch: int | None = None
    ) -> str | None:
        """Apply safety-critical completed turns even if Live never delegates.

        The media adapter supplies the turn boundary. Identity, exact-proposal
        confirmation, and post-task follow-up consent are consumed here; normal
        scheduling turns stay available to the delegated backend.
        """

        async with self._tool_lock:
            async with self._transcript_lock:
                transcript = "".join(self._transcript_fragments).strip()
            if not transcript:
                if (
                    self.state_machine.state is WorkflowState.CONFIRMATION_PENDING
                    and self._confirmation_armed
                    and self.runtime.pending is not None
                ):
                    directive = (
                        "say_exactly: I didn't catch your answer, so I have not made "
                        "the change. Do you confirm the exact proposal I just read? "
                        "Please say yes or no."
                    )
                    await self._record(
                        "confirmation.classification_recovery",
                        proposal_id=self.runtime.pending.proposal_id,
                        reason="completed_turn_transcript_missing",
                        turn_epoch=turn_epoch,
                    )
                    await self._record(
                        "patient.turn_processed",
                        transcript="",
                        transcript_characters=0,
                        workflow_before=self.state_machine.state.value,
                        workflow_after=self.state_machine.state.value,
                        directive_kind="say_exactly",
                        directive_digest=spoken_contract_digest(directive.split(":", 1)[1].strip()),
                        handoff_reference=None,
                    )
                    return directive
                return None
            if self.state_machine.state is WorkflowState.AWAITING_INTENT:
                observation = await self._flush_patient_turn()
                return observation.directive if observation else None
            if (
                self.state_machine.state is WorkflowState.ACTIVE
                and _looks_like_call_closure_candidate(transcript)
            ):
                observation = await self._classify_active_call_closure(transcript)
                return observation.directive if observation else None
            if self.state_machine.state not in {
                WorkflowState.AWAITING_NAME_CONFIRMATION,
                WorkflowState.CONFIRMATION_PENDING,
                WorkflowState.AWAITING_FOLLOW_UP_DECISION,
                WorkflowState.COMPLETED,
            }:
                async with self._transcript_lock:
                    current = "".join(self._transcript_fragments).strip()
                    self._transcript_fragments.clear()
                if current:
                    self.runtime.begin_patient_turn(current)
                    await self._record(
                        "patient.turn_observed",
                        transcript=current,
                        transcript_characters=len(current),
                        turn_epoch=turn_epoch,
                        workflow_before=self.state_machine.state.value,
                        workflow_after=self.state_machine.state.value,
                    )
                return None
            if self.state_machine.state is WorkflowState.CONFIRMATION_PENDING:
                if (
                    not self._confirmation_armed
                    or (
                        self._proposal_origin_turn_epoch is not None
                        and turn_epoch is not None
                        and turn_epoch <= self._proposal_origin_turn_epoch
                    )
                ):
                    observation = await self._handle_pre_confirmation_continuation(
                        transcript,
                        turn_epoch=turn_epoch,
                    )
                    return observation.directive if observation else None
                observation = await self._classify_confirmation_turn(
                    transcript,
                    turn_epoch=turn_epoch,
                )
                return observation.directive if observation else None
            if self.state_machine.state is WorkflowState.AWAITING_FOLLOW_UP_DECISION:
                observation = await self._classify_follow_up_turn(transcript)
                return observation.directive if observation else None
            if self.state_machine.state is WorkflowState.COMPLETED:
                observation = await self._classify_follow_up_turn(transcript)
                return observation.directive if observation else None

            decision = IdentityDecision.UNCLEAR
            try:
                if self.identity_classifier is None:
                    raise RuntimeError("identity classifier is not configured")
                decision = await self.identity_classifier.classify(
                    transcript=transcript,
                    expected_patient_name=self.state_machine.patient_display_name,
                )
            except Exception as exc:
                await self._record(
                    "identity.classification_failed",
                    error_class=type(exc).__name__,
                )
            async with self._transcript_lock:
                current_transcript = "".join(self._transcript_fragments).strip()
            if current_transcript != transcript:
                await self._record("identity.classification_stale")
                return None
            observation = await self._flush_patient_turn(
                identity_decision=decision,
                require_semantic_identity=True,
            )
            await self._record(
                "identity.completed_turn_classified",
                decision=decision.value,
                identity_key_locked=(
                    self.state_machine.identity_confirmation_key is not None
                ),
            )
            return observation.directive if observation else None

    async def handle_tool_intent(self, intent: ToolIntent) -> ToolResult:
        # The Responses backend is configured for serial tool calls, but the
        # application enforces the invariant independently of provider behavior.
        async with self._tool_lock:
            workflow_before = self.state_machine.state.value
            receipt = self._confirmation_commit_receipt
            if (
                receipt is not None
                and intent.tool_name == receipt["tool_name"]
                and dict(intent.arguments) == receipt["arguments"]
            ):
                replayed_result = dict(receipt["result"])
                replayed_status = self._tool_status(
                    OperationKind.WRITE, replayed_result
                )
                await self._record(
                    "confirmation.mutation_replayed",
                    operation_id=intent.operation_id,
                    proposal_id=receipt["proposal_id"],
                    tool_name=intent.tool_name,
                    domain_status=str(replayed_result.get("status", "failed")),
                )
                return ToolResult(
                    operation_id=intent.operation_id,
                    status=replayed_status,
                    payload={"result": replayed_result, "replayed": True},
                    error_class=(
                        None
                        if replayed_status is ToolStatus.SUCCEEDED
                        else str(
                            replayed_result.get("reason")
                            or replayed_result.get("status")
                            or "ToolRejected"
                        )
                    ),
                )
            has_pending_transcript = False
            if intent.tool_name == IDENTITY_INTERPRETATION_TOOL_NAME:
                async with self._transcript_lock:
                    has_pending_transcript = bool(
                        "".join(self._transcript_fragments).strip()
                    )
            if (
                intent.tool_name == IDENTITY_INTERPRETATION_TOOL_NAME
                and self.state_machine.identity_confirmation_key is not None
                and self.state_machine.state is not WorkflowState.AWAITING_NAME_CONFIRMATION
                and not has_pending_transcript
            ):
                result = {
                    "status": "ok",
                    "semantic_decision": "affirmed",
                    "workflow_state": self.state_machine.state.value,
                    "identity_confirmation_key": self.state_machine.identity_confirmation_key,
                    "immutable": True,
                    "evidence_digest": self.state_machine.identity_evidence_digest,
                    "replayed": True,
                }
                await self._record(
                    "identity.semantic_replay_ignored",
                    operation_id=intent.operation_id,
                )
                return ToolResult(
                    operation_id=intent.operation_id,
                    status=ToolStatus.SUCCEEDED,
                    payload={"result": result},
                )
            identity_decision = (
                self._identity_decision(intent)
                if intent.tool_name == IDENTITY_INTERPRETATION_TOOL_NAME
                else None
            )
            confirmation_write_blocked = False
            if self.state_machine.state is WorkflowState.AWAITING_FOLLOW_UP_DECISION:
                async with self._transcript_lock:
                    follow_up_transcript = "".join(self._transcript_fragments).strip()
                observation = (
                    await self._classify_follow_up_turn(follow_up_transcript)
                    if follow_up_transcript
                    else None
                )
            elif self.state_machine.state is WorkflowState.CONFIRMATION_PENDING:
                async with self._transcript_lock:
                    confirmation_transcript = "".join(
                        self._transcript_fragments
                    ).strip()
                if confirmation_transcript:
                    observation = await self._classify_confirmation_turn(
                        confirmation_transcript
                    )
                    receipt = self._confirmation_commit_receipt
                    if (
                        receipt is not None
                        and intent.tool_name == receipt["tool_name"]
                        and dict(intent.arguments) == receipt["arguments"]
                    ):
                        replayed_result = dict(receipt["result"])
                        replayed_status = self._tool_status(
                            OperationKind.WRITE, replayed_result
                        )
                        await self._record(
                            "confirmation.mutation_replayed",
                            operation_id=intent.operation_id,
                            proposal_id=receipt["proposal_id"],
                            tool_name=intent.tool_name,
                            domain_status=str(
                                replayed_result.get("status", "failed")
                            ),
                        )
                        payload: dict[str, Any] = {
                            "result": replayed_result,
                            "replayed": True,
                        }
                        if observation and observation.directive:
                            payload["application_directive"] = observation.directive
                        return ToolResult(
                            operation_id=intent.operation_id,
                            status=replayed_status,
                            payload=payload,
                            error_class=(
                                None
                                if replayed_status is ToolStatus.SUCCEEDED
                                else str(
                                    replayed_result.get("reason")
                                    or replayed_result.get("status")
                                    or "ToolRejected"
                                )
                            ),
                        )
                    confirmation_write_blocked = (
                        not self.runtime.confirmation_authorized_for_turn
                    )
                else:
                    observation = None
                # The application-owned completed-turn path may already have
                # consumed an unclear transcript. Authorization is durable for
                # the current exact proposal; the presence of buffered text is
                # not. Fail closed when a provider eagerly repeats the write.
                if intent.tool_name in WRITE_TOOLS:
                    confirmation_write_blocked = (
                        not self.runtime.confirmation_authorized_for_turn
                    )
            else:
                observation = await self._flush_patient_turn(
                    identity_decision=identity_decision,
                    require_semantic_identity=True,
                )
            transition_index = len(self.state_machine.transitions)
            if intent.tool_name == IDENTITY_INTERPRETATION_TOOL_NAME:
                result = self._identity_result(
                    workflow_before=workflow_before,
                    decision=identity_decision,
                    observation=observation,
                )
                await self._record(
                    "identity.semantic_interpreted",
                    operation_id=intent.operation_id,
                    decision=identity_decision.value,
                    identity_key_locked=(
                        self.state_machine.identity_confirmation_key is not None
                    ),
                    evidence_digest=self.state_machine.identity_evidence_digest,
                )
            elif confirmation_write_blocked and intent.tool_name in WRITE_TOOLS:
                result = {
                    "status": "rejected",
                    "reason": "exact_proposal_not_confirmed",
                    "workflow_state": self.state_machine.state.value,
                }
            else:
                result = await asyncio.to_thread(
                    self.runtime.execute,
                    intent.tool_name,
                    dict(intent.arguments),
                )
            if (
                intent.tool_name in WRITE_TOOLS
                and result.get("status") == "confirmation_required"
                and self.runtime.pending is not None
            ):
                self._confirmation_armed = False
                self._proposal_origin_turn_epoch = intent.state_revision
                self._proposal_origin_transcript = (
                    observation.transcript
                    if observation is not None
                    else (
                        self._last_observation.transcript
                        if self._last_observation is not None
                        else None
                    )
                )
                await self._record(
                    "confirmation.proposal_prepared_for_delivery",
                    proposal_id=self.runtime.pending.proposal_id,
                    origin_turn_epoch=self._proposal_origin_turn_epoch,
                )
            await self._record_new_transitions(transition_index)
        payload: dict[str, Any] = {"result": result}
        if observation and observation.directive:
            payload["application_directive"] = observation.directive
        if (
            result.get("status") == "confirmation_required"
            and self.runtime.pending is not None
        ):
            proposal_directive = self._proposal_confirmation_directive(
                self.runtime.pending.tool_name,
                self.runtime.pending.summary,
            )
            payload["application_directive"] = proposal_directive
            payload["application_directive_digest"] = spoken_contract_digest(
                proposal_directive.split(":", 1)[1].strip()
            )
        if (
            intent.tool_name == "search_slots"
            and result.get("status") == "ok"
            and not result.get("slots")
        ):
            payload["application_directive"] = result.get(
                "patient_facing_summary",
                "No matching slots were available. Ask for another day or time.",
            )
        status = self._tool_status(intent.operation_kind, result)
        error_class = None
        if status is not ToolStatus.SUCCEEDED:
            error_class = str(result.get("reason") or result.get("status") or "ToolRejected")
        await self._record(
            "tool.decision",
            operation_id=intent.operation_id,
            tool_name=intent.tool_name,
            operation_kind=intent.operation_kind.value,
            domain_status=str(result.get("status", "failed")),
            tool_status=status.value,
            workflow_before=workflow_before,
            workflow_after=self.state_machine.state.value,
            delegation_id=intent.delegation_id,
            result_count=(
                len(result.get("slots", ()))
                if intent.tool_name == "search_slots"
                else None
            ),
        )
        return ToolResult(
            operation_id=intent.operation_id,
            status=status,
            payload=payload,
            error_class=error_class,
        )

    async def reconcile(self, operation_id: str) -> ToolResult:
        """Return an explicit unknown result; writes are never blindly replayed."""

        return ToolResult(
            operation_id=operation_id,
            status=ToolStatus.UNKNOWN,
            payload={
                "status": "reconciliation_required",
                "instruction": "Read authoritative appointment state before retrying.",
            },
            error_class="ReconciliationRequired",
        )

    async def _flush_patient_turn(
        self,
        *,
        identity_decision: IdentityDecision | None = None,
        require_semantic_identity: bool = True,
        confirmation_decision: ConfirmationDecision | None = None,
        require_semantic_confirmation: bool = False,
        follow_up_decision: FollowUpDecision | None = None,
        require_semantic_follow_up: bool = False,
        expected_transcript: str | None = None,
        expected_proposal_id: str | None = None,
        expected_turn_epoch: int | None = None,
    ) -> PatientTurnObservation | None:
        async with self._transcript_lock:
            if not self._transcript_fragments:
                return None
            transcript = "".join(self._transcript_fragments).strip()
            if (
                (expected_transcript is not None and transcript != expected_transcript)
                or (
                    expected_turn_epoch is not None
                    and self._caller_turn_epoch is not None
                    and expected_turn_epoch != self._caller_turn_epoch
                )
                or (
                    expected_proposal_id is not None
                    and (
                        self.runtime.pending is None
                        or self.runtime.pending.proposal_id != expected_proposal_id
                        or self.state_machine.state is not WorkflowState.CONFIRMATION_PENDING
                    )
                )
            ):
                return None
            self._transcript_fragments.clear()
        if not transcript:
            return None

        workflow_before = self.state_machine.state.value
        transition_index = len(self.state_machine.transitions)
        pending_before = self.runtime.pending
        confirmation_commit_event: dict[str, Any] | None = None
        self.runtime.begin_patient_turn(
            transcript,
            confirmation_decision=confirmation_decision,
            require_semantic_confirmation=require_semantic_confirmation,
        )
        directive = self.state_machine.accept_patient_turn(
            transcript,
            authorize_name_confirmation=lambda confirmed: (
                self.harness.confirm_identity_by_name(
                    session_id=self.session_id,
                    patient_id=self.patient_id,
                    confirmed=confirmed,
                )
            ),
            authorize_proxy=lambda caller_name, relationship: (
                self.harness.confirm_proxy_authority(
                    session_id=self.session_id,
                    patient_id=self.patient_id,
                    caller_display_name=caller_name,
                    relationship=relationship,
                )
            ),
            identity_decision=identity_decision,
            require_semantic_identity=require_semantic_identity,
            follow_up_decision=follow_up_decision,
            require_semantic_follow_up=require_semantic_follow_up,
        )
        text = (
            f"say_exactly: {directive.reply}"
            if directive.reply is not None
            else directive.model_input
        )
        if (
            identity_decision is IdentityDecision.AFFIRMED
            and workflow_before == WorkflowState.AWAITING_NAME_CONFIRMATION.value
            and self.state_machine.state is WorkflowState.ACTIVE
            and self._notification_appointment_id is not None
        ):
            notification_reference = self._notification_reference
            appointment_id = self._notification_appointment_id
            try:
                appointment_result = await asyncio.to_thread(
                    self.runtime.execute,
                    "get_appointment",
                    {"appointment_id": appointment_id},
                )
                if appointment_result.get("status") != "ok":
                    raise RuntimeError("authoritative appointment read was rejected")
                summary = str(
                    appointment_result["appointment"]["patient_facing_summary"]
                )
                text = (
                    "say_exactly: Thank you for confirming your identity. "
                    f"{summary} {POST_TASK_HELP_OFFER}"
                )
                self.state_machine.notification_delivered()
                await self._record(
                    "notification.context_resolved",
                    notification_reference=notification_reference,
                    appointment_id=appointment_id,
                    directive_kind="say_exactly",
                )
            except Exception as exc:
                text = (
                    "say_exactly: Thank you for confirming your identity. I cannot "
                    "safely retrieve the approved appointment details right now. "
                    "The clinic team will follow up; I will not guess the details."
                )
                self.state_machine.notification_unresolved()
                await self._record(
                    "notification.context_resolution_failed",
                    notification_reference=notification_reference,
                    error_class=type(exc).__name__,
                )
            finally:
                # A notification is delivered at most once per call. Subsequent
                # turns are ordinary verified scheduling turns.
                self._notification_reference = None
                self._notification_appointment_id = None
        elif (
            identity_decision is IdentityDecision.AFFIRMED
            and workflow_before == WorkflowState.AWAITING_NAME_CONFIRMATION.value
            and self.state_machine.state is WorkflowState.ACTIVE
            and self._call_direction is CallDirection.OUTBOUND
        ):
            text = (
                "say_exactly: Thank you. Your identity is confirmed. I'm ready to "
                "help with this scheduling callback. What would you like to check or "
                "change about your appointment?"
            )
            await self._record(
                "identity.continuation_prepared",
                direction=CallDirection.OUTBOUND.value,
                purpose="scheduling_callback",
                identity_key_locked=True,
            )
        if require_semantic_confirmation and confirmation_decision is not None:
            if confirmation_decision is ConfirmationDecision.CONFIRMED:
                if pending_before is None:
                    result: Mapping[str, Any] = {
                        "status": "rejected",
                        "reason": "confirmed_proposal_missing",
                    }
                    tool_name = "unknown"
                    proposal_id = "missing"
                else:
                    tool_name = pending_before.tool_name
                    proposal_id = pending_before.proposal_id
                    result = await asyncio.to_thread(
                        self.runtime.execute,
                        tool_name,
                        dict(pending_before.arguments),
                    )
                domain_status = str(result.get("status", "failed"))
                tool_status = self._tool_status(OperationKind.WRITE, result)
                if (
                    tool_status is ToolStatus.SUCCEEDED
                    and domain_status not in {"proposed", "verified"}
                ):
                    tool_status = ToolStatus.FAILED
                confirmation_commit_event = {
                    "operation_id": f"confirmation:{proposal_id}",
                    "proposal_id": proposal_id,
                    "tool_name": tool_name,
                    "domain_status": domain_status,
                    "tool_status": tool_status.value,
                }
                self._confirmation_commit_receipt = {
                    "proposal_id": proposal_id,
                    "tool_name": tool_name,
                    "arguments": (
                        dict(pending_before.arguments) if pending_before else {}
                    ),
                    "result": dict(result),
                }
                if tool_status is ToolStatus.SUCCEEDED:
                    summary = str(
                        result.get("patient_facing_summary")
                        or "Your confirmed request has been recorded."
                    )
                    text = f"say_exactly: {summary}"
                elif tool_status is ToolStatus.UNKNOWN:
                    text = (
                        "say_exactly: I could not verify whether the request was "
                        "completed. I will not submit it again. The clinic must "
                        "check the appointment record before any retry."
                    )
                else:
                    text = (
                        "say_exactly: I could not complete that request. I have not "
                        "claimed that the appointment changed. Please contact the "
                        "clinic or ask me to connect you with the front desk."
                    )
            elif confirmation_decision is ConfirmationDecision.UNCLEAR:
                proposal = (
                    pending_before.summary
                    if pending_before is not None
                    else "the pending appointment request"
                )
                text = (
                    "say_exactly: I heard an unclear answer, so I have not made the "
                    f"change. Here is the exact proposal: {proposal}. Do you confirm "
                    "this exact proposal? Please say yes or no."
                )
            elif confirmation_decision is ConfirmationDecision.DENIED:
                text = (
                    "Tell the caller the proposed change was not made, then ask what "
                    "they would like to do instead."
                )
            else:
                text = (
                    "The caller corrected the proposal. Do not apply the old proposal. "
                    f"Continue from this correction: {transcript}"
                )
        if (
            require_semantic_follow_up
            and follow_up_decision is FollowUpDecision.DECLINED
        ):
            text = "say_exactly: Thank you. Take care."
        handoff_reference: str | None = None
        if directive.handoff_reason:
            handoff = self.harness.request_general_handoff(
                GeneralHandoffCommand(
                    session_id=self.session_id,
                    patient_id=self.patient_id,
                    reason=directive.handoff_reason,
                    topics=("caller_requested_assistance",),
                    idempotency_key=f"{self.session_id}:handoff:voice",
                )
            )
            accepted = handoff.outcome.value in {"accepted", "pending"}
            self.state_machine.handoff_resolved(accepted=accepted)
            handoff_reference = handoff.handoff_id
            text = (
                "say_exactly: The front-desk request was accepted. Your reference is "
                f"{handoff.handoff_id}."
                if accepted
                else "say_exactly: The automated handoff failed. Please call the clinic directly."
            )
        observation = PatientTurnObservation(
            transcript=transcript,
            directive=text,
            handoff_reference=handoff_reference,
        )
        self._last_observation = observation
        await self._record_new_transitions(transition_index)
        if confirmation_commit_event is not None:
            await self._record(
                "confirmation.mutation_executed",
                **confirmation_commit_event,
            )
            await self._record(
                "tool.decision",
                operation_id=confirmation_commit_event["operation_id"],
                tool_name=confirmation_commit_event["tool_name"],
                operation_kind=OperationKind.WRITE.value,
                domain_status=confirmation_commit_event["domain_status"],
                tool_status=confirmation_commit_event["tool_status"],
                workflow_before=workflow_before,
                workflow_after=self.state_machine.state.value,
                delegation_id=None,
                result_count=None,
                application_owned=True,
            )
        directive_kind = (
            "handoff"
            if handoff_reference
            else "say_exactly"
            if isinstance(text, str)
            and text.casefold().startswith("say_exactly:")
            else "application_reply"
            if directive.reply
            else "model_input"
        )
        directive_digest = None
        if directive_kind == "say_exactly" and isinstance(text, str):
            directive_digest = spoken_contract_digest(text.split(":", 1)[1].strip())
        await self._record(
            "patient.turn_processed",
            transcript=transcript,
            transcript_characters=len(transcript),
            workflow_before=workflow_before,
            workflow_after=self.state_machine.state.value,
            directive_kind=directive_kind,
            directive_digest=directive_digest,
            handoff_reference=handoff_reference,
        )
        return observation

    async def arm_pending_confirmation(
        self, *, after_turn_epoch: int | None
    ) -> None:
        """Arm one exact proposal only after its authoritative prompt is accepted."""

        async with self._tool_lock:
            pending = self.runtime.pending
            if (
                pending is None
                or self.state_machine.state is not WorkflowState.CONFIRMATION_PENDING
            ):
                await self._record(
                    "confirmation.proposal_arm_skipped",
                    after_turn_epoch=after_turn_epoch,
                )
                return
            self._confirmation_armed = True
            if after_turn_epoch is not None:
                self._proposal_origin_turn_epoch = max(
                    self._proposal_origin_turn_epoch or 0, after_turn_epoch
                )
            await self._record(
                "confirmation.proposal_armed",
                proposal_id=pending.proposal_id,
                after_turn_epoch=self._proposal_origin_turn_epoch,
            )

    async def _handle_pre_confirmation_continuation(
        self,
        transcript: str,
        *,
        turn_epoch: int | None,
    ) -> PatientTurnObservation | None:
        """Rejoin speech that began before the patient could hear the proposal."""

        stale = False
        async with self._transcript_lock:
            current_transcript = "".join(self._transcript_fragments).strip()
            if current_transcript != transcript:
                stale = True
            else:
                self._transcript_fragments.clear()
        if stale:
            await self._record("confirmation.pre_delivery_continuation_stale")
            return None

        origin = (self._proposal_origin_transcript or "").strip()
        if origin and transcript.casefold() not in origin.casefold():
            consolidated = f"{origin} {transcript}".strip()
        else:
            consolidated = origin or transcript
        workflow_before = self.state_machine.state.value
        transition_index = len(self.state_machine.transitions)
        proposal_id = self.runtime.pending.proposal_id if self.runtime.pending else None
        origin_turn_epoch = self._proposal_origin_turn_epoch
        self.runtime.begin_patient_turn(
            consolidated,
            confirmation_decision=ConfirmationDecision.CORRECTION,
            require_semantic_confirmation=True,
        )
        self._confirmation_armed = True
        self._proposal_origin_turn_epoch = None
        self._proposal_origin_transcript = None
        await self._record_new_transitions(transition_index)
        directive = (
            "Application event: the caller continued the same request before the "
            "confirmation proposal was delivered. Treat the following as one "
            f"consolidated request: {consolidated}. Briefly acknowledge that you are "
            "checking this request now, then continue the scheduling task. Do not "
            "claim that any appointment has changed."
        )
        observation = PatientTurnObservation(
            transcript=consolidated,
            directive=directive,
        )
        self._last_observation = observation
        await self._record(
            "patient.turn_processed",
            transcript=consolidated,
            transcript_characters=len(consolidated),
            workflow_before=workflow_before,
            workflow_after=self.state_machine.state.value,
            directive_kind="model_input",
            directive_digest=None,
            handoff_reference=None,
        )
        await self._record(
            "confirmation.pre_delivery_continuation",
            proposal_id=proposal_id,
            origin_turn_epoch=origin_turn_epoch,
            continuation_turn_epoch=turn_epoch,
        )
        return observation

    @staticmethod
    def _proposal_confirmation_directive(tool_name: str, summary: str) -> str:
        action = {
            "create_appointment": "booking",
            "edit_appointment": "rescheduling",
            "delete_appointment": "cancellation",
        }.get(tool_name, "appointment request")
        consequence = {
            "booking": "I will submit it for clinic approval.",
            "rescheduling": (
                "Your current appointment stays in place until the clinic approves "
                "the change."
            ),
            "cancellation": "The appointment will be cancelled.",
            "appointment request": "I will submit only the request you confirm.",
        }[action]
        return (
            f"say_exactly: Please confirm this {action} request: {summary}. "
            f"{consequence} Please say yes or no."
        )

    async def _classify_follow_up_turn(
        self, transcript: str
    ) -> PatientTurnObservation | None:
        decision = FollowUpDecision.UNCLEAR
        try:
            if self.follow_up_classifier is None:
                raise RuntimeError("follow-up classifier is not configured")
            decision = await self.follow_up_classifier.classify_follow_up(
                transcript=transcript,
                offered_help=POST_TASK_HELP_OFFER,
            )
        except Exception as exc:
            await self._record(
                "follow_up.classification_failed",
                error_class=type(exc).__name__,
            )
        async with self._transcript_lock:
            current_transcript = "".join(self._transcript_fragments).strip()
        if current_transcript != transcript:
            await self._record("follow_up.classification_stale")
            return None
        observation = await self._flush_patient_turn(
            follow_up_decision=decision,
            require_semantic_follow_up=True,
        )
        await self._record(
            "follow_up.completed_turn_classified",
            decision=decision.value,
            tools_unlocked=self.state_machine.tools_allowed,
        )
        return observation

    async def _classify_active_call_closure(
        self, transcript: str
    ) -> PatientTurnObservation | None:
        """Semantically decide whether an active, mutation-free call is over."""

        decision = FollowUpDecision.UNCLEAR
        try:
            if self.follow_up_classifier is None:
                raise RuntimeError("call-closure classifier is not configured")
            decision = await self.follow_up_classifier.classify_follow_up(
                transcript=transcript,
                offered_help=(
                    "No scheduling change is pending. Decide whether the caller is "
                    "explicitly ending the call or is continuing with another request."
                ),
            )
        except Exception as exc:
            await self._record(
                "call_closure.classification_failed",
                error_class=type(exc).__name__,
            )
        stale = False
        async with self._transcript_lock:
            current_transcript = "".join(self._transcript_fragments).strip()
            if current_transcript != transcript:
                stale = True
            else:
                self._transcript_fragments.clear()
        if stale:
            await self._record("call_closure.classification_stale")
            return None

        workflow_before = self.state_machine.state.value
        transition_index = len(self.state_machine.transitions)
        self.runtime.begin_patient_turn(transcript)
        if decision is FollowUpDecision.DECLINED:
            self.state_machine.patient_ended_call()
            directive = "say_exactly: Thank you for calling. Take care."
            directive_kind = "say_exactly"
            digest = spoken_contract_digest("Thank you for calling. Take care.")
        else:
            directive = (
                "The caller did not clearly end the call. Respond naturally to this "
                f"completed turn without assuming a scheduling change: {transcript}"
            )
            directive_kind = "model_input"
            digest = None

        observation = PatientTurnObservation(
            transcript=transcript,
            directive=directive,
        )
        self._last_observation = observation
        await self._record_new_transitions(transition_index)
        await self._record(
            "patient.turn_processed",
            transcript=transcript,
            transcript_characters=len(transcript),
            workflow_before=workflow_before,
            workflow_after=self.state_machine.state.value,
            directive_kind=directive_kind,
            directive_digest=digest,
            handoff_reference=None,
        )
        await self._record(
            "call_closure.completed_turn_classified",
            decision=decision.value,
            terminal=(decision is FollowUpDecision.DECLINED),
        )
        return observation

    async def _classify_confirmation_turn(
        self,
        transcript: str,
        *,
        turn_epoch: int | None = None,
    ) -> PatientTurnObservation | None:
        pending = self.runtime.pending
        if pending is None:
            await self._record("confirmation.pending_proposal_missing")
            return None
        observation = None
        # A speech-end event is not a transcript-final event. Classify an
        # immutable snapshot, then compare AND consume it atomically. One late
        # revision gets one restart; further churn gets a spoken recovery.
        for attempt in range(2):
            decision = ConfirmationDecision.UNCLEAR
            try:
                if self.confirmation_classifier is None:
                    raise RuntimeError("confirmation classifier is not configured")
                decision = await asyncio.wait_for(
                    self.confirmation_classifier.classify_confirmation(
                        transcript=transcript,
                        exact_proposal=pending.summary,
                    ),
                    timeout=self._confirmation_classification_timeout,
                )
            except Exception as exc:
                await self._record(
                    "confirmation.classification_failed",
                    proposal_id=pending.proposal_id,
                    error_class=type(exc).__name__,
                )
            observation = await self._flush_patient_turn(
                confirmation_decision=decision,
                require_semantic_confirmation=True,
                expected_transcript=transcript,
                expected_proposal_id=pending.proposal_id,
                expected_turn_epoch=turn_epoch,
            )
            if observation is not None:
                break
            await self._record(
                "confirmation.classification_stale",
                proposal_id=pending.proposal_id,
                attempt=attempt + 1,
                turn_epoch=turn_epoch,
            )
            if (
                self.runtime.pending is not pending
                or self.state_machine.state is not WorkflowState.CONFIRMATION_PENDING
                or (
                    turn_epoch is not None
                    and self._caller_turn_epoch is not None
                    and turn_epoch != self._caller_turn_epoch
                )
            ):
                # A newer speech turn / urgent or human preemption owns recovery.
                return None
            async with self._transcript_lock:
                transcript = "".join(self._transcript_fragments).strip()
            if attempt == 0:
                await self._record(
                    "confirmation.classification_restarted",
                    proposal_id=pending.proposal_id,
                    turn_epoch=turn_epoch,
                    transcript_characters=len(transcript),
                )
        if observation is None:
            decision = ConfirmationDecision.UNCLEAR
            observation = await self._flush_patient_turn(
                confirmation_decision=decision,
                require_semantic_confirmation=True,
                expected_proposal_id=pending.proposal_id,
                expected_turn_epoch=turn_epoch,
            )
            await self._record(
                "confirmation.classification_recovery",
                proposal_id=pending.proposal_id,
                reason="transcript_revision_limit",
                turn_epoch=turn_epoch,
            )
        if decision is not ConfirmationDecision.UNCLEAR:
            self._confirmation_armed = True
            self._proposal_origin_turn_epoch = None
            self._proposal_origin_transcript = None
        await self._record(
            "confirmation.semantic_interpreted",
            proposal_id=pending.proposal_id,
            decision=decision.value,
            turn_epoch=turn_epoch,
            authorized_for_turn=(decision is ConfirmationDecision.CONFIRMED),
            mutation_executed=(
                decision is ConfirmationDecision.CONFIRMED
                and self.runtime.pending is None
            ),
        )
        return observation

    @staticmethod
    def _identity_decision(intent: ToolIntent) -> IdentityDecision:
        supplied = intent.arguments.get("decision")
        try:
            return IdentityDecision(str(supplied))
        except ValueError as exc:
            raise ValueError("identity decision must be affirmed, denied, or unclear") from exc

    def _identity_result(
        self,
        *,
        workflow_before: str,
        decision: IdentityDecision,
        observation: PatientTurnObservation | None,
    ) -> dict[str, Any]:
        if observation is None:
            return {
                "status": "rejected",
                "reason": "identity_interpretation_has_no_patient_turn",
            }
        if workflow_before != WorkflowState.AWAITING_NAME_CONFIRMATION.value:
            return {
                "status": "rejected",
                "reason": "identity_confirmation_is_immutable_or_not_requested",
                "identity_confirmation_key": "locked"
                if self.state_machine.identity_confirmation_key
                else "unset",
            }
        payload: dict[str, Any] = {
            "status": "ok",
            "semantic_decision": decision.value,
            "workflow_state": self.state_machine.state.value,
            "identity_confirmation_key": "unset",
        }
        if self.state_machine.identity_confirmation_key:
            payload.update(
                {
                    "identity_confirmation_key": self.state_machine.identity_confirmation_key,
                    "immutable": True,
                    "evidence_digest": self.state_machine.identity_evidence_digest,
                }
            )
        return payload

    async def _record_new_transitions(self, start_index: int) -> None:
        for transition in self.state_machine.transitions[start_index:]:
            await self._record(
                "workflow.transition",
                sequence=transition.sequence,
                previous=transition.previous.value,
                current=transition.current.value,
                reason=transition.reason,
            )

    async def _record(self, event_type: str, **metadata: Any) -> None:
        if self.event_sink is None:
            return
        try:
            await self.event_sink.record(
                event_type,
                session_id=self.session_id,
                workflow_state=self.state_machine.state.value,
                **metadata,
            )
        except Exception:
            return

    @staticmethod
    def _tool_status(
        operation_kind: OperationKind, result: Mapping[str, Any]
    ) -> ToolStatus:
        status = str(result.get("status", "failed"))
        successful = {
            "ok",
            "verified",
            "proposed",
            "confirmation_required",
            "accepted",
            "pending",
        }
        if status in successful:
            return ToolStatus.SUCCEEDED
        if operation_kind is OperationKind.WRITE and status in {
            "unknown",
            "timeout",
            "reconciliation_required",
        }:
            return ToolStatus.UNKNOWN
        return ToolStatus.FAILED


def tool_intent_from_live_call(
    *,
    operation_id: str,
    tool_name: str,
    arguments_json: str | Mapping[str, Any],
    delegation_id: str | None,
) -> ToolIntent:
    """Validate a provider function call before it enters the actor mailbox."""

    if tool_name not in BACKEND_TOOL_CATALOG:
        raise ValueError(f"unknown tool: {tool_name}")
    arguments = (
        json.loads(arguments_json)
        if isinstance(arguments_json, str)
        else dict(arguments_json)
    )
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must decode to an object")
    return ToolIntent(
        operation_id=operation_id,
        tool_name=tool_name,
        operation_kind=(
            OperationKind.WRITE if tool_name in WRITE_TOOLS else OperationKind.READ
        ),
        arguments=arguments,
        delegation_id=delegation_id,
    )
