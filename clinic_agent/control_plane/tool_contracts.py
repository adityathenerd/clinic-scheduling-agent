"""Typed boundaries for rescheduling, cancellation, and cancellation follow-up.

Model-facing schemas contain only domain arguments. Authorization evidence,
versions, proposal references, and idempotency keys are server-owned values
attached by the Python control plane after its guards pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol


def _require(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} is required")


class AppointmentMutationOutcome(str, Enum):
    PROPOSED = "proposed"
    CREATED = "created"
    UPDATED = "updated"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class FollowUpOutcome(str, Enum):
    ACCEPTED = "accepted"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EditAppointmentCommand:
    session_id: str
    patient_id: str
    appointment_id: str
    expected_appointment_version: int
    replacement_slot_id: str
    availability_snapshot_id: str
    proposal_id: str
    confirmation_token: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in (
            "session_id",
            "patient_id",
            "appointment_id",
            "replacement_slot_id",
            "availability_snapshot_id",
            "proposal_id",
            "confirmation_token",
            "idempotency_key",
        ):
            _require(getattr(self, field_name), field_name)
        if self.expected_appointment_version < 0:
            raise ValueError("expected_appointment_version must be non-negative")


@dataclass(frozen=True, slots=True)
class DeleteAppointmentCommand:
    session_id: str
    patient_id: str
    appointment_id: str
    expected_appointment_version: int
    proposal_id: str
    confirmation_token: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in (
            "session_id",
            "patient_id",
            "appointment_id",
            "proposal_id",
            "confirmation_token",
            "idempotency_key",
        ):
            _require(getattr(self, field_name), field_name)
        if self.expected_appointment_version < 0:
            raise ValueError("expected_appointment_version must be non-negative")


@dataclass(frozen=True, slots=True)
class PostCancellationFollowUpCommand:
    session_id: str
    patient_id: str
    cancelled_appointment_id: str
    follow_up_topics: tuple[str, ...]
    idempotency_key: str
    reason: str = "post_cancellation_follow_up"

    def __post_init__(self) -> None:
        for field_name in (
            "session_id",
            "patient_id",
            "cancelled_appointment_id",
            "idempotency_key",
            "reason",
        ):
            _require(getattr(self, field_name), field_name)
        if self.reason != "post_cancellation_follow_up":
            raise ValueError("post-cancellation follow-up reason is fixed by policy")
        if any(not topic.strip() for topic in self.follow_up_topics):
            raise ValueError("follow_up_topics cannot contain blank values")


@dataclass(frozen=True, slots=True)
class AppointmentMutationResult:
    appointment_id: str
    outcome: AppointmentMutationOutcome
    appointment_version: int | None = None
    reconciliation_reference: str | None = None


@dataclass(frozen=True, slots=True)
class FollowUpResult:
    outcome: FollowUpOutcome
    handoff_id: str | None = None


class SchedulingMutationGateway(Protocol):
    async def edit_appointment(
        self, command: EditAppointmentCommand
    ) -> AppointmentMutationResult: ...

    async def delete_appointment(
        self, command: DeleteAppointmentCommand
    ) -> AppointmentMutationResult: ...


class FrontDeskFollowUpGateway(Protocol):
    async def request_follow_up(
        self, command: PostCancellationFollowUpCommand
    ) -> FollowUpResult: ...


EDIT_APPOINTMENT_TOOL: Mapping[str, Any] = {
    "type": "function",
    "name": "edit_appointment",
    "description": (
        "Request rescheduling of the verified patient's selected appointment "
        "to a freshly offered replacement slot. Application guards decide "
        "whether the request is authorized."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "appointment_id": {"type": "string"},
            "replacement_slot_id": {"type": "string"},
        },
        "required": ["appointment_id", "replacement_slot_id"],
        "additionalProperties": False,
    },
}


DELETE_APPOINTMENT_TOOL: Mapping[str, Any] = {
    "type": "function",
    "name": "delete_appointment",
    "description": (
        "Request cancellation of the verified patient's selected appointment. "
        "The backend retains an auditable cancelled record, and the application "
        "creates a front-desk follow-up after verification."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "appointment_id": {"type": "string"},
        },
        "required": ["appointment_id"],
        "additionalProperties": False,
    },
}
