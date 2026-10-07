"""In-process synthetic scheduling API with deterministic failure semantics."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from json import dumps
from typing import Any

from clinic_agent.control_plane.tool_contracts import (
    AppointmentMutationOutcome,
    AppointmentMutationResult,
    DeleteAppointmentCommand,
    EditAppointmentCommand,
    FollowUpOutcome,
    FollowUpResult,
    PostCancellationFollowUpCommand,
)

from .clock import FixedClock
from .faults import (
    ContractRejected,
    FaultInjector,
    FaultKind,
    FaultPoint,
    IdempotencyConflict,
    SyntheticFailure,
    SyntheticTimeout,
    VersionConflict,
)
from .models import (
    Appointment,
    AppointmentConfirmationCall,
    AppointmentStatus,
    AuditEvent,
    AvailabilitySnapshot,
    ConfirmedProposal,
    ConfirmationCallStatus,
    ClinicConfirmationResult,
    CreateAppointmentCommand,
    FollowUpRequest,
    GeneralHandoffCommand,
    HandoffRequest,
    IdempotencyRecord,
    Slot,
    SlotStatus,
)
from .store import InMemoryAppointmentStore


class AppointmentHarness:
    """Authoritative synthetic backend for agent and eval runs.

    The harness validates backend facts and server-owned references. It does
    not decide conversational policy, infer consent, or construct prompts.
    """

    def __init__(
        self,
        store: InMemoryAppointmentStore,
        clock: FixedClock,
        faults: FaultInjector | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.faults = faults or FaultInjector()

    def verify_identity(
        self,
        *,
        session_id: str,
        patient_id: str,
        date_of_birth: str,
        postal_code: str,
    ) -> bool:
        self._raise_fault("verify_identity", FaultPoint.BEFORE_READ)
        with self.store.lock:
            patient = self.store.patients.get(patient_id)
            existing_patient_id = self.store.authorized_sessions.get(session_id)
            verified = bool(
                patient
                and patient.date_of_birth == date_of_birth
                and patient.postal_code == postal_code
                and existing_patient_id in (None, patient_id)
            )
            if verified:
                self.store.authorized_sessions[session_id] = patient_id
            self._audit(
                "identity.verified" if verified else "identity.rejected",
                actor="appointment_harness",
                correlation_id=session_id,
                payload={"patient_id": patient_id},
            )
            return verified

    def confirm_identity_by_name(
        self,
        *,
        session_id: str,
        patient_id: str,
        confirmed: bool,
    ) -> bool:
        """Apply the deliberately low-assurance synthetic demo policy.

        Production identity must replace this policy. It exists only to test the
        conversational state transition without sending identity answers to a model.
        """

        self._raise_fault("confirm_identity_by_name", FaultPoint.BEFORE_READ)
        with self.store.lock:
            patient = self.store.patients.get(patient_id)
            existing_patient_id = self.store.authorized_sessions.get(session_id)
            verified = bool(
                confirmed
                and patient
                and existing_patient_id in (None, patient_id)
            )
            if verified:
                self.store.authorized_sessions[session_id] = patient_id
            self._audit(
                "identity.name_confirmed" if verified else "identity.rejected",
                actor="appointment_harness",
                correlation_id=session_id,
                payload={
                    "patient_id": patient_id,
                    "assurance": "low_demo_only",
                },
            )
            return verified

    def confirm_proxy_authority(
        self,
        *,
        session_id: str,
        patient_id: str,
        caller_display_name: str,
        relationship: str,
    ) -> bool:
        """Authorize a synthetic proxy only when an active stored grant matches."""

        self._raise_fault("confirm_proxy_authority", FaultPoint.BEFORE_READ)
        normalized_name = " ".join(caller_display_name.casefold().split())
        normalized_relationship = relationship.casefold().strip()
        with self.store.lock:
            match = next(
                (
                    authority
                    for authority in self.store.caller_authorities.values()
                    if authority.patient_id == patient_id
                    and authority.status == "active"
                    and " ".join(authority.caller_display_name.casefold().split())
                    == normalized_name
                    and authority.relationship.casefold() == normalized_relationship
                ),
                None,
            )
            existing_patient_id = self.store.authorized_sessions.get(session_id)
            verified = bool(match and existing_patient_id in (None, patient_id))
            if verified and match:
                self.store.authorized_sessions[session_id] = patient_id
                self.store.session_authorities[session_id] = match.authority_id
            self._audit(
                "authority.proxy_confirmed" if verified else "authority.proxy_rejected",
                actor="appointment_harness",
                correlation_id=session_id,
                payload={
                    "patient_id": patient_id,
                    "relationship": normalized_relationship,
                },
            )
            return verified

    def search_slots(
        self,
        *,
        session_id: str,
        patient_id: str,
        appointment_type_id: str,
        provider_id: str | None = None,
        location_id: str | None = None,
        starts_after: datetime | None = None,
        starts_before: datetime | None = None,
    ) -> AvailabilitySnapshot:
        operation = "search_slots"
        self._raise_fault(operation, FaultPoint.BEFORE_READ)
        with self.store.lock:
            self._require_authorized(session_id, patient_id)
            matches = [
                deepcopy(slot)
                for slot in self.store.slots.values()
                if slot.status is SlotStatus.AVAILABLE
                and slot.appointment_type_id == appointment_type_id
                and (provider_id is None or slot.provider_id == provider_id)
                and (location_id is None or slot.location_id == location_id)
                and (starts_after is None or slot.starts_at >= starts_after)
                and (starts_before is None or slot.starts_at < starts_before)
            ]
            matches.sort(key=lambda slot: (slot.starts_at, slot.slot_id))
            self.store.snapshot_sequence += 1
            snapshot_id = f"availability-{self.store.snapshot_sequence:04d}"
            snapshot = AvailabilitySnapshot(
                snapshot_id=snapshot_id,
                patient_id=patient_id,
                created_at=self.clock.now(),
                slots=tuple(matches),
                slot_versions={slot.slot_id: slot.version for slot in matches},
            )
            self.store.availability_snapshots[snapshot_id] = snapshot

            fault = self.faults.trigger(operation, FaultPoint.AFTER_READ)
            if fault is FaultKind.STALE_RESPONSE and matches:
                live_slot = self.store.slots[matches[0].slot_id]
                live_slot.version += 1
            elif fault is FaultKind.TIMEOUT:
                raise SyntheticTimeout("search_slots timed out after reading")
            elif fault is FaultKind.FAILURE:
                raise SyntheticFailure("search_slots failed after reading")

            self._audit(
                "availability.read",
                actor="appointment_harness",
                correlation_id=snapshot_id,
                payload={
                    "patient_id": patient_id,
                    "slot_ids": [slot.slot_id for slot in matches],
                },
            )
            return snapshot

    def get_appointment(
        self,
        *,
        session_id: str,
        patient_id: str,
        appointment_id: str,
        for_final_verification: bool = False,
    ) -> Appointment:
        operation = "get_appointment"
        point = (
            FaultPoint.BEFORE_FINAL_READ
            if for_final_verification
            else FaultPoint.BEFORE_READ
        )
        fault = self.faults.trigger(operation, point)
        if fault in (FaultKind.TIMEOUT, FaultKind.FINAL_READ_CONFLICT):
            if fault is FaultKind.TIMEOUT:
                raise SyntheticTimeout("appointment read timed out")
            raise VersionConflict("synthetic final-state read conflict")
        if fault is FaultKind.FAILURE:
            raise SyntheticFailure("appointment read failed")

        with self.store.lock:
            self._require_authorized(session_id, patient_id)
            appointment = self._owned_appointment(patient_id, appointment_id)
            self._audit(
                "appointment.read",
                actor="appointment_harness",
                correlation_id=appointment_id,
                payload={
                    "patient_id": patient_id,
                    "status": appointment.status.value,
                    "version": appointment.version,
                },
            )
            return deepcopy(appointment)

    def list_appointments(
        self, *, session_id: str, patient_id: str
    ) -> tuple[Appointment, ...]:
        with self.store.lock:
            self._require_authorized(session_id, patient_id)
            values = [
                deepcopy(appointment)
                for appointment in self.store.appointments.values()
                if appointment.patient_id == patient_id
            ]
            values.sort(key=lambda item: (item.starts_at, item.appointment_id))
            return tuple(values)

    def register_confirmation(self, confirmation: ConfirmedProposal) -> None:
        with self.store.lock:
            self._require_authorized(confirmation.session_id, confirmation.patient_id)
            if confirmation.operation not in {
                "create_appointment",
                "edit_appointment",
                "delete_appointment",
            }:
                raise ContractRejected("unsupported confirmation operation")
            key = (confirmation.session_id, confirmation.proposal_id)
            existing = self.store.confirmed_proposals.get(key)
            if existing and existing != confirmation:
                raise ContractRejected("proposal identifier already registered differently")
            self.store.confirmed_proposals[key] = confirmation
            self._audit(
                "confirmation.recorded",
                actor="control_plane",
                correlation_id=confirmation.proposal_id,
                payload={
                    "patient_id": confirmation.patient_id,
                    "operation": confirmation.operation,
                    "appointment_id": confirmation.appointment_id,
                    "slot_id": confirmation.slot_id,
                },
            )

    def create_appointment(
        self, command: CreateAppointmentCommand
    ) -> AppointmentMutationResult:
        operation = "create_appointment"
        fingerprint = self._fingerprint(command)
        with self.store.lock:
            replay = self._idempotent_result(command.idempotency_key, operation, fingerprint)
            if replay:
                return replay
            if not self._is_authorized(command.session_id, command.patient_id):
                return self._result("", AppointmentMutationOutcome.REJECTED)
            slot = self.store.slots.get(command.slot_id)
            if not slot or not self._snapshot_matches(
                command.availability_snapshot_id, command.patient_id, slot
            ):
                return self._result("", AppointmentMutationOutcome.CONFLICT)
            confirmation = self._matching_confirmation(
                session_id=command.session_id,
                patient_id=command.patient_id,
                proposal_id=command.proposal_id,
                token=command.confirmation_token,
                operation=operation,
                appointment_id=None,
                expected_version=None,
                slot_id=command.slot_id,
            )
            if confirmation is None:
                return self._result("", AppointmentMutationOutcome.REJECTED)

            self._apply_precommit_fault(operation, slot)
            if slot.status is not SlotStatus.AVAILABLE or not self._snapshot_matches(
                command.availability_snapshot_id, command.patient_id, slot
            ):
                return self._result("", AppointmentMutationOutcome.CONFLICT)

            self.store.appointment_sequence += 1
            appointment_id = f"appointment-{self.store.appointment_sequence:04d}"
            appointment = Appointment(
                appointment_id=appointment_id,
                patient_id=command.patient_id,
                slot_id=slot.slot_id,
                provider_id=slot.provider_id,
                appointment_type_id=slot.appointment_type_id,
                location_id=slot.location_id,
                starts_at=slot.starts_at,
                status=AppointmentStatus.PROPOSED,
            )
            slot.status = SlotStatus.HELD
            slot.version += 1
            self.store.appointments[appointment_id] = appointment
            confirmation.consumed = True
            result = self._result(
                appointment_id,
                AppointmentMutationOutcome.PROPOSED,
                appointment.version,
            )
            self._record_idempotency(command.idempotency_key, operation, fingerprint, result)
            self._audit_mutation("appointment.proposed", appointment, command.idempotency_key)
            self._raise_after_commit_fault(operation)
            return result

    def confirm_proposed_appointment(
        self, appointment_id: str
    ) -> ClinicConfirmationResult:
        """Approve a new-booking or reschedule proposal and enqueue one call job.

        Repeating the clinic action is idempotent.  Confirmation is committed
        before a provider call is attempted, so a Twilio outage can never roll
        back the clinic's scheduling decision.
        """

        with self.store.lock:
            appointment = self.store.appointments.get(appointment_id)
            if appointment is None:
                raise ContractRejected("appointment proposal does not exist")
            if appointment.pending_replacement_slot_id:
                replacement = self.store.slots[appointment.pending_replacement_slot_id]
                if replacement.status is not SlotStatus.HELD:
                    raise VersionConflict("reschedule request no longer owns its held slot")
                original = self.store.slots[appointment.slot_id]
                if original.status is not SlotStatus.BOOKED:
                    raise VersionConflict("current appointment no longer owns its booked slot")
                approval_reference = appointment.pending_reschedule_id
                if not approval_reference:
                    raise ContractRejected("reschedule approval reference is missing")
                original.status = SlotStatus.AVAILABLE
                original.version += 1
                replacement.status = SlotStatus.BOOKED
                replacement.version += 1
                appointment.slot_id = replacement.slot_id
                appointment.provider_id = replacement.provider_id
                appointment.appointment_type_id = replacement.appointment_type_id
                appointment.location_id = replacement.location_id
                appointment.starts_at = replacement.starts_at
                appointment.status = AppointmentStatus.CONFIRMED
                appointment.pending_replacement_slot_id = None
                appointment.pending_reschedule_id = None
                appointment.version += 1
                notification = self._confirmation_call_job(
                    appointment,
                    idempotency_key=f"reschedule-confirmed:{approval_reference}",
                )
                event_type = "appointment.reschedule_confirmed"
            elif appointment.status is AppointmentStatus.PROPOSED:
                slot = self.store.slots[appointment.slot_id]
                if slot.status is not SlotStatus.HELD:
                    raise VersionConflict("proposed appointment no longer owns its held slot")
                appointment.status = AppointmentStatus.CONFIRMED
                appointment.version += 1
                slot.status = SlotStatus.BOOKED
                slot.version += 1
                notification = self._confirmation_call_job(
                    appointment,
                    idempotency_key=f"appointment-confirmed:{appointment_id}",
                )
                event_type = "appointment.confirmed"
            else:
                existing = next(
                    (
                        item
                        for item in reversed(tuple(self.store.confirmation_calls.values()))
                        if item.appointment_id == appointment_id
                    ),
                    None,
                )
                if appointment.status is AppointmentStatus.CONFIRMED and existing:
                    return ClinicConfirmationResult(
                        appointment_id=appointment_id,
                        appointment_status=appointment.status,
                        notification_id=existing.notification_id,
                        notification_status=existing.status,
                    )
                raise ContractRejected("appointment has no clinic-reviewable proposal")
            self._audit_mutation(
                event_type, appointment, notification.idempotency_key
            )
            self._audit(
                "confirmation_call.requested",
                actor="clinic_dashboard",
                correlation_id=notification.notification_id,
                payload={
                    "appointment_id": appointment_id,
                    "patient_id": appointment.patient_id,
                },
            )
            return ClinicConfirmationResult(
                appointment_id=appointment_id,
                appointment_status=appointment.status,
                notification_id=notification.notification_id,
                notification_status=notification.status,
            )

    def _confirmation_call_job(
        self, appointment: Appointment, *, idempotency_key: str
    ) -> AppointmentConfirmationCall:
        existing = next(
            (
                item
                for item in self.store.confirmation_calls.values()
                if item.idempotency_key == idempotency_key
            ),
            None,
        )
        if existing:
            return existing
        self.store.notification_sequence += 1
        notification_id = f"confirmation-call-{self.store.notification_sequence:04d}"
        now = self.clock.now()
        notification = AppointmentConfirmationCall(
            notification_id=notification_id,
            appointment_id=appointment.appointment_id,
            patient_id=appointment.patient_id,
            status=ConfirmationCallStatus.PENDING,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        self.store.confirmation_calls[notification_id] = notification
        return notification

    def record_confirmation_call_result(
        self,
        notification_id: str,
        *,
        provider_call_id: str | None = None,
        error_class: str | None = None,
    ) -> AppointmentConfirmationCall:
        """Record a placed call or retain the job for a safe explicit retry."""

        with self.store.lock:
            notification = self.store.confirmation_calls.get(notification_id)
            if notification is None:
                raise ContractRejected("confirmation call job does not exist")
            notification.attempts += 1
            notification.updated_at = self.clock.now()
            if provider_call_id:
                notification.status = ConfirmationCallStatus.PLACED
                notification.provider_call_id = provider_call_id
                notification.error_class = None
                event_type = "confirmation_call.placed"
            else:
                notification.status = ConfirmationCallStatus.RETRY_PENDING
                notification.error_class = error_class or "ProviderCallFailed"
                event_type = "confirmation_call.retry_pending"
            self._audit(
                event_type,
                actor="confirmation_call_dispatcher",
                correlation_id=notification_id,
                payload={
                    "appointment_id": notification.appointment_id,
                    "attempts": notification.attempts,
                    "provider_call_id": notification.provider_call_id,
                    "error_class": notification.error_class,
                },
            )
            return deepcopy(notification)

    def edit_appointment(
        self, command: EditAppointmentCommand
    ) -> AppointmentMutationResult:
        operation = "edit_appointment"
        fingerprint = self._fingerprint(command)
        with self.store.lock:
            replay = self._idempotent_result(command.idempotency_key, operation, fingerprint)
            if replay:
                return replay
            if not self._is_authorized(command.session_id, command.patient_id):
                return self._result(command.appointment_id, AppointmentMutationOutcome.REJECTED)
            appointment = self._maybe_owned_appointment(
                command.patient_id, command.appointment_id
            )
            if appointment is None:
                return self._result(command.appointment_id, AppointmentMutationOutcome.REJECTED)
            if (
                appointment.status
                not in {AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED}
                or appointment.version != command.expected_appointment_version
            ):
                return self._result(command.appointment_id, AppointmentMutationOutcome.CONFLICT)
            replacement = self.store.slots.get(command.replacement_slot_id)
            if not replacement or not self._snapshot_matches(
                command.availability_snapshot_id, command.patient_id, replacement
            ):
                return self._result(command.appointment_id, AppointmentMutationOutcome.CONFLICT)
            confirmation = self._matching_confirmation(
                session_id=command.session_id,
                patient_id=command.patient_id,
                proposal_id=command.proposal_id,
                token=command.confirmation_token,
                operation=operation,
                appointment_id=command.appointment_id,
                expected_version=command.expected_appointment_version,
                slot_id=command.replacement_slot_id,
            )
            if confirmation is None:
                return self._result(command.appointment_id, AppointmentMutationOutcome.REJECTED)

            self._apply_precommit_fault(operation, replacement)
            if replacement.status is not SlotStatus.AVAILABLE or not self._snapshot_matches(
                command.availability_snapshot_id, command.patient_id, replacement
            ):
                return self._result(command.appointment_id, AppointmentMutationOutcome.CONFLICT)

            if appointment.pending_replacement_slot_id is not None:
                return self._result(
                    command.appointment_id, AppointmentMutationOutcome.CONFLICT
                )
            replacement.status = SlotStatus.HELD
            replacement.version += 1
            self.store.reschedule_sequence += 1
            appointment.pending_replacement_slot_id = replacement.slot_id
            appointment.pending_reschedule_id = (
                f"reschedule-{self.store.reschedule_sequence:04d}"
            )
            appointment.version += 1
            confirmation.consumed = True
            result = self._result(
                appointment.appointment_id,
                AppointmentMutationOutcome.PROPOSED,
                appointment.version,
                reconciliation_reference=appointment.pending_reschedule_id,
            )
            self._record_idempotency(command.idempotency_key, operation, fingerprint, result)
            self._audit_mutation(
                "appointment.reschedule_proposed", appointment, command.idempotency_key
            )
            self._raise_after_commit_fault(operation)
            return result

    def delete_appointment(
        self, command: DeleteAppointmentCommand
    ) -> AppointmentMutationResult:
        operation = "delete_appointment"
        fingerprint = self._fingerprint(command)
        with self.store.lock:
            replay = self._idempotent_result(command.idempotency_key, operation, fingerprint)
            if replay:
                return replay
            if not self._is_authorized(command.session_id, command.patient_id):
                return self._result(command.appointment_id, AppointmentMutationOutcome.REJECTED)
            appointment = self._maybe_owned_appointment(
                command.patient_id, command.appointment_id
            )
            if appointment is None:
                return self._result(command.appointment_id, AppointmentMutationOutcome.REJECTED)
            if (
                appointment.status
                not in {AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED}
                or appointment.version != command.expected_appointment_version
            ):
                return self._result(command.appointment_id, AppointmentMutationOutcome.CONFLICT)
            confirmation = self._matching_confirmation(
                session_id=command.session_id,
                patient_id=command.patient_id,
                proposal_id=command.proposal_id,
                token=command.confirmation_token,
                operation=operation,
                appointment_id=command.appointment_id,
                expected_version=command.expected_appointment_version,
                slot_id=None,
            )
            if confirmation is None:
                return self._result(command.appointment_id, AppointmentMutationOutcome.REJECTED)

            self._apply_precommit_fault(operation)
            slot = self.store.slots[appointment.slot_id]
            slot.status = SlotStatus.AVAILABLE
            slot.version += 1
            appointment.status = AppointmentStatus.CANCELLED
            appointment.cancelled_at = self.clock.now()
            appointment.version += 1
            confirmation.consumed = True
            result = self._result(
                appointment.appointment_id,
                AppointmentMutationOutcome.CANCELLED,
                appointment.version,
            )
            self._record_idempotency(command.idempotency_key, operation, fingerprint, result)
            self._audit_mutation("appointment.cancelled", appointment, command.idempotency_key)
            self._raise_after_commit_fault(operation)
            return result

    def request_front_desk_follow_up(
        self, command: PostCancellationFollowUpCommand
    ) -> FollowUpResult:
        operation = "request_front_desk_follow_up"
        fingerprint = self._fingerprint(command)
        with self.store.lock:
            replay = self._idempotent_result(command.idempotency_key, operation, fingerprint)
            if replay:
                return replay
            self._require_authorized(command.session_id, command.patient_id)
            appointment = self._owned_appointment(
                command.patient_id, command.cancelled_appointment_id
            )
            if appointment.status is not AppointmentStatus.CANCELLED:
                raise ContractRejected("follow-up requires a verified cancelled appointment")

            fault = self.faults.trigger(operation, FaultPoint.FOLLOW_UP)
            if fault is FaultKind.FOLLOW_UP_OUTAGE:
                outcome = FollowUpOutcome.FAILED
            elif fault is FaultKind.TIMEOUT:
                outcome = FollowUpOutcome.PENDING
            elif fault is FaultKind.FAILURE:
                outcome = FollowUpOutcome.FAILED
            else:
                outcome = FollowUpOutcome.ACCEPTED

            self.store.handoff_sequence += 1
            handoff_id = f"handoff-{self.store.handoff_sequence:04d}"
            request = FollowUpRequest(
                handoff_id=handoff_id,
                patient_id=command.patient_id,
                cancelled_appointment_id=command.cancelled_appointment_id,
                reason=command.reason,
                topics=command.follow_up_topics,
                status=outcome.value,
                created_at=self.clock.now(),
            )
            self.store.follow_ups[handoff_id] = request
            result = FollowUpResult(outcome=outcome, handoff_id=handoff_id)
            self._record_idempotency(command.idempotency_key, operation, fingerprint, result)
            self._audit(
                f"follow_up.{outcome.value}",
                actor="front_desk_gateway",
                correlation_id=handoff_id,
                payload={
                    "patient_id": command.patient_id,
                    "appointment_id": command.cancelled_appointment_id,
                    "topics": list(command.follow_up_topics),
                },
            )
            return result

    def lookup_by_idempotency_key(self, key: str) -> Any | None:
        with self.store.lock:
            record = self.store.idempotency_records.get(key)
            return deepcopy(record.result) if record else None

    def request_general_handoff(
        self, command: GeneralHandoffCommand
    ) -> FollowUpResult:
        """Create a front-desk handoff even when patient authority is unresolved."""

        operation = "request_general_handoff"
        fingerprint = self._fingerprint(command)
        with self.store.lock:
            replay = self._idempotent_result(
                command.idempotency_key, operation, fingerprint
            )
            if replay:
                return replay
            fault = self.faults.trigger(operation, FaultPoint.FOLLOW_UP)
            if fault is FaultKind.TIMEOUT:
                outcome = FollowUpOutcome.PENDING
            elif fault in (FaultKind.FAILURE, FaultKind.FOLLOW_UP_OUTAGE):
                outcome = FollowUpOutcome.FAILED
            else:
                outcome = FollowUpOutcome.ACCEPTED

            self.store.handoff_sequence += 1
            handoff_id = f"handoff-{self.store.handoff_sequence:04d}"
            self.store.general_handoffs[handoff_id] = HandoffRequest(
                handoff_id=handoff_id,
                session_id=command.session_id,
                patient_id=command.patient_id,
                reason=command.reason,
                topics=command.topics,
                status=outcome.value,
                created_at=self.clock.now(),
            )
            result = FollowUpResult(outcome=outcome, handoff_id=handoff_id)
            self._record_idempotency(
                command.idempotency_key, operation, fingerprint, result
            )
            self._audit(
                f"handoff.{outcome.value}",
                actor="front_desk_gateway",
                correlation_id=handoff_id,
                payload={
                    "patient_id": command.patient_id,
                    "reason": command.reason,
                    "topics": list(command.topics),
                },
            )
            return result

    def verify_final_state(
        self,
        *,
        session_id: str,
        patient_id: str,
        appointment_id: str,
        expected_status: AppointmentStatus,
        expected_slot_id: str | None = None,
    ) -> Appointment:
        appointment = self.get_appointment(
            session_id=session_id,
            patient_id=patient_id,
            appointment_id=appointment_id,
            for_final_verification=True,
        )
        if appointment.status is not expected_status:
            raise VersionConflict("final appointment status does not match")
        if expected_slot_id and appointment.slot_id != expected_slot_id:
            raise VersionConflict("final appointment slot does not match")
        self._audit(
            "appointment.verified",
            actor="appointment_harness",
            correlation_id=appointment_id,
            payload={
                "patient_id": patient_id,
                "status": appointment.status.value,
                "slot_id": appointment.slot_id,
                "pending_replacement_slot_id": appointment.pending_replacement_slot_id,
                "pending_reschedule_id": appointment.pending_reschedule_id,
            },
        )
        return appointment

    def _require_authorized(self, session_id: str, patient_id: str) -> None:
        if not self._is_authorized(session_id, patient_id):
            raise ContractRejected("session is not authorized for this patient")

    def _is_authorized(self, session_id: str, patient_id: str) -> bool:
        return self.store.authorized_sessions.get(session_id) == patient_id

    def _maybe_owned_appointment(
        self, patient_id: str, appointment_id: str
    ) -> Appointment | None:
        appointment = self.store.appointments.get(appointment_id)
        if appointment is None or appointment.patient_id != patient_id:
            return None
        return appointment

    def _owned_appointment(self, patient_id: str, appointment_id: str) -> Appointment:
        appointment = self._maybe_owned_appointment(patient_id, appointment_id)
        if appointment is None:
            raise ContractRejected("appointment does not belong to the verified patient")
        return appointment

    def _snapshot_matches(
        self, snapshot_id: str, patient_id: str, slot: Slot
    ) -> bool:
        snapshot = self.store.availability_snapshots.get(snapshot_id)
        return bool(
            snapshot
            and snapshot.patient_id == patient_id
            and snapshot.slot_versions.get(slot.slot_id) == slot.version
        )

    def _matching_confirmation(
        self,
        *,
        session_id: str,
        patient_id: str,
        proposal_id: str,
        token: str,
        operation: str,
        appointment_id: str | None,
        expected_version: int | None,
        slot_id: str | None,
    ) -> ConfirmedProposal | None:
        confirmation = self.store.confirmed_proposals.get((session_id, proposal_id))
        if confirmation is None or confirmation.consumed:
            return None
        if (
            confirmation.patient_id != patient_id
            or confirmation.confirmation_token != token
            or confirmation.operation != operation
            or confirmation.appointment_id != appointment_id
            or confirmation.expected_appointment_version != expected_version
            or confirmation.slot_id != slot_id
        ):
            return None
        return confirmation

    def _apply_precommit_fault(
        self, operation: str, slot: Slot | None = None
    ) -> None:
        fault = self.faults.trigger(operation, FaultPoint.BEFORE_COMMIT)
        if fault is FaultKind.SLOT_RACE and slot is not None:
            slot.status = SlotStatus.UNAVAILABLE
            slot.version += 1
            self._audit(
                "slot.race_injected",
                actor="fault_injector",
                correlation_id=slot.slot_id,
                payload={"slot_id": slot.slot_id},
            )
        elif fault is FaultKind.TIMEOUT:
            raise SyntheticTimeout(f"{operation} timed out before commit")
        elif fault is FaultKind.FAILURE:
            raise SyntheticFailure(f"{operation} failed before commit")

    def _raise_after_commit_fault(self, operation: str) -> None:
        fault = self.faults.trigger(operation, FaultPoint.AFTER_COMMIT)
        if fault is FaultKind.TIMEOUT:
            raise SyntheticTimeout(f"{operation} committed but response timed out")
        if fault is FaultKind.FAILURE:
            raise SyntheticFailure(f"{operation} committed but response failed")

    def _raise_fault(self, operation: str, point: FaultPoint) -> None:
        fault = self.faults.trigger(operation, point)
        if fault is FaultKind.TIMEOUT:
            raise SyntheticTimeout(f"{operation} timed out at {point.value}")
        if fault is FaultKind.FAILURE:
            raise SyntheticFailure(f"{operation} failed at {point.value}")

    def _idempotent_result(
        self, key: str, operation: str, fingerprint: str
    ) -> Any | None:
        record = self.store.idempotency_records.get(key)
        if record is None:
            return None
        if record.operation != operation or record.request_fingerprint != fingerprint:
            raise IdempotencyConflict("idempotency key reused for a different request")
        return deepcopy(record.result)

    def _record_idempotency(
        self, key: str, operation: str, fingerprint: str, result: Any
    ) -> None:
        self.store.idempotency_records[key] = IdempotencyRecord(
            key=key,
            operation=operation,
            request_fingerprint=fingerprint,
            result=deepcopy(result),
        )

    @staticmethod
    def _fingerprint(command: Any) -> str:
        serialized = dumps(asdict(command), sort_keys=True, default=str)
        return sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _result(
        appointment_id: str,
        outcome: AppointmentMutationOutcome,
        version: int | None = None,
        *,
        reconciliation_reference: str | None = None,
    ) -> AppointmentMutationResult:
        return AppointmentMutationResult(
            appointment_id=appointment_id,
            outcome=outcome,
            appointment_version=version,
            reconciliation_reference=reconciliation_reference,
        )

    def _audit_mutation(
        self, event_type: str, appointment: Appointment, correlation_id: str
    ) -> None:
        self._audit(
            event_type,
            actor="appointment_harness",
            correlation_id=correlation_id,
            payload={
                "appointment_id": appointment.appointment_id,
                "patient_id": appointment.patient_id,
                "slot_id": appointment.slot_id,
                "status": appointment.status.value,
                "version": appointment.version,
            },
        )

    def _audit(
        self,
        event_type: str,
        *,
        actor: str,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> None:
        with self.store.lock:
            self.store.audit_sequence += 1
            self.store.audit_log.append(
                AuditEvent(
                    sequence=self.store.audit_sequence,
                    occurred_at=self.clock.now(),
                    event_type=event_type,
                    actor=actor,
                    correlation_id=correlation_id,
                    payload=deepcopy(payload),
                )
            )
