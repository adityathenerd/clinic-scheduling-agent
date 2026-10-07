"""JSON fixture loading for reproducible clinic states."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from .clock import FixedClock
from .models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    CallerAuthority,
    Patient,
    Provider,
    Slot,
    SlotStatus,
)
from .store import InMemoryAppointmentStore


DEFAULT_FIXTURE = Path(__file__).with_name("fixtures") / "clinic.json"


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include an offset: {value}")
    return parsed


def load_fixture(
    path: str | Path = DEFAULT_FIXTURE,
) -> tuple[InMemoryAppointmentStore, FixedClock]:
    fixture_path = Path(path)
    data: dict[str, Any] = json.loads(fixture_path.read_text(encoding="utf-8"))
    store = InMemoryAppointmentStore()
    clock_value = _timestamp(data["clock"])

    for value in data.get("patients", []):
        patient = Patient(**value)
        store.patients[patient.patient_id] = patient

    for value in data.get("caller_authorities", []):
        authority = CallerAuthority(**value)
        store.caller_authorities[authority.authority_id] = authority

    for value in data.get("appointment_types", []):
        appointment_type = AppointmentType(
            appointment_type_id=value["appointment_type_id"],
            display_name=value["display_name"],
            duration_minutes=value["duration_minutes"],
            prerequisites=tuple(value.get("prerequisites", [])),
        )
        store.appointment_types[appointment_type.appointment_type_id] = appointment_type

    for value in data.get("providers", []):
        provider = Provider(
            provider_id=value["provider_id"],
            display_name=value["display_name"],
            appointment_type_ids=tuple(value["appointment_type_ids"]),
            location_ids=tuple(value["location_ids"]),
            specialty=value.get("specialty", ""),
        )
        store.providers[provider.provider_id] = provider

    for value in data.get("slots", []):
        slot = Slot(
            slot_id=value["slot_id"],
            provider_id=value["provider_id"],
            appointment_type_id=value["appointment_type_id"],
            location_id=value["location_id"],
            starts_at=_timestamp(value["starts_at"]),
            duration_minutes=value["duration_minutes"],
            status=SlotStatus(value.get("status", "available")),
            version=value.get("version", 1),
        )
        store.slots[slot.slot_id] = slot

    # Compact, explicit provider calendars make multi-day availability readable
    # in the fixture while still expanding to ordinary authoritative Slot rows.
    for calendar in data.get("availability_calendars", []):
        provider_id = str(calendar["provider_id"])
        appointment_type_id = str(calendar["appointment_type_id"])
        location_id = str(calendar["location_id"])
        duration_minutes = int(calendar["duration_minutes"])
        provider = store.providers.get(provider_id)
        if provider is None:
            raise ValueError(f"availability calendar has unknown provider: {provider_id}")
        if appointment_type_id not in provider.appointment_type_ids:
            raise ValueError(
                f"provider {provider_id} does not support {appointment_type_id}"
            )
        if location_id not in provider.location_ids:
            raise ValueError(
                f"provider {provider_id} does not work at {location_id}"
            )
        for date_text, times in calendar.get("dates", {}).items():
            calendar_date = date.fromisoformat(str(date_text))
            for time_text in times:
                calendar_time = time.fromisoformat(str(time_text))
                starts_at = datetime.combine(
                    calendar_date,
                    calendar_time,
                    tzinfo=clock_value.tzinfo,
                )
                provider_slug = provider_id.removeprefix("provider-")
                slot_id = (
                    f"slot-{provider_slug}-{calendar_date:%Y%m%d}-"
                    f"{calendar_time:%H%M}"
                )
                if slot_id in store.slots:
                    raise ValueError(f"duplicate generated slot: {slot_id}")
                store.slots[slot_id] = Slot(
                    slot_id=slot_id,
                    provider_id=provider_id,
                    appointment_type_id=appointment_type_id,
                    location_id=location_id,
                    starts_at=starts_at,
                    duration_minutes=duration_minutes,
                    status=SlotStatus.AVAILABLE,
                    version=1,
                )

    for value in data.get("appointments", []):
        appointment = Appointment(
            appointment_id=value["appointment_id"],
            patient_id=value["patient_id"],
            slot_id=value["slot_id"],
            provider_id=value["provider_id"],
            appointment_type_id=value["appointment_type_id"],
            location_id=value["location_id"],
            starts_at=_timestamp(value["starts_at"]),
            status=AppointmentStatus(value.get("status", "scheduled")),
            version=value.get("version", 1),
            cancelled_at=(
                _timestamp(value["cancelled_at"])
                if value.get("cancelled_at")
                else None
            ),
            visit_reason=str(value.get("visit_reason", "")),
            prior_treatment_context=str(
                value.get("prior_treatment_context", "")
            ),
        )
        store.appointments[appointment.appointment_id] = appointment

    return store, FixedClock(clock_value)
