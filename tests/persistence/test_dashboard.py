from __future__ import annotations

import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from appointment_harness.models import (
    ConfirmedProposal,
    CreateAppointmentCommand,
)
from appointment_harness.sqlite_repository import open_sqlite_harness
from appointment_harness.web import _calendar, _render, build_server
from clinic_agent.control_plane.tool_contracts import EditAppointmentCommand


class DashboardCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "doctors": [
                {
                    "provider_id": "provider-mehta",
                    "display_name": "Dr. N. Mehta",
                    "specialty": "Dermatology",
                }
            ],
            "slots": [
                {
                    "slot_id": "slot-open",
                    "provider_id": "provider-mehta",
                    "starts_at": "2026-10-09T15:30:00+05:30",
                    "duration_minutes": 30,
                    "status": "available",
                },
                {
                    "slot_id": "slot-booked",
                    "provider_id": "provider-mehta",
                    "starts_at": "2026-10-08T09:00:00+05:30",
                    "duration_minutes": 30,
                    "status": "booked",
                },
            ],
            "appointments": [
                {
                    "appointment_id": "appointment-proposed",
                    "patient_id": "patient-001",
                    "provider_id": "provider-mehta",
                    "slot_id": "slot-open",
                    "starts_at": "2026-10-09T15:30:00+05:30",
                    "status": "proposed",
                    "version": 1,
                }
            ],
        }

    def test_calendar_groups_and_orders_slots_by_date(self) -> None:
        html = _calendar(self.snapshot)

        self.assertIn("Provider availability calendar", html)
        self.assertIn("1 available · 0 held · 1 booked across 2 dates", html)
        self.assertIn("Dr. N. Mehta", html)
        self.assertIn("3:30–4:00 PM", html)
        self.assertIn("9:00–9:30 AM", html)
        self.assertIn("Week of Oct 5–Oct 11, 2026", html)

    def test_calendar_marks_statuses_for_visual_and_machine_checks(self) -> None:
        html = _calendar(self.snapshot)

        self.assertIn("class='slot-event booked' data-status='booked'", html)
        self.assertIn("class='slot-event available' data-status='available'", html)

    def test_calendar_distinguishes_held_slots_from_booked_slots(self) -> None:
        self.snapshot["slots"][0]["status"] = "held"

        html = _calendar(self.snapshot)

        self.assertIn("class='slot-event held' data-status='held'", html)
        self.assertIn("0 available · 1 held · 1 booked", html)
        self.assertIn("Held for review", html)

    def test_calendar_duration_controls_end_time_and_grid_height(self) -> None:
        self.snapshot["slots"][0]["duration_minutes"] = 60

        html = _calendar(self.snapshot)

        self.assertIn("3:30–4:30 PM", html)
        self.assertIn("grid-row:15 / span 2", html)
        self.assertIn("60 minutes", html)

    def test_calendar_escapes_provider_names_and_ignores_invalid_dates(self) -> None:
        self.snapshot["doctors"][0]["display_name"] = "Dr. <Unsafe>"
        self.snapshot["slots"].append(
            {
                "slot_id": "bad",
                "provider_id": "provider-mehta",
                "starts_at": "not-a-date",
                "status": "available",
            }
        )

        html = _calendar(self.snapshot)

        self.assertIn("Dr. &lt;Unsafe&gt;", html)
        self.assertNotIn("not-a-date", html)
        self.assertIn("across 2 dates", html)

    def test_page_places_calendar_before_operational_tables(self) -> None:
        html = _render(self.snapshot)

        self.assertIn("data-testid='provider-calendar'", html)
        self.assertLess(
            html.index("Provider availability calendar"),
            html.index("<h2>Appointments</h2>"),
        )
        self.assertIn("<summary>Raw slot records</summary>", html)

    def test_page_polls_and_patches_sections_without_full_page_refresh(self) -> None:
        html = _render(self.snapshot)

        self.assertNotIn("http-equiv='refresh'", html)
        self.assertIn("fetch('/api/snapshot'", html)
        self.assertIn("fetch('/fragment'", html)
        self.assertIn("window.setInterval(refreshDashboard, 3000)", html)
        self.assertIn("data-refresh-key='calendar'", html)
        self.assertIn("data-refresh-key='appointments'", html)
        self.assertIn("current.innerHTML = fresh.innerHTML", html)

    def test_week_navigation_is_accessible_and_preserved_across_updates(self) -> None:
        self.snapshot["slots"].append(
            {
                "slot_id": "slot-next-week",
                "provider_id": "provider-mehta",
                "starts_at": "2026-10-15T10:00:00+05:30",
                "duration_minutes": 30,
                "status": "available",
            }
        )

        calendar = _calendar(self.snapshot)
        page = _render(self.snapshot)

        self.assertIn("aria-label='Previous week' disabled", calendar)
        self.assertIn("aria-label='Next week'", calendar)
        self.assertIn("data-calendar-week-position>1 of 2", calendar)
        self.assertIn("data-week-start='2026-10-05'", calendar)
        self.assertIn("data-week-start='2026-10-12'", calendar)
        self.assertIn("data-week-label='Week of Oct 12–Oct 18, 2026' hidden", calendar)
        self.assertIn("showCalendarWeek(activeWeekStart, movement)", page)
        self.assertIn("showCalendarWeek(activeWeekStart);", page)

    def test_held_proposal_opens_review_modal_before_confirm_and_call(self) -> None:
        self.snapshot["slots"][0]["status"] = "held"
        page = _render(
            self.snapshot,
            clinic_actions_enabled=True,
            action_token="test-action-token",
        )

        self.assertIn("Review held booking for patient-001", page)
        self.assertIn("id='proposal-modal'", page)
        self.assertIn("Confirm appointment &amp; call patient", page)
        self.assertIn("converts the held slot to booked", page)
        self.assertIn("data-review-appointment", page)
        self.assertIn("class='slot-event held reviewable'", page)
        self.assertNotIn("clinic_action", page)
        self.assertIn("data-modal-confirm", page)
        self.assertIn("X-Clinic-Action-Token", page)
        self.assertIn("function resetModalConfirmButton", page)
        self.assertIn("async function reconcileClinicConfirmation", page)
        self.assertIn("response recovered from clinic state", page)
        self.assertIn("Confirmation outcome unknown", page)
        self.assertNotIn("Action failed · ${error.message}", page)
        self.assertIn(
            "resetModalConfirmButton(modal.querySelector('[data-modal-confirm]'));",
            page,
        )

    def test_pending_reschedule_shows_old_and_held_replacement_in_review_modal(self) -> None:
        snapshot = dict(self.snapshot)
        snapshot["appointments"] = [
            {
                "appointment_id": "appointment-existing",
                "patient_id": "patient-001",
                "provider_id": "provider-mehta",
                "starts_at": "2026-10-08T09:00:00+05:30",
                "status": "scheduled",
                "version": 2,
                "pending_reschedule_id": "reschedule-0001",
                "pending_replacement_slot_id": "slot-open",
                "proposed_provider_id": "provider-mehta",
                "proposed_starts_at": "2026-10-09T15:30:00+05:30",
            }
        ]
        snapshot["slots"] = [dict(row) for row in self.snapshot["slots"]]
        snapshot["slots"][0]["status"] = "held"

        page = _render(
            snapshot,
            clinic_actions_enabled=True,
            action_token="test-action-token",
        )

        self.assertIn("Review held reschedule for patient-001", page)
        self.assertIn("data-request-kind='reschedule'", page)
        self.assertIn("data-current-starts-at='2026-10-08T09:00:00+05:30'", page)
        self.assertIn("data-starts-at='2026-10-09T15:30:00+05:30'", page)
        self.assertIn("replacement held · current appointment preserved", page)


