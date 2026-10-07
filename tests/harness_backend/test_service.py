from __future__ import annotations

from datetime import datetime
import threading
import unittest

from appointment_harness import (
    AppointmentHarness,
    AppointmentStatus,
    ConfirmedProposal,
    CreateAppointmentCommand,
    Fault,
    FaultInjector,
    FaultKind,
    FaultPoint,
    load_fixture,
)
from appointment_harness.faults import (
    ContractRejected,
    IdempotencyConflict,
    SyntheticFailure,
    SyntheticTimeout,
    VersionConflict,
)
from appointment_harness.models import ConfirmationCallStatus, SlotStatus
from clinic_agent.control_plane.tool_contracts import (
    AppointmentMutationOutcome,
    DeleteAppointmentCommand,
    EditAppointmentCommand,
    FollowUpOutcome,
    PostCancellationFollowUpCommand,
)


SESSION = "session-001"
PATIENT = "patient-001"


def make_harness(*faults: Fault) -> AppointmentHarness:
    store, clock = load_fixture()
    return AppointmentHarness(store, clock, FaultInjector(tuple(faults)))


def authorize(harness: AppointmentHarness, session_id: str = SESSION) -> None:
    assert harness.verify_identity(
        session_id=session_id,
        patient_id=PATIENT,
        date_of_birth="1990-04-12",
        postal_code="411001",
    )


def confirmation(
    harness: AppointmentHarness,
    *,
    operation: str,
    proposal_id: str,
    token: str,
    session_id: str = SESSION,
    appointment_id: str | None = None,
    version: int | None = None,
    slot_id: str | None = None,
) -> None:
    harness.register_confirmation(
        ConfirmedProposal(
            session_id=session_id,
            patient_id=PATIENT,
            proposal_id=proposal_id,
            confirmation_token=token,
            operation=operation,
            appointment_id=appointment_id,
            expected_appointment_version=version,
            slot_id=slot_id,
        )
    )


def search(harness: AppointmentHarness, session_id: str = SESSION):
    return harness.search_slots(
        session_id=session_id,
        patient_id=PATIENT,
        appointment_type_id="dermatology-followup",
    )


class IdentityAndReadTests(unittest.TestCase):
    def test_fixture_is_deterministic(self) -> None:
        harness = make_harness()
        self.assertEqual(harness.clock.now().isoformat(), "2026-10-05T10:00:00+05:30")
        self.assertEqual(len(harness.store.patients), 2)
        self.assertEqual(len(harness.store.providers), 2)
        self.assertEqual(len(harness.store.slots), 28)
        self.assertIn("slot-mehta-20261012-0900", harness.store.slots)
        self.assertIn("slot-iyer-20261022-1430", harness.store.slots)
        self.assertIn("appointment-existing", harness.store.appointments)

    def test_provider_calendar_has_requested_october_12_and_13_options(self) -> None:
        harness = make_harness()
        authorize(harness)

        snapshot = harness.search_slots(
            session_id=SESSION,
            patient_id=PATIENT,
            appointment_type_id="dermatology-followup",
            provider_id="provider-mehta",
            starts_after=datetime.fromisoformat("2026-10-12T00:00:00+05:30"),
            starts_before=datetime.fromisoformat("2026-10-14T00:00:00+05:30"),
        )

        self.assertEqual(
            [
                "slot-mehta-20261012-0900",
                "slot-mehta-20261012-1130",
                "slot-mehta-20261013-0900",
                "slot-mehta-20261013-1500",
            ],
            [slot.slot_id for slot in snapshot.slots],
        )

    def test_failed_identity_does_not_authorize_session(self) -> None:
        harness = make_harness()
        self.assertFalse(
            harness.verify_identity(
                session_id=SESSION,
                patient_id=PATIENT,
                date_of_birth="1990-04-12",
                postal_code="wrong",
            )
        )
        with self.assertRaises(ContractRejected):
            search(harness)

    def test_patient_cannot_read_another_patients_appointment(self) -> None:
        harness = make_harness()
        authorize(harness)
        with self.assertRaises(ContractRejected):
            harness.get_appointment(
                session_id=SESSION,
                patient_id=PATIENT,
                appointment_id="missing-or-foreign",
            )

    def test_authorized_session_cannot_be_rebound_to_another_patient(self) -> None:
        harness = make_harness()
        authorize(harness)
        rebound = harness.verify_identity(
            session_id=SESSION,
            patient_id="patient-002",
            date_of_birth="1985-08-20",
            postal_code="400001",
        )
        self.assertFalse(rebound)
        self.assertEqual(harness.store.authorized_sessions[SESSION], PATIENT)

    def test_read_timeout_is_deterministic(self) -> None:
        harness = make_harness(
            Fault("search_slots", FaultPoint.BEFORE_READ, FaultKind.TIMEOUT)
        )
        authorize(harness)
        with self.assertRaises(SyntheticTimeout):
            search(harness)


