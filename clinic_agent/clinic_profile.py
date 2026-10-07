"""Single source of truth for the synthetic clinic's patient-facing identity."""

from __future__ import annotations

from datetime import timedelta, timezone


CLINIC_NAME = "2care Clinic"
CLINIC_ADDRESS = "Koramangala, Bengaluru"
CLINIC_TIMEZONE_NAME = "Asia/Kolkata"
CLINIC_TIMEZONE_LABEL = "IST"
CLINIC_TIMEZONE = timezone(timedelta(hours=5, minutes=30), name=CLINIC_TIMEZONE_LABEL)

# Keep the persisted fixture identifier stable so existing SQLite databases,
# appointments, slots, and captured regression logs remain replayable.
LEGACY_LOCATION_ID = "downtown"
PATIENT_FACING_LOCATIONS = {
    LEGACY_LOCATION_ID: f"{CLINIC_NAME}, {CLINIC_ADDRESS}",
}


def patient_facing_location(location_id: str) -> str:
    """Return a spoken label without leaking the legacy storage identifier."""

    return PATIENT_FACING_LOCATIONS.get(
        location_id,
        f"{location_id.replace('-', ' ').title()} clinic",
    )


__all__ = [
    "CLINIC_ADDRESS",
    "CLINIC_NAME",
    "CLINIC_TIMEZONE",
    "CLINIC_TIMEZONE_LABEL",
    "CLINIC_TIMEZONE_NAME",
    "LEGACY_LOCATION_ID",
    "PATIENT_FACING_LOCATIONS",
    "patient_facing_location",
]
