"""SQLite persistence for the synthetic clinic scheduling harness."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from clinic_agent.control_plane.tool_contracts import (
    AppointmentMutationOutcome,
    AppointmentMutationResult,
    FollowUpOutcome,
    FollowUpResult,
)

from .clock import FixedClock
from .faults import FaultInjector
from .fixtures import DEFAULT_FIXTURE, load_fixture
from .models import (
    Appointment,
    AppointmentConfirmationCall,
    AppointmentStatus,
    AppointmentType,
    AuditEvent,
    AvailabilitySnapshot,
    CallerAuthority,
    ConfirmedProposal,
    ConfirmationCallStatus,
    ClinicConfirmationResult,
    FollowUpRequest,
    HandoffRequest,
    IdempotencyRecord,
    Patient,
    Provider,
    Slot,
    SlotStatus,
)
from .service import AppointmentHarness
from .store import InMemoryAppointmentStore


SCHEMA_VERSION = 5


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clinic_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
    patient_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    date_of_birth TEXT NOT NULL DEFAULT '',
    postal_code TEXT NOT NULL DEFAULT '',
    phone_e164 TEXT NOT NULL DEFAULT '',
    preferred_language TEXT NOT NULL DEFAULT 'en'
);

CREATE TABLE IF NOT EXISTS caller_authorities (
    authority_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    caller_display_name TEXT NOT NULL,
    relationship TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired'))
);

CREATE TABLE IF NOT EXISTS appointment_types (
    appointment_type_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
    prerequisites_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS providers (
    provider_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    specialty TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS provider_appointment_types (
    provider_id TEXT NOT NULL REFERENCES providers(provider_id),
    appointment_type_id TEXT NOT NULL REFERENCES appointment_types(appointment_type_id),
    PRIMARY KEY (provider_id, appointment_type_id)
);

CREATE TABLE IF NOT EXISTS provider_locations (
    provider_id TEXT NOT NULL REFERENCES providers(provider_id),
    location_id TEXT NOT NULL,
    PRIMARY KEY (provider_id, location_id)
);

CREATE TABLE IF NOT EXISTS slots (
    slot_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES providers(provider_id),
    appointment_type_id TEXT NOT NULL REFERENCES appointment_types(appointment_type_id),
    location_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
    status TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0)
);

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    slot_id TEXT NOT NULL REFERENCES slots(slot_id),
    provider_id TEXT NOT NULL REFERENCES providers(provider_id),
    appointment_type_id TEXT NOT NULL REFERENCES appointment_types(appointment_type_id),
    location_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0),
    cancelled_at TEXT,
    pending_replacement_slot_id TEXT,
    pending_reschedule_id TEXT,
    visit_reason TEXT NOT NULL DEFAULT '',
    prior_treatment_context TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS appointment_confirmation_calls (
    notification_id TEXT PRIMARY KEY,
    appointment_id TEXT NOT NULL REFERENCES appointments(appointment_id),
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    provider_call_id TEXT,
    error_class TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    patient_id TEXT REFERENCES patients(patient_id),
    identity_status TEXT NOT NULL DEFAULT 'unverified',
    authority_id TEXT REFERENCES caller_authorities(authority_id),
    authority_mode TEXT,
    workflow_state TEXT NOT NULL DEFAULT 'awaiting_intent',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS availability_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS availability_snapshot_slots (
    snapshot_id TEXT NOT NULL REFERENCES availability_snapshots(snapshot_id) ON DELETE CASCADE,
    slot_id TEXT NOT NULL,
    slot_version INTEGER NOT NULL,
    slot_json TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, slot_id)
);

CREATE TABLE IF NOT EXISTS confirmed_proposals (
    session_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    confirmation_token TEXT NOT NULL,
    operation TEXT NOT NULL,
    appointment_id TEXT,
    expected_appointment_version INTEGER,
    slot_id TEXT,
    consumed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, proposal_id)
);

CREATE TABLE IF NOT EXISTS idempotency_records (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    session_id TEXT,
    patient_id TEXT REFERENCES patients(patient_id),
    cancelled_appointment_id TEXT,
    reason TEXT NOT NULL,
    topics_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_slots_starts_at ON slots(starts_at);
CREATE INDEX IF NOT EXISTS idx_appointments_patient ON appointments(patient_id, starts_at);
CREATE INDEX IF NOT EXISTS idx_confirmation_calls_status ON appointment_confirmation_calls(status, created_at);
CREATE INDEX IF NOT EXISTS idx_handoffs_session ON handoffs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit_events(correlation_id, sequence);
"""


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SQLiteClinicRepository:
    """Durable relational snapshot and query boundary for one demo clinic."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA_SQL)
            snapshot_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(availability_snapshot_slots)"
                )
            }
            if "position" not in snapshot_columns:
                connection.execute(
                    "ALTER TABLE availability_snapshot_slots "
                    "ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
                )
            appointment_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(appointments)")
            }
            if "pending_replacement_slot_id" not in appointment_columns:
                connection.execute(
                    "ALTER TABLE appointments ADD COLUMN pending_replacement_slot_id TEXT"
                )
            if "pending_reschedule_id" not in appointment_columns:
                connection.execute(
                    "ALTER TABLE appointments ADD COLUMN pending_reschedule_id TEXT"
                )
            if "visit_reason" not in appointment_columns:
                connection.execute(
                    "ALTER TABLE appointments "
                    "ADD COLUMN visit_reason TEXT NOT NULL DEFAULT ''"
                )
            if "prior_treatment_context" not in appointment_columns:
                connection.execute(
                    "ALTER TABLE appointments "
                    "ADD COLUMN prior_treatment_context TEXT NOT NULL DEFAULT ''"
                )
            call_table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='appointment_confirmation_calls'"
            ).fetchone()
            if call_table_sql and "APPOINTMENT_ID TEXT NOT NULL UNIQUE" in str(
                call_table_sql["sql"]
            ).upper():
                connection.executescript(
                    """
                    ALTER TABLE appointment_confirmation_calls RENAME TO appointment_confirmation_calls_v3;
                    CREATE TABLE appointment_confirmation_calls (
                        notification_id TEXT PRIMARY KEY,
                        appointment_id TEXT NOT NULL REFERENCES appointments(appointment_id),
                        patient_id TEXT NOT NULL REFERENCES patients(patient_id),
                        status TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                        provider_call_id TEXT,
                        error_class TEXT
                    );
                    INSERT INTO appointment_confirmation_calls
                    SELECT * FROM appointment_confirmation_calls_v3;
                    DROP TABLE appointment_confirmation_calls_v3;
                    """
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now().astimezone().isoformat()),
            )

    def has_patients(self) -> bool:
        self.initialize_schema()
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM patients").fetchone()
            return bool(row and row["count"])

    def sync_store(self, store: InMemoryAppointmentStore, clock: FixedClock) -> None:
        self.initialize_schema()
        with store.lock, self.connect() as connection:
            for table in (
                "sessions",
                "availability_snapshot_slots",
                "availability_snapshots",
                "confirmed_proposals",
                "idempotency_records",
                "handoffs",
                "audit_events",
                "appointment_confirmation_calls",
                "appointments",
                "slots",
                "provider_locations",
                "provider_appointment_types",
                "providers",
                "appointment_types",
                "caller_authorities",
                "patients",
            ):
                connection.execute(f"DELETE FROM {table}")

            connection.executemany(
                """INSERT INTO patients
                (patient_id, display_name, date_of_birth, postal_code, phone_e164, preferred_language)
                VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        item.patient_id,
                        item.display_name,
                        item.date_of_birth,
                        item.postal_code,
                        item.phone_e164,
                        item.preferred_language,
                    )
                    for item in store.patients.values()
                ],
            )
            connection.executemany(
                """INSERT INTO caller_authorities
                (authority_id, patient_id, caller_display_name, relationship, status)
                VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        item.authority_id,
                        item.patient_id,
                        item.caller_display_name,
                        item.relationship,
                        item.status,
                    )
                    for item in store.caller_authorities.values()
                ],
            )
            connection.executemany(
                """INSERT INTO appointment_types
                (appointment_type_id, display_name, duration_minutes, prerequisites_json)
                VALUES (?, ?, ?, ?)""",
                [
                    (
                        item.appointment_type_id,
                        item.display_name,
                        item.duration_minutes,
                        json.dumps(item.prerequisites),
                    )
                    for item in store.appointment_types.values()
                ],
            )
            connection.executemany(
                """INSERT INTO providers(provider_id, display_name, specialty, active)
                VALUES (?, ?, ?, 1)""",
                [
                    (item.provider_id, item.display_name, item.specialty)
                    for item in store.providers.values()
                ],
            )
            connection.executemany(
                "INSERT INTO provider_appointment_types VALUES (?, ?)",
                [
                    (provider.provider_id, appointment_type_id)
                    for provider in store.providers.values()
                    for appointment_type_id in provider.appointment_type_ids
                ],
            )
            connection.executemany(
                "INSERT INTO provider_locations VALUES (?, ?)",
                [
                    (provider.provider_id, location_id)
                    for provider in store.providers.values()
                    for location_id in provider.location_ids
                ],
            )
            connection.executemany(
                """INSERT INTO slots
                (slot_id, provider_id, appointment_type_id, location_id, starts_at,
                 duration_minutes, status, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        item.slot_id,
                        item.provider_id,
                        item.appointment_type_id,
                        item.location_id,
                        item.starts_at.isoformat(),
                        item.duration_minutes,
                        item.status.value,
                        item.version,
                    )
                    for item in store.slots.values()
                ],
            )
            connection.executemany(
                """INSERT INTO appointments
                (appointment_id, patient_id, slot_id, provider_id, appointment_type_id,
                 location_id, starts_at, status, version, cancelled_at,
                 pending_replacement_slot_id, pending_reschedule_id, visit_reason,
                 prior_treatment_context)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        item.appointment_id,
                        item.patient_id,
                        item.slot_id,
                        item.provider_id,
                        item.appointment_type_id,
                        item.location_id,
                        item.starts_at.isoformat(),
                        item.status.value,
                        item.version,
                        item.cancelled_at.isoformat() if item.cancelled_at else None,
                        item.pending_replacement_slot_id,
                        item.pending_reschedule_id,
                        item.visit_reason,
                        item.prior_treatment_context,
                    )
                    for item in store.appointments.values()
                ],
            )
            connection.executemany(
                """INSERT INTO appointment_confirmation_calls
                (notification_id, appointment_id, patient_id, status, idempotency_key,
                 created_at, updated_at, attempts, provider_call_id, error_class)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        item.notification_id,
                        item.appointment_id,
                        item.patient_id,
                        item.status.value,
                        item.idempotency_key,
                        item.created_at.isoformat(),
                        item.updated_at.isoformat(),
                        item.attempts,
                        item.provider_call_id,
                        item.error_class,
                    )
                    for item in store.confirmation_calls.values()
                ],
            )
            for snapshot in store.availability_snapshots.values():
                connection.execute(
                    "INSERT INTO availability_snapshots VALUES (?, ?, ?)",
                    (
                        snapshot.snapshot_id,
                        snapshot.patient_id,
                        snapshot.created_at.isoformat(),
                    ),
                )
                connection.executemany(
                    """INSERT INTO availability_snapshot_slots
                    (snapshot_id, slot_id, slot_version, slot_json, position)
                    VALUES (?, ?, ?, ?, ?)""",
                    [
                        (
                            snapshot.snapshot_id,
                            slot.slot_id,
                            snapshot.slot_versions[slot.slot_id],
                            json.dumps(_jsonable(slot), sort_keys=True),
                            position,
                        )
                        for position, slot in enumerate(snapshot.slots)
                    ],
                )
            connection.executemany(
                """INSERT INTO confirmed_proposals
                (session_id, proposal_id, patient_id, confirmation_token, operation,
                 appointment_id, expected_appointment_version, slot_id, consumed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        item.session_id,
                        item.proposal_id,
                        item.patient_id,
                        item.confirmation_token,
                        item.operation,
                        item.appointment_id,
                        item.expected_appointment_version,
                        item.slot_id,
                        int(item.consumed),
                    )
                    for item in store.confirmed_proposals.values()
                ],
            )
            connection.executemany(
                "INSERT INTO idempotency_records VALUES (?, ?, ?, ?)",
                [
                    (
                        item.key,
                        item.operation,
                        item.request_fingerprint,
                        json.dumps(_jsonable(item.result), sort_keys=True),
                    )
                    for item in store.idempotency_records.values()
                ],
            )
            connection.executemany(
                """INSERT INTO handoffs
                (handoff_id, kind, session_id, patient_id, cancelled_appointment_id,
                 reason, topics_json, status, created_at)
                VALUES (?, 'post_cancellation', NULL, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        item.handoff_id,
                        item.patient_id,
                        item.cancelled_appointment_id,
                        item.reason,
                        json.dumps(item.topics),
                        item.status,
                        item.created_at.isoformat(),
                    )
                    for item in store.follow_ups.values()
                ],
            )
            connection.executemany(
                """INSERT INTO handoffs
                (handoff_id, kind, session_id, patient_id, cancelled_appointment_id,
                 reason, topics_json, status, created_at)
                VALUES (?, 'general', ?, ?, NULL, ?, ?, ?, ?)""",
                [
                    (
                        item.handoff_id,
                        item.session_id,
                        item.patient_id,
                        item.reason,
                        json.dumps(item.topics),
                        item.status,
                        item.created_at.isoformat(),
                    )
                    for item in store.general_handoffs.values()
                ],
            )
            connection.executemany(
                "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        item.sequence,
                        item.occurred_at.isoformat(),
                        item.event_type,
                        item.actor,
                        item.correlation_id,
                        json.dumps(_jsonable(item.payload), sort_keys=True),
                    )
                    for item in store.audit_log
                ],
            )
            metadata = {
                "clock": clock.now().isoformat(),
                "appointment_sequence": str(store.appointment_sequence),
                "reschedule_sequence": str(store.reschedule_sequence),
                "notification_sequence": str(store.notification_sequence),
                "snapshot_sequence": str(store.snapshot_sequence),
                "handoff_sequence": str(store.handoff_sequence),
                "audit_sequence": str(store.audit_sequence),
            }
            connection.executemany(
                """INSERT INTO clinic_metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                metadata.items(),
            )
            now = clock.now().isoformat()
            for session_id, patient_id in store.authorized_sessions.items():
                authority_id = store.session_authorities.get(session_id)
                connection.execute(
                    """INSERT INTO sessions
                    (session_id, patient_id, identity_status, authority_id, authority_mode,
                     workflow_state, created_at, updated_at)
                    VALUES (?, ?, 'verified', ?, ?, 'active', ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                      patient_id=excluded.patient_id,
                      identity_status=excluded.identity_status,
                      authority_id=excluded.authority_id,
                      authority_mode=excluded.authority_mode,
                      updated_at=excluded.updated_at""",
                    (
                        session_id,
                        patient_id,
                        authority_id,
                        "authorized_proxy" if authority_id else "patient_name_confirmation",
                        now,
                        now,
                    ),
                )

    def load_store(self) -> tuple[InMemoryAppointmentStore, FixedClock]:
        self.initialize_schema()
        store = InMemoryAppointmentStore()
        with self.connect() as connection:
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM clinic_metadata")
            }
            if "clock" not in metadata:
                raise RuntimeError("clinic database is not seeded")
            clock = FixedClock(datetime.fromisoformat(metadata["clock"]))
            for row in connection.execute("SELECT * FROM patients"):
                item = Patient(**dict(row))
                store.patients[item.patient_id] = item
            for row in connection.execute("SELECT * FROM caller_authorities"):
                item = CallerAuthority(**dict(row))
                store.caller_authorities[item.authority_id] = item
            for row in connection.execute("SELECT * FROM appointment_types"):
                item = AppointmentType(
                    appointment_type_id=row["appointment_type_id"],
                    display_name=row["display_name"],
                    duration_minutes=row["duration_minutes"],
                    prerequisites=tuple(json.loads(row["prerequisites_json"])),
                )
                store.appointment_types[item.appointment_type_id] = item
            for row in connection.execute("SELECT * FROM providers"):
                appointment_type_ids = tuple(
                    value["appointment_type_id"]
                    for value in connection.execute(
                        "SELECT appointment_type_id FROM provider_appointment_types WHERE provider_id=? ORDER BY appointment_type_id",
                        (row["provider_id"],),
                    )
                )
                location_ids = tuple(
                    value["location_id"]
                    for value in connection.execute(
                        "SELECT location_id FROM provider_locations WHERE provider_id=? ORDER BY location_id",
                        (row["provider_id"],),
                    )
                )
                item = Provider(
                    provider_id=row["provider_id"],
                    display_name=row["display_name"],
                    appointment_type_ids=appointment_type_ids,
                    location_ids=location_ids,
                    specialty=row["specialty"],
                )
                store.providers[item.provider_id] = item
            for row in connection.execute("SELECT * FROM slots"):
                item = Slot(
                    slot_id=row["slot_id"],
                    provider_id=row["provider_id"],
                    appointment_type_id=row["appointment_type_id"],
                    location_id=row["location_id"],
                    starts_at=datetime.fromisoformat(row["starts_at"]),
                    duration_minutes=row["duration_minutes"],
                    status=SlotStatus(row["status"]),
                    version=row["version"],
                )
                store.slots[item.slot_id] = item
            for row in connection.execute("SELECT * FROM appointments"):
                item = Appointment(
                    appointment_id=row["appointment_id"],
                    patient_id=row["patient_id"],
                    slot_id=row["slot_id"],
                    provider_id=row["provider_id"],
                    appointment_type_id=row["appointment_type_id"],
                    location_id=row["location_id"],
                    starts_at=datetime.fromisoformat(row["starts_at"]),
                    status=AppointmentStatus(row["status"]),
                    version=row["version"],
                    cancelled_at=_timestamp(row["cancelled_at"]),
                    pending_replacement_slot_id=row["pending_replacement_slot_id"],
                    pending_reschedule_id=row["pending_reschedule_id"],
                    visit_reason=row["visit_reason"],
                    prior_treatment_context=row["prior_treatment_context"],
                )
                store.appointments[item.appointment_id] = item
            for row in connection.execute("SELECT * FROM appointment_confirmation_calls"):
                item = AppointmentConfirmationCall(
                    notification_id=row["notification_id"],
                    appointment_id=row["appointment_id"],
                    patient_id=row["patient_id"],
                    status=ConfirmationCallStatus(row["status"]),
                    idempotency_key=row["idempotency_key"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                    attempts=row["attempts"],
                    provider_call_id=row["provider_call_id"],
                    error_class=row["error_class"],
                )
                store.confirmation_calls[item.notification_id] = item
            for row in connection.execute("SELECT * FROM availability_snapshots"):
                snapshot_rows = list(
                    connection.execute(
                        """SELECT * FROM availability_snapshot_slots
                        WHERE snapshot_id=? ORDER BY position, slot_id""",
                        (row["snapshot_id"],),
                    )
                )
                slots = []
                versions = {}
                for snapshot_row in snapshot_rows:
                    value = json.loads(snapshot_row["slot_json"])
                    slot = Slot(
                        slot_id=value["slot_id"],
                        provider_id=value["provider_id"],
                        appointment_type_id=value["appointment_type_id"],
                        location_id=value["location_id"],
                        starts_at=datetime.fromisoformat(value["starts_at"]),
                        duration_minutes=value["duration_minutes"],
                        status=SlotStatus(value["status"]),
                        version=value["version"],
                    )
                    slots.append(slot)
                    versions[slot.slot_id] = snapshot_row["slot_version"]
                item = AvailabilitySnapshot(
                    snapshot_id=row["snapshot_id"],
                    patient_id=row["patient_id"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    slots=tuple(slots),
                    slot_versions=versions,
                )
                store.availability_snapshots[item.snapshot_id] = item
            for row in connection.execute("SELECT * FROM confirmed_proposals"):
                item = ConfirmedProposal(
                    session_id=row["session_id"],
                    patient_id=row["patient_id"],
                    proposal_id=row["proposal_id"],
                    confirmation_token=row["confirmation_token"],
                    operation=row["operation"],
                    appointment_id=row["appointment_id"],
                    expected_appointment_version=row["expected_appointment_version"],
                    slot_id=row["slot_id"],
                    consumed=bool(row["consumed"]),
                )
                store.confirmed_proposals[(item.session_id, item.proposal_id)] = item
            for row in connection.execute("SELECT * FROM idempotency_records"):
                result = self._deserialize_result(row["operation"], row["result_json"])
                item = IdempotencyRecord(
                    key=row["idempotency_key"],
                    operation=row["operation"],
                    request_fingerprint=row["request_fingerprint"],
                    result=result,
                )
                store.idempotency_records[item.key] = item
            for row in connection.execute("SELECT * FROM handoffs"):
                if row["kind"] == "post_cancellation":
                    item = FollowUpRequest(
                        handoff_id=row["handoff_id"],
                        patient_id=row["patient_id"],
                        cancelled_appointment_id=row["cancelled_appointment_id"],
                        reason=row["reason"],
                        topics=tuple(json.loads(row["topics_json"])),
                        status=row["status"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                    store.follow_ups[item.handoff_id] = item
                else:
                    item = HandoffRequest(
                        handoff_id=row["handoff_id"],
                        session_id=row["session_id"],
                        patient_id=row["patient_id"],
                        reason=row["reason"],
                        topics=tuple(json.loads(row["topics_json"])),
                        status=row["status"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                    store.general_handoffs[item.handoff_id] = item
            for row in connection.execute("SELECT * FROM audit_events ORDER BY sequence"):
                store.audit_log.append(
                    AuditEvent(
                        sequence=row["sequence"],
                        occurred_at=datetime.fromisoformat(row["occurred_at"]),
                        event_type=row["event_type"],
                        actor=row["actor"],
                        correlation_id=row["correlation_id"],
                        payload=json.loads(row["payload_json"]),
                    )
                )
            for row in connection.execute(
                "SELECT session_id, patient_id, authority_id FROM sessions WHERE identity_status='verified'"
            ):
                store.authorized_sessions[row["session_id"]] = row["patient_id"]
                if row["authority_id"]:
                    store.session_authorities[row["session_id"]] = row["authority_id"]
            store.appointment_sequence = int(metadata.get("appointment_sequence", 0))
            store.reschedule_sequence = int(metadata.get("reschedule_sequence", 0))
            store.notification_sequence = int(metadata.get("notification_sequence", 0))
            store.snapshot_sequence = int(metadata.get("snapshot_sequence", 0))
            store.handoff_sequence = int(metadata.get("handoff_sequence", 0))
            store.audit_sequence = int(metadata.get("audit_sequence", 0))
        return store, clock

    def upsert_session_state(
        self,
        *,
        session_id: str,
        patient_id: str | None,
        identity_status: str,
        authority_mode: str | None,
        workflow_state: str,
        now: datetime,
    ) -> None:
        self.initialize_schema()
        with self.connect() as connection:
            authority_id = connection.execute(
                "SELECT authority_id FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            connection.execute(
                """INSERT INTO sessions
                (session_id, patient_id, identity_status, authority_id, authority_mode,
                 workflow_state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  patient_id=excluded.patient_id,
                  identity_status=excluded.identity_status,
                  authority_mode=excluded.authority_mode,
                  workflow_state=excluded.workflow_state,
                  updated_at=excluded.updated_at""",
                (
                    session_id,
                    patient_id,
                    identity_status,
                    authority_id["authority_id"] if authority_id else None,
                    authority_mode,
                    workflow_state,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    def dashboard_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        self.initialize_schema()
        queries = {
            "patients": "SELECT patient_id, display_name, CASE WHEN phone_e164='' THEN '' ELSE '***' || substr(phone_e164, -4) END AS phone, preferred_language FROM patients ORDER BY display_name",
            "doctors": "SELECT provider_id, display_name, specialty, active FROM providers ORDER BY display_name",
            "authorities": "SELECT authority_id, patient_id, caller_display_name, relationship, status FROM caller_authorities ORDER BY caller_display_name",
            "appointments": (
                "SELECT a.appointment_id, a.patient_id, a.provider_id, a.slot_id, a.starts_at, "
                "a.status, a.version, a.pending_reschedule_id, "
                "a.pending_replacement_slot_id, replacement.provider_id AS proposed_provider_id, "
                "replacement.starts_at AS proposed_starts_at "
                "FROM appointments a LEFT JOIN slots replacement "
                "ON replacement.slot_id=a.pending_replacement_slot_id "
                "ORDER BY a.starts_at"
            ),
            "confirmation_calls": "SELECT notification_id, appointment_id, patient_id, status, attempts, provider_call_id, error_class, updated_at FROM appointment_confirmation_calls ORDER BY created_at DESC",
            "slots": "SELECT slot_id, provider_id, appointment_type_id, location_id, starts_at, "
            "duration_minutes, status, version FROM slots ORDER BY starts_at",
            "sessions": "SELECT session_id, patient_id, identity_status, authority_mode, workflow_state, updated_at FROM sessions ORDER BY updated_at DESC",
            "handoffs": "SELECT handoff_id, kind, session_id, patient_id, reason, status, created_at FROM handoffs ORDER BY created_at DESC",
            "audit": "SELECT sequence, occurred_at, event_type, actor, correlation_id FROM audit_events ORDER BY sequence DESC LIMIT 100",
        }
        with self.connect() as connection:
            return {
                name: [dict(row) for row in connection.execute(query)]
                for name, query in queries.items()
            }

    @staticmethod
    def _deserialize_result(operation: str, value: str) -> Any:
        data = json.loads(value)
        if operation in {"create_appointment", "edit_appointment", "delete_appointment"}:
            return AppointmentMutationResult(
                appointment_id=data["appointment_id"],
                outcome=AppointmentMutationOutcome(data["outcome"]),
                appointment_version=data.get("appointment_version"),
                reconciliation_reference=data.get("reconciliation_reference"),
            )
        if operation in {"request_front_desk_follow_up", "request_general_handoff"}:
            return FollowUpResult(
                outcome=FollowUpOutcome(data["outcome"]),
                handoff_id=data.get("handoff_id"),
            )
        return data


class SQLiteBackedAppointmentHarness(AppointmentHarness):
    """Persist the tested in-memory domain model after every public operation."""

    def __init__(
        self,
        store: InMemoryAppointmentStore,
        clock: FixedClock,
        repository: SQLiteClinicRepository,
        faults: FaultInjector | None = None,
    ) -> None:
        super().__init__(store, clock, faults)
        self.repository = repository

    def _persisting(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        finally:
            self.repository.sync_store(self.store, self.clock)

    def verify_identity(self, **kwargs: Any) -> bool:
        return self._persisting(super().verify_identity, **kwargs)

    def confirm_identity_by_name(self, **kwargs: Any) -> bool:
        return self._persisting(super().confirm_identity_by_name, **kwargs)

    def confirm_proxy_authority(self, **kwargs: Any) -> bool:
        return self._persisting(super().confirm_proxy_authority, **kwargs)

    def search_slots(self, **kwargs: Any) -> AvailabilitySnapshot:
        return self._persisting(super().search_slots, **kwargs)

    def get_appointment(self, **kwargs: Any) -> Appointment:
        return self._persisting(super().get_appointment, **kwargs)

    def register_confirmation(self, confirmation: ConfirmedProposal) -> None:
        self._persisting(super().register_confirmation, confirmation)

    def create_appointment(self, command: Any) -> AppointmentMutationResult:
        return self._persisting(super().create_appointment, command)

    def edit_appointment(self, command: Any) -> AppointmentMutationResult:
        return self._persisting(super().edit_appointment, command)

    def delete_appointment(self, command: Any) -> AppointmentMutationResult:
        return self._persisting(super().delete_appointment, command)

    def confirm_proposed_appointment(
        self, appointment_id: str
    ) -> ClinicConfirmationResult:
        return self._persisting(super().confirm_proposed_appointment, appointment_id)

    def record_confirmation_call_result(
        self, notification_id: str, **kwargs: Any
    ) -> AppointmentConfirmationCall:
        return self._persisting(
            super().record_confirmation_call_result, notification_id, **kwargs
        )

    def request_front_desk_follow_up(self, command: Any) -> FollowUpResult:
        return self._persisting(super().request_front_desk_follow_up, command)

    def request_general_handoff(self, command: Any) -> FollowUpResult:
        return self._persisting(super().request_general_handoff, command)

    def verify_final_state(self, **kwargs: Any) -> Appointment:
        return self._persisting(super().verify_final_state, **kwargs)


def open_sqlite_harness(
    path: str | Path,
    *,
    fixture_path: str | Path = DEFAULT_FIXTURE,
) -> tuple[SQLiteBackedAppointmentHarness, SQLiteClinicRepository]:
    repository = SQLiteClinicRepository(path)
    repository.initialize_schema()
    if not repository.has_patients():
        store, clock = load_fixture(fixture_path)
        repository.sync_store(store, clock)
    store, clock = repository.load_store()
    return SQLiteBackedAppointmentHarness(store, clock, repository), repository
