from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from clinic_agent.__main__ import PROJECT_ROOT, main
from clinic_agent.runtime.transcript_evaluator import evaluate_voice_events
from clinic_agent.runtime.voice_log import load_voice_events
from clinic_agent.runtime.speech_contract import spoken_contract_digest


def _issue_codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


class TranscriptEvaluatorTests(unittest.TestCase):
    def test_application_audio_without_final_playback_mark_is_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "assistant.application_audio_render_started"},
                {"event_type": "assistant.application_audio_queued"},
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("application_audio_delivery_incomplete", _issue_codes(result))

    def test_application_audio_final_playback_mark_disarms_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "assistant.application_audio_render_started"},
                {"event_type": "assistant.application_audio_queued"},
                {"event_type": "assistant.application_audio_completed"},
                {"event_type": "assistant.turn", "transcript": "Identity accepted."},
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertNotIn("application_audio_delivery_incomplete", _issue_codes(result))

    def test_application_audio_timeout_is_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "assistant.application_audio_render_started"},
                {"event_type": "assistant.application_audio_timeout"},
                {"event_type": "call.ended", "outcome": "failed"},
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("application_audio_delivery_failed", _issue_codes(result))

    def test_exact_reply_seen_only_at_call_stop_is_unverified(self) -> None:
        expected = "Thank you. Your identity is confirmed."
        result = evaluate_voice_events(
            [
                {
                    "event_type": "patient.turn_processed",
                    "occurred_at": "2026-10-08T00:00:00+00:00",
                    "directive_kind": "say_exactly",
                    "directive_digest": spoken_contract_digest(expected),
                },
                {
                    "event_type": "assistant.turn",
                    "occurred_at": "2026-10-08T00:00:30+00:00",
                    "reason": "call_stopped",
                    "transcript": expected,
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("application_reply_playback_unverified", _issue_codes(result))

    def test_substantive_follow_up_request_cannot_be_closed_as_declined(self) -> None:
        result = evaluate_voice_events(
            [
                {
                    "event_type": "patient.turn_processed",
                    "workflow_before": "awaiting_follow_up_decision",
                    "workflow_after": "completed",
                    "transcript": (
                        "No, that's all. Do I have prerequisites for this appointment? "
                        "I would like to know whether parking is available at the clinic."
                    ),
                },
                {
                    "event_type": "follow_up.completed_turn_classified",
                    "decision": "declined",
                    "tools_unlocked": False,
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "Thank you. Take care.",
                },
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn(
            "substantive_follow_up_request_discarded",
            _issue_codes(result),
        )

    def test_availability_retrieval_failure_without_search_is_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {
                    "event_type": "caller.turn",
                    "transcript": "Do you have any other time available later this week?",
                },
                {
                    "event_type": "tool.requested",
                    "tool_name": "get_appointment",
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": (
                        "I'm sorry, I wasn't able to retrieve availability, so I "
                        "can't confirm another time."
                    ),
                },
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("availability_failure_without_search", _issue_codes(result))

    def test_availability_retrieval_failure_is_grounded_by_search_attempt(self) -> None:
        result = evaluate_voice_events(
            [
                {
                    "event_type": "caller.turn",
                    "transcript": "Are there any available timings on Friday?",
                },
                {
                    "event_type": "tool.requested",
                    "tool_name": "search_slots",
                },
                {
                    "event_type": "tool.decision",
                    "tool_name": "search_slots",
                    "domain_status": "failed",
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "I couldn't retrieve availability for Friday.",
                },
            ]
        )

        self.assertNotIn("availability_failure_without_search", _issue_codes(result))

    def test_identity_pending_claim_after_unlock_is_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "workflow.transition",
                    "previous": "awaiting_name_confirmation",
                    "current": "active",
                    "reason": "name_confirmation_accepted",
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "I'm waiting on the identity verification to go through.",
                },
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("identity_state_contradiction", _issue_codes(result))

    def test_outbound_generic_reset_after_identity_is_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "workflow.transition",
                    "previous": "awaiting_name_confirmation",
                    "current": "active",
                    "reason": "name_confirmation_accepted",
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "Thanks for waiting. How can I help today?",
                },
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("outbound_post_identity_purpose_reset", _issue_codes(result))

    def test_exact_application_reply_digest_is_enforced(self) -> None:
        expected = "Thank you for confirming your identity. What would you like to change?"
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "patient.turn_processed",
                    "directive_kind": "say_exactly",
                    "directive_digest": spoken_contract_digest(expected),
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "I'm still waiting for verification.",
                },
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("exact_application_reply_not_honored", _issue_codes(result))

    def test_exact_application_reply_allows_punctuation_and_case_variation(self) -> None:
        expected = "Thank you for confirming your identity."
        result = evaluate_voice_events(
            [
                {
                    "event_type": "patient.turn_processed",
                    "directive_kind": "say_exactly",
                    "directive_digest": spoken_contract_digest(expected),
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "THANK YOU for confirming your identity!",
                },
            ]
        )

        self.assertNotIn("exact_application_reply_not_honored", _issue_codes(result))

    def test_exact_application_reply_allows_bounded_spoken_time_variation(self) -> None:
        expected = "Wednesday, October 21, 2026 at 11:00 AM (UTC+05:30)."
        result = evaluate_voice_events(
            [
                {
                    "event_type": "patient.turn_processed",
                    "directive_kind": "say_exactly",
                    "directive_digest": spoken_contract_digest(expected),
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": (
                        "Wednesday, October 21st, 2026 at 11 AM, UTC plus 05:30."
                    ),
                },
            ]
        )

        self.assertNotIn("exact_application_reply_not_honored", _issue_codes(result))

    def test_identity_progress_prefix_does_not_invalidate_exact_application_reply(self) -> None:
        expected = "Thank you for confirming your identity. Your appointment is confirmed."
        result = evaluate_voice_events(
            [
                {
                    "event_type": "control.application_gate_progress_appended",
                    "occurred_at": "2026-10-07T12:00:00+00:00",
                },
                {
                    "event_type": "assistant.gate_progress_audio_started",
                    "occurred_at": "2026-10-07T12:00:00.200000+00:00",
                },
                {
                    "event_type": "patient.turn_processed",
                    "directive_kind": "say_exactly",
                    "directive_digest": spoken_contract_digest(expected),
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": (
                        "Thank you. I'm checking your identity confirmation now. "
                        + expected
                    ),
                },
            ]
        )

        self.assertNotIn("exact_application_reply_not_honored", _issue_codes(result))

    def test_confirmation_progress_prefix_does_not_invalidate_exact_application_reply(self) -> None:
        expected = "Your rescheduling request is held pending clinic approval."
        result = evaluate_voice_events(
            [
                {
                    "event_type": "control.application_gate_progress_appended",
                    "gate_kind": "confirmation",
                    "occurred_at": "2026-10-07T12:00:00+00:00",
                },
                {
                    "event_type": "assistant.gate_progress_audio_started",
                    "occurred_at": "2026-10-07T12:00:00.200000+00:00",
                },
                {
                    "event_type": "patient.turn_processed",
                    "directive_kind": "say_exactly",
                    "directive_digest": spoken_contract_digest(expected),
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": (
                        "Thank you. I'm checking your confirmation now. " + expected
                    ),
                },
            ]
        )

        self.assertNotIn("exact_application_reply_not_honored", _issue_codes(result))

    def test_confirmation_from_proposal_origin_epoch_is_a_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {
                    "event_type": "confirmation.proposal_prepared_for_delivery",
                    "origin_turn_epoch": 7,
                },
                {
                    "event_type": "confirmation.proposal_armed",
                    "after_turn_epoch": 7,
                },
                {
                    "event_type": "confirmation.semantic_interpreted",
                    "turn_epoch": 7,
                    "decision": "confirmed",
                },
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("confirmation_before_proposal_armed", _issue_codes(result))

    def test_confirmation_from_later_epoch_passes_delivery_gate(self) -> None:
        result = evaluate_voice_events(
            [
                {
                    "event_type": "confirmation.proposal_prepared_for_delivery",
                    "origin_turn_epoch": 7,
                },
                {
                    "event_type": "confirmation.proposal_armed",
                    "after_turn_epoch": 7,
                },
                {
                    "event_type": "confirmation.semantic_interpreted",
                    "turn_epoch": 8,
                    "decision": "confirmed",
                },
            ]
        )

        self.assertNotIn("confirmation_before_proposal_armed", _issue_codes(result))

    def test_application_owned_proposal_must_be_spoken_without_autonomous_prefix(self) -> None:
        expected = (
            "Here is the exact proposed rescheduling: move the visit to Wednesday "
            "at 11 AM. Do you explicitly confirm this rescheduling? Please say yes or no."
        )
        result = evaluate_voice_events(
            [
                {
                    "event_type": "confirmation.proposal_directive_appended",
                    "directive_digest": spoken_contract_digest(expected),
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "Got it. Checking that one. " + expected,
                },
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("exact_application_reply_not_honored", _issue_codes(result))

    def test_pre_delivery_continuation_supersedes_unspoken_proposal_contract(self) -> None:
        expected = "Here is the exact proposed rescheduling: Wednesday at 11 AM."
        result = evaluate_voice_events(
            [
                {
                    "event_type": "confirmation.proposal_directive_appended",
                    "directive_digest": spoken_contract_digest(expected),
                },
                {
                    "event_type": "confirmation.pre_delivery_continuation",
                    "origin_turn_epoch": 7,
                    "continuation_turn_epoch": 7,
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "Got it. I'm checking Wednesday at 11 AM now.",
                },
            ]
        )

        self.assertNotIn("exact_application_reply_not_honored", _issue_codes(result))

    def test_explicit_caller_close_followed_by_stall_is_a_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "inbound"},
                {
                    "event_type": "patient.turn_observed",
                    "workflow_before": "active",
                    "transcript": "That's all. Thank you so much.",
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "Okay. I'll pause here and wait for you.",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("caller_close_not_honored", _issue_codes(result))

    def test_explicit_caller_close_followed_by_clean_goodbye_passes(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "inbound"},
                {
                    "event_type": "patient.turn_processed",
                    "workflow_before": "active",
                    "workflow_after": "completed",
                    "transcript": "That's all. Thank you so much.",
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "Thank you for calling. Take care.",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertNotIn("caller_close_not_honored", _issue_codes(result))

    def test_authoritative_directive_delivery_failure_is_a_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "control.completed_turn_delivery_failed",
                    "error_class": "TimeoutError",
                },
                {"event_type": "call.ended", "outcome": "failed"},
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn(
            "authoritative_directive_delivery_failed", _issue_codes(result)
        )

    def test_missing_identity_progress_audio_is_a_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "control.application_gate_progress_appended",
                    "occurred_at": "2026-10-07T12:00:00+00:00",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("identity_progress_update_missing", _issue_codes(result))

    def test_late_identity_progress_audio_is_a_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "control.application_gate_progress_appended",
                    "occurred_at": "2026-10-07T12:00:00+00:00",
                },
                {
                    "event_type": "assistant.gate_progress_audio_started",
                    "occurred_at": "2026-10-07T12:00:03+00:00",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertEqual("fail", result.status)
        self.assertIn("identity_progress_update_late", _issue_codes(result))

    def test_timely_identity_progress_audio_passes_latency_gate(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "control.application_gate_progress_appended",
                    "occurred_at": "2026-10-07T12:00:00+00:00",
                },
                {
                    "event_type": "assistant.gate_progress_audio_started",
                    "occurred_at": "2026-10-07T12:00:01+00:00",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertNotIn("identity_progress_update_missing", _issue_codes(result))
        self.assertNotIn("identity_progress_update_late", _issue_codes(result))

    def test_repeated_semantic_acceptance_for_same_proposal_is_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "confirmation.semantic_interpreted",
                    "proposal_id": "proposal-0001",
                    "decision": "confirmed",
                },
                {
                    "event_type": "confirmation.semantic_interpreted",
                    "proposal_id": "proposal-0001",
                    "decision": "confirmed",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertIn("confirmation_acceptance_loop", _issue_codes(result))
        self.assertEqual("fail", result.status)

    def test_confirmed_backend_conflict_is_a_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "confirmation.mutation_executed",
                    "proposal_id": "proposal-0001",
                    "tool_name": "edit_appointment",
                    "domain_status": "conflict",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        issue = next(
            item for item in result.hard_failures
            if item.code == "confirmed_mutation_conflict"
        )
        self.assertIn("proposal_id=proposal-0001", issue.evidence[0])
        self.assertEqual("fail", result.status)

    def test_v04_is_a_hard_failure_for_observed_confirmation_loop(self) -> None:
        result = evaluate_voice_events(
            load_voice_events(PROJECT_ROOT / "data" / "voice-v04.jsonl")
        )

        self.assertEqual("fail", result.status)
        self.assertEqual(1, result.exit_code)
        self.assertIn("repeated_confirmation_cycle", _issue_codes(result))
        self.assertIn("repeated_confirmation_prompt", _issue_codes(result))
        self.assertIn("workflow_incomplete_at_hangup", _issue_codes(result))
        self.assertEqual(0, result.metrics["verified_writes"])

    def test_v05_now_fails_the_reschedule_clinic_approval_gate(self) -> None:
        result = evaluate_voice_events(
            load_voice_events(PROJECT_ROOT / "data" / "voice-v05.jsonl")
        )

        self.assertEqual("fail", result.status)
        self.assertEqual(1, result.exit_code)
        self.assertIn("premature_commitment_wording", _issue_codes(result))
        self.assertIn("reschedule_clinic_approval_bypassed", _issue_codes(result))
        self.assertEqual(1, result.metrics["verified_writes"])

    def test_v06_fails_when_follow_on_booking_is_blocked_after_cancellation(self) -> None:
        result = evaluate_voice_events(
            load_voice_events(PROJECT_ROOT / "data" / "voice-v06.jsonl")
        )

        self.assertEqual("fail", result.status)
        self.assertIn("follow_on_request_blocked", _issue_codes(result))
        self.assertEqual(2, result.metrics["write_requests"])
        self.assertEqual(1, result.metrics["verified_writes"])

    def test_v07_reports_real_reschedule_failure_and_redundant_identity_observation(self) -> None:
        result = evaluate_voice_events(
            load_voice_events(PROJECT_ROOT / "data" / "voice-v07.jsonl")
        )

        self.assertEqual("fail", result.status)
        self.assertIn("reschedule_clinic_approval_bypassed", _issue_codes(result))
        self.assertIn(
            "redundant_identity_interpretation_after_unlock", _issue_codes(result)
        )

    def test_claiming_success_before_verified_write_is_a_hard_failure(self) -> None:
        events = [
            {
                "event_type": "tool.decision",
                "tool_name": "edit_appointment",
                "domain_status": "confirmation_required",
            },
            {
                "event_type": "assistant.turn",
                "transcript": "It's set. Your appointment has been rescheduled.",
            },
        ]

        result = evaluate_voice_events(events)

        self.assertEqual("fail", result.status)
        self.assertIn("unverified_success_claim", _issue_codes(result))

    def test_directly_verified_reschedule_is_a_clinic_approval_bypass(self) -> None:
        events = [
            {
                "event_type": "tool.decision",
                "tool_name": "edit_appointment",
                "domain_status": "confirmation_required",
            },
            {
                "event_type": "tool.decision",
                "tool_name": "edit_appointment",
                "domain_status": "verified",
            },
            {
                "event_type": "assistant.turn",
                "transcript": "It's set. Your appointment has been rescheduled.",
            },
        ]

        result = evaluate_voice_events(events)

        self.assertNotIn("unverified_success_claim", _issue_codes(result))
        self.assertIn("reschedule_clinic_approval_bypassed", _issue_codes(result))
        self.assertEqual("fail", result.status)

    def test_completed_transition_after_last_patient_turn_prevents_false_incomplete_flag(self) -> None:
        events = [
            {"event_type": "call.started", "direction": "outbound"},
            {
                "event_type": "patient.turn_processed",
                "workflow_after": "confirmation_pending",
            },
            {
                "event_type": "workflow.transition",
                "previous": "confirmation_pending",
                "current": "completed",
            },
            {"event_type": "call.ended", "outcome": "closed"},
        ]

        result = evaluate_voice_events(events)

        self.assertNotIn("workflow_incomplete_at_hangup", _issue_codes(result))

    def test_post_commit_verification_read_is_not_misclassified_as_follow_on_request(self) -> None:
        events = [
            {
                "event_type": "patient.turn_processed",
                "workflow_before": "confirmation_pending",
                "workflow_after": "confirmation_pending",
            },
            {
                "event_type": "workflow.transition",
                "previous": "confirmation_pending",
                "current": "completed",
            },
            {
                "event_type": "tool.decision",
                "tool_name": "get_appointment",
                "domain_status": "rejected",
                "workflow_before": "completed",
                "workflow_after": "completed",
            },
        ]

        result = evaluate_voice_events(events)

        self.assertNotIn("follow_on_request_blocked", _issue_codes(result))

    def test_patient_turn_after_completion_still_flags_blocked_follow_on_request(self) -> None:
        events = [
            {
                "event_type": "workflow.transition",
                "previous": "confirmation_pending",
                "current": "completed",
            },
            {
                "event_type": "patient.turn_processed",
                "workflow_before": "completed",
                "workflow_after": "completed",
                "transcript": "I also need to book another appointment.",
            },
            {
                "event_type": "tool.decision",
                "tool_name": "search_slots",
                "domain_status": "rejected",
                "workflow_before": "completed",
                "workflow_after": "completed",
            },
        ]

        result = evaluate_voice_events(events)

        self.assertIn("follow_on_request_blocked", _issue_codes(result))

    def test_repeated_identity_prompt_before_unlock_is_a_hard_failure(self) -> None:
        events = [
            {
                "event_type": "assistant.turn",
                "transcript": "Am I speaking with Asha Rao?",
            },
            {
                "event_type": "assistant.turn",
                "transcript": "Am I speaking with Asha Rao?",
            },
        ]

        result = evaluate_voice_events(events)

        self.assertIn("repeated_identity_prompt", _issue_codes(result))
        self.assertEqual("fail", result.status)

    def test_repetition_does_not_leak_across_separate_calls(self) -> None:
        events = [
            {"event_type": "call.started", "direction": "inbound"},
            {
                "event_type": "assistant.turn",
                "transcript": "Am I speaking with Asha Rao?",
            },
            {"event_type": "call.ended", "outcome": "closed"},
            {"event_type": "call.started", "direction": "inbound"},
            {
                "event_type": "assistant.turn",
                "transcript": "Am I speaking with Asha Rao?",
            },
            {"event_type": "call.ended", "outcome": "closed"},
        ]

        result = evaluate_voice_events(events)

        self.assertNotIn("repeated_identity_prompt", _issue_codes(result))
        self.assertNotIn("repeated_assistant_question", _issue_codes(result))

    def test_long_phrase_repeated_inside_one_turn_is_observed(self) -> None:
        phrase = "To confirm here is the exact proposal for your appointment"
        result = evaluate_voice_events(
            [
                {
                    "event_type": "assistant.turn",
                    "transcript": f"{phrase}. {phrase}.",
                }
            ]
        )

        self.assertEqual("pass_with_observations", result.status)
        self.assertIn("repeated_assistant_phrase", _issue_codes(result))

    def test_approval_claim_without_resolved_notification_context_fails(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "assistant.turn",
                    "transcript": "I see your appointment's approved.",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertIn("unverified_success_claim", _issue_codes(result))
        self.assertEqual("fail", result.status)

    def test_resolved_notification_context_grounds_approval_claim(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {"event_type": "notification.context_resolved"},
                {
                    "event_type": "assistant.turn",
                    "transcript": "Your appointment is confirmed.",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertNotIn("unverified_success_claim", _issue_codes(result))

    def test_confirmation_call_cannot_reset_to_generic_help_after_context_resolution(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {"event_type": "notification.context_resolved"},
                {
                    "event_type": "assistant.turn",
                    "transcript": (
                        "How can I help with your scheduling today? Thank you for "
                        "confirming your identity. Your appointment is confirmed."
                    ),
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertIn("notification_call_purpose_reset", _issue_codes(result))
        self.assertEqual("fail", result.status)

    def test_application_owned_close_detects_prefixed_autonomous_reply(self) -> None:
        result = evaluate_voice_events(
            [
                {
                    "event_type": "patient.turn_processed",
                    "workflow_before": "awaiting_follow_up_decision",
                    "workflow_after": "completed",
                    "directive_kind": "application_reply",
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": (
                        "Okay. You're all set. Take care. Understood. I won't take "
                        "any further scheduling action."
                    ),
                },
            ]
        )

        self.assertIn(
            "application_reply_mixed_with_autonomous_output", _issue_codes(result)
        )
        self.assertEqual("pass_with_observations", result.status)

    def test_accepted_follow_up_that_strands_after_instruction_is_a_hard_failure(self) -> None:
        result = evaluate_voice_events(
            [
                {
                    "event_type": "patient.turn_processed",
                    "occurred_at": "2026-10-08T10:32:15+00:00",
                    "workflow_before": "awaiting_follow_up_decision",
                    "workflow_after": "active",
                    "directive_kind": "model_input",
                    "transcript": "Can I get medicines from the clinic pharmacy?",
                },
                {
                    "event_type": "control.completed_turn_directive",
                    "occurred_at": "2026-10-08T10:32:16+00:00",
                },
                {
                    "event_type": "assistant.authoritative_audio_started",
                    "occurred_at": "2026-10-08T10:32:16.100000+00:00",
                },
                {
                    "event_type": "call.remote_stop",
                    "occurred_at": "2026-10-08T10:32:58+00:00",
                },
            ]
        )

        self.assertIn("accepted_follow_up_no_progress", _issue_codes(result))
        self.assertEqual("fail", result.status)

    def test_accepted_follow_up_with_faq_tool_progress_passes_liveness_check(self) -> None:
        result = evaluate_voice_events(
            [
                {
                    "event_type": "patient.turn_processed",
                    "occurred_at": "2026-10-08T10:32:15+00:00",
                    "workflow_before": "awaiting_follow_up_decision",
                    "workflow_after": "active",
                    "directive_kind": "model_input",
                    "transcript": "Can I get medicines from the clinic pharmacy?",
                },
                {
                    "event_type": "tool.requested",
                    "occurred_at": "2026-10-08T10:32:16+00:00",
                    "tool_name": "search_clinic_faqs",
                },
                {
                    "event_type": "assistant.turn",
                    "occurred_at": "2026-10-08T10:32:18+00:00",
                    "transcript": "The clinic pharmacy can dispense prescribed medicines.",
                },
            ]
        )

        self.assertNotIn("accepted_follow_up_no_progress", _issue_codes(result))

    def test_clean_exact_close_has_no_mixed_application_reply_observation(self) -> None:
        result = evaluate_voice_events(
            [
                {
                    "event_type": "patient.turn_processed",
                    "workflow_before": "awaiting_follow_up_decision",
                    "workflow_after": "completed",
                    "directive_kind": "say_exactly",
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "Thank you. Take care.",
                },
            ]
        )

        self.assertNotIn(
            "application_reply_mixed_with_autonomous_output", _issue_codes(result)
        )

    def test_semantically_repeated_cancellation_confirmation_is_observed(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "assistant.turn",
                    "transcript": "Would you like me to cancel your appointment?",
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "Please confirm this cancellation so I can proceed.",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        issue = next(
            issue
            for issue in result.issues
            if issue.code == "repeated_confirmation_prompt"
        )
        self.assertEqual("observation", issue.severity)
        self.assertEqual((1, 2), issue.event_indexes)
        self.assertIn("cancellation", issue.message)

    def test_confirmations_for_distinct_mutations_are_not_conflated(self) -> None:
        result = evaluate_voice_events(
            [
                {"event_type": "call.started", "direction": "outbound"},
                {
                    "event_type": "assistant.turn",
                    "transcript": "Would you like me to cancel your appointment?",
                },
                {
                    "event_type": "assistant.turn",
                    "transcript": "Would you like me to book the new appointment?",
                },
                {"event_type": "call.ended", "outcome": "closed"},
            ]
        )

        self.assertNotIn("repeated_confirmation_prompt", _issue_codes(result))

    def test_cli_json_is_machine_readable_and_failure_exit_is_nonzero(self) -> None:
        events = [
            {
                "event_type": "tool.decision",
                "tool_name": "delete_appointment",
                "domain_status": "confirmation_required",
            },
            {"event_type": "assistant.turn", "transcript": "It's all set."},
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.jsonl"
            path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--evaluate-voice-log",
                        "--evaluation-format",
                        "json",
                        "--voice-log",
                        str(path),
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(1, exit_code)
        self.assertEqual("fail", payload["status"])
        self.assertGreaterEqual(payload["hard_failure_count"], 1)

    def test_cli_can_scope_evaluation_to_latest_call(self) -> None:
        events = [
            {"event_type": "call.started", "direction": "inbound"},
            {
                "event_type": "tool.decision",
                "tool_name": "delete_appointment",
                "domain_status": "confirmation_required",
            },
            {"event_type": "assistant.turn", "transcript": "It's all set."},
            {"event_type": "call.ended", "outcome": "closed"},
            {"event_type": "call.started", "direction": "inbound"},
            {
                "event_type": "assistant.turn",
                "transcript": "How can I help with scheduling?",
            },
            {"event_type": "call.ended", "outcome": "closed"},
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.jsonl"
            path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--evaluate-voice-log",
                        "--latest-call",
                        "--evaluation-format",
                        "json",
                        "--voice-log",
                        str(path),
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("pass", payload["status"])
        self.assertEqual(1, payload["metrics"]["calls"])


if __name__ == "__main__":
    unittest.main()
