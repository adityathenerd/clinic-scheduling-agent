"""Local clinic dashboard with explicit, guarded proposal approval actions."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
from threading import Lock
from typing import Any, Callable, Sequence
from urllib.parse import urlparse

from .sqlite_repository import (
    SQLiteBackedAppointmentHarness,
    SQLiteClinicRepository,
    open_sqlite_harness,
)


STYLE = """
:root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
body { margin: 0; background: #f4f7fb; color: #162033; }
header { background: #11243e; color: white; padding: 24px 32px; }
header h1 { margin: 0 0 6px; font-size: 24px; }
header p { margin: 0; color: #c7d5e7; }
#live-status { color: #8ee3b6; }
main { padding: 24px 32px 48px; display: grid; gap: 22px; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.metric, section { background: white; border: 1px solid #dae3ef; border-radius: 12px; box-shadow: 0 2px 8px #18324f10; }
.metric { padding: 16px; }
.metric strong { display: block; font-size: 28px; color: #0b6bcb; }
section { overflow: hidden; }
section h2 { font-size: 17px; margin: 0; padding: 14px 16px; background: #edf4fb; }
.calendar-section { overflow: visible; }
.calendar-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.calendar-heading h2 { flex: 1; }
.calendar-legend { display: flex; gap: 12px; padding: 0 16px; color: #526477; font-size: 12px; }
.legend-key { display: inline-flex; align-items: center; gap: 5px; }
.legend-dot { width: 9px; height: 9px; border-radius: 50%; background: #16854b; }
.legend-dot.booked { background: #9a5b13; }
.legend-dot.held { background: #6f42c1; }
.calendar-summary { padding: 14px 16px 0; color: #526477; font-size: 13px; }
.calendar-explainer { margin: 12px 16px 0; padding: 11px 13px; border-radius: 8px; background: #f1f6fc; color: #33475b; font-size: 13px; }
.calendar-explainer strong { color: #15324e; }
.calendar-navigation { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 16px 0; }
.calendar-navigation-label { min-width: 0; }
.calendar-navigation-label strong { display: block; color: #18324f; font-size: 15px; }
.calendar-navigation-label span { color: #687789; font-size: 12px; }
.calendar-navigation-buttons { display: flex; gap: 7px; }
.calendar-nav-button { width: 34px; height: 34px; border: 1px solid #cbd8e6; border-radius: 50%; background: white; color: #18324f; cursor: pointer; font-size: 18px; line-height: 1; }
.calendar-nav-button:hover:not(:disabled) { background: #eaf3fc; border-color: #8eb6dd; }
.calendar-nav-button:focus-visible { outline: 3px solid #92c7f8; outline-offset: 2px; }
.calendar-nav-button:disabled { color: #aeb9c5; background: #f5f7f9; cursor: not-allowed; }
.calendar-weeks { padding: 10px 16px 16px; }
.calendar-week[hidden] { display: none; }
.week-scroll { overflow-x: auto; border: 1px solid #dce6f1; border-radius: 10px; }
.week-grid { display: grid; grid-template-columns: 74px repeat(7, minmax(132px, 1fr)); min-width: 998px; position: relative; background: white; }
.time-head, .day-head { position: sticky; top: 0; z-index: 4; padding: 8px 6px; border-bottom: 1px solid #cfdbe8; background: #edf4fb; text-align: center; }
.time-head { grid-column: 1; grid-row: 1; left: 0; z-index: 5; }
.day-head { font-size: 12px; color: #526477; }
.day-head strong { display: block; color: #18324f; font-size: 13px; }
.day-head.no-slots { color: #8996a4; background: #f5f7fa; }
.time-label { grid-column: 1; z-index: 3; padding: 5px 8px 0 0; border-right: 1px solid #dce6f1; border-bottom: 1px solid #e8eef5; background: #f8fafc; color: #607184; font-size: 11px; text-align: right; }
.grid-cell { z-index: 1; border-right: 1px solid #e8eef5; border-bottom: 1px solid #e8eef5; background: #fff; }
.grid-cell.off-day { background: #fafbfc; }
.slot-event { z-index: 2; margin: 2px 3px; padding: 4px 6px; border: 0; border-left: 4px solid #16854b; border-radius: 6px; background: #e3f6ec; color: #163c2a; overflow: hidden; box-shadow: 0 1px 2px #18324f18; font: inherit; text-align: left; }
.slot-event.booked { border-left-color: #9a5b13; background: #fff0d9; color: #553710; }
.slot-event.held { border-left-color: #6f42c1; background: #f0e9ff; color: #432475; }
.slot-event.cancelled, .slot-event.unavailable { border-left-color: #7a8796; background: #eef1f5; color: #46525f; }
.slot-event.reviewable { cursor: pointer; outline: 1px solid #6f42c155; }
.slot-event.reviewable:hover { background: #e6d9ff; outline-color: #6f42c1; }
.slot-event.reviewable:focus-visible { outline: 3px solid #8d62d6; outline-offset: 2px; }
.slot-event strong, .slot-event span { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.slot-event strong { font-size: 11px; }
.slot-event span { font-size: 11px; }
.slot-event small { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); }
.raw-slots summary { cursor: pointer; padding: 14px 16px; background: #edf4fb; font-size: 17px; font-weight: 600; }
.raw-slots .table-wrap { border-top: 1px solid #dce6f1; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 10px 12px; border-top: 1px solid #e6edf5; vertical-align: top; }
th { color: #526477; font-weight: 600; }
td { max-width: 420px; overflow-wrap: anywhere; }
.empty { padding: 16px; color: #6b7785; }
.pill { display: inline-block; padding: 2px 7px; border-radius: 999px; background: #e8f3ff; color: #075aa8; }
.clinic-action { border: 0; border-radius: 7px; background: #0b6bcb; color: white; padding: 7px 10px; cursor: pointer; font-weight: 600; }
.clinic-action:disabled { background: #9aa9b8; cursor: wait; }
.modal-backdrop[hidden] { display: none; }
.modal-backdrop { position: fixed; inset: 0; z-index: 20; display: grid; place-items: center; padding: 20px; background: #0a1728aa; }
.modal-card { width: min(520px, 100%); border-radius: 14px; background: white; box-shadow: 0 20px 60px #07152666; overflow: hidden; }
.modal-card h2 { margin: 0; padding: 18px 20px; background: #edf4fb; font-size: 19px; }
.modal-body { padding: 20px; display: grid; gap: 12px; }
.modal-detail { display: grid; grid-template-columns: 120px 1fr; gap: 8px; font-size: 14px; }
.modal-detail dt { color: #687789; }
.modal-detail dd { margin: 0; color: #162033; font-weight: 600; overflow-wrap: anywhere; }
.modal-warning { margin: 0; padding: 12px; border-radius: 8px; background: #fff5df; color: #60420c; font-size: 13px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; padding: 0 20px 20px; }
.modal-cancel { border: 1px solid #cbd8e6; border-radius: 7px; background: white; color: #18324f; padding: 7px 10px; cursor: pointer; }
footer { padding: 0 32px 30px; color: #687789; font-size: 12px; }
@media (max-width: 640px) {
  header, main { padding-left: 16px; padding-right: 16px; }
  footer { padding-left: 16px; padding-right: 16px; }
}
"""


def _load_conversations(path: Path) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        return {"conversation_sessions": [], "conversation_events": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"conversation_sessions": [], "conversation_events": []}
    table = raw.get("conversation_events", {})
    events = list(table.values()) if isinstance(table, dict) else []
    events.sort(
        key=lambda row: (row.get("occurred_at", ""), row.get("sequence", 0)),
        reverse=True,
    )
    summaries: dict[str, dict[str, Any]] = {}
    for event in events:
        session_id = str(event.get("session_id", "unknown"))
        summary = summaries.setdefault(
            session_id,
            {
                "session_id": session_id,
                "event_count": 0,
                "last_event_at": event.get("occurred_at", ""),
                "last_event_type": event.get("event_type", ""),
            },
        )
        summary["event_count"] += 1
    return {
        "conversation_sessions": list(summaries.values()),
        "conversation_events": events[:100],
    }


def _table(
    title: str,
    rows: list[dict[str, Any]],
    *,
    refresh_key: str | None = None,
) -> str:
    refresh_attribute = (
        f" data-refresh-key='{escape(refresh_key)}'" if refresh_key is not None else ""
    )
    if not rows:
        return (
            f"<section{refresh_attribute}><h2>{escape(title)}</h2>"
            "<div class='empty'>No records</div></section>"
        )
    columns = list(rows[0])
    header = "".join(f"<th>{escape(str(column))}</th>" for column in columns)
    body = []
    for row in rows:
        cells = []
        for column in columns:
            value = row.get(column)
            rendered = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value if value is not None else "")
            if column in {"status", "workflow_state", "identity_status"}:
                rendered = f"<span class='pill'>{escape(rendered)}</span>"
            else:
                rendered = escape(rendered)
            cells.append(f"<td>{rendered}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f"<section{refresh_attribute}><h2>{escape(title)}</h2><div class='table-wrap'><table>"
        f"<thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody>"
        "</table></div></section>"
    )


def _appointments_table(
    rows: list[dict[str, Any]], *, clinic_actions_enabled: bool
) -> str:
    # Operational actions belong to their calendar event.  Keeping this table
    # read-only prevents two competing interaction surfaces for the same hold.
    del clinic_actions_enabled
    return _table("Appointments", rows, refresh_key="appointments")


def _format_clock(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _format_range(starts_at: datetime, duration_minutes: int) -> str:
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    start_text = _format_clock(starts_at)
    end_text = _format_clock(ends_at)
    period = starts_at.strftime("%p")
    if period == ends_at.strftime("%p"):
        start_text = start_text.removesuffix(f" {period}")
    return f"{start_text}–{end_text}"


def _slot_duration(row: dict[str, Any]) -> int | None:
    try:
        value = int(row.get("duration_minutes", 0))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _calendar(
    snapshot: dict[str, list[dict[str, Any]]], *, clinic_actions_enabled: bool = False
) -> str:
    """Render slot inventory on a weekly time grid with explicit end times."""
    provider_names = {
        str(row.get("provider_id")): str(row.get("display_name") or row.get("provider_id"))
        for row in snapshot.get("doctors", [])
    }
    review_by_slot: dict[str, dict[str, Any]] = {}
    for appointment in snapshot.get("appointments", []):
        replacement_slot_id = appointment.get("pending_replacement_slot_id")
        if replacement_slot_id:
            review_by_slot[str(replacement_slot_id)] = {
                **appointment,
                "request_kind": "reschedule",
                "review_starts_at": appointment.get("proposed_starts_at"),
                "review_provider_id": appointment.get("proposed_provider_id")
                or appointment.get("provider_id"),
            }
        elif appointment.get("status") == "proposed" and appointment.get("slot_id"):
            review_by_slot[str(appointment["slot_id"])] = {
                **appointment,
                "request_kind": "booking",
                "review_starts_at": appointment.get("starts_at"),
                "review_provider_id": appointment.get("provider_id"),
            }
    by_week: dict[date, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
    for row in snapshot.get("slots", []):
        try:
            starts_at = datetime.fromisoformat(str(row.get("starts_at", "")))
        except ValueError:
            continue
        by_week[_week_start(starts_at.date())].append((starts_at, row))

    if not by_week:
        return (
            "<section class='calendar-section' data-testid='provider-calendar' "
            "data-refresh-key='calendar'>"
            "<h2>Provider availability calendar</h2>"
            "<div class='empty'>No dated slots are configured.</div></section>"
        )

    available_count = sum(
        str(row.get("status", "")).lower() == "available"
        for rows in by_week.values()
        for _, row in rows
    )
    booked_count = sum(
        str(row.get("status", "")).lower() == "booked"
        for rows in by_week.values()
        for _, row in rows
    )
    held_count = sum(
        str(row.get("status", "")).lower() == "held"
        for rows in by_week.values()
        for _, row in rows
    )
    dated_slots = [item for rows in by_week.values() for item in rows]
    start_minutes = min(starts_at.hour * 60 + starts_at.minute for starts_at, _ in dated_slots)
    end_minutes = max(
        starts_at.hour * 60
        + starts_at.minute
        + (_slot_duration(row) or 30)
        for starts_at, row in dated_slots
    )
    start_minutes = (start_minutes // 30) * 30
    end_minutes = ((end_minutes + 29) // 30) * 30
    row_count = max(1, (end_minutes - start_minutes) // 30)

    weeks: list[str] = []
    ordered_weeks = sorted(by_week.items())
    for week_index, (monday, rows) in enumerate(ordered_weeks):
        rows.sort(key=lambda item: (item[0], str(item[1].get("provider_id", ""))))
        dates_with_slots = {starts_at.date() for starts_at, _ in rows}
        days = [monday + timedelta(days=offset) for offset in range(7)]
        headers = ["<div class='time-head'>Time</div>"]
        for offset, day in enumerate(days):
            no_slots = " no-slots" if day not in dates_with_slots else ""
            headers.append(
                f"<div class='day-head{no_slots}' style='grid-column:{offset + 2};grid-row:1'>"
                f"<strong>{escape(day.strftime('%a'))}</strong>"
                f"{escape(day.strftime('%b %d').replace(' 0', ' '))}</div>"
            )

        grid_cells: list[str] = []
        time_labels: list[str] = []
        for row_index in range(row_count):
            minute_value = start_minutes + row_index * 30
            label_time = datetime(2000, 1, 1) + timedelta(minutes=minute_value)
            time_labels.append(
                f"<div class='time-label' style='grid-row:{row_index + 2}'>"
                f"{escape(_format_clock(label_time))}</div>"
            )
            for day_index, day in enumerate(days):
                off_day = " off-day" if day not in dates_with_slots else ""
                grid_cells.append(
                    f"<div class='grid-cell{off_day}' aria-hidden='true' "
                    f"style='grid-column:{day_index + 2};grid-row:{row_index + 2}'></div>"
                )

        events: list[str] = []
        for starts_at, row in rows:
            status = str(row.get("status") or "unknown").lower()
            css_status = status if status in {"booked", "held", "cancelled", "unavailable"} else "available"
            provider_id = str(row.get("provider_id") or "Unknown provider")
            provider_name = provider_names.get(provider_id, provider_id)
            duration = _slot_duration(row)
            range_text = (
                _format_range(starts_at, duration)
                if duration is not None
                else f"{_format_clock(starts_at)} · end time unavailable"
            )
            start_index = (starts_at.hour * 60 + starts_at.minute - start_minutes) // 30
            row_span = max(1, ((duration or 30) + 29) // 30)
            day_index = starts_at.date().weekday()
            detail = f"{provider_name}; {status}; {duration} minutes" if duration else f"{provider_name}; {status}"
            review = review_by_slot.get(str(row.get("slot_id", "")))
            reviewable = clinic_actions_enabled and status == "held" and review is not None
            if reviewable:
                appointment_id = str(review.get("appointment_id", ""))
                request_kind = str(review.get("request_kind", "booking"))
                patient_id = str(review.get("patient_id", ""))
                review_provider_id = str(review.get("review_provider_id", provider_id))
                review_provider = provider_names.get(review_provider_id, review_provider_id)
                review_starts_at = str(review.get("review_starts_at") or row.get("starts_at", ""))
                current_starts_at = (
                    str(review.get("starts_at", ""))
                    if request_kind == "reschedule"
                    else ""
                )
                review_label = (
                    f"Review held {request_kind} for {patient_id} with "
                    f"{review_provider} at {range_text}"
                )
                event_open = (
                    "<button type='button' class='slot-event held reviewable' "
                    f"data-status='held' data-review-appointment='{escape(appointment_id, quote=True)}' "
                    f"data-patient='{escape(patient_id, quote=True)}' "
                    f"data-provider='{escape(review_provider, quote=True)}' "
                    f"data-starts-at='{escape(review_starts_at, quote=True)}' "
                    f"data-current-starts-at='{escape(current_starts_at, quote=True)}' "
                    f"data-request-kind='{escape(request_kind, quote=True)}' "
                    f"aria-label='{escape(review_label, quote=True)}' "
                )
                event_close = "</button>"
            else:
                event_open = (
                    f"<div class='slot-event {escape(css_status)}' "
                    f"data-status='{escape(status)}' "
                )
                event_close = "</div>"
            events.append(
                event_open
                +
                f"style='grid-column:{day_index + 2};grid-row:{start_index + 2} / span {row_span}' "
                f"title='{escape(detail)}'>"
                f"<strong>{escape(range_text)}</strong><span>{escape(provider_name)}</span>"
                f"<small>{escape(detail)}</small>{event_close}"
            )

        sunday = monday + timedelta(days=6)
        week_label = (
            f"Week of {monday.strftime('%b %d').replace(' 0', ' ')}–"
            f"{sunday.strftime('%b %d, %Y').replace(' 0', ' ')}"
        )
        hidden = " hidden" if week_index else ""
        weeks.append(
            f"<article class='calendar-week' data-week-start='{monday.isoformat()}' "
            f"data-week-label='{escape(week_label)}'{hidden}>"
            "<div class='week-scroll'><div class='week-grid' "
            f"style='grid-template-rows:auto repeat({row_count}, 48px)'>"
            f"{''.join(headers)}{''.join(time_labels)}{''.join(grid_cells)}{''.join(events)}"
            "</div></div></article>"
        )

    date_count = len({starts_at.date() for starts_at, _ in dated_slots})
    first_monday = ordered_weeks[0][0]
    first_sunday = first_monday + timedelta(days=6)
    first_label = (
        f"Week of {first_monday.strftime('%b %d').replace(' 0', ' ')}–"
        f"{first_sunday.strftime('%b %d, %Y').replace(' 0', ' ')}"
    )
    return (
        "<section class='calendar-section' data-testid='provider-calendar' "
        "data-refresh-key='calendar'>"
        "<div class='calendar-heading'><h2>Provider availability calendar</h2>"
        "<div class='calendar-legend' aria-label='Calendar legend'>"
        "<span class='legend-key'><span class='legend-dot'></span>Available</span>"
        "<span class='legend-key'><span class='legend-dot held'></span>Held for review</span>"
        "<span class='legend-key'><span class='legend-dot booked'></span>Booked</span>"
        "</div></div>"
        f"<div class='calendar-summary'>{available_count} available · {held_count} held · {booked_count} booked "
        f"across {date_count} dates</div>"
        "<div class='calendar-explainer'><strong>How to read this:</strong> every block is one "
        "bookable slot. Its label shows the exact start and end time from the backend; a gap "
        "between blocks is not implied availability.</div>"
        "<div class='calendar-navigation'>"
        "<div class='calendar-navigation-label'><strong data-calendar-week-title "
        f"aria-live='polite'>{escape(first_label)}</strong>"
        f"<span data-calendar-week-position>1 of {len(ordered_weeks)}</span></div>"
        "<div class='calendar-navigation-buttons'>"
        "<button class='calendar-nav-button' type='button' data-calendar-nav='previous' "
        "aria-label='Previous week' disabled>&larr;</button>"
        "<button class='calendar-nav-button' type='button' data-calendar-nav='next' "
        f"aria-label='Next week'{(' disabled' if len(ordered_weeks) == 1 else '')}>&rarr;</button>"
        "</div></div>"
        f"<div class='calendar-weeks'>{''.join(weeks)}</div></section>"
    )


def _raw_slots(rows: list[dict[str, Any]]) -> str:
    table = _table("Raw slot records", rows)
    table = table.removeprefix("<section>").removesuffix("</section>")
    return (
        "<section data-refresh-key='raw-slots'><details class='raw-slots'>"
        f"<summary>Raw slot records</summary>{table}</details></section>"
    )


def _render_content(
    snapshot: dict[str, list[dict[str, Any]]], *, clinic_actions_enabled: bool = False
) -> str:
    metrics = ("patients", "doctors", "appointments", "confirmation_calls", "sessions", "handoffs", "conversation_sessions")
    metric_html = "".join(
        f"<div class='metric'><strong>{len(snapshot.get(name, []))}</strong>{escape(name.replace('_', ' ').title())}</div>"
        for name in metrics
    )
    order = (
        ("Active and historical sessions", "sessions"),
        ("Outbound confirmation calls", "confirmation_calls"),
        ("Conversation sessions — TinyDB", "conversation_sessions"),
        ("Recent conversation events — TinyDB", "conversation_events"),
        ("Front-desk handoffs", "handoffs"),
        ("Patients", "patients"),
        ("Doctors", "doctors"),
        ("Authorized proxies", "authorities"),
        ("Audit events", "audit"),
    )
    sections = "".join(
        _table(title, snapshot.get(key, []), refresh_key=key)
        for title, key in order
    )
    appointments = _appointments_table(
        snapshot.get("appointments", []),
        clinic_actions_enabled=clinic_actions_enabled,
    )
    calendar = _calendar(
        snapshot, clinic_actions_enabled=clinic_actions_enabled
    )
    raw_slots = _raw_slots(snapshot.get("slots", []))
    return (
        f"<div class='metrics' data-refresh-key='metrics'>{metric_html}</div>"
        f"{calendar}{appointments}{sections}{raw_slots}"
    )


POLL_SCRIPT = """
<script>
(() => {
  const status = document.getElementById('live-status');
  let lastSnapshot = null;
  let activeWeekStart = null;

  function resetModalConfirmButton(button = document.querySelector('[data-modal-confirm]')) {
    if (!button) return;
    button.disabled = false;
    button.textContent = 'Confirm appointment & call patient';
  }

  async function reconcileClinicConfirmation(appointmentId) {
    const response = await fetch('/api/snapshot', {cache: 'no-store'});
    if (!response.ok) throw new Error('snapshot unavailable');
    const snapshot = await response.json();
    const appointment = (snapshot.appointments || []).find(
      (candidate) => candidate.appointment_id === appointmentId
    );
    const committed = appointment
      && appointment.status === 'confirmed'
      && !appointment.pending_replacement_slot_id
      && !appointment.pending_reschedule_id;
    if (!committed) return null;
    const calls = (snapshot.confirmation_calls || []).filter(
      (candidate) => candidate.appointment_id === appointmentId
    );
    const call = calls.length ? calls[calls.length - 1] : null;
    return {callStatus: call && call.status === 'placed' ? 'placed' : 'retry_pending'};
  }

  function showCalendarWeek(preferredWeekStart = null, movement = 0) {
    const weeks = Array.from(document.querySelectorAll('.calendar-week'));
    if (!weeks.length) return;

    let index = preferredWeekStart
      ? weeks.findIndex((week) => week.dataset.weekStart === preferredWeekStart)
      : 0;
    if (index < 0) index = 0;
    index = Math.max(0, Math.min(weeks.length - 1, index + movement));

    weeks.forEach((week, weekIndex) => { week.hidden = weekIndex !== index; });
    activeWeekStart = weeks[index].dataset.weekStart;

    const title = document.querySelector('[data-calendar-week-title]');
    const position = document.querySelector('[data-calendar-week-position]');
    const previous = document.querySelector('[data-calendar-nav="previous"]');
    const next = document.querySelector('[data-calendar-nav="next"]');
    if (title) title.textContent = weeks[index].dataset.weekLabel;
    if (position) position.textContent = `${index + 1} of ${weeks.length}`;
    if (previous) previous.disabled = index === 0;
    if (next) next.disabled = index === weeks.length - 1;
  }

  document.addEventListener('click', (event) => {
    const reviewButton = event.target.closest('[data-review-appointment]');
    if (reviewButton) {
      const modal = document.getElementById('proposal-modal');
      // The modal lives outside the periodically refreshed dashboard fragment.
      // Always clear state left by a previous successful approval before showing
      // it for a new held request.
      resetModalConfirmButton(modal.querySelector('[data-modal-confirm]'));
      modal.dataset.appointmentId = reviewButton.dataset.reviewAppointment;
      modal.querySelector('[data-modal-appointment]').textContent = reviewButton.dataset.reviewAppointment;
      modal.querySelector('[data-modal-patient]').textContent = reviewButton.dataset.patient;
      modal.querySelector('[data-modal-provider]').textContent = reviewButton.dataset.provider;
      modal.querySelector('[data-modal-time]').textContent = new Date(reviewButton.dataset.startsAt).toLocaleString();
      const isReschedule = reviewButton.dataset.requestKind === 'reschedule';
      modal.querySelector('[data-modal-current-time]').textContent = isReschedule
        ? new Date(reviewButton.dataset.currentStartsAt).toLocaleString()
        : 'Not applicable — new appointment';
      modal.querySelector('[data-modal-state]').textContent = isReschedule
        ? 'Reschedule requested · replacement held · current appointment preserved'
        : 'New appointment proposed · slot held';
      modal.querySelector('[data-modal-title]').textContent = isReschedule
        ? 'Review held reschedule request'
        : 'Review held appointment request';
      modal.hidden = false;
      modal.querySelector('[data-modal-confirm]').focus();
      return;
    }
    const closeModal = event.target.closest('[data-modal-close]');
    if (closeModal) {
      const modal = document.getElementById('proposal-modal');
      modal.hidden = true;
      resetModalConfirmButton(modal.querySelector('[data-modal-confirm]'));
      return;
    }
    const clinicButton = event.target.closest('[data-modal-confirm]');
    if (clinicButton && !clinicButton.disabled) {
      const modal = document.getElementById('proposal-modal');
      const appointmentId = modal.dataset.appointmentId;
      clinicButton.disabled = true;
      clinicButton.textContent = 'Confirming & calling…';
      fetch(`/api/appointments/${encodeURIComponent(appointmentId)}/confirm`, {
        method: 'POST',
        headers: {'X-Clinic-Action-Token': document.body.dataset.clinicActionToken || ''}
      }).then(async (response) => {
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'confirmation failed');
        status.textContent = result.call_status === 'placed'
          ? 'Confirmed · outbound call placed'
          : 'Confirmed · outbound call queued for retry';
        lastSnapshot = null;
        resetModalConfirmButton(clinicButton);
        modal.hidden = true;
        await refreshDashboard();
      }).catch(async (error) => {
        // A network failure can occur after the clinic decision and Twilio side
        // effect have committed. Reconcile durable state before reporting a
        // failure or allowing the operator to retry the action.
        let reconciled = null;
        try {
          reconciled = await reconcileClinicConfirmation(appointmentId);
        } catch (_reconciliationError) {
          // The periodic dashboard poll will continue attempting recovery.
        }
        resetModalConfirmButton(clinicButton);
        if (reconciled) {
          modal.hidden = true;
          lastSnapshot = null;
          status.textContent = reconciled.callStatus === 'placed'
            ? 'Confirmed · outbound call placed · response recovered from clinic state'
            : 'Confirmed · outbound call queued for retry · response recovered from clinic state';
          await refreshDashboard();
        } else {
          status.textContent = `Confirmation outcome unknown · ${error.message} · refresh before retrying`;
        }
      });
      return;
    }
    const button = event.target.closest('[data-calendar-nav]');
    if (!button || button.disabled) return;
    const movement = button.dataset.calendarNav === 'next' ? 1 : -1;
    showCalendarWeek(activeWeekStart, movement);
  });

  async function refreshDashboard() {
    try {
      const snapshotResponse = await fetch('/api/snapshot', {cache: 'no-store'});
      if (!snapshotResponse.ok) throw new Error('snapshot unavailable');
      const snapshotText = await snapshotResponse.text();
      const snapshotPayload = JSON.parse(snapshotText);
      if (snapshotPayload._portal && snapshotPayload._portal.action_token) {
        document.body.dataset.clinicActionToken = snapshotPayload._portal.action_token;
      }
      if (snapshotText === lastSnapshot) return;

      const fragmentResponse = await fetch('/fragment', {cache: 'no-store'});
      if (!fragmentResponse.ok) throw new Error('fragment unavailable');
      const template = document.createElement('template');
      template.innerHTML = (await fragmentResponse.text()).trim();

      for (const fresh of template.content.querySelectorAll('[data-refresh-key]')) {
        const key = fresh.dataset.refreshKey;
        const current = document.querySelector(`[data-refresh-key="${key}"]`);
        if (current && current.innerHTML !== fresh.innerHTML) {
          current.innerHTML = fresh.innerHTML;
        }
      }
      showCalendarWeek(activeWeekStart);
      lastSnapshot = snapshotText;
      status.textContent = `Live · updated ${new Date().toLocaleTimeString()}`;
    } catch (_error) {
      status.textContent = 'Disconnected · retrying';
    }
  }

  showCalendarWeek();
  refreshDashboard();
  window.setInterval(refreshDashboard, 3000);
})();
</script>
"""


def _render(
    snapshot: dict[str, list[dict[str, Any]]],
    *,
    clinic_actions_enabled: bool = False,
    action_token: str = "",
) -> str:
    content = _render_content(snapshot, clinic_actions_enabled=clinic_actions_enabled)
    return f"""<!doctype html>
<html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Clinic Agent State</title><style>{STYLE}</style></head>
<body data-clinic-action-token='{escape(action_token, quote=True)}'><header><h1>Clinic scheduling agent</h1>
<p>Synthetic state · SQLite clinic database + TinyDB conversation documents ·
<span id='live-status' aria-live='polite'>Live</span></p></header>
<main id='dashboard-content'>{content}</main>
<div class='modal-backdrop' id='proposal-modal' role='dialog' aria-modal='true'
  aria-labelledby='proposal-modal-title' hidden data-appointment-id=''>
  <div class='modal-card'>
    <h2 id='proposal-modal-title' data-modal-title>Review held appointment request</h2>
    <div class='modal-body'>
      <dl class='modal-detail'>
        <dt>Appointment</dt><dd data-modal-appointment></dd>
        <dt>Patient</dt><dd data-modal-patient></dd>
        <dt>Provider</dt><dd data-modal-provider></dd>
        <dt>Current appointment</dt><dd data-modal-current-time></dd>
        <dt>Requested date &amp; time</dt><dd data-modal-time></dd>
        <dt>Current state</dt><dd data-modal-state>Proposed · slot held</dd>
      </dl>
      <p class='modal-warning'>For a reschedule, confirming atomically keeps the old appointment until the held replacement becomes booked, then releases the old slot. For a new booking, it converts the held slot to booked. The outbound confirmation call is attempted only after the clinic decision commits; if Twilio fails, the appointment remains confirmed and the call is queued for retry.</p>
    </div>
    <div class='modal-actions'>
      <button class='modal-cancel' type='button' data-modal-close>Keep pending</button>
      <button class='clinic-action' type='button' data-modal-confirm>Confirm appointment &amp; call patient</button>
    </div>
  </div>
</div>
<footer>Synthetic demo data only. Clinic actions are enabled only when this portal is co-hosted by the voice process.</footer>
{POLL_SCRIPT}
</body></html>"""


def build_server(
    *,
    harness: SQLiteBackedAppointmentHarness,
    repository: SQLiteClinicRepository,
    conversation_path: Path,
    host: str,
    port: int,
    confirmation_dispatcher: Callable[[str, str], str] | None = None,
) -> ThreadingHTTPServer:
    action_token = secrets.token_urlsafe(24)
    confirmation_action_lock = Lock()

    def confirm_and_dispatch(appointment_id: str) -> dict[str, str]:
        # The dashboard is threaded. Serialize clinic approval and dispatch so
        # two simultaneous button submissions cannot place duplicate calls.
        with confirmation_action_lock:
            result = harness.confirm_proposed_appointment(appointment_id)
            patient = harness.store.patients[
                harness.store.appointments[appointment_id].patient_id
            ]
            if result.notification_status.value == "placed":
                call_status = "placed"
            else:
                try:
                    provider_call_id = confirmation_dispatcher(
                        result.notification_id, patient.phone_e164
                    )
                    harness.record_confirmation_call_result(
                        result.notification_id, provider_call_id=provider_call_id
                    )
                    call_status = "placed"
                except Exception as error:
                    harness.record_confirmation_call_result(
                        result.notification_id, error_class=type(error).__name__
                    )
                    call_status = "retry_pending"
            return {
                "appointment_id": result.appointment_id,
                "appointment_status": result.appointment_status.value,
                "notification_id": result.notification_id,
                "call_status": call_status,
            }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            snapshot = repository.dashboard_snapshot()
            snapshot.update(_load_conversations(conversation_path))
            if parsed.path == "/api/snapshot":
                api_snapshot = dict(snapshot)
                api_snapshot["_portal"] = {
                    "action_token": (
                        action_token if confirmation_dispatcher is not None else ""
                    )
                }
                payload = json.dumps(api_snapshot, indent=2, sort_keys=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            elif parsed.path == "/fragment":
                payload = _render_content(
                    snapshot,
                    clinic_actions_enabled=confirmation_dispatcher is not None,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif parsed.path == "/":
                payload = _render(
                    snapshot,
                    clinic_actions_enabled=confirmation_dispatcher is not None,
                    action_token=action_token,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                payload = b"Not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            match = re.fullmatch(
                r"/api/appointments/([^/]+)/confirm", urlparse(self.path).path
            )
            supplied_token = self.headers.get("X-Clinic-Action-Token", "")
            if confirmation_dispatcher is None:
                self._json_response(403, {"error": "clinic actions are disabled"})
                return
            if not secrets.compare_digest(supplied_token, action_token):
                self._json_response(403, {"error": "invalid clinic action token"})
                return
            if match is None:
                self._json_response(404, {"error": "not found"})
                return
            appointment_id = match.group(1)
            try:
                body = confirm_and_dispatch(appointment_id)
            except Exception as error:
                self._json_response(
                    409, {"error": f"{type(error).__name__}: {error}"}
                )
                return
            self._json_response(200, body)

        def _json_response(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve(
    *,
    harness: SQLiteBackedAppointmentHarness,
    repository: SQLiteClinicRepository,
    conversation_path: Path,
    host: str,
    port: int,
    confirmation_dispatcher: Callable[[str, str], str] | None = None,
) -> None:
    server = build_server(
        harness=harness,
        repository=repository,
        conversation_path=conversation_path,
        host=host,
        port=port,
        confirmation_dispatcher=confirmation_dispatcher,
    )
    print(f"Clinic dashboard: http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local clinic state dashboard.")
    parser.add_argument("--database", default="data/clinic.db")
    parser.add_argument("--conversations", default="data/conversations.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    harness, repository = open_sqlite_harness(args.database)
    serve(
        harness=harness,
        repository=repository,
        conversation_path=Path(args.conversations).resolve(),
        host=args.host,
        port=args.port,
        confirmation_dispatcher=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
