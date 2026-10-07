"""Human-readable rendering for privacy-aware voice decision logs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping


HIGH_SIGNAL_EVENTS = frozenset(
    {
        "call.started",
        "call.opening_selected",
        "voice.connected",
        "live.session_started",
        "patient.turn_processed",
        "confirmation.semantic_interpreted",
        "confirmation.classification_failed",
        "confirmation.classification_stale",
        "confirmation.classification_restarted",
        "confirmation.classification_recovery",
        "workflow.transition",
        "delegation.created",
        "backend.response_started",
        "tool.requested",
        "tool.decision",
        "tool.completed",
        "tool.outcome_unknown",
        "assistant.interrupted",
        "assistant.turn",
        "control.directive_appended",
        "control.normal_turn_completed",
        "control.normal_turn_watchdog_started",
        "control.normal_turn_resumed",
        "control.normal_turn_progress",
        "control.normal_turn_liveness_failed",
        "control.normal_turn_recovery_appended",
        "control.normal_turn_recovery_failed",
        "voice.connect_failed",
        "voice.failed_mid_call",
        "media.sequence_gap",
        "media.duplicate_or_out_of_order",
        "call.remote_stop",
        "call.ended",
    }
)


_INTERRUPTION_BURST_COUNT = 3
_INTERRUPTION_BURST_WINDOW_NS = 6_000_000_000


def load_voice_events(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"voice event log not found: {source}")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        source.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON on voice log line {line_number}") from error
        if not isinstance(event, dict) or not isinstance(event.get("event_type"), str):
            raise ValueError(f"invalid voice event on line {line_number}")
        events.append(event)
    return events


def render_voice_timeline(events: Iterable[Mapping[str, Any]]) -> str:
    all_events = list(events)
    rows = [
        event for event in all_events if event.get("event_type") in HIGH_SIGNAL_EVENTS
    ]
    if not rows:
        return "No high-signal voice events found."
    lines: list[str] = []
    call_ids = sorted(
        {
            str(event["provider_call_id"])
            for event in rows
            if event.get("provider_call_id")
        }
    )
    lines.append(
        f"Voice decision timeline: {len(rows)} high-signal events"
        + (f"; calls={','.join(call_ids)}" if call_ids else "")
    )
    for index, event in enumerate(rows, start=1):
        event_type = str(event["event_type"])
        occurred_at = str(event.get("occurred_at", "unknown-time"))
        details = _event_details(event_type, event)
        lines.append(f"{index:02d}. {occurred_at} {event_type}{details}")
    problems = [
        event
        for event in rows
        if event.get("event_type")
        in {
            "tool.outcome_unknown",
            "voice.connect_failed",
            "voice.failed_mid_call",
            "media.sequence_gap",
            "media.duplicate_or_out_of_order",
        }
    ]
    terminal = [event for event in rows if event.get("event_type") == "call.ended"]
    lines.append(
        "Summary: "
        f"terminal={terminal[-1].get('outcome', 'not_observed') if terminal else 'not_observed'}, "
        f"anomalies={len(problems)}, "
        f"quality_flags={','.join(voice_quality_flags(all_events)) or 'none'}"
    )
    return "\n".join(lines)


def voice_quality_flags(events: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Detect run-level defects that transport-only health checks cannot see."""

    rows = list(events)
    flags: list[str] = []
    event_types = [str(event.get("event_type", "")) for event in rows]
    runs = _call_runs(rows) or [rows]

    if any(
        any(
            event.get("event_type") == "call.started"
            and event.get("direction") == "outbound"
            for event in run
        )
        and not any(event.get("event_type") == "call.opening_selected" for event in run)
        for run in runs
    ):
        flags.append("outbound_opening_unverified")

    if any(
        sum(
            1
            for event in run
            if event.get("event_type") == "tool.decision"
            and event.get("domain_status") == "rejected"
            and event.get("workflow_after") == "awaiting_name_confirmation"
        )
        >= 2
        for run in runs
    ):
        flags.append("repeated_identity_verification")

    if any(_has_repeated_proposal(run) for run in runs):
        flags.append("repeated_confirmation_cycle")

    patient_turns = sum(1 for value in event_types if value == "patient.turn_processed")
    assistant_turns = sum(1 for value in event_types if value == "assistant.turn")
    if patient_turns and not assistant_turns:
        flags.append("assistant_transcript_missing")

    completed_delegations: set[str] = set()
    premature_stale = False
    for event in rows:
        delegation_id = str(event.get("delegation_id", ""))
        if event.get("event_type") == "delegation.completed" and delegation_id:
            completed_delegations.add(delegation_id)
        if (
            event.get("event_type") == "delegation.stale"
            and delegation_id in completed_delegations
            and event.get("stage")
            in {"backend.response_started", "backend.tool_requested"}
        ):
            premature_stale = True
    if premature_stale:
        flags.append("premature_delegation_completion")

    unknown_provider_events = sum(
        1 for value in event_types if value == "provider.unknown_event"
    )
    if unknown_provider_events > 20:
        flags.append("excessive_unknown_provider_events")

    # Identity is a privacy gate, so a caller utterance at that gate must be
    # accounted for by the application before the call ends.  Evaluate each
    # call independently: a successful earlier call must not hide a later
    # call that heard the caller but never processed their turn.
    for run in runs:
        if not _is_identity_gated_outbound_call(run):
            continue
        caller_was_heard = any(
            event.get("event_type")
            in {"caller.speech_started", "assistant.interrupted"}
            for event in run
        )
        identity_was_processed = any(
            event.get("event_type") == "patient.turn_processed"
            or (
                event.get("event_type") == "tool.decision"
                and event.get("tool_name") == "interpret_identity_response"
            )
            for event in run
        )
        call_ended = any(event.get("event_type") == "call.ended" for event in run)
        if caller_was_heard and call_ended and not identity_was_processed:
            flags.append("identity_turn_unprocessed")
            break

    if any(_has_interruption_burst(run) for run in runs):
        flags.append("excessive_interruption_burst")

    for run in runs:
        flags.extend(_slot_quality_flags(run))
        last_workflow_transition = next(
            (
                event
                for event in reversed(run)
                if event.get("event_type") == "workflow.transition"
            ),
            None,
        )
        last_patient_turn = next(
            (
                event
                for event in reversed(run)
                if event.get("event_type") == "patient.turn_processed"
            ),
            None,
        )
        if (
            (last_workflow_transition or last_patient_turn)
            and (
                (last_workflow_transition or {}).get("current")
                if last_workflow_transition
                else (last_patient_turn or {}).get("workflow_after")
            )
            in {"active", "confirmation_pending", "recovery_required"}
            and any(event.get("event_type") == "call.ended" for event in run)
        ):
            flags.append("workflow_incomplete_at_hangup")

    return tuple(dict.fromkeys(flags))


