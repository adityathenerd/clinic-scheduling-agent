"""Deterministic synthetic appointment backend used by agent evaluations."""

from .clock import FixedClock
from .faults import Fault, FaultInjector, FaultKind, FaultPoint
from .fixtures import load_fixture
from .models import (
    Appointment,
    AppointmentConfirmationCall,
    AppointmentStatus,
    AppointmentType,
    AuditEvent,
    AvailabilitySnapshot,
    ConfirmedProposal,
    ConfirmationCallStatus,
    ClinicConfirmationResult,
    CreateAppointmentCommand,
    FollowUpRequest,
    Patient,
    Provider,
    Slot,
    SlotStatus,
)
from .service import AppointmentHarness
from .store import InMemoryAppointmentStore

__all__ = [
    "Appointment",
    "AppointmentConfirmationCall",
    "AppointmentHarness",
    "AppointmentStatus",
    "AppointmentType",
    "AuditEvent",
    "AvailabilitySnapshot",
    "ConfirmedProposal",
    "ConfirmationCallStatus",
    "ClinicConfirmationResult",
    "CreateAppointmentCommand",
    "Fault",
    "FaultInjector",
    "FaultKind",
    "FaultPoint",
    "FixedClock",
    "FollowUpRequest",
    "InMemoryAppointmentStore",
    "Patient",
    "Provider",
    "Slot",
    "SlotStatus",
    "load_fixture",
]