class DashboardClinicActionTests(unittest.TestCase):
    def test_modal_confirmation_endpoint_confirms_and_places_one_call(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            harness, repository = open_sqlite_harness(root / "clinic.db")
            harness.confirm_identity_by_name(
                session_id="portal-test",
                patient_id="patient-001",
                confirmed=True,
            )
            snapshot = harness.search_slots(
                session_id="portal-test",
                patient_id="patient-001",
                appointment_type_id="dermatology-followup",
            )
            slot_id = snapshot.slots[0].slot_id
            harness.register_confirmation(
                ConfirmedProposal(
                    session_id="portal-test",
                    patient_id="patient-001",
                    proposal_id="portal-proposal",
                    confirmation_token="portal-token",
                    operation="create_appointment",
                    slot_id=slot_id,
                )
            )
            proposed = harness.create_appointment(
                CreateAppointmentCommand(
                    session_id="portal-test",
                    patient_id="patient-001",
                    slot_id=slot_id,
                    availability_snapshot_id=snapshot.snapshot_id,
                    proposal_id="portal-proposal",
                    confirmation_token="portal-token",
                    idempotency_key="portal-create",
                )
            )
            dispatched: list[tuple[str, str]] = []
            server = build_server(
                harness=harness,
                repository=repository,
                conversation_path=root / "conversations.json",
                host="127.0.0.1",
                port=0,
                confirmation_dispatcher=lambda notification_id, destination: (
                    dispatched.append((notification_id, destination)) or "CA-test-call"
                ),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                page = urlopen(base + "/", timeout=2).read().decode("utf-8")
                token = re.search(
                    r"data-clinic-action-token='([^']+)'", page
                ).group(1)
                api_snapshot = json.loads(
                    urlopen(base + "/api/snapshot", timeout=2)
                    .read()
                    .decode("utf-8")
                )
                self.assertEqual(token, api_snapshot["_portal"]["action_token"])
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(
                        Request(
                            base
                            + f"/api/appointments/{proposed.appointment_id}/confirm",
                            method="POST",
                        ),
                        timeout=2,
                    )
                self.assertEqual(403, rejected.exception.code)

                request = Request(
                    base + f"/api/appointments/{proposed.appointment_id}/confirm",
                    method="POST",
                    headers={"X-Clinic-Action-Token": token},
                )
                response = json.loads(
                    urlopen(request, timeout=2).read().decode("utf-8")
                )
                repeated = json.loads(
                    urlopen(request, timeout=2).read().decode("utf-8")
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual("confirmed", response["appointment_status"])
            self.assertEqual("placed", response["call_status"])
            self.assertEqual("placed", repeated["call_status"])
            self.assertEqual(1, len(dispatched))
            notification = next(iter(harness.store.confirmation_calls.values()))
            self.assertEqual("placed", notification.status.value)
            self.assertEqual("CA-test-call", notification.provider_call_id)

    def test_pending_reschedule_confirmation_swaps_slots_and_places_context_call(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            harness, repository = open_sqlite_harness(root / "clinic.db")
            harness.confirm_identity_by_name(
                session_id="portal-reschedule",
                patient_id="patient-001",
                confirmed=True,
            )
            snapshot = harness.search_slots(
                session_id="portal-reschedule",
                patient_id="patient-001",
                appointment_type_id="dermatology-followup",
            )
            replacement_slot_id = snapshot.slots[0].slot_id
            harness.register_confirmation(
                ConfirmedProposal(
                    session_id="portal-reschedule",
                    patient_id="patient-001",
                    proposal_id="portal-reschedule-proposal",
                    confirmation_token="portal-reschedule-token",
                    operation="edit_appointment",
                    appointment_id="appointment-existing",
                    expected_appointment_version=1,
                    slot_id=replacement_slot_id,
                )
            )
            proposed = harness.edit_appointment(
                EditAppointmentCommand(
                    session_id="portal-reschedule",
                    patient_id="patient-001",
                    appointment_id="appointment-existing",
                    expected_appointment_version=1,
                    replacement_slot_id=replacement_slot_id,
                    availability_snapshot_id=snapshot.snapshot_id,
                    proposal_id="portal-reschedule-proposal",
                    confirmation_token="portal-reschedule-token",
                    idempotency_key="portal-reschedule",
                )
            )
            self.assertEqual("proposed", proposed.outcome.value)

            dispatched: list[tuple[str, str]] = []
            server = build_server(
                harness=harness,
                repository=repository,
                conversation_path=root / "conversations.json",
                host="127.0.0.1",
                port=0,
                confirmation_dispatcher=lambda notification_id, destination: (
                    dispatched.append((notification_id, destination)) or "CA-reschedule-call"
                ),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                page = urlopen(base + "/", timeout=2).read().decode("utf-8")
                token = re.search(
                    r"data-clinic-action-token='([^']+)'", page
                ).group(1)
                request = Request(
                    base + "/api/appointments/appointment-existing/confirm",
                    method="POST",
                    headers={"X-Clinic-Action-Token": token},
                )
                response = json.loads(
                    urlopen(request, timeout=2).read().decode("utf-8")
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            appointment = harness.store.appointments["appointment-existing"]
            self.assertEqual("confirmed", response["appointment_status"])
            self.assertEqual("placed", response["call_status"])
            self.assertEqual(replacement_slot_id, appointment.slot_id)
            self.assertIsNone(appointment.pending_replacement_slot_id)
            self.assertEqual("available", harness.store.slots["slot-existing"].status.value)
            self.assertEqual("booked", harness.store.slots[replacement_slot_id].status.value)
            self.assertEqual(
                [(response["notification_id"], harness.store.patients["patient-001"].phone_e164)],
                dispatched,
            )


if __name__ == "__main__":
    unittest.main()