def _has_repeated_proposal(run: list[Mapping[str, Any]]) -> bool:
    requests_by_operation = {
        str(event.get("operation_id")): event
        for event in run
        if event.get("event_type") == "tool.requested" and event.get("operation_id")
    }
    repeated_proposals: dict[str, int] = {}
    for event in run:
        if (
            event.get("event_type") != "tool.decision"
            or event.get("domain_status") != "confirmation_required"
        ):
            continue
        request = requests_by_operation.get(str(event.get("operation_id")))
        if not request:
            continue
        signature = json.dumps(
            {
                "tool_name": request.get("tool_name"),
                "arguments": request.get("arguments", {}),
            },
            sort_keys=True,
            default=str,
        )
        repeated_proposals[signature] = repeated_proposals.get(signature, 0) + 1
    return any(count >= 2 for count in repeated_proposals.values())


def _slot_quality_flags(run: list[Mapping[str, Any]]) -> list[str]:
    flags: list[str] = []
    slot_requests_by_operation = {
        str(event.get("operation_id")): event
        for event in run
        if event.get("event_type") == "tool.requested"
        and event.get("tool_name") == "search_slots"
        and event.get("operation_id")
    }
    for index, event in enumerate(run):
        if not (
            event.get("event_type") == "tool.decision"
            and event.get("tool_name") == "search_slots"
            and event.get("domain_status") == "ok"
        ):
            continue
        request = slot_requests_by_operation.get(str(event.get("operation_id")))
        arguments = request.get("arguments") if request else None
        if (
            event.get("result_count") == 0
            and isinstance(arguments, Mapping)
            and "location" in arguments
            and "location_id" not in arguments
        ):
            flags.append("noncanonical_slot_filter")
        later_assistant_turn = any(
            candidate.get("event_type") == "assistant.turn"
            for candidate in run[index + 1 :]
        )
        if event.get("result_count") == 0 and not later_assistant_turn:
            flags.append("no_slot_follow_up_missing")
        elif event.get("result_count") is None and not later_assistant_turn:
            flags.append("slot_search_follow_up_unverifiable")
    return flags