class CreateTests(unittest.TestCase):
    def _command(self, harness: AppointmentHarness, *, key: str = "create-001"):
        snapshot = search(harness)
        confirmation(
            harness,
            operation="create_appointment",
            proposal_id="proposal-create",
            token="token-create",
            slot_id="slot-1530",
        )
        return CreateAppointmentCommand(
            session_id=SESSION,
            patient_id=PATIENT,
            slot_id="slot-1530",
            availability_snapshot_id=snapshot.snapshot_id,
            proposal_id="proposal-create",
            confirmation_token="token-create",
            idempotency_key=key,
        )

    def test_create_and_verify(self) -> None:
        harness = make_harness()
        authorize(harness)
        command = self._command(harness)
        result = harness.create_appointment(command)
        self.assertEqual(result.outcome, AppointmentMutationOutcome.PROPOSED)
        appointment = harness.verify_final_state(
            session_id=SESSION,
            patient_id=PATIENT,
            appointment_id=result.appointment_id,
            expected_status=AppointmentStatus.PROPOSED,
            expected_slot_id="slot-1530",
        )
        self.assertEqual(appointment.version, 1)
        self.assertIs(harness.store.slots["slot-1530"].status, SlotStatus.HELD)

    def test_clinic_confirmation_books_hold_and_enqueues_exactly_one_call(self) -> None:
        harness = make_harness()
        authorize(harness)
        proposed = harness.create_appointment(self._command(harness))

        first = harness.confirm_proposed_appointment(proposed.appointment_id)
        second = harness.confirm_proposed_appointment(proposed.appointment_id)

        appointment = harness.store.appointments[proposed.appointment_id]
        self.assertEqual(AppointmentStatus.CONFIRMED, appointment.status)
        self.assertEqual(SlotStatus.BOOKED, harness.store.slots["slot-1530"].status)
        self.assertEqual(first, second)
        self.assertEqual(1, len(harness.store.confirmation_calls))
        self.assertEqual(ConfirmationCallStatus.PENDING, first.notification_status)

    def test_call_failure_is_retryable_and_does_not_rollback_confirmation(self) -> None:
        harness = make_harness()
        authorize(harness)
        proposed = harness.create_appointment(self._command(harness))
        confirmed = harness.confirm_proposed_appointment(proposed.appointment_id)

        notification = harness.record_confirmation_call_result(
            confirmed.notification_id, error_class="SyntheticProviderFailure"
        )

        self.assertEqual(ConfirmationCallStatus.RETRY_PENDING, notification.status)
        self.assertEqual(1, notification.attempts)
        self.assertEqual(
            AppointmentStatus.CONFIRMED,
            harness.store.appointments[proposed.appointment_id].status,
        )

    def test_stale_confirmation_is_rejected(self) -> None:
        harness = make_harness()
        authorize(harness)
        command = self._command(harness)
        bad = CreateAppointmentCommand(
            session_id=command.session_id,
            patient_id=command.patient_id,
            slot_id=command.slot_id,
            availability_snapshot_id=command.availability_snapshot_id,
            proposal_id=command.proposal_id,
            confirmation_token="old-token",
            idempotency_key=command.idempotency_key,
        )
        result = harness.create_appointment(bad)
        self.assertEqual(result.outcome, AppointmentMutationOutcome.REJECTED)
        self.assertIs(harness.store.slots["slot-1530"].status, SlotStatus.AVAILABLE)

    def test_stale_availability_is_rejected(self) -> None:
        harness = make_harness(
            Fault("search_slots", FaultPoint.AFTER_READ, FaultKind.STALE_RESPONSE)
        )
        authorize(harness)
        command = self._command(harness)
        result = harness.create_appointment(command)
        self.assertEqual(result.outcome, AppointmentMutationOutcome.CONFLICT)

    def test_same_idempotent_request_is_applied_once(self) -> None:
        harness = make_harness()
        authorize(harness)
        command = self._command(harness)
        first = harness.create_appointment(command)
        second = harness.create_appointment(command)
        self.assertEqual(first, second)
        created = [
            item
            for item in harness.store.appointments.values()
            if item.appointment_id.startswith("appointment-0")
        ]
        self.assertEqual(len(created), 1)

    def test_idempotency_key_reuse_with_new_payload_fails(self) -> None:
        harness = make_harness()
        authorize(harness)
        first = self._command(harness)
        harness.create_appointment(first)
        changed = CreateAppointmentCommand(
            session_id=first.session_id,
            patient_id=first.patient_id,
            slot_id="slot-1630",
            availability_snapshot_id=first.availability_snapshot_id,
            proposal_id=first.proposal_id,
            confirmation_token=first.confirmation_token,
            idempotency_key=first.idempotency_key,
        )
        with self.assertRaises(IdempotencyConflict):
            harness.create_appointment(changed)

    def test_commit_then_timeout_is_reconciled_by_idempotency_key(self) -> None:
        harness = make_harness(
            Fault("create_appointment", FaultPoint.AFTER_COMMIT, FaultKind.TIMEOUT)
        )
        authorize(harness)
        command = self._command(harness)
        with self.assertRaises(SyntheticTimeout):
            harness.create_appointment(command)
        result = harness.lookup_by_idempotency_key(command.idempotency_key)
        self.assertEqual(result.outcome, AppointmentMutationOutcome.PROPOSED)
        self.assertIn(result.appointment_id, harness.store.appointments)

    def test_failure_before_commit_leaves_no_appointment(self) -> None:
        harness = make_harness(
            Fault("create_appointment", FaultPoint.BEFORE_COMMIT, FaultKind.FAILURE)
        )
        authorize(harness)
        command = self._command(harness)
        before = set(harness.store.appointments)
        with self.assertRaises(SyntheticFailure):
            harness.create_appointment(command)
        self.assertEqual(set(harness.store.appointments), before)
        self.assertIs(harness.store.slots["slot-1530"].status, SlotStatus.AVAILABLE)


