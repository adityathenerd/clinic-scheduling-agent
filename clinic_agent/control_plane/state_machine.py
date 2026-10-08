"""Application-owned conversation state for the scheduling workflow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import re
import secrets
from typing import Callable


class WorkflowState(str, Enum):
    AWAITING_INTENT = "awaiting_intent"
    AWAITING_NAME_CONFIRMATION = "awaiting_name_confirmation"
    ACTIVE = "active"
    CONFIRMATION_PENDING = "confirmation_pending"
    AWAITING_FOLLOW_UP_DECISION = "awaiting_follow_up_decision"
    COMPLETED = "completed"
    IDENTITY_REJECTED = "identity_rejected"
    RECOVERY_REQUIRED = "recovery_required"
    URGENT_HANDOFF = "urgent_handoff"
    AWAITING_PROXY_DETAILS = "awaiting_proxy_details"
    HUMAN_HANDOFF_PENDING = "human_handoff_pending"
    HUMAN_HANDOFF_COMPLETED = "human_handoff_completed"
    HUMAN_HANDOFF_FAILED = "human_handoff_failed"


class IdentityDecision(str, Enum):
    """Semantic meaning of the caller's answer to the active identity question."""

    AFFIRMED = "affirmed"
    DENIED = "denied"
    UNCLEAR = "unclear"


class ConfirmationDecision(str, Enum):
    """Semantic meaning of a caller turn against one exact pending proposal."""

    CONFIRMED = "confirmed"
    DENIED = "denied"
    CORRECTION = "correction"
    UNCLEAR = "unclear"


class FollowUpDecision(str, Enum):
    """Semantic meaning of a caller turn after an explicit offer of more help."""

    ACCEPTED = "accepted"
    DECLINED = "declined"
    NEW_REQUEST = "new_request"
    UNCLEAR = "unclear"


POST_TASK_HELP_OFFER = (
    "Would you like any other scheduling or clinic information help?"
)


@dataclass(frozen=True, slots=True)
class StateTransition:
    sequence: int
    previous: WorkflowState
    current: WorkflowState
    reason: str


@dataclass(frozen=True, slots=True)
class TurnDirective:
    reply: str | None = None
    model_input: str | None = None
    handoff_reason: str | None = None


_AFFIRMATIVE_NAME_CONFIRMATIONS = frozenset(
    {
        "speaking",
        "yes",
        "yes i am",
        "yes speaking",
    }
)
_NEGATIVE_NAME_CONFIRMATIONS = frozenset(
    {"no", "no i am not", "wrong person"}
)


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


