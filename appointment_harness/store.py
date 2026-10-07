"""Mutable state owned exclusively by the synthetic scheduling backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from .models import (
    Appointment,
    AppointmentConfirmationCall,
    AppointmentType,
    AuditEvent,
    AvailabilitySnapshot,
    CallerAuthority,
    ConfirmedProposal,
    FollowUpRequest,
    HandoffRequest,
    IdempotencyRecord,
    Patient,
    Provider,
    Slot,
)


@dataclass(slots=True)
class InMemoryAppointmentStore:
    patients: dict[str, Patient] = field(default_factory=dict)
    caller_authorities: dict[str, CallerAuthority] = field(default_factory=dict)
    providers: dict[str, Provider] = field(default_factory=dict)
    appointment_types: dict[str, AppointmentType] = field(default_factory=dict)
    slots: dict[str, Slot] = field(default_factory=dict)
    appointments: dict[str, Appointment] = field(default_factory=dict)
    confirmation_calls: dict[str, AppointmentConfirmationCall] = field(default_factory=dict)
    availability_snapshots: dict[str, AvailabilitySnapshot] = field(default_factory=dict)
    authorized_sessions: dict[str, str] = field(default_factory=dict)
    session_authorities: dict[str, str] = field(default_factory=dict)
    confirmed_proposals: dict[tuple[str, str], ConfirmedProposal] = field(default_factory=dict)
    idempotency_records: dict[str, IdempotencyRecord] = field(default_factory=dict)
    follow_ups: dict[str, FollowUpRequest] = field(default_factory=dict)
    general_handoffs: dict[str, HandoffRequest] = field(default_factory=dict)
    audit_log: list[AuditEvent] = field(default_factory=list)
    lock: RLock = field(default_factory=RLock)
    appointment_sequence: int = 0
    reschedule_sequence: int = 0
    notification_sequence: int = 0
    snapshot_sequence: int = 0
    handoff_sequence: int = 0
    audit_sequence: int = 0