class EditTests(unittest.TestCase):
    def _command(self, harness: AppointmentHarness) -> EditAppointmentCommand:
        snapshot = search(harness)
        confirmation(
            harness,
            operation="edit_appointment",
            proposal_id="proposal-edit",
            token="token-edit",
            appointment_id="appointment-existing",
            version=1,
            slot_id="slot-1630",
        )
        return EditAppointmentCommand(
            session_id=SESSION,
            patient_id=PATIENT,
            appointment_id="appointment-existing",
            expected_appointment_version=1,
            replacement_slot_id="slot-1630",
            availability_snapshot_id=snapshot.snapshot_id,
            proposal_id="proposal-edit",
            confirmation_token="token-edit",
            idempotency_key="edit-001",
        )

    def test_edit_holds_replacement_then_clinic_approval_swaps_atomically(self) -> None:
        harness = make_harness()
        authorize(harness)
        result = harness.edit_appointment(self._command(harness))
        self.assertEqual(result.outcome, AppointmentMutationOutcome.PROPOSED)
        pending = harness.store.appointments["appointment-existing"]
        self.assertEqual("slot-existing", pending.slot_id)
        self.assertEqual("slot-1630", pending.pending_replacement_slot_id)
        self.assertIs(harness.store.slots["slot-existing"].status, SlotStatus.BOOKED)
        self.assertIs(harness.store.slots["slot-1630"].status, SlotStatus.HELD)

        approval = harness.confirm_proposed_appointment("appointment-existing")
        appointment = harness.verify_final_state(
            session_id=SESSION,
            patient_id=PATIENT,
            appointment_id="appointment-existing",
            expected_status=AppointmentStatus.CONFIRMED,
            expected_slot_id="slot-1630",
        )
        self.assertEqual(AppointmentStatus.CONFIRMED, approval.appointment_status)
        self.assertEqual(appointment.version, 3)
        self.assertIsNone(appointment.pending_replacement_slot_id)
        self.assertIs(harness.store.slots["slot-existing"].status, SlotStatus.AVAILABLE)
        self.assertIs(harness.store.slots["slot-1630"].status, SlotStatus.BOOKED)

    def test_same_reschedule_retry_replays_one_held_request(self) -> None:
        harness = make_harness()
        authorize(harness)
        command = self._command(harness)
        first = harness.edit_appointment(command)

        self.assertEqual(AppointmentMutationOutcome.PROPOSED, first.outcome)
        appointment = harness.store.appointments["appointment-existing"]
        self.assertEqual("slot-existing", appointment.slot_id)
        self.assertEqual("slot-1630", appointment.pending_replacement_slot_id)

        repeated = harness.edit_appointment(command)
        self.assertEqual(first, repeated)
        self.assertEqual(1, len([e for e in harness.store.audit_log if e.event_type == "appointment.reschedule_proposed"]))

    def test_slot_race_preserves_original_appointment(self) -> None:
        harness = make_harness(
            Fault("edit_appointment", FaultPoint.BEFORE_COMMIT, FaultKind.SLOT_RACE)
        )
        authorize(harness)
        result = harness.edit_appointment(self._command(harness))
        self.assertEqual(result.outcome, AppointmentMutationOutcome.CONFLICT)
        appointment = harness.store.appointments["appointment-existing"]
        self.assertEqual(appointment.slot_id, "slot-existing")
        self.assertIs(harness.store.slots["slot-existing"].status, SlotStatus.BOOKED)

    def test_stale_appointment_version_conflicts(self) -> None:
        harness = make_harness()
        authorize(harness)
        command = self._command(harness)
        harness.store.appointments["appointment-existing"].version = 2
        result = harness.edit_appointment(command)
        self.assertEqual(result.outcome, AppointmentMutationOutcome.CONFLICT)


