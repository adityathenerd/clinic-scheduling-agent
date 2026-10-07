from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from clinic_agent.runtime.voice_log import (
    load_voice_events,
    render_voice_timeline,
    voice_quality_flags,
)


class VoiceLogTests(unittest.TestCase):
    def test_flags_same_exact_write_reprepared_after_confirmation(self) -> None:
        arguments = {
            "appointment_id": "appointment-existing",
            "replacement_slot_id": "slot-mehta-20261012-1130",
        }
        events = [
            {
                "event_type": "tool.requested",
                "operation_id": "op-1",
                "tool_name": "edit_appointment",
                "arguments": arguments,
            },
            {
                "event_type": "tool.decision",
                "operation_id": "op-1",
                "tool_name": "edit_appointment",
                "domain_status": "confirmation_required",
            },
            {
                "event_type": "tool.requested",
                "operation_id": "op-2",
                "tool_name": "edit_appointment",
                "arguments": arguments,
            },
            {
                "event_type": "tool.decision",
                "operation_id": "op-2",
                "tool_name": "edit_appointment",
                "domain_status": "confirmation_required",
            },
        ]

        self.assertIn("repeated_confirmation_cycle", voice_quality_flags(events))

    def test_same_proposal_in_two_calls_is_not_a_confirmation_loop(self) -> None:
        arguments = {
            "appointment_id": "appointment-existing",
            "replacement_slot_id": "slot-mehta-20261012-1130",
        }
        events = []
        for call_number in (1, 2):
            operation_id = f"op-{call_number}"
            events.extend(
                [
                    {
                        "event_type": "call.started",
                        "direction": "inbound",
                    },
                    {
                        "event_type": "tool.requested",
                        "operation_id": operation_id,
                        "tool_name": "edit_appointment",
                        "arguments": arguments,
                    },
                    {
                        "event_type": "tool.decision",
                        "operation_id": operation_id,
                        "tool_name": "edit_appointment",
                        "domain_status": "confirmation_required",
                    },
                    {"event_type": "call.ended", "outcome": "closed"},
                ]
            )

        self.assertNotIn("repeated_confirmation_cycle", voice_quality_flags(events))

    def test_identity_retries_and_interruptions_do_not_accumulate_across_calls(self) -> None:
        events = []
        for call_number in (1, 2):
            events.extend(
                [
                    {
                        "event_type": "call.started",
                        "direction": "inbound",
                    },
                    {
                        "event_type": "tool.decision",
                        "domain_status": "rejected",
                        "workflow_after": "awaiting_name_confirmation",
                    },
                    {
                        "event_type": "assistant.interrupted",
                        "monotonic_ns": call_number * 1_000_000_000,
                    },
                    {"event_type": "call.ended", "outcome": "closed"},
                ]
            )

        flags = voice_quality_flags(events)
        self.assertNotIn("repeated_identity_verification", flags)
        self.assertNotIn("excessive_interruption_burst", flags)

    def test_renders_state_tools_terminal_and_anomaly_count(self) -> None:
        events = [
            {
                "event_type": "call.started",
                "occurred_at": "2026-10-06T00:00:00+00:00",
                "provider_call_id": "CA1",
                "direction": "outbound",
            },
            {
                "event_type": "workflow.transition",
                "occurred_at": "2026-10-06T00:00:01+00:00",
                "previous": "active",
                "current": "confirmation_pending",
                "reason": "exact_proposal_prepared",
            },
            {
                "event_type": "tool.decision",
                "occurred_at": "2026-10-06T00:00:02+00:00",
                "tool_name": "create_appointment",
                "domain_status": "confirmation_required",
                "workflow_before": "active",
                "workflow_after": "confirmation_pending",
            },
            {
                "event_type": "call.ended",
                "occurred_at": "2026-10-06T00:00:03+00:00",
                "outcome": "closed",
            },
        ]
        rendered = render_voice_timeline(events)
        self.assertIn("calls=CA1", rendered)
        self.assertIn("active -> confirmation_pending", rendered)
        self.assertIn("domain=confirmation_required", rendered)
        self.assertIn("terminal=closed, anomalies=0", rendered)

    def test_flags_first_run_identity_lifecycle_and_missing_spoken_evidence(self) -> None:
        events = [
            {
                "event_type": "call.started",
                "direction": "outbound",
            },
            {
                "event_type": "tool.decision",
                "domain_status": "rejected",
                "workflow_after": "awaiting_name_confirmation",
            },
            {
                "event_type": "tool.decision",
                "domain_status": "rejected",
                "workflow_after": "awaiting_name_confirmation",
            },
            {
                "event_type": "patient.turn_processed",
                "workflow_after": "active",
            },
            {
                "event_type": "delegation.completed",
                "delegation_id": "del-1",
            },
            {
                "event_type": "delegation.stale",
                "delegation_id": "del-1",
                "stage": "backend.response_started",
            },
            {
                "event_type": "tool.decision",
                "tool_name": "search_slots",
                "domain_status": "ok",
                "result_count": 0,
            },
            {"event_type": "call.ended", "outcome": "closed"},
        ]

        flags = voice_quality_flags(events)

        self.assertIn("outbound_opening_unverified", flags)
        self.assertIn("repeated_identity_verification", flags)
        self.assertIn("assistant_transcript_missing", flags)
        self.assertIn("premature_delegation_completion", flags)
        self.assertIn("no_slot_follow_up_missing", flags)
        self.assertIn("workflow_incomplete_at_hangup", flags)

    def test_clean_outbound_recovery_run_has_no_quality_flags(self) -> None:
        events = [
            {"event_type": "call.started", "direction": "outbound"},
            {
                "event_type": "call.opening_selected",
                "direction": "outbound",
                "opening_state": "awaiting_name_confirmation",
            },
            {
                "event_type": "patient.turn_processed",
                "workflow_after": "completed",
            },
            {
                "event_type": "tool.decision",
                "tool_name": "search_slots",
                "domain_status": "ok",
                "result_count": 0,
            },
            {
                "event_type": "assistant.turn",
                "transcript": "No slots were available.",
            },
            {"event_type": "call.ended", "outcome": "closed"},
        ]

        self.assertEqual((), voice_quality_flags(events))

    def test_flags_identity_turn_heard_but_not_processed_in_later_call(self) -> None:
        events = [
            {"event_type": "call.started", "direction": "outbound"},
            {"event_type": "patient.turn_processed", "workflow_after": "active"},
            {"event_type": "call.ended", "outcome": "closed"},
            {
                "event_type": "workflow.transition",
                "current": "awaiting_name_confirmation",
            },
            {
                "event_type": "call.opening_selected",
                "opening_state": "awaiting_name_confirmation",
            },
            {"event_type": "call.started", "direction": "outbound"},
            {
                "event_type": "assistant.interrupted",
                "monotonic_ns": 10_000_000_000,
            },
            {
                "event_type": "assistant.turn",
                "reason": "call_stopped",
                "transcript": "One moment while I pull up the details.",
            },
            {"event_type": "call.ended", "outcome": "closed"},
        ]

        self.assertIn("identity_turn_unprocessed", voice_quality_flags(events))

    def test_processed_identity_turn_is_not_flagged(self) -> None:
        events = [
            {
                "event_type": "call.opening_selected",
                "opening_state": "awaiting_name_confirmation",
            },
            {"event_type": "call.started", "direction": "outbound"},
            {"event_type": "assistant.interrupted", "monotonic_ns": 1},
            {
                "event_type": "patient.turn_processed",
                "workflow_before": "awaiting_name_confirmation",
                "workflow_after": "active",
            },
            {"event_type": "call.ended", "outcome": "closed"},
        ]

        self.assertNotIn("identity_turn_unprocessed", voice_quality_flags(events))

    def test_flags_noncanonical_location_label_that_returns_no_slots(self) -> None:
        events = [
            {
                "event_type": "tool.requested",
                "tool_name": "search_slots",
                "operation_id": "search-1",
                "arguments": {
                    "provider_id": "provider-mehta",
                    "location": "Downtown clinic",
                },
            },
            {
                "event_type": "tool.decision",
                "tool_name": "search_slots",
                "operation_id": "search-1",
                "domain_status": "ok",
                "result_count": 0,
            },
            {"event_type": "assistant.turn", "transcript": "No slots found."},
        ]

        self.assertIn("noncanonical_slot_filter", voice_quality_flags(events))

    def test_canonical_location_id_with_no_slots_is_not_noncanonical(self) -> None:
        events = [
            {
                "event_type": "tool.requested",
                "tool_name": "search_slots",
                "operation_id": "search-1",
                "arguments": {"location_id": "downtown"},
            },
            {
                "event_type": "tool.decision",
                "tool_name": "search_slots",
                "operation_id": "search-1",
                "domain_status": "ok",
                "result_count": 0,
            },
            {"event_type": "assistant.turn", "transcript": "No slots found."},
        ]

        self.assertNotIn("noncanonical_slot_filter", voice_quality_flags(events))

    def test_flags_three_interruptions_inside_six_second_window(self) -> None:
        events = [
            {"event_type": "assistant.interrupted", "monotonic_ns": 1_000_000_000},
            {"event_type": "assistant.interrupted", "monotonic_ns": 4_000_000_000},
            {"event_type": "assistant.interrupted", "monotonic_ns": 7_000_000_000},
        ]

        self.assertIn("excessive_interruption_burst", voice_quality_flags(events))

    def test_does_not_flag_sparse_interruptions_as_a_burst(self) -> None:
        events = [
            {"event_type": "assistant.interrupted", "monotonic_ns": 1_000_000_000},
            {"event_type": "assistant.interrupted", "monotonic_ns": 8_000_000_000},
            {"event_type": "assistant.interrupted", "monotonic_ns": 16_000_000_000},
        ]

        self.assertNotIn("excessive_interruption_burst", voice_quality_flags(events))

    def test_load_rejects_malformed_jsonl_with_line_number(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text('{"event_type":"ok"}\nnot-json\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 2"):
                load_voice_events(path)


if __name__ == "__main__":
    unittest.main()