class ConversationStateMachine:
    """Own state transitions and keep protected tools locked until identity."""

    def __init__(
        self,
        *,
        patient_display_name: str,
        approved_urgent_message: str = (
            "I can't assess urgent symptoms. Please contact local emergency services "
            "now, or have someone nearby help you do so."
        ),
    ) -> None:
        if not patient_display_name.strip():
            raise ValueError("patient_display_name is required")
        self.patient_display_name = patient_display_name
        self.approved_urgent_message = approved_urgent_message
        self.state = WorkflowState.AWAITING_INTENT
        self.pending_intent: str | None = None
        self.authority_mode: str | None = None
        self._identity_confirmation_key: str | None = None
        self._identity_evidence_digest: str | None = None
        self.transitions: list[StateTransition] = []

    @property
    def identity_confirmation_key(self) -> str | None:
        """Opaque application-minted lock; model output can never overwrite it."""

        return self._identity_confirmation_key

    @property
    def identity_evidence_digest(self) -> str | None:
        return self._identity_evidence_digest

    def greeting(self, *, assistant_name: str, clinic_name: str) -> str:
        return (
            f"Hello, this is {assistant_name}, the scheduling assistant for "
            f"{clinic_name}. How can I help with your appointment today?"
        )

    def begin_outbound_call(
        self,
        *,
        assistant_name: str,
        clinic_name: str,
        pending_intent: str | None = None,
    ) -> str:
        """Open an outbound scheduling call without pretending the patient called us."""

        if self.state is not WorkflowState.AWAITING_INTENT:
            raise RuntimeError("outbound call can only begin from awaiting_intent")
        self.pending_intent = pending_intent or (
            "Review the upcoming appointment and help with any requested "
            "scheduling change."
        )
        self._transition(
            WorkflowState.AWAITING_NAME_CONFIRMATION,
            "outbound_scheduling_call_started",
        )
        return (
            f"Hello, this is {assistant_name}, the scheduling assistant for "
            f"{clinic_name}. I'm calling about a scheduling matter. Before I "
            f"share any details, am I speaking with {self.patient_display_name}?"
        )

    def accept_patient_turn(
        self,
        utterance: str,
        *,
        authorize_name_confirmation: Callable[[bool], bool],
        authorize_proxy: Callable[[str, str], bool] | None = None,
        identity_decision: IdentityDecision | None = None,
        require_semantic_identity: bool = False,
        follow_up_decision: FollowUpDecision | None = None,
        require_semantic_follow_up: bool = False,
    ) -> TurnDirective:
        if not utterance.strip():
            return TurnDirective(reply="I didn't catch that. Could you say it again?")

        if self._contains_urgent_signal(utterance):
            self._transition(
                WorkflowState.URGENT_HANDOFF,
                "configured_urgent_signal_detected",
            )
            return TurnDirective(reply=self.approved_urgent_message)

        if self._requests_human(utterance):
            self._transition(
                WorkflowState.HUMAN_HANDOFF_PENDING,
                "caller_requested_human",
            )
            return TurnDirective(handoff_reason="caller_requested_human")

        if self.state is WorkflowState.AWAITING_INTENT:
            self.pending_intent = utterance.strip()
            self._transition(
                WorkflowState.AWAITING_NAME_CONFIRMATION,
                "scheduling_intent_captured",
            )
            intent = self._intent_label(utterance)
            return TurnDirective(
                reply=(
                    f"I can help with {intent}. Before I access the patient record, "
                    f"am I speaking with {self.patient_display_name}?"
                )
            )

        if self.state is WorkflowState.AWAITING_NAME_CONFIRMATION:
            decision = identity_decision
            if decision is None and not require_semantic_identity:
                # Credential-free mock/text compatibility only. The live voice path
                # requires the Responses backend's strict semantic tool instead of
                # growing this phrase list.
                normalized = _normalize(utterance)
                patient_name = _normalize(self.patient_display_name)
                patient_first_name = patient_name.split()[0]
                affirmative = _AFFIRMATIVE_NAME_CONFIRMATIONS.union(
                    {
                        "yes it is",
                        "yes that is me",
                        "yes thats me",
                        f"this is {patient_first_name}",
                        f"this is {patient_name}",
                        f"this is {patient_first_name} speaking",
                        f"this is {patient_name} speaking",
                        f"yes this is {patient_first_name}",
                        f"yes this is {patient_name}",
                        f"yes this is {patient_first_name} speaking",
                        f"yes this is {patient_name} speaking",
                        f"yes i am {patient_first_name}",
                        f"yes i am {patient_name}",
                    }
                )
                negative = _NEGATIVE_NAME_CONFIRMATIONS.union(
                    {
                        f"no i am not {patient_first_name}",
                        f"no i am not {patient_name}",
                        f"no this is not {patient_first_name}",
                        f"no this is not {patient_name}",
                        f"this is not {patient_first_name}",
                        f"this is not {patient_name}",
                    }
                )
                if normalized in affirmative:
                    decision = IdentityDecision.AFFIRMED
                elif normalized in negative:
                    decision = IdentityDecision.DENIED
                else:
                    decision = IdentityDecision.UNCLEAR

            if decision is IdentityDecision.AFFIRMED:
                if not authorize_name_confirmation(True):
                    self._transition(
                        WorkflowState.IDENTITY_REJECTED,
                        "name_confirmation_rejected_by_backend",
                    )
                    return TurnDirective(
                        reply=(
                            "I couldn't verify the patient record, so I won't access "
                            "or change any appointment information."
                        )
                    )
                self._transition(
                    WorkflowState.ACTIVE, "name_confirmation_accepted"
                )
                self.authority_mode = "patient_name_confirmation"
                if self._identity_confirmation_key is None:
                    self._identity_confirmation_key = (
                        "identity_" + secrets.token_urlsafe(24)
                    )
                    self._identity_evidence_digest = sha256(
                        utterance.encode("utf-8")
                    ).hexdigest()
                intent = self.pending_intent or "Help with the appointment."
                self.pending_intent = None
                return TurnDirective(
                    model_input=(
                        f"Application event: the caller confirmed they are "
                        f"{self.patient_display_name}. Briefly acknowledge the "
                        f"confirmation, then continue with the caller's earlier "
                        f"request: {intent}"
                    )
                )
            if decision is IdentityDecision.DENIED:
                authorize_name_confirmation(False)
                self._transition(
                    WorkflowState.AWAITING_PROXY_DETAILS,
                    "caller_denied_patient_identity",
                )
                return TurnDirective(
                    reply=(
                        "Thanks for clarifying. If you're an authorized caregiver or "
                        "proxy, please say your full name and relationship, for example: "
                        "Rohan Rao, spouse. You can also ask for the front desk."
                    )
                )
            return TurnDirective(
                reply=(
                    f"For privacy, please answer yes or no: are you "
                    f"{self.patient_display_name}?"
                )
            )

        if self.state is WorkflowState.AWAITING_PROXY_DETAILS:
            parsed = self._parse_proxy_details(utterance)
            if parsed is None:
                return TurnDirective(
                    reply=(
                        "Please provide the caller's full name followed by the "
                        "relationship, such as: Rohan Rao, spouse."
                    )
                )
            caller_name, relationship = parsed
            if authorize_proxy and authorize_proxy(caller_name, relationship):
                self.authority_mode = "authorized_proxy"
                self._transition(
                    WorkflowState.ACTIVE, "stored_proxy_authority_confirmed"
                )
                intent = self.pending_intent or "Help with the appointment."
                self.pending_intent = None
                return TurnDirective(
                    model_input=(
                        f"Application event: stored authority confirms {caller_name} "
                        f"is an authorized {relationship} for "
                        f"{self.patient_display_name}. Briefly acknowledge that "
                        f"authorization, then continue with the earlier request: {intent}"
                    )
                )
            self._transition(
                WorkflowState.HUMAN_HANDOFF_PENDING,
                "proxy_authority_not_found",
            )
            return TurnDirective(handoff_reason="proxy_authority_unverified")

        if self.state in {
            WorkflowState.ACTIVE,
            WorkflowState.CONFIRMATION_PENDING,
            WorkflowState.RECOVERY_REQUIRED,
        }:
            return TurnDirective(model_input=utterance)

        if self.state is WorkflowState.AWAITING_FOLLOW_UP_DECISION:
            decision = follow_up_decision
            if decision is None and not require_semantic_follow_up:
                normalized = _normalize(utterance)
                if normalized in {"yes", "yes please", "sure", "please do"}:
                    decision = FollowUpDecision.ACCEPTED
                elif normalized in {"no", "no thanks", "thank you", "thanks", "goodbye", "bye"}:
                    decision = FollowUpDecision.DECLINED
                elif any(
                    word in normalized
                    for word in (
                        "book",
                        "schedule",
                        "reschedule",
                        "cancel",
                        "appointment",
                        "parking",
                        "insurance",
                        "prerequisite",
                        "clinic",
                    )
                ):
                    decision = FollowUpDecision.NEW_REQUEST
                else:
                    decision = FollowUpDecision.UNCLEAR
            if decision is FollowUpDecision.DECLINED:
                self._transition(WorkflowState.COMPLETED, "follow_up_declined")
                return TurnDirective(reply="Understood. No further scheduling action will be taken.")
            if decision in {FollowUpDecision.ACCEPTED, FollowUpDecision.NEW_REQUEST}:
                self._transition(WorkflowState.ACTIVE, "follow_up_accepted")
                if decision is FollowUpDecision.NEW_REQUEST:
                    return TurnDirective(
                        model_input=(
                            "Application event: the caller explicitly stated another "
                            "supported appointment or clinic-information request. "
                            f"Handle the full request now: {utterance.strip()}"
                        )
                    )
                return TurnDirective(
                    model_input=(
                        "Application event: the caller accepted the offer of more help. "
                        "Ask what scheduling or clinic information they need."
                    )
                )
            return TurnDirective(
                reply=(
                    f"{POST_TASK_HELP_OFFER} Please say yes or no, or tell me the request."
                )
            )

        if self.state is WorkflowState.COMPLETED:
            if require_semantic_follow_up:
                if follow_up_decision in {
                    FollowUpDecision.ACCEPTED,
                    FollowUpDecision.NEW_REQUEST,
                }:
                    self._transition(
                        WorkflowState.ACTIVE,
                        "follow_up_reopened_after_close",
                    )
                    return TurnDirective(
                        model_input=(
                            "Application event: before disconnecting, the caller stated "
                            "another supported appointment or clinic-information request. "
                            f"Handle the full request now: {utterance.strip()}"
                        )
                    )
                if follow_up_decision is FollowUpDecision.DECLINED:
                    return TurnDirective(reply="Thank you. Take care.")
            # A completed task never silently becomes another task. Present an
            # explicit offer first while keeping all protected tools locked.
            self._transition(
                WorkflowState.AWAITING_FOLLOW_UP_DECISION,
                "follow_up_offer_presented",
            )
            return TurnDirective(
                reply=f"That scheduling task is complete. {POST_TASK_HELP_OFFER}"
            )

        return TurnDirective(
            reply=(
                "I can't access or change appointment information in this session. "
                "Please contact the front desk for assistance."
            )
        )

    def proposal_prepared(self) -> None:
        if self.state is WorkflowState.ACTIVE:
            self._transition(
                WorkflowState.CONFIRMATION_PENDING, "exact_proposal_prepared"
            )

    def proposal_invalidated(self, reason: str) -> None:
        if self.state is WorkflowState.CONFIRMATION_PENDING:
            self._transition(WorkflowState.ACTIVE, reason)

    def mutation_verified(self, *, offer_follow_up: bool = False) -> None:
        if self.state is WorkflowState.CONFIRMATION_PENDING:
            self._transition(
                (
                    WorkflowState.AWAITING_FOLLOW_UP_DECISION
                    if offer_follow_up
                    else WorkflowState.COMPLETED
                ),
                (
                    "mutation_verified_follow_up_offered"
                    if offer_follow_up
                    else "mutation_verified"
                ),
            )

    def mutation_unresolved(self, reason: str) -> None:
        if self.state is WorkflowState.CONFIRMATION_PENDING:
            self._transition(WorkflowState.RECOVERY_REQUIRED, reason)

    def notification_delivered(self) -> None:
        """Close a clinic-approval notification before offering more help."""

        if self.state is WorkflowState.ACTIVE:
            self._transition(
                WorkflowState.AWAITING_FOLLOW_UP_DECISION,
                "confirmation_notification_delivered",
            )

    def notification_unresolved(self) -> None:
        if self.state is WorkflowState.ACTIVE:
            self._transition(
                WorkflowState.RECOVERY_REQUIRED,
                "confirmation_notification_context_unresolved",
            )

    def patient_ended_call(self) -> None:
        """Finish an active, mutation-free call when the patient explicitly closes it.

        This transition is intentionally unavailable while a write confirmation or
        recovery is pending.  The semantic decision is made by the application
        before this method is called; this method only locks the resulting state.
        """

        if self.state is WorkflowState.ACTIVE:
            self._transition(WorkflowState.COMPLETED, "patient_ended_call")

    def handoff_resolved(self, *, accepted: bool) -> None:
        if self.state is WorkflowState.HUMAN_HANDOFF_PENDING:
            self._transition(
                (
                    WorkflowState.HUMAN_HANDOFF_COMPLETED
                    if accepted
                    else WorkflowState.HUMAN_HANDOFF_FAILED
                ),
                "front_desk_handoff_accepted" if accepted else "front_desk_handoff_failed",
            )

    @property
    def tools_allowed(self) -> bool:
        return self.state in {
            WorkflowState.ACTIVE,
            WorkflowState.CONFIRMATION_PENDING,
            WorkflowState.RECOVERY_REQUIRED,
        }

    def requires_immediate_preemption(self, utterance: str) -> bool:
        """Return true for turns that must not wait for a model tool request."""

        if self.state in {
            WorkflowState.IDENTITY_REJECTED,
            WorkflowState.URGENT_HANDOFF,
            WorkflowState.HUMAN_HANDOFF_PENDING,
            WorkflowState.HUMAN_HANDOFF_COMPLETED,
            WorkflowState.HUMAN_HANDOFF_FAILED,
        }:
            return False
        return self._contains_urgent_signal(utterance) or self._requests_human(
            utterance
        )

    def prompt_context(self) -> str:
        identity = self.authority_mode or "not_verified"
        identity_lock = (
            "locked" if self._identity_confirmation_key is not None else "unset"
        )
        return (
            "Application-owned workflow state:\n"
            f"- current_state: {self.state.value}\n"
            f"- expected_patient_display_name: {self.patient_display_name}\n"
            f"- identity_status: {identity}\n"
            f"- identity_confirmation_key: {identity_lock}\n"
            "- identity assurance: low; synthetic name-confirmation policy only\n"
            "Treat this state as authoritative. Never infer or advance it yourself."
        )

    def _transition(self, current: WorkflowState, reason: str) -> None:
        previous = self.state
        self.state = current
        self.transitions.append(
            StateTransition(
                sequence=len(self.transitions) + 1,
                previous=previous,
                current=current,
                reason=reason,
            )
        )

    @staticmethod
    def _intent_label(utterance: str) -> str:
        normalized = _normalize(utterance)
        if any(word in normalized for word in ("cancel", "cancellation")):
            return "cancelling an appointment"
        if any(word in normalized for word in ("move", "reschedule", "change")):
            return "rescheduling an appointment"
        if any(word in normalized for word in ("book", "schedule", "appointment")):
            return "scheduling an appointment"
        return "your scheduling request"

    @staticmethod
    def _contains_urgent_signal(utterance: str) -> bool:
        normalized = _normalize(utterance)
        configured_signals = (
            "cannot breathe",
            "cant breathe",
            "chest pain",
            "difficulty breathing",
            "severe bleeding",
            "unconscious",
        )
        return any(signal in normalized for signal in configured_signals)

    @staticmethod
    def _requests_human(utterance: str) -> bool:
        normalized = _normalize(utterance)
        phrases = (
            "front desk",
            "human",
            "real person",
            "representative",
            "someone at the clinic",
        )
        return any(phrase in normalized for phrase in phrases)

    @staticmethod
    def _parse_proxy_details(utterance: str) -> tuple[str, str] | None:
        cleaned = " ".join(utterance.strip().split())
        match = re.fullmatch(
            r"(?:my name is\s+)?(?P<name>[A-Za-z][A-Za-z .'-]+?)"
            r"\s*,\s*(?P<relationship>spouse|husband|wife|parent|guardian|caregiver|proxy)",
            cleaned,
            flags=re.IGNORECASE,
        )
        if match is None:
            return None
        relationship = match.group("relationship").casefold()
        if relationship in {"husband", "wife"}:
            relationship = "spouse"
        return match.group("name").strip(), relationship