def _call_runs(rows: list[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Split an ordered JSONL stream into calls, retaining pre-start state."""

    runs: list[list[Mapping[str, Any]]] = []
    prelude: list[Mapping[str, Any]] = []
    current: list[Mapping[str, Any]] | None = None
    for event in rows:
        event_type = event.get("event_type")
        if event_type == "call.started":
            if current:
                runs.append(current)
            current = [*prelude, event]
            prelude = []
            continue
        if current is not None:
            current.append(event)
            if event_type == "call.ended":
                runs.append(current)
                current = None
            continue
        prelude.append(event)
    if current:
        runs.append(current)
    return runs


def latest_voice_call(
    rows: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return only the newest call, including its pre-start workflow events."""

    runs = _call_runs(rows)
    if not runs:
        raise ValueError("voice log contains no call runs")
    return runs[-1]


def _is_identity_gated_outbound_call(run: list[Mapping[str, Any]]) -> bool:
    outbound = any(
        event.get("event_type") == "call.started"
        and event.get("direction") == "outbound"
        for event in run
    )
    identity_gate = any(
        (
            event.get("event_type") == "call.opening_selected"
            and event.get("opening_state") == "awaiting_name_confirmation"
        )
        or (
            event.get("event_type") == "workflow.transition"
            and event.get("current") == "awaiting_name_confirmation"
        )
        for event in run
    )
    return outbound and identity_gate


def _has_interruption_burst(rows: list[Mapping[str, Any]]) -> bool:
    timestamps = sorted(
        int(event["monotonic_ns"])
        for event in rows
        if event.get("event_type") == "assistant.interrupted"
        and isinstance(event.get("monotonic_ns"), int)
    )
    for start in range(len(timestamps) - _INTERRUPTION_BURST_COUNT + 1):
        end = start + _INTERRUPTION_BURST_COUNT - 1
        if timestamps[end] - timestamps[start] <= _INTERRUPTION_BURST_WINDOW_NS:
            return True
    return False


def _event_details(event_type: str, event: Mapping[str, Any]) -> str:
    if event_type == "workflow.transition":
        return (
            f" {event.get('previous')} -> {event.get('current')}"
            f" reason={event.get('reason')}"
        )
    if event_type == "tool.decision":
        return (
            f" tool={event.get('tool_name')} domain={event.get('domain_status')}"
            f" state={event.get('workflow_before')}->{event.get('workflow_after')}"
            f" count={event.get('result_count', '')}"
        )
    if event_type == "patient.turn_processed":
        return (
            f" transcript={event.get('transcript')}"
            f" state={event.get('workflow_before')}->{event.get('workflow_after')}"
            f" directive={event.get('directive_kind')}"
        )
    if event_type == "confirmation.semantic_interpreted":
        return (
            f" proposal={event.get('proposal_id')}"
            f" decision={event.get('decision')}"
            f" authorized={event.get('authorized_for_turn')}"
        )
    if event_type in {
        "confirmation.classification_failed",
        "confirmation.classification_stale",
    }:
        return (
            f" proposal={event.get('proposal_id')}"
            f" error={event.get('error_class', '')}"
        )
    if event_type == "call.started":
        return f" direction={event.get('direction')}"
    if event_type == "call.ended":
        return f" outcome={event.get('outcome')}"
    if event_type in {"tool.requested", "tool.completed", "tool.outcome_unknown"}:
        return (
            f" tool={event.get('tool_name')} operation={event.get('operation_id')}"
            f" arguments={event.get('arguments', '')}"
            f" status={event.get('status', '')}"
        )
    if event_type == "assistant.interrupted":
        return f" played_ms={event.get('audio_end_ms')}"
    if event_type == "assistant.turn":
        return (
            f" transcript={event.get('transcript')}"
            f" reason={event.get('reason')}"
        )
    if event_type == "call.opening_selected":
        return (
            f" direction={event.get('direction')}"
            f" state={event.get('opening_state')}"
        )
    return ""


__all__ = [
    "HIGH_SIGNAL_EVENTS",
    "latest_voice_call",
    "load_voice_events",
    "render_voice_timeline",
    "voice_quality_flags",
]