class CancellationAndFollowUpTests(unittest.TestCase):
    def _delete(self, harness: AppointmentHarness) -> DeleteAppointmentCommand:
        confirmation(
            harness,
            operation="delete_appointment",
            proposal_id="proposal-delete",
            token="token-delete",
            appointment_id="appointment-existing",
            version=1,
        )
        return DeleteAppointmentCommand(
            session_id=SESSION,
            patient_id=PATIENT,
            appointment_id="appointment-existing",
            expected_appointment_version=1,
            proposal_id="proposal-delete",
            confirmation_token="token-delete",
            idempotency_key="delete-001",
        )

    @staticmethod
    def _follow_up() -> PostCancellationFollowUpCommand:
        return PostCancellationFollowUpCommand(
            session_id=SESSION,
            patient_id=PATIENT,
            cancelled_appointment_id="appointment-existing",
            follow_up_topics=("future_scheduling_options",),
            idempotency_key="follow-up-001",
        )

    def test_delete_is_soft_cancel_then_follow_up_is_accepted(self) -> None:
        harness = make_harness()
        authorize(harness)
        result = harness.delete_appointment(self._delete(harness))
        self.assertEqual(result.outcome, AppointmentMutationOutcome.CANCELLED)
        verified = harness.verify_final_state(
            session_id=SESSION,
            patient_id=PATIENT,
            appointment_id="appointment-existing",
            expected_status=AppointmentStatus.CANCELLED,
        )
        self.assertIn(verified.appointment_id, harness.store.appointments)
        self.assertIs(harness.store.slots["slot-existing"].status, SlotStatus.AVAILABLE)
        handoff = harness.request_front_desk_follow_up(self._follow_up())
        self.assertEqual(handoff.outcome, FollowUpOutcome.ACCEPTED)

    def test_follow_up_outage_does_not_undo_cancellation(self) -> None:
        harness = make_harness(
            Fault(
                "request_front_desk_follow_up",
                FaultPoint.FOLLOW_UP,
                FaultKind.FOLLOW_UP_OUTAGE,
            )
        )
        authorize(harness)
        harness.delete_appointment(self._delete(harness))
        handoff = harness.request_front_desk_follow_up(self._follow_up())
        self.assertEqual(handoff.outcome, FollowUpOutcome.FAILED)
        self.assertIs(
            harness.store.appointments["appointment-existing"].status,
            AppointmentStatus.CANCELLED,
        )

    def test_follow_up_timeout_is_recorded_pending(self) -> None:
        harness = make_harness(
            Fault(
                "request_front_desk_follow_up",
                FaultPoint.FOLLOW_UP,
                FaultKind.TIMEOUT,
            )
        )
        authorize(harness)
        harness.delete_appointment(self._delete(harness))
        handoff = harness.request_front_desk_follow_up(self._follow_up())
        self.assertEqual(handoff.outcome, FollowUpOutcome.PENDING)
        self.assertEqual(harness.store.follow_ups[handoff.handoff_id].status, "pending")

    def test_follow_up_is_idempotent(self) -> None:
        harness = make_harness()
        authorize(harness)
        harness.delete_appointment(self._delete(harness))
        first = harness.request_front_desk_follow_up(self._follow_up())
        second = harness.request_front_desk_follow_up(self._follow_up())
        self.assertEqual(first, second)
        self.assertEqual(len(harness.store.follow_ups), 1)

    def test_follow_up_before_cancellation_is_rejected(self) -> None:
        harness = make_harness()
        authorize(harness)
        with self.assertRaises(ContractRejected):
            harness.request_front_desk_follow_up(self._follow_up())


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def test_final_read_conflict_is_visible(self) -> None:
        harness = make_harness(
            Fault(
                "get_appointment",
                FaultPoint.BEFORE_FINAL_READ,
                FaultKind.FINAL_READ_CONFLICT,
            )
        )
        authorize(harness)
        with self.assertRaises(VersionConflict):
            harness.verify_final_state(
                session_id=SESSION,
                patient_id=PATIENT,
                appointment_id="appointment-existing",
                expected_status=AppointmentStatus.SCHEDULED,
            )

    def test_parallel_create_attempts_book_slot_at_most_once(self) -> None:
        harness = make_harness()
        sessions = ("session-a", "session-b")
        commands: list[CreateAppointmentCommand] = []
        for index, session_id in enumerate(sessions):
            authorize(harness, session_id)
            snapshot = search(harness, session_id)
            confirmation(
                harness,
                operation="create_appointment",
                proposal_id=f"proposal-{index}",
                token=f"token-{index}",
                session_id=session_id,
                slot_id="slot-1530",
            )
            commands.append(
                CreateAppointmentCommand(
                    session_id=session_id,
                    patient_id=PATIENT,
                    slot_id="slot-1530",
                    availability_snapshot_id=snapshot.snapshot_id,
                    proposal_id=f"proposal-{index}",
                    confirmation_token=f"token-{index}",
                    idempotency_key=f"create-{index}",
                )
            )

        outcomes: list[AppointmentMutationOutcome] = []

        def run(command: CreateAppointmentCommand) -> None:
            outcomes.append(harness.create_appointment(command).outcome)

        threads = [threading.Thread(target=run, args=(command,)) for command in commands]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(outcomes.count(AppointmentMutationOutcome.PROPOSED), 1)
        self.assertEqual(outcomes.count(AppointmentMutationOutcome.CONFLICT), 1)
        sequences = [event.sequence for event in harness.store.audit_log]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

    def test_audit_log_excludes_identity_answers(self) -> None:
        harness = make_harness()
        authorize(harness)
        serialized = repr(harness.store.audit_log)
        self.assertNotIn("1990-04-12", serialized)
        self.assertNotIn("411001", serialized)


if __name__ == "__main__":
    unittest.main()
