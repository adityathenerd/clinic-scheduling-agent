"""Domain records for the synthetic appointment harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


def _required(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} is required")


class SlotStatus(str, Enum):
    AVAILABLE = "available"
    BOOKED = "booked"
    HELD = "held"
    UNAVAILABLE = "unavailable"


class AppointmentStatus(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"


class ConfirmationCallStatus(str, Enum):
    PENDING = "pending"
    PLACED = "placed"
    RETRY_PENDING = "retry_pending"


@dataclass(frozen=True, slots=True)
class Patient:
    patient_id: str
    display_name: str
    date_of_birth: str
    postal_code: str
    phone_e164: str = ""
    preferred_language: str = "en"


@dataclass(frozen=True, slots=True)
class Provider:
    provider_id: str
    display_name: str
    appointment_type_ids: tuple[str, ...]
    location_ids: tuple[str, ...]
    specialty: str = ""


@dataclass(frozen=True, slots=True)
class CallerAuthority:
    authority_id: str
    patient_id: str
    caller_display_name: str
    relationship: str
    status: str = "active"


@dataclass(frozen=True, slots=True)
class AppointmentType:
    appointment_type_id: str
    display_name: str
    duration_minutes: int
    prerequisites: tuple[str, ...] = ()


@dataclass(slots=True)
class Slot:
    slot_id: str
    provider_id: str
    appointment_type_id: str
    location_id: str
    starts_at: datetime
    duration_minutes: int
    status: SlotStatus = SlotStatus.AVAILABLE
    version: int = 1


@dataclass(slots=True)
class Appointment:
    appointment_id: str
    patient_id: str
    slot_id: str
    provider_id: str
    appointment_type_id: str
    location_id: str
    starts_at: datetime
    status: AppointmentStatus = AppointmentStatus.SCHEDULED
    version: int = 1
    cancelled_at: datetime | None = None
    pending_replacement_slot_id: str | None = None
    pending_reschedule_id: str | None = None
    visit_reason: str = ""
    prior_treatment_context: str = ""


@dataclass(slots=True)
class AppointmentConfirmationCall:
    notification_id: str
    appointment_id: str
    patient_id: str
    status: ConfirmationCallStatus
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    attempts: int = 0
    provider_call_id: str | None = None
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class ClinicConfirmationResult:
    appointment_id: str
    appointment_status: AppointmentStatus
    notification_id: str
    notification_status: ConfirmationCallStatus


@dataclass(frozen=True, slots=True)
class AvailabilitySnapshot:
    snapshot_id: str
    patient_id: str
    created_at: datetime
    slots: tuple[Slot, ...]
    slot_versions: Mapping[str, int]


@dataclass(slots=True)
class ConfirmedProposal:
    session_id: str
    patient_id: str
    proposal_id: str
    confirmation_token: str
    operation: str
    appointment_id: str | None = None
    expected_appointment_version: int | None = None
    slot_id: str | None = None
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class CreateAppointmentCommand:
    session_id: str
    patient_id: str
    slot_id: str
    availability_snapshot_id: str
    proposal_id: str
    confirmation_token: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in (
            "session_id",
            "patient_id",
            "slot_id",
            "availability_snapshot_id",
            "proposal_id",
            "confirmation_token",
            "idempotency_key",
        ):
            _required(getattr(self, field_name), field_name)


@dataclass(slots=True)
class FollowUpRequest:
    handoff_id: str
    patient_id: str
    cancelled_appointment_id: str
    reason: str
    topics: tuple[str, ...]
    status: str
    created_at: datetime


@dataclass(slots=True)
class HandoffRequest:
    handoff_id: str
    session_id: str
    patient_id: str | None
    reason: str
    topics: tuple[str, ...]
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GeneralHandoffCommand:
    session_id: str
    patient_id: str | None
    reason: str
    topics: tuple[str, ...]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class AuditEvent:
    sequence: int
    occurred_at: datetime
    event_type: str
    actor: str
    correlation_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    key: str
    operation: str
    request_fingerprint: str
    result: Any
