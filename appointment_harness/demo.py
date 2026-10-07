"""Dependency-free terminal frontend for the appointment harness."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from json import dumps
from typing import Any, Sequence

from clinic_agent.control_plane.tool_contracts import (
    DeleteAppointmentCommand,
    EditAppointmentCommand,
    PostCancellationFollowUpCommand,
)

from .faults import Fault, FaultInjector, FaultKind, FaultPoint, SyntheticTimeout
from .fixtures import load_fixture
from .models import (
    AppointmentStatus,
    ConfirmedProposal,
    CreateAppointmentCommand,
)
from .service import AppointmentHarness


SESSION = "demo-session"
PATIENT = "patient-001"


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _new_harness(*faults: Fault) -> AppointmentHarness:
    store, clock = load_fixture()
    harness = AppointmentHarness(store, clock, FaultInjector(tuple(faults)))
    verified = harness.verify_identity(
        session_id=SESSION,
        patient_id=PATIENT,
        date_of_birth="1990-04-12",
        postal_code="411001",
    )
    if not verified:
        raise RuntimeError("demo fixture identity verification failed")
    return harness


def _search(harness: AppointmentHarness):
    return harness.search_slots(
        session_id=SESSION,
        patient_id=PATIENT,
        appointment_type_id="dermatology-followup",
    )


def _confirm(
    harness: AppointmentHarness,
    *,
    operation: str,
    proposal_id: str,
    token: str,
    appointment_id: str | None = None,
    version: int | None = None,
    slot_id: str | None = None,
) -> None:
    harness.register_confirmation(
        ConfirmedProposal(
            session_id=SESSION,
            patient_id=PATIENT,
            proposal_id=proposal_id,
            confirmation_token=token,
            operation=operation,
            appointment_id=appointment_id,
            expected_appointment_version=version,
            slot_id=slot_id,
        )
    )


def _book(harness: AppointmentHarness) -> dict[str, Any]:
    snapshot = _search(harness)
    _confirm(
        harness,
        operation="create_appointment",
        proposal_id="demo-book-proposal",
        token="demo-book-token",
        slot_id="slot-1530",
    )
    command = CreateAppointmentCommand(
        session_id=SESSION,
        patient_id=PATIENT,
        slot_id="slot-1530",
        availability_snapshot_id=snapshot.snapshot_id,
        proposal_id="demo-book-proposal",
        confirmation_token="demo-book-token",
        idempotency_key="demo-book",
    )
    result = harness.create_appointment(command)
    appointment = harness.verify_final_state(
        session_id=SESSION,
        patient_id=PATIENT,
        appointment_id=result.appointment_id,
        expected_status=AppointmentStatus.PROPOSED,
        expected_slot_id="slot-1530",
    )
    return {"result": result, "appointment": appointment}


def _reschedule(harness: AppointmentHarness) -> dict[str, Any]:
    snapshot = _search(harness)
    _confirm(
        harness,
        operation="edit_appointment",
        proposal_id="demo-edit-proposal",
        token="demo-edit-token",
        appointment_id="appointment-existing",
        version=1,
        slot_id="slot-1630",
    )
    result = harness.edit_appointment(
        EditAppointmentCommand(
            session_id=SESSION,
            patient_id=PATIENT,
            appointment_id="appointment-existing",
            expected_appointment_version=1,
            replacement_slot_id="slot-1630",
            availability_snapshot_id=snapshot.snapshot_id,
            proposal_id="demo-edit-proposal",
            confirmation_token="demo-edit-token",
            idempotency_key="demo-edit",
        )
    )
    pending = harness.get_appointment(
        session_id=SESSION,
        patient_id=PATIENT,
        appointment_id="appointment-existing",
    )
    approval = harness.confirm_proposed_appointment("appointment-existing")
    appointment = harness.verify_final_state(
        session_id=SESSION,
        patient_id=PATIENT,
        appointment_id="appointment-existing",
        expected_status=AppointmentStatus.CONFIRMED,
        expected_slot_id="slot-1630",
    )
    return {
        "result": result,
        "pending": pending,
        "approval": approval,
        "appointment": appointment,
    }


def _cancel(harness: AppointmentHarness) -> dict[str, Any]:
    _confirm(
        harness,
        operation="delete_appointment",
        proposal_id="demo-delete-proposal",
        token="demo-delete-token",
        appointment_id="appointment-existing",
        version=1,
    )
    result = harness.delete_appointment(
        DeleteAppointmentCommand(
            session_id=SESSION,
            patient_id=PATIENT,
            appointment_id="appointment-existing",
            expected_appointment_version=1,
            proposal_id="demo-delete-proposal",
            confirmation_token="demo-delete-token",
            idempotency_key="demo-delete",
        )
    )
    appointment = harness.verify_final_state(
        session_id=SESSION,
        patient_id=PATIENT,
        appointment_id="appointment-existing",
        expected_status=AppointmentStatus.CANCELLED,
    )
    follow_up = harness.request_front_desk_follow_up(
        PostCancellationFollowUpCommand(
            session_id=SESSION,
            patient_id=PATIENT,
            cancelled_appointment_id="appointment-existing",
            follow_up_topics=("future_scheduling_options",),
            idempotency_key="demo-follow-up",
        )
    )
    return {
        "result": result,
        "appointment": appointment,
        "follow_up": follow_up,
    }


def run_scenario(name: str) -> tuple[AppointmentHarness, dict[str, Any]]:
    if name == "overview":
        harness = _new_harness()
        return harness, {
            "available_slots": _search(harness).slots,
            "appointments": harness.list_appointments(
                session_id=SESSION, patient_id=PATIENT
            ),
        }
    if name == "book":
        harness = _new_harness()
        return harness, _book(harness)
    if name == "reschedule":
        harness = _new_harness()
        return harness, _reschedule(harness)
    if name == "cancel":
        harness = _new_harness()
        return harness, _cancel(harness)
    if name == "commit-timeout":
        harness = _new_harness(
            Fault("create_appointment", FaultPoint.AFTER_COMMIT, FaultKind.TIMEOUT)
        )
        try:
            _book(harness)
        except SyntheticTimeout as error:
            reconciled = harness.lookup_by_idempotency_key("demo-book")
            return harness, {
                "observed_error": str(error),
                "reconciled_result": reconciled,
            }
    if name == "follow-up-outage":
        harness = _new_harness(
            Fault(
                "request_front_desk_follow_up",
                FaultPoint.FOLLOW_UP,
                FaultKind.FOLLOW_UP_OUTAGE,
            )
        )
        return harness, _cancel(harness)
    raise ValueError(f"unknown scenario: {name}")


def _render_text(
    name: str, harness: AppointmentHarness, result: dict[str, Any]
) -> str:
    lines = [
        "Appointment Harness",
        f"Scenario: {name}",
        f"Clock: {harness.clock.now().isoformat()}",
        "",
        "Result",
        dumps(_jsonable(result), indent=2, sort_keys=True),
        "",
        "Audit trail",
    ]
    for event in harness.store.audit_log:
        lines.append(
            f"{event.sequence:02d}  {event.event_type:<28} "
            f"{event.correlation_id}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic appointment harness frontend."
    )
    parser.add_argument(
        "--scenario",
        choices=(
            "overview",
            "book",
            "reschedule",
            "cancel",
            "commit-timeout",
            "follow-up-outage",
        ),
        default="overview",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable result and audit trail.",
    )
    args = parser.parse_args(argv)
    harness, result = run_scenario(args.scenario)
    if args.json:
        print(
            dumps(
                {
                    "scenario": args.scenario,
                    "result": _jsonable(result),
                    "audit": _jsonable(harness.store.audit_log),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(_render_text(args.scenario, harness, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
