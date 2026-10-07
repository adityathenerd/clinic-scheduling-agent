"""Direct Responses API text adapter over the synthetic appointment harness.

This is the fastest executable path for exercising multi-turn behavior before
the voice transport is connected.  The model can request domain operations,
but Python owns identity, proposal binding, confirmation, versions,
idempotency, writes, final-state verification, and cancellation follow-up.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Protocol, Sequence

from appointment_harness.models import (
    AppointmentStatus,
    ConfirmedProposal,
    CreateAppointmentCommand,
    SlotStatus,
)
from appointment_harness.faults import SyntheticTimeout
from appointment_harness.service import AppointmentHarness
from clinic_agent.agent.tool_catalog import BACKEND_TOOL_CATALOG
from clinic_agent.clinic_profile import (
    CLINIC_TIMEZONE,
    CLINIC_TIMEZONE_LABEL,
    patient_facing_location,
)
from clinic_agent.knowledge import ClinicFAQKnowledgeBase
from clinic_agent.control_plane.state_machine import (
    ConfirmationDecision,
    ConversationStateMachine,
    WorkflowState,
)
from clinic_agent.control_plane.tool_contracts import (
    DeleteAppointmentCommand,
    EditAppointmentCommand,
    PostCancellationFollowUpCommand,
)


WRITE_TOOLS = frozenset(
    {"create_appointment", "edit_appointment", "delete_appointment"}
)
PUBLIC_READ_TOOLS = frozenset({"search_clinic_faqs"})


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _normalized_confirmation(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


_EXPLICIT_CONFIRMATIONS = frozenset(
    {
        "confirm",
        "confirm it",
        "go ahead",
        "yes",
        "yes book it",
        "yes cancel it",
        "yes confirm",
        "yes confirm it",
        "yes go ahead",
        "yes please",
        "yes please confirm",
        "yes reschedule it",
    }
)


@dataclass(frozen=True, slots=True)
class PendingMutation:
    tool_name: str
    arguments: Mapping[str, Any]
    proposal_id: str
    confirmation_token: str
    summary: str
    guard_context: Mapping[str, Any]


class ResponsesClient(Protocol):
    class _Responses(Protocol):
        def create(self, **kwargs: Any) -> Any: ...

    responses: _Responses


class GuardedToolRuntime:
    """Dispatch model-visible tools through application-owned safety gates."""

    def __init__(
        self,
        harness: AppointmentHarness,
        *,
        session_id: str,
        patient_id: str,
        state_machine: ConversationStateMachine | None = None,
        faq_knowledge: ClinicFAQKnowledgeBase | None = None,
    ) -> None:
        self.harness = harness
        self.session_id = session_id
        self.patient_id = patient_id
        self.state_machine = state_machine
        self.faq_knowledge = faq_knowledge or ClinicFAQKnowledgeBase.load()
        self.pending: PendingMutation | None = None
        self._confirmed_for_turn = False
        self._latest_snapshot_id: str | None = None
        self._sequence = 0
        self.trace: list[dict[str, Any]] = []

    def begin_patient_turn(
        self,
        utterance: str,
        *,
        confirmation_decision: ConfirmationDecision | None = None,
        require_semantic_confirmation: bool = False,
    ) -> None:
        """Bind an explicit affirmative only to the proposal from the prior turn."""

        if not self.pending:
            self._confirmed_for_turn = False
            return
        if require_semantic_confirmation:
            self._confirmed_for_turn = (
                confirmation_decision is ConfirmationDecision.CONFIRMED
            )
            if confirmation_decision is ConfirmationDecision.UNCLEAR:
                self.trace.append(
                    {
                        "event": "proposal.confirmation_unclear",
                        "proposal_id": self.pending.proposal_id,
                    }
                )
                return
        else:
            self._confirmed_for_turn = (
                _normalized_confirmation(utterance) in _EXPLICIT_CONFIRMATIONS
            )
        if not self._confirmed_for_turn:
            self.trace.append(
                {
                    "event": "proposal.invalidated",
                    "proposal_id": self.pending.proposal_id,
                    "reason": "non_confirmation_or_correction",
                }
            )
            self.pending = None
            if self.state_machine:
                self.state_machine.proposal_invalidated(
                    "patient_correction_or_non_confirmation"
                )

    @property
    def confirmation_authorized_for_turn(self) -> bool:
        return self._confirmed_for_turn

    def execute(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if (
            self.state_machine
            and not self.state_machine.tools_allowed
            and name not in PUBLIC_READ_TOOLS
        ):
            result = {
                "status": "rejected",
                "reason": "tools_locked_by_workflow_state",
                "workflow_state": self.state_machine.state.value,
            }
        elif name not in BACKEND_TOOL_CATALOG:
            result = {"status": "rejected", "reason": "unknown_tool"}
        else:
            try:
                handler = getattr(self, f"_tool_{name}")
                result = handler(dict(arguments))
            except (KeyError, TypeError, ValueError) as error:
                result = {
                    "status": "rejected",
                    "reason": "invalid_arguments",
                    "detail": str(error),
                }
            except SyntheticTimeout as error:
                # A write timeout may have happened before or after the backend
                # committed. Preserve that ambiguity for the control plane so it
                # enters reconciliation instead of reporting a definite failure
                # or blindly replaying the mutation.
                result = {
                    "status": "timeout",
                    "reason": type(error).__name__,
                    "detail": str(error),
                }
            except Exception as error:  # Boundary converts backend faults to typed output.
                result = {
                    "status": "failed",
                    "reason": type(error).__name__,
                    "detail": str(error),
                }
        self.trace.append(
            {
                "event": "tool.result",
                "tool": name,
                "status": result.get("status", "ok"),
            }
        )
        return result

    def _tool_search_clinic_faqs(
        self, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        query = str(arguments["query"])
        category = self._optional(arguments.get("category"))
        matches = self.faq_knowledge.search(query, category=category)
        if not matches:
            return {
                "status": "ok",
                "matches": [],
                "patient_facing_summary": (
                    "I don't have a clinic-approved answer for that question. "
                    "Offer the front desk rather than guessing."
                ),
                "scope": "administrative_information_only",
            }
        return {
            "status": "ok",
            "matches": [
                {
                    "faq_id": item.faq_id,
                    "category": item.category,
                    "title": item.title,
                    "answer": item.answer,
                    "source_label": item.source_label,
                    "effective_from": item.effective_from,
                    "review_after": item.review_after,
                }
                for item in matches
            ],
            "patient_facing_summary": matches[0].answer,
            "scope": "administrative_information_only",
            "patient_facing_instruction": (
                "Answer only from the returned FAQ text. If the caller asks for "
                "personalized treatment, medicine dose, substitution, diagnosis, "
                "or coverage guarantees, explain the boundary and route them to "
                "the clinician, pharmacist, billing desk, or front desk as appropriate."
            ),
        }

    def _tool_list_appointments(self, _: Mapping[str, Any]) -> Mapping[str, Any]:
        appointments = self.harness.list_appointments(
            session_id=self.session_id, patient_id=self.patient_id
        )
        enriched = [self._appointment_details(item) for item in appointments]
        return {
            "status": "ok",
            "appointments": enriched,
            "patient_facing_instruction": (
                "When the caller asks about an appointment, state its visit type, "
                "provider and specialty, exact date and time, location, duration, "
                "arrival guidance, and any non-empty visit reason or prior treatment "
                "context from patient_facing_details. Repeat that context without "
                "clinical interpretation. Mention "
                "prerequisites only when that list is non-empty. A listed "
                "prerequisite is not proof it has been completed or is on file."
            ),
        }

    def _tool_get_appointment(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        appointment = self.harness.get_appointment(
            session_id=self.session_id,
            patient_id=self.patient_id,
            appointment_id=str(arguments["appointment_id"]),
        )
        return {
            "status": "ok",
            "appointment": self._appointment_details(appointment),
            "patient_facing_instruction": (
                "Answer with the material patient-facing appointment details; do not "
                "substitute internal IDs for patient-facing names. Include any "
                "non-empty visit reason or prior treatment context without adding "
                "clinical interpretation. Mention "
                "prerequisites only when the returned list is non-empty. A listed "
                "prerequisite is a requirement only; never claim it is satisfied or "
                "on file unless an authoritative result explicitly says so."
            ),
        }

    def _tool_check_eligibility(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        appointment_type_id = self._appointment_type_id(
            str(arguments["appointment_type"])
        )
        appointment_type = self.harness.store.appointment_types[appointment_type_id]
        return {
            "status": "ok",
            "appointment_type_id": appointment_type_id,
            "display_name": appointment_type.display_name,
            "administrative_status": (
                "supported_if_prerequisites_met"
                if appointment_type.prerequisites
                else "supported"
            ),
            "prerequisites": list(appointment_type.prerequisites),
            "note": (
                "Caller statements about prerequisites are not clinical verification."
                if appointment_type.prerequisites
                else "No administrative prerequisites are configured for this visit type."
            ),
        }

    def _tool_search_slots(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        appointment_type_id = self._appointment_type_id(
            str(arguments["appointment_type"])
        )
        starts_after = self._day_boundary(str(arguments["date_from"]))
        starts_before = self._day_boundary(str(arguments["date_to"])) + timedelta(
            days=1
        )
        provider_id = self._optional(arguments.get("provider_id"))
        # The model-facing contract uses canonical ``location_id``.  Keep the
        # legacy key at this internal boundary so old transcripts/replays remain
        # executable, but normalize display labels before the exact store query.
        location_id = self._location_id(
            arguments.get("location_id", arguments.get("location"))
        )
        snapshot = self.harness.search_slots(
            session_id=self.session_id,
            patient_id=self.patient_id,
            appointment_type_id=appointment_type_id,
            provider_id=provider_id,
            location_id=location_id,
            starts_after=starts_after,
            starts_before=starts_before,
        )
        self._latest_snapshot_id = snapshot.snapshot_id
        slot_details = [self._slot_details(slot) for slot in snapshot.slots]
        date_from = str(arguments["date_from"])
        date_to = str(arguments["date_to"])
        time_window = self._optional(arguments.get("time_window"))
        if slot_details:
            availability_summary = (
                f"I found {len(slot_details)} matching appointment option"
                f"{'s' if len(slot_details) != 1 else ''}."
            )
        else:
            window = f" with the time preference {time_window}" if time_window else ""
            availability_summary = (
                f"I couldn't find an available appointment from {date_from} through "
                f"{date_to}{window}. Would you like to try another day or time?"
            )
        return {
            "status": "ok",
            "snapshot_id": snapshot.snapshot_id,
            "fresh_as_of": snapshot.created_at.isoformat(),
            "slots": slot_details,
            "patient_facing_summary": availability_summary,
        }

    def _tool_create_appointment(
        self, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        slot_id = str(arguments["slot_id"])
        if self._may_commit("create_appointment", arguments):
            pending = self._consume_pending()
            snapshot_id = str(pending.guard_context["availability_snapshot_id"])
        else:
            snapshot_id = self._required_snapshot_for(slot_id)
            summary = self._slot_summary(
                slot_id, prefix="Request (held pending clinic approval)"
            )
            return self._prepare(
                "create_appointment",
                arguments,
                summary,
                guard_context={"availability_snapshot_id": snapshot_id},
            )
        self.harness.register_confirmation(
            ConfirmedProposal(
                session_id=self.session_id,
                patient_id=self.patient_id,
                proposal_id=pending.proposal_id,
                confirmation_token=pending.confirmation_token,
                operation="create_appointment",
                slot_id=slot_id,
            )
        )
        result = self.harness.create_appointment(
            CreateAppointmentCommand(
                session_id=self.session_id,
                patient_id=self.patient_id,
                slot_id=slot_id,
                availability_snapshot_id=snapshot_id,
                proposal_id=pending.proposal_id,
                confirmation_token=pending.confirmation_token,
                idempotency_key=self._reference("create"),
            )
        )
        if result.outcome.value != "proposed":
            if self.state_machine:
                self.state_machine.mutation_unresolved(
                    f"create_{result.outcome.value}"
                )
            return {"status": result.outcome.value, "result": _jsonable(result)}
        appointment = self.harness.verify_final_state(
            session_id=self.session_id,
            patient_id=self.patient_id,
            appointment_id=result.appointment_id,
            expected_status=AppointmentStatus.PROPOSED,
            expected_slot_id=slot_id,
        )
        if self.state_machine:
            self.state_machine.mutation_verified(offer_follow_up=True)
        appointment_summary = self._appointment_details(appointment)[
            "patient_facing_summary"
        ]
        return {
            "status": "proposed",
            "operation": "proposal_created",
            "appointment": _jsonable(appointment),
            "patient_facing_summary": (
                f"{appointment_summary} Would you like any other scheduling help?"
            ),
        }

    def _tool_edit_appointment(
        self, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        appointment_id = str(arguments["appointment_id"])
        slot_id = str(arguments["replacement_slot_id"])
        current = self.harness.get_appointment(
            session_id=self.session_id,
            patient_id=self.patient_id,
            appointment_id=appointment_id,
        )
        if self._may_commit("edit_appointment", arguments):
            pending = self._consume_pending()
            snapshot_id = str(pending.guard_context["availability_snapshot_id"])
            expected_version = int(
                pending.guard_context["expected_appointment_version"]
            )
        else:
            snapshot_id = self._required_snapshot_for(slot_id)
            current_provider = self.harness.store.providers[current.provider_id]
            current_type = self.harness.store.appointment_types[
                current.appointment_type_id
            ]
            replacement = self.harness.store.slots[slot_id]
            replacement_provider = self.harness.store.providers[
                replacement.provider_id
            ]
            summary = (
                f"Move your {current_type.display_name} with "
                f"{current_provider.display_name} from "
                f"{self._spoken_time(current.starts_at)} at "
                f"{self._location_name(current.location_id)} to "
                f"{self._spoken_time(replacement.starts_at)} with "
                f"{replacement_provider.display_name} at "
                f"{self._location_name(replacement.location_id)}"
            )
            return self._prepare(
                "edit_appointment",
                arguments,
                summary,
                guard_context={
                    "availability_snapshot_id": snapshot_id,
                    "expected_appointment_version": current.version,
                },
            )
        self.harness.register_confirmation(
            ConfirmedProposal(
                session_id=self.session_id,
                patient_id=self.patient_id,
                proposal_id=pending.proposal_id,
                confirmation_token=pending.confirmation_token,
                operation="edit_appointment",
                appointment_id=appointment_id,
                expected_appointment_version=expected_version,
                slot_id=slot_id,
            )
        )
        result = self.harness.edit_appointment(
            EditAppointmentCommand(
                session_id=self.session_id,
                patient_id=self.patient_id,
                appointment_id=appointment_id,
                expected_appointment_version=expected_version,
                replacement_slot_id=slot_id,
                availability_snapshot_id=snapshot_id,
                proposal_id=pending.proposal_id,
                confirmation_token=pending.confirmation_token,
                idempotency_key=self._reference("edit"),
            )
        )
        if result.outcome.value != "proposed":
            if self.state_machine:
                self.state_machine.mutation_unresolved(
                    f"edit_{result.outcome.value}"
                )
            return {"status": result.outcome.value, "result": _jsonable(result)}
        appointment = self.harness.get_appointment(
            session_id=self.session_id,
            patient_id=self.patient_id,
            appointment_id=appointment_id,
            for_final_verification=True,
        )
        replacement = self.harness.store.slots[slot_id]
        if (
            appointment.pending_replacement_slot_id != slot_id
            or replacement.status is not SlotStatus.HELD
        ):
            if self.state_machine:
                self.state_machine.mutation_unresolved("edit_proposal_verification_failed")
            return {"status": "conflict", "result": _jsonable(result)}
        if self.state_machine:
            self.state_machine.mutation_verified(offer_follow_up=True)
        return {
            "status": "proposed",
            "operation": "reschedule_requested",
            "appointment": _jsonable(appointment),
            "replacement_slot": self._slot_details(replacement),
            "patient_facing_summary": (
                "Your request to move the appointment to "
                f"{self._spoken_time(replacement.starts_at)} at "
                f"{self._location_name(replacement.location_id)} is held for clinic "
                "review. Your current appointment on "
                f"{self._spoken_time(appointment.starts_at)} remains in place until "
                "approval. We will call you once the clinic decides. Would you like "
                "any other scheduling help?"
            ),
        }

    def _tool_delete_appointment(
        self, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        appointment_id = str(arguments["appointment_id"])
        current = self.harness.get_appointment(
            session_id=self.session_id,
            patient_id=self.patient_id,
            appointment_id=appointment_id,
        )
        if self._may_commit("delete_appointment", arguments):
            pending = self._consume_pending()
            expected_version = int(
                pending.guard_context["expected_appointment_version"]
            )
        else:
            provider = self.harness.store.providers[current.provider_id]
            appointment_type = self.harness.store.appointment_types[
                current.appointment_type_id
            ]
            summary = (
                f"Cancel your {appointment_type.display_name} with "
                f"{provider.display_name} on {self._spoken_time(current.starts_at)} "
                f"at {self._location_name(current.location_id)}"
            )
            return self._prepare(
                "delete_appointment",
                arguments,
                summary,
                guard_context={"expected_appointment_version": current.version},
            )
        self.harness.register_confirmation(
            ConfirmedProposal(
                session_id=self.session_id,
                patient_id=self.patient_id,
                proposal_id=pending.proposal_id,
                confirmation_token=pending.confirmation_token,
                operation="delete_appointment",
                appointment_id=appointment_id,
                expected_appointment_version=expected_version,
            )
        )
        result = self.harness.delete_appointment(
            DeleteAppointmentCommand(
                session_id=self.session_id,
                patient_id=self.patient_id,
                appointment_id=appointment_id,
                expected_appointment_version=expected_version,
                proposal_id=pending.proposal_id,
                confirmation_token=pending.confirmation_token,
                idempotency_key=self._reference("delete"),
            )
        )
        if result.outcome.value != "cancelled":
            if self.state_machine:
                self.state_machine.mutation_unresolved(
                    f"delete_{result.outcome.value}"
                )
            return {"status": result.outcome.value, "result": _jsonable(result)}
        appointment = self.harness.verify_final_state(
            session_id=self.session_id,
            patient_id=self.patient_id,
            appointment_id=appointment_id,
            expected_status=AppointmentStatus.CANCELLED,
        )
        follow_up = self.harness.request_front_desk_follow_up(
            PostCancellationFollowUpCommand(
                session_id=self.session_id,
                patient_id=self.patient_id,
                cancelled_appointment_id=appointment_id,
                follow_up_topics=("future_scheduling_options",),
                idempotency_key=self._reference("follow-up"),
            )
        )
        if self.state_machine:
            self.state_machine.mutation_verified(offer_follow_up=True)
        return {
            "status": "verified",
            "operation": "cancelled",
            "appointment": _jsonable(appointment),
            "front_desk_follow_up": _jsonable(follow_up),
            "patient_facing_summary": (
                "The appointment was cancelled and the front-desk follow-up "
                f"status is {follow_up.outcome.value}. Would you like me to help "
                "request a new appointment now?"
            ),
        }

    def _prepare(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        summary: str,
        *,
        guard_context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._sequence += 1
        proposal_id = f"proposal-{self._sequence:04d}"
        token = sha256(
            f"{self.session_id}:{proposal_id}:{tool_name}".encode("utf-8")
        ).hexdigest()
        self.pending = PendingMutation(
            tool_name=tool_name,
            arguments=dict(arguments),
            proposal_id=proposal_id,
            confirmation_token=token,
            summary=summary,
            guard_context=dict(guard_context),
        )
        self._confirmed_for_turn = False
        if self.state_machine:
            self.state_machine.proposal_prepared()
        return {
            "status": "confirmation_required",
            "proposal_id": proposal_id,
            "exact_proposal": summary,
            "instruction": (
                "Read the exact proposal to the patient and ask for explicit "
                "confirmation. Do not claim the change has happened."
            ),
        }

    def _may_commit(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        return bool(
            self.pending
            and self._confirmed_for_turn
            and self.pending.tool_name == tool_name
            and dict(self.pending.arguments) == dict(arguments)
        )

    def _consume_pending(self) -> PendingMutation:
        if self.pending is None:
            raise RuntimeError("no confirmed proposal")
        pending = self.pending
        self.pending = None
        self._confirmed_for_turn = False
        return pending

    def _required_snapshot_for(self, slot_id: str) -> str:
        if self._latest_snapshot_id is None:
            raise ValueError("fresh availability search required before this mutation")
        snapshot = self.harness.store.availability_snapshots[self._latest_snapshot_id]
        if slot_id not in snapshot.slot_versions:
            raise ValueError("slot is not part of the latest availability result")
        return self._latest_snapshot_id

    def _slot_summary(self, slot_id: str, *, prefix: str = "") -> str:
        slot = self.harness.store.slots.get(slot_id)
        if slot is None:
            raise ValueError("unknown slot")
        provider = self.harness.store.providers[slot.provider_id]
        appointment_type = self.harness.store.appointment_types[
            slot.appointment_type_id
        ]
        body = (
            f"{appointment_type.display_name} with {provider.display_name} at "
            f"{self._location_name(slot.location_id)} on "
            f"{self._spoken_time(slot.starts_at)}"
        )
        return f"{prefix} {body}".strip()

    def _appointment_details(self, appointment: Any) -> Mapping[str, Any]:
        provider = self.harness.store.providers[appointment.provider_id]
        appointment_type = self.harness.store.appointment_types[
            appointment.appointment_type_id
        ]
        slot = self.harness.store.slots[appointment.slot_id]
        prerequisites = [
            self._humanize_policy_term(value)
            for value in appointment_type.prerequisites
        ]
        location_name = self._location_name(appointment.location_id)
        spoken_time = self._spoken_time(appointment.starts_at)
        prerequisite_sentence = (
            " Requirements listed by clinic policy, not confirmed as completed "
            f"or on file: {', '.join(prerequisites)}."
            if prerequisites
            else ""
        )
        visit_context_parts = [
            value
            for value in (
                f"The reason on file is {appointment.visit_reason.strip()}."
                if appointment.visit_reason.strip()
                else "",
                f"The record notes {appointment.prior_treatment_context.strip()}."
                if appointment.prior_treatment_context.strip()
                else "",
            )
            if value
        ]
        visit_context_sentence = (
            " ".join(visit_context_parts) + " " if visit_context_parts else ""
        )
        pending_replacement = (
            self.harness.store.slots.get(appointment.pending_replacement_slot_id)
            if appointment.pending_replacement_slot_id
            else None
        )
        appointment_subject = (
            f"Your {appointment_type.display_name} with {provider.display_name}, "
            f"{provider.specialty}"
        )
        if pending_replacement is not None:
            status_text = (
                f"{appointment_subject}, remains scheduled for {spoken_time} at "
                f"{location_name}. A request for "
                f"{self._spoken_time(pending_replacement.starts_at)} at "
                f"{self._location_name(pending_replacement.location_id)} is held for "
                "clinic review; the current appointment remains in place until approval."
            )
        elif appointment.status is AppointmentStatus.PROPOSED:
            status_text = (
                f"{appointment_subject}, is held for clinic review for {spoken_time} "
                f"at {location_name}. It is not confirmed yet; we will call after "
                "the clinic decides."
            )
        elif appointment.status is AppointmentStatus.CONFIRMED:
            status_text = (
                f"{appointment_subject}, is confirmed for {spoken_time} at "
                f"{location_name}."
            )
        elif appointment.status is AppointmentStatus.CANCELLED:
            status_text = f"{appointment_subject}, on {spoken_time} is cancelled."
        else:
            status_text = (
                f"{appointment_subject}, is scheduled for {spoken_time} at "
                f"{location_name}."
            )
        summary = (
            f"{status_text} "
            f"{visit_context_sentence}The visit duration is {slot.duration_minutes} "
            "minutes. Please arrive 15 "
            f"minutes early.{prerequisite_sentence}"
        )
        return {
            "appointment_id": appointment.appointment_id,
            "status": appointment.status.value,
            "version": appointment.version,
            "slot_id": appointment.slot_id,
            "provider_id": appointment.provider_id,
            "appointment_type_id": appointment.appointment_type_id,
            "starts_at": appointment.starts_at.isoformat(),
            "patient_facing_details": {
                "appointment_type": appointment_type.display_name,
                "provider": provider.display_name,
                "specialty": provider.specialty,
                "date_time": spoken_time,
                "location": location_name,
                "duration_minutes": slot.duration_minutes,
                "arrival_guidance": "Please arrive 15 minutes early.",
                "visit_reason": appointment.visit_reason,
                "prior_treatment_context": appointment.prior_treatment_context,
                "prerequisites": prerequisites,
                "prerequisite_status": (
                    "requirements_listed_not_verified"
                    if prerequisites
                    else "not_required"
                ),
                "pending_reschedule": (
                    self._slot_details(pending_replacement)
                    if pending_replacement is not None
                    else None
                ),
            },
            "patient_facing_summary": summary,
        }

    def _slot_details(self, slot: Any) -> Mapping[str, Any]:
        provider = self.harness.store.providers[slot.provider_id]
        appointment_type = self.harness.store.appointment_types[
            slot.appointment_type_id
        ]
        return {
            "slot_id": slot.slot_id,
            "starts_at": slot.starts_at.isoformat(),
            "date_time": self._spoken_time(slot.starts_at),
            "provider": provider.display_name,
            "specialty": provider.specialty,
            "appointment_type": appointment_type.display_name,
            "location": self._location_name(slot.location_id),
            "duration_minutes": slot.duration_minutes,
            "version": slot.version,
        }

    @staticmethod
    def _humanize_policy_term(value: str) -> str:
        return value.replace("_", " ").strip().capitalize()

    @staticmethod
    def _location_name(location_id: str) -> str:
        return patient_facing_location(location_id)

    @staticmethod
    def _spoken_time(value: datetime) -> str:
        local_value = (
            value.replace(tzinfo=CLINIC_TIMEZONE)
            if value.tzinfo is None
            else value.astimezone(CLINIC_TIMEZONE)
        )
        hour = local_value.strftime("%I").lstrip("0") or "0"
        return (
            f"{local_value.strftime('%A')}, {local_value.strftime('%B')} "
            f"{local_value.day}, {local_value.year} at {hour}:"
            f"{local_value.strftime('%M %p')} {CLINIC_TIMEZONE_LABEL}"
        )

    def _appointment_type_id(self, supplied: str) -> str:
        normalized = supplied.casefold().replace("_", "-").strip()
        for key, value in self.harness.store.appointment_types.items():
            if normalized in {key.casefold(), value.display_name.casefold()}:
                return key
        raise ValueError("unsupported appointment type")

    def _location_id(self, supplied: Any) -> str | None:
        if supplied is None or not str(supplied).strip():
            return None
        normalized = str(supplied).casefold().replace("_", "-").strip()
        location_ids = {
            location_id
            for provider in self.harness.store.providers.values()
            for location_id in provider.location_ids
        }
        for location_id in location_ids:
            aliases = {
                location_id.casefold(),
                location_id.casefold().replace("-", " "),
                f"{location_id.replace('-', ' ').title()} clinic".casefold(),
                self._location_name(location_id).casefold(),
            }
            if normalized in aliases:
                return location_id
        raise ValueError("unsupported location")

    def _day_boundary(self, supplied: str) -> datetime:
        parsed = date.fromisoformat(supplied)
        return datetime.combine(parsed, time.min, tzinfo=self.harness.clock.now().tzinfo)

    @staticmethod
    def _optional(value: Any) -> str | None:
        if value is None or not str(value).strip():
            return None
        return str(value)

    def _reference(self, operation: str) -> str:
        self._sequence += 1
        return f"{self.session_id}:{operation}:{self._sequence:04d}"


class ResponsesTextSession:
    """Run one stateful patient conversation using OpenAI Responses."""

    def __init__(
        self,
        client: ResponsesClient,
        runtime: GuardedToolRuntime,
        *,
        instructions: str,
        model: str = "gpt-6-sol",
        max_tool_rounds: int = 8,
        state_machine: ConversationStateMachine | None = None,
    ) -> None:
        self.client = client
        self.runtime = runtime
        self.instructions = instructions
        self.model = model
        self.max_tool_rounds = max_tool_rounds
        self.state_machine = state_machine
        self.previous_response_id: str | None = None

    def respond(self, patient_text: str) -> str:
        pending_before = self.runtime.pending
        self.runtime.begin_patient_turn(patient_text)
        response = self._create(input=patient_text)
        for _ in range(self.max_tool_rounds):
            calls = [
                item
                for item in response.output
                if getattr(item, "type", None) == "function_call"
            ]
            if not calls:
                self.previous_response_id = response.id
                pending_after = self.runtime.pending
                if pending_after and (
                    pending_before is None
                    or pending_before.proposal_id != pending_after.proposal_id
                ):
                    return self._confirmation_message(pending_after)
                return response.output_text
            outputs = []
            for call in calls:
                arguments = json.loads(call.arguments)
                result = self.runtime.execute(call.name, arguments)
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result, sort_keys=True),
                    }
                )
            response = self._create(
                input=outputs,
                previous_response_id=response.id,
            )
        raise RuntimeError("tool loop exceeded the configured round limit")

    @staticmethod
    def _confirmation_message(pending: PendingMutation) -> str:
        action = {
            "create_appointment": "booking",
            "edit_appointment": "rescheduling",
            "delete_appointment": "cancellation",
        }[pending.tool_name]
        consequence = {
            "booking": "I will submit it for clinic approval.",
            "rescheduling": (
                "Your current appointment stays in place until the clinic approves "
                "the change."
            ),
            "cancellation": "The appointment will be cancelled.",
        }[action]
        return (
            f"Please confirm this {action} request: {pending.summary}. "
            f"{consequence} Please say yes or no."
        )

    def _create(self, **kwargs: Any) -> Any:
        instructions = self.instructions
        tools: Sequence[Mapping[str, Any]] = tuple(BACKEND_TOOL_CATALOG.values())
        if self.state_machine:
            instructions = (
                f"{instructions}\n\n{self.state_machine.prompt_context()}"
            )
            if self.state_machine.state is WorkflowState.CONFIRMATION_PENDING:
                if self.runtime.confirmation_authorized_for_turn and self.runtime.pending:
                    tools = (
                        BACKEND_TOOL_CATALOG[self.runtime.pending.tool_name],
                    )
                else:
                    tools = ()
            elif self.state_machine.state is not WorkflowState.ACTIVE:
                tools = ()
        request = {
            "model": self.model,
            "instructions": instructions,
            "tools": list(tools),
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "reasoning": {"effort": "low"},
            "store": True,
            **kwargs,
        }
        if "previous_response_id" not in request and self.previous_response_id:
            request["previous_response_id"] = self.previous_response_id
        return self.client.responses.create(**request)


def text_runtime_instructions(base_prompt: str, *, current_time: datetime) -> str:
    """Add the executable text-adapter protocol to the approved prompt."""

    return (
        base_prompt
        + "\n\nText evaluation adapter protocol:\n"
        + f"- Current synthetic clinic time: {current_time.isoformat()}.\n"
        + "- The application has already verified the synthetic patient's identity.\n"
        + "- Speak directly to the patient; do not expose internal state or tokens.\n"
        + "- Use reads before writes and current availability before create or edit.\n"
        + "- A first create, edit, or delete tool call prepares an exact proposal and "
        + "returns confirmation_required. Read that proposal and ask one explicit "
        + "yes-or-no confirmation question.\n"
        + "- Only after the next patient turn explicitly confirms may you repeat the "
        + "same write tool call. Python, not you, decides whether the confirmation is valid.\n"
        + "- For create or edit, status=proposed means held pending clinic approval; "
        + "never call it booked, moved, scheduled, or confirmed. For a proposed edit, "
        + "say the existing appointment remains confirmed. For delete, claim a change "
        + "only when the tool returns status=verified.\n"
        + "- After cancellation, report the separate front-desk follow-up status."
    )
