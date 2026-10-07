from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from appointment_harness.conversation_store import ConversationDocumentStore
from appointment_harness.models import (
    AppointmentStatus,
    ConfirmedProposal,
    ConfirmationCallStatus,
    CreateAppointmentCommand,
    GeneralHandoffCommand,
    SlotStatus,
)
from appointment_harness.sqlite_repository import open_sqlite_harness
from clinic_agent.control_plane.tool_contracts import EditAppointmentCommand


class SQLitePersistenceTests(unittest.TestCase):
    def test_fixture_seeds_patient_doctor_and_appointment_tables(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "clinic.db"

            harness, repository = open_sqlite_harness(database)
            snapshot = repository.dashboard_snapshot()

            self.assertTrue(database.exists())
            self.assertEqual(2, len(snapshot["patients"]))
            self.assertEqual("Dr. N. Mehta", snapshot["doctors"][0]["display_name"])
            self.assertEqual(1, len(snapshot["appointments"]))
            self.assertEqual(1, len(snapshot["authorities"]))
            appointment = harness.store.appointments["appointment-existing"]
            self.assertEqual("receding hairline follow-up", appointment.visit_reason)
            self.assertEqual(
                "previously prescribed minoxidil",
                appointment.prior_treatment_context,
            )

            reopened, _ = open_sqlite_harness(database)
            persisted = reopened.store.appointments["appointment-existing"]
            self.assertEqual(appointment.visit_reason, persisted.visit_reason)
            self.assertEqual(
                appointment.prior_treatment_context,
                persisted.prior_treatment_context,
            )

    def test_proxy_authority_and_general_handoff_survive_reopen(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "clinic.db"
            harness, _ = open_sqlite_harness(database)

            authorized = harness.confirm_proxy_authority(
                session_id="proxy-session",
                patient_id="patient-001",
                caller_display_name="Rohan Rao",
                relationship="spouse",
            )
            handoff = harness.request_general_handoff(
                GeneralHandoffCommand(
                    session_id="proxy-session",
                    patient_id="patient-001",
                    reason="caller_requested_human",
                    topics=("appointment_support",),
                    idempotency_key="proxy-session:handoff:1",
                )
            )

            reopened, repository = open_sqlite_harness(database)
            snapshot = repository.dashboard_snapshot()

            self.assertTrue(authorized)
            self.assertEqual("accepted", handoff.outcome.value)
            self.assertEqual("patient-001", reopened.store.authorized_sessions["proxy-session"])
            self.assertIn("proxy-session", reopened.store.session_authorities)
            self.assertEqual(1, len(snapshot["handoffs"]))

    def test_availability_snapshot_survives_reopen(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "clinic.db"
            harness, _ = open_sqlite_harness(database)
            self.assertTrue(
                harness.confirm_identity_by_name(
                    session_id="patient-session",
                    patient_id="patient-001",
                    confirmed=True,
                )
            )
            snapshot = harness.search_slots(
                session_id="patient-session",
                patient_id="patient-001",
                appointment_type_id="dermatology-followup",
            )

            reopened, _ = open_sqlite_harness(database)

            restored = reopened.store.availability_snapshots[snapshot.snapshot_id]
            self.assertEqual(snapshot.slot_versions, restored.slot_versions)
            self.assertEqual(
                [slot.slot_id for slot in snapshot.slots],
                [slot.slot_id for slot in restored.slots],
            )

    def test_proposal_confirmation_and_call_job_survive_reopen(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "clinic.db"
            harness, _ = open_sqlite_harness(database)
            self.assertTrue(
                harness.confirm_identity_by_name(
                    session_id="booking-session",
                    patient_id="patient-001",
                    confirmed=True,
                )
            )
            snapshot = harness.search_slots(
                session_id="booking-session",
                patient_id="patient-001",
                appointment_type_id="dermatology-followup",
            )
            slot_id = snapshot.slots[0].slot_id
            harness.register_confirmation(
                ConfirmedProposal(
                    session_id="booking-session",
                    patient_id="patient-001",
                    proposal_id="proposal-persisted",
                    confirmation_token="token-persisted",
                    operation="create_appointment",
                    slot_id=slot_id,
                )
            )
            proposed = harness.create_appointment(
                CreateAppointmentCommand(
                    session_id="booking-session",
                    patient_id="patient-001",
                    slot_id=slot_id,
                    availability_snapshot_id=snapshot.snapshot_id,
                    proposal_id="proposal-persisted",
                    confirmation_token="token-persisted",
                    idempotency_key="create-persisted",
                )
            )
            confirmed = harness.confirm_proposed_appointment(proposed.appointment_id)
            harness.record_confirmation_call_result(
                confirmed.notification_id,
                error_class="SyntheticProviderFailure",
            )

            reopened, repository = open_sqlite_harness(database)
            appointment = reopened.store.appointments[proposed.appointment_id]
            notification = reopened.store.confirmation_calls[confirmed.notification_id]

            self.assertEqual(AppointmentStatus.CONFIRMED, appointment.status)
            self.assertEqual(SlotStatus.BOOKED, reopened.store.slots[slot_id].status)
            self.assertEqual(ConfirmationCallStatus.RETRY_PENDING, notification.status)
            self.assertEqual(1, len(repository.dashboard_snapshot()["confirmation_calls"]))

    def test_pending_reschedule_survives_reopen_and_approval_swaps_slots(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "clinic.db"
            harness, _ = open_sqlite_harness(database)
            self.assertTrue(
                harness.confirm_identity_by_name(
                    session_id="reschedule-session",
                    patient_id="patient-001",
                    confirmed=True,
                )
            )
            snapshot = harness.search_slots(
                session_id="reschedule-session",
                patient_id="patient-001",
                appointment_type_id="dermatology-followup",
            )
            replacement = snapshot.slots[0]
            harness.register_confirmation(
                ConfirmedProposal(
                    session_id="reschedule-session",
                    patient_id="patient-001",
                    proposal_id="reschedule-persisted",
                    confirmation_token="reschedule-token",
                    operation="edit_appointment",
                    appointment_id="appointment-existing",
                    expected_appointment_version=1,
                    slot_id=replacement.slot_id,
                )
            )
            result = harness.edit_appointment(
                EditAppointmentCommand(
                    session_id="reschedule-session",
                    patient_id="patient-001",
                    appointment_id="appointment-existing",
                    expected_appointment_version=1,
                    replacement_slot_id=replacement.slot_id,
                    availability_snapshot_id=snapshot.snapshot_id,
                    proposal_id="reschedule-persisted",
                    confirmation_token="reschedule-token",
                    idempotency_key="reschedule-persisted",
                )
            )
            self.assertEqual("proposed", result.outcome.value)

            reopened, repository = open_sqlite_harness(database)
            pending = reopened.store.appointments["appointment-existing"]
            self.assertEqual("slot-existing", pending.slot_id)
            self.assertEqual(replacement.slot_id, pending.pending_replacement_slot_id)
            self.assertIs(SlotStatus.BOOKED, reopened.store.slots["slot-existing"].status)
            self.assertIs(SlotStatus.HELD, reopened.store.slots[replacement.slot_id].status)

            approved = reopened.confirm_proposed_appointment("appointment-existing")
            reopened_again, _ = open_sqlite_harness(database)
            final = reopened_again.store.appointments["appointment-existing"]
            self.assertEqual(AppointmentStatus.CONFIRMED, approved.appointment_status)
            self.assertEqual(replacement.slot_id, final.slot_id)
            self.assertIsNone(final.pending_replacement_slot_id)
            self.assertIs(SlotStatus.AVAILABLE, reopened_again.store.slots["slot-existing"].status)
            self.assertIs(SlotStatus.BOOKED, reopened_again.store.slots[replacement.slot_id].status)
            self.assertEqual(1, len(repository.dashboard_snapshot()["confirmation_calls"]))


class ConversationDocumentStoreTests(unittest.TestCase):
    def test_turns_are_ordered_and_sensitive_metadata_is_redacted(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "conversations.json"
            store = ConversationDocumentStore(path)
            occurred_at = datetime(2026, 10, 5, tzinfo=timezone.utc)

            store.append_turn(
                session_id="session-1",
                role="patient",
                content="I need to reschedule",
                occurred_at=occurred_at,
                metadata={"confirmation_token": "secret", "state": "active"},
            )
            store.append_event(
                session_id="session-1",
                event_type="workflow.transition",
                occurred_at=occurred_at,
                payload={"current": "confirmation_pending"},
            )

            events = store.events_for_session("session-1")
            store.close()

            self.assertEqual([1, 2], [event["sequence"] for event in events])
            self.assertEqual("[REDACTED]", events[0]["payload"]["confirmation_token"])
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
