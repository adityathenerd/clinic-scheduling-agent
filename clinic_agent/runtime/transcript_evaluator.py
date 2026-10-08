"""Deterministic quality gate for real voice call logs.

This evaluator deliberately uses only structured events and conservative text
patterns.  It is suitable for CI and incident review: the same log always
produces the same result, and a model cannot waive a safety invariant.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
from typing import Any, Iterable, Literal, Mapping

from clinic_agent.runtime.voice_log import voice_quality_flags
from clinic_agent.runtime.speech_contract import spoken_contract_digest
from clinic_agent.call_mechanics.models import NORMAL_TURN_PROGRESS_TIMEOUT


Severity = Literal["failure", "observation"]


_BASELINE_FAILURES = frozenset(
    {
        "outbound_opening_unverified",
        "repeated_identity_verification",
        "repeated_confirmation_cycle",
        "assistant_transcript_missing",
        "premature_delegation_completion",
        "identity_turn_unprocessed",
        "noncanonical_slot_filter",
        "no_slot_follow_up_missing",
        "availability_failure_without_search",
        "substantive_follow_up_request_discarded",
        "confirmed_mutation_conflict",
        "slot_search_follow_up_unverifiable",
        "workflow_incomplete_at_hangup",
        "follow_on_request_blocked",
        "reschedule_clinic_approval_bypassed",
        "accepted_follow_up_no_progress",
        "application_gate_exact_reply_misclassified",
    }
)

_FLAG_MESSAGES = {
    "outbound_opening_unverified": "Outbound call did not record a direction-aware opening.",
    "repeated_identity_verification": "Identity verification was rejected repeatedly.",
    "repeated_confirmation_cycle": "The same write proposal was prepared more than once.",
    "assistant_transcript_missing": "Caller turns exist without auditable assistant transcripts.",
    "premature_delegation_completion": "A completed delegation later emitted active work.",
    "excessive_unknown_provider_events": "Provider emitted an excessive number of unknown events.",
    "identity_turn_unprocessed": "Caller spoke at the identity gate but the turn was never processed.",
    "excessive_interruption_burst": "Assistant playback was interrupted at least three times in six seconds.",
    "noncanonical_slot_filter": "Availability search used a display label where a canonical identifier was required.",
    "no_slot_follow_up_missing": "A zero-result availability search had no spoken follow-up.",
    "availability_failure_without_search": "The assistant claimed availability could not be retrieved without attempting a fresh slot search.",
    "substantive_follow_up_request_discarded": "A substantive request after the post-task offer was classified as call closure and discarded.",
    "confirmed_mutation_conflict": "A caller-confirmed appointment mutation reached a backend conflict instead of the promised outcome.",
    "slot_search_follow_up_unverifiable": "Availability search result could not be verified and had no follow-up.",
    "workflow_incomplete_at_hangup": "The call ended with a scheduling workflow still in progress.",
    "follow_on_request_blocked": "A new scheduling request was rejected only because an earlier task in the same call had completed.",
    "reschedule_clinic_approval_bypassed": "A reschedule was directly committed instead of holding the replacement for clinic approval.",
    "redundant_identity_interpretation_after_unlock": "The backend requested identity interpretation after application identity was already locked.",
    "notification_call_purpose_reset": "A clinic-initiated confirmation call asked the patient how it could help instead of delivering the confirmed appointment context.",
    "application_reply_mixed_with_autonomous_output": "The assistant combined an autonomous response with the application-owned reply in one spoken turn.",
    "confirmation_acceptance_loop": "The application accepted confirmation for the same proposal more than once instead of executing it atomically.",
    "identity_state_contradiction": "Assistant described identity verification as pending after the application locked it as confirmed.",
    "outbound_post_identity_purpose_reset": "Outbound call reset to a generic help prompt after identity instead of preserving its scheduling-callback purpose.",
    "exact_application_reply_not_honored": "Assistant did not say the application-owned exact reply and nothing else.",
    "authoritative_directive_delivery_failed": "The application could not deliver an authoritative gate response to the Live session.",
    "application_audio_delivery_failed": "Application-owned speech failed before verified caller playback.",
    "application_audio_delivery_incomplete": "Application-owned speech started but never reached a verified final playback mark.",
    "application_reply_playback_unverified": "An exact application reply was only observed when the call stopped, so caller playback was not verified.",
    "identity_progress_update_missing": "Identity verification ran without the promised immediate caller-facing progress update.",
    "identity_progress_update_late": "The caller-facing identity progress update started too late.",
    "confirmation_progress_update_missing": "Confirmation classification ran without the promised immediate caller-facing progress update.",
    "confirmation_progress_update_late": "The caller-facing confirmation progress update started too late.",
    "confirmation_before_proposal_armed": "A caller turn was interpreted as confirmation before the exact proposal was armed for a later turn.",
    "caller_close_not_honored": "Caller explicitly ended the call, but the assistant continued or stalled instead of closing cleanly.",
    "normal_active_turn_no_progress": "A completed normal ACTIVE caller turn received no downstream progress within its deadline.",
    "accepted_follow_up_no_progress": "An accepted post-task request returned to ACTIVE but produced no tool, completed reply, recovery, or handoff.",
    "application_gate_exact_reply_misclassified": "An application-owned exact gate reply was incorrectly adopted as a normal conversational turn.",
}

_WRITE_TOOLS = frozenset(
    {"create_appointment", "edit_appointment", "delete_appointment"}
)

_PREMATURE_COMMITMENT_PATTERNS = (
    re.compile(r"\b(?:i(?:'m| am)\s+)?putting (?:that|the) change through\b", re.I),
    re.compile(r"\b(?:i(?:'m| am)|i'll|we(?:'re| are))?\s*submitting (?:that|it|the change) now\b", re.I),
    re.compile(r"\b(?:i(?:'m| am)|i'll|we(?:'re| are))?\s*processing (?:that|it|the)(?: change)? now\b", re.I),
    re.compile(r"\b(?:i(?:'m| am)|i'll|we(?:'re| are))?\s*making (?:that|the) change now\b", re.I),
)

_SUCCESS_CLAIM_PATTERNS = (
    re.compile(r"\b(?:it|that)(?:'s| is) (?:all )?set\b", re.I),
    re.compile(
        r"\b(?:your|the) (?:appointment|visit|change).{0,50}"
        r"(?:has been|is now) (?:booked|scheduled|rescheduled|cancelled|canceled|complete|completed)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:successfully|now) (?:booked|scheduled|rescheduled|cancelled|canceled)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:your|the) appointment(?:'s| is| has been) (?:approved|confirmed)\b",
        re.I,
    ),
)

_IDENTITY_PROMPT_PATTERNS = (
    re.compile(r"\bam i speaking with\b", re.I),
    re.compile(r"\b(?:confirm|verify) (?:your|the) (?:full )?name\b", re.I),
    re.compile(r"\bis this [a-z][a-z .'-]+(?: speaking)?\b", re.I),
)

_IDENTITY_PENDING_PATTERNS = (
    re.compile(r"\bwait(?:ing)? (?:on|for).{0,40}(?:identity )?verification\b", re.I),
    re.compile(r"\b(?:identity )?verification.{0,30}(?:pending|go through|complete)\b", re.I),
    re.compile(r"\bonce (?:your )?(?:identity )?verification (?:is|has)\b", re.I),
)

_CONFIRMATION_PROMPT_PATTERN = re.compile(
    r"\b(?:can you confirm|do you confirm|would you like me to make that change|"
    r"would you like me to (?:book|cancel|reschedule)|"
    r"please confirm (?:this|the) (?:booking|cancellation|cancelation|rescheduling|reschedule)|"
    r"is that correct)\b",
    re.I,
)

_APPLICATION_PROGRESS_PREFIX = re.compile(
    r"^\s*thank you[.!]?\s+i(?:'m| am) checking your "
    r"(?:identity confirmation|confirmation) now[.!]?\s*",
    re.I,
)

_CALLER_CLOSE_PATTERNS = (
    re.compile(r"\b(?:that(?:'s| is)|this is) all\b", re.I),
    re.compile(r"\bnothing else\b", re.I),
    re.compile(r"\b(?:goodbye|bye)\b", re.I),
    re.compile(r"\bi(?:'m| am) (?:all )?done\b", re.I),
)

_ASSISTANT_TERMINAL_CLOSE_PATTERNS = (
    re.compile(r"\bthank you for calling\b", re.I),
    re.compile(r"\btake care\b", re.I),
    re.compile(r"\b(?:goodbye|bye)\b", re.I),
    re.compile(r"\bhave a (?:good|great|nice) day\b", re.I),
)

_AVAILABILITY_REQUEST_PATTERN = re.compile(
    r"\b(?:available|availability|opening|openings|slot|slots|timing|timings)\b",
    re.I,
)

_AVAILABILITY_RETRIEVAL_FAILURE_PATTERN = re.compile(
    r"\b(?:can(?:not|'t)|could(?: not|n't)|was(?: not|n't) able to|unable to)\s+"
    r"(?:retrieve|check|access|find)\s+(?:the\s+)?availability\b",
    re.I,
)

_SUBSTANTIVE_FOLLOW_UP_REQUEST_PATTERN = re.compile(
    r"\b(?:do i have|are there|is there|can you|could you|would you|"
    r"i would like to know|tell me|what|when|where|how|parking|insurance|"
    r"prerequisite|prerequisites|provider|doctor|arrival)\b",
    re.I,
)


@dataclass(frozen=True)
class VoiceEvaluationIssue:
    code: str
    severity: Severity
    message: str
    event_indexes: tuple[int, ...] = ()
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VoiceEvaluationResult:
    schema_version: str
    status: Literal["pass", "pass_with_observations", "fail"]
    issues: tuple[VoiceEvaluationIssue, ...]
    metrics: Mapping[str, int]

    @property
    def hard_failures(self) -> tuple[VoiceEvaluationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "failure")

    @property
    def observations(self) -> tuple[VoiceEvaluationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "observation")

    @property
    def exit_code(self) -> int:
        return 1 if self.hard_failures else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "hard_failure_count": len(self.hard_failures),
            "observation_count": len(self.observations),
            "metrics": dict(self.metrics),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def evaluate_voice_events(
    events: Iterable[Mapping[str, Any]],
) -> VoiceEvaluationResult:
    """Evaluate one JSONL stream without network calls or mutable state."""

    rows = list(events)
    issues: list[VoiceEvaluationIssue] = []

    for flag in voice_quality_flags(rows):
        issues.append(
            VoiceEvaluationIssue(
                code=flag,
                severity=("failure" if flag in _BASELINE_FAILURES else "observation"),
                message=_FLAG_MESSAGES.get(flag, "Structured voice-log quality check failed."),
            )
        )

    for run in _indexed_call_runs(rows):
        patient_requested_follow_on = False
        blocked_indexes: list[int] = []
        confirmed_proposals: dict[str, list[int]] = {}
        for index, event in run:
            if (
                event.get("event_type") == "confirmation.semantic_interpreted"
                and event.get("decision") == "confirmed"
            ):
                proposal_id = str(event.get("proposal_id") or "unknown")
                confirmed_proposals.setdefault(proposal_id, []).append(index)
            if (
                event.get("event_type") == "patient.turn_processed"
                and event.get("workflow_before") == "completed"
            ):
                patient_requested_follow_on = True
            if (
                patient_requested_follow_on
                and event.get("event_type") == "tool.decision"
                and event.get("domain_status") == "rejected"
                and event.get("workflow_before") == "completed"
                and event.get("tool_name") != "interpret_identity_response"
            ):
                blocked_indexes.append(index)
        for proposal_id, indexes in confirmed_proposals.items():
            if len(indexes) < 2:
                continue
            issues.append(
                VoiceEvaluationIssue(
                    code="confirmation_acceptance_loop",
                    severity="failure",
                    message=_FLAG_MESSAGES["confirmation_acceptance_loop"],
                    event_indexes=tuple(indexes),
                    evidence=(f"proposal_id={proposal_id}",),
                )
            )
        blocked = tuple(
            blocked_indexes
        )
        if blocked:
            issues.append(
                VoiceEvaluationIssue(
                    code="follow_on_request_blocked",
                    severity="failure",
                    message=_FLAG_MESSAGES["follow_on_request_blocked"],
                    event_indexes=blocked,
                )
            )
        direct_reschedules = tuple(
            index
            for index, event in run
            if event.get("event_type") == "tool.decision"
            and event.get("tool_name") == "edit_appointment"
            and event.get("domain_status") in {"updated", "verified"}
        )
        if direct_reschedules:
            issues.append(
                VoiceEvaluationIssue(
                    code="reschedule_clinic_approval_bypassed",
                    severity="failure",
                    message=_FLAG_MESSAGES["reschedule_clinic_approval_bypassed"],
                    event_indexes=direct_reschedules,
                )
            )
        redundant_identity = tuple(
            index
            for index, event in run
            if event.get("event_type") == "tool.decision"
            and event.get("tool_name") == "interpret_identity_response"
            and event.get("workflow_before") not in {
                None,
                "awaiting_name_confirmation",
            }
        )
        if redundant_identity:
            issues.append(
                VoiceEvaluationIssue(
                    code="redundant_identity_interpretation_after_unlock",
                    severity="observation",
                    message=_FLAG_MESSAGES[
                        "redundant_identity_interpretation_after_unlock"
                    ],
                    event_indexes=redundant_identity,
                )
            )

    issues.extend(_transcript_issues(rows))
    issues.extend(_availability_grounding_issues(rows))
    issues.extend(_normal_turn_liveness_issues(rows))
    issues.extend(_accepted_follow_up_liveness_issues(rows))
    issues.extend(_application_gate_branch_issues(rows))
    issues = _deduplicate_issues(issues)
    failures = sum(issue.severity == "failure" for issue in issues)
    observations = sum(issue.severity == "observation" for issue in issues)
    if failures:
        status: Literal["pass", "pass_with_observations", "fail"] = "fail"
    elif observations:
        status = "pass_with_observations"
    else:
        status = "pass"

    return VoiceEvaluationResult(
        schema_version="voice-quality-v1",
        status=status,
        issues=tuple(issues),
        metrics=_metrics(rows),
    )


def render_voice_evaluation(result: VoiceEvaluationResult) -> str:
    lines = [
        f"Voice quality gate: {result.status.upper()}",
        (
            f"hard_failures={len(result.hard_failures)} "
            f"observations={len(result.observations)}"
        ),
    ]
    for issue in result.issues:
        indexes = (
            f" events={','.join(str(value) for value in issue.event_indexes)}"
            if issue.event_indexes
            else ""
        )
        lines.append(
            f"- {issue.severity.upper()} {issue.code}:{indexes} {issue.message}"
        )
        for evidence in issue.evidence:
            lines.append(f"  evidence={evidence}")
    lines.append("metrics=" + json.dumps(dict(result.metrics), sort_keys=True))
    return "\n".join(lines)


def _availability_grounding_issues(
    rows: list[Mapping[str, Any]],
) -> list[VoiceEvaluationIssue]:
    """Fail closed when a retrieval-failure claim has no matching tool attempt."""

    issues: list[VoiceEvaluationIssue] = []
    for run in _indexed_call_runs(rows):
        request_index: int | None = None
        search_attempted = False
        for index, event in run:
            event_type = event.get("event_type")
            if event_type == "caller.turn":
                transcript = event.get("transcript")
                request_index = (
                    index
                    if isinstance(transcript, str)
                    and _AVAILABILITY_REQUEST_PATTERN.search(transcript)
                    else None
                )
                search_attempted = False
                continue
            if (
                request_index is not None
                and event_type in {"tool.requested", "tool.decision"}
                and event.get("tool_name") == "search_slots"
            ):
                search_attempted = True
                continue
            if event_type != "assistant.turn" or request_index is None:
                continue
            transcript = event.get("transcript")
            if (
                isinstance(transcript, str)
                and _AVAILABILITY_RETRIEVAL_FAILURE_PATTERN.search(
                    transcript.replace("’", "'")
                )
                and not search_attempted
            ):
                issues.append(
                    VoiceEvaluationIssue(
                        code="availability_failure_without_search",
                        severity="failure",
                        message=_FLAG_MESSAGES[
                            "availability_failure_without_search"
                        ],
                        event_indexes=(request_index, index),
                        evidence=(_compact_evidence(transcript),),
                    )
                )
            request_index = None
            search_attempted = False
    return issues


def _normal_turn_liveness_issues(rows: list[Mapping[str, Any]]) -> list[VoiceEvaluationIssue]:
    """Check the first-progress deadline without mistaking backend latency for silence.

    Resume/acknowledgement and suppressed output are not downstream progress.
    Legacy assistant.turn timestamps may be deferred transcript flushes, so an
    actual recorded assistant turn discharges the sequence invariant without
    pretending its flush time was its audio start time.
    """
    groups: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    current_key = "unscoped"
    for index, event in enumerate(rows):
        identity = event.get("provider_call_id") or event.get("session_id")
        key = str(identity).removeprefix("voice-") if identity else current_key
        if event.get("event_type") == "call.started":
            current_key = key
        groups.setdefault(key, []).append((index, event))

    progress_events = {
        "delegation.created", "backend.response_started", "tool.requested",
        "tool.decision", "tool.completed", "backend.tool_completed",
        "assistant.first_audio", "assistant.authoritative_audio_started",
        "assistant.turn", "control.directive_appended", "control.completed_turn_directive",
        "control.normal_turn_progress", "handoff.requested", "voice.error",
        "voice.failed_mid_call", "control.completed_turn_failed",
    }

    def is_attributable_progress(event: Mapping[str, Any]) -> bool:
        """Raw model output is ambiguous immediately after a barge-in.

        A trailing audio/transcript frame from the preceding response used to
        clear the next turn's liveness check.  Runtime-owned/backend events and a
        completed assistant turn are attributable; raw output markers alone are
        not sufficient evidence that the latest caller request was handled.
        """
        kind = event.get("event_type")
        if kind == "control.normal_turn_progress" and event.get("progress_kind") in {
            "assistant_audio", "assistant_transcript",
        }:
            return False
        return kind in progress_events

    issues: list[VoiceEvaluationIssue] = []
    for run in groups.values():
        pending: dict[str, Any] | None = None
        boundary_index = -1
        attributable_progress_since_boundary = False

        def failure(index: int, event: Mapping[str, Any]) -> None:
            nonlocal pending
            if pending is None:
                return
            started_at = pending["started_at"]
            ended_at = _occurred_at(event)
            elapsed = None
            if started_at is not None and ended_at is not None:
                try:
                    elapsed = (ended_at - started_at).total_seconds()
                except TypeError:
                    pass
            evidence = [
                f"turn_epoch={pending['epoch']}; activity_token={pending['token'] or 'legacy'}",
                f"deadline_seconds={pending['timeout']}; elapsed_seconds={elapsed}",
            ]
            transcript = pending.get("transcript")
            if isinstance(transcript, str):
                evidence.append(_compact_evidence(transcript))
            issues.append(VoiceEvaluationIssue(
                code="normal_active_turn_no_progress", severity="failure",
                message=_FLAG_MESSAGES["normal_active_turn_no_progress"],
                event_indexes=(pending["index"], index), evidence=tuple(evidence),
            ))
            pending = None

        for index, event in run:
            kind = event.get("event_type")
            timestamp = _occurred_at(event)
            is_deferred_output = kind == "assistant.turn" and bool(event.get("transcript"))
            if pending is not None and timestamp is not None and pending["started_at"] is not None:
                try:
                    elapsed = (timestamp - pending["started_at"]).total_seconds()
                except TypeError:
                    elapsed = 0
                if elapsed > pending["timeout"] and not is_deferred_output:
                    failure(index, event)

            if kind == "caller.turn_boundary_detected":
                # A new caller turn replaces an unanswered turn; a deadline that
                # already elapsed above remains a failure.
                pending = None
                boundary_index = index
                attributable_progress_since_boundary = False
            if (
                kind == "patient.turn_observed"
                and event.get("workflow_before") == "active"
                and event.get("workflow_after") == "active"
                and (event.get("transcript") or event.get("transcript_characters", 0))
            ):
                pending = {
                    "index": index, "started_at": timestamp,
                    "epoch": event.get("turn_epoch"), "token": None,
                    "timeout": NORMAL_TURN_PROGRESS_TIMEOUT,
                    "transcript": event.get("transcript"),
                }
                # Work can start between VAD/turn settling and observation.
                if boundary_index >= 0 and any(is_attributable_progress(e) for j, e in run
                       if boundary_index < j < index):
                    pending = None
            if kind == "control.normal_turn_completed" and not event.get("progress_seen") and pending is None:
                pending = {
                    "index": index, "started_at": timestamp,
                    "epoch": event.get("turn_epoch"), "token": event.get("activity_token"),
                    "timeout": NORMAL_TURN_PROGRESS_TIMEOUT, "transcript": None,
                }
            if (
                kind == "control.normal_turn_completed"
                and event.get("progress_seen")
                and attributable_progress_since_boundary
            ):
                pending = None
            if pending is not None:
                if kind == "caller.turn" and pending["epoch"] is None:
                    pending["epoch"] = event.get("turn_epoch")
                if kind == "control.normal_turn_watchdog_started":
                    pending["token"] = event.get("activity_token")
                    pending["epoch"] = event.get("turn_epoch")
                    timeout = event.get("timeout_seconds")
                    if isinstance(timeout, (float, int)) and timeout > 0:
                        pending["timeout"] = timeout
                epoch = event.get("turn_epoch")
                token = event.get("activity_token")
                same_activity = (
                    (epoch is None or pending["epoch"] is None or epoch == pending["epoch"])
                    and (token is None or pending["token"] is None or token == pending["token"])
                )
                if kind == "control.normal_turn_liveness_failed" and same_activity:
                    failure(index, event)
                elif same_activity and (
                    is_attributable_progress(event)
                    or (kind == "workflow.transition" and event.get("current")
                        and event.get("current") != "active")
                ):
                    pending = None
            if is_attributable_progress(event):
                attributable_progress_since_boundary = True
    return issues


def _accepted_follow_up_liveness_issues(
    rows: list[Mapping[str, Any]],
) -> list[VoiceEvaluationIssue]:
    """Require every gate-to-ACTIVE branch to reach a terminally useful edge.

    Instruction acceptance and a first audio frame are deliberately insufficient:
    the production failure that motivated this check recorded both, then stranded
    the provider response without a tool call or completed caller-visible turn.
    """

    useful_progress = {
        "tool.requested",
        "tool.decision",
        "tool.completed",
        "backend.tool_completed",
        "assistant.turn",
        "handoff.requested",
        "control.normal_turn_recovery_appended",
        "voice.error",
        "voice.failed_mid_call",
    }
    issues: list[VoiceEvaluationIssue] = []
    for run in _indexed_call_runs(rows):
        pending: dict[str, Any] | None = None
        for index, event in run:
            kind = event.get("event_type")
            timestamp = _occurred_at(event)
            if pending is not None:
                started_at = pending["started_at"]
                elapsed = None
                if started_at is not None and timestamp is not None:
                    try:
                        elapsed = (timestamp - started_at).total_seconds()
                    except TypeError:
                        pass
                if kind in useful_progress:
                    pending = None
                elif kind == "control.normal_turn_liveness_failed":
                    elapsed_text = "unknown" if elapsed is None else f"{elapsed:.3f}"
                    issues.append(
                        VoiceEvaluationIssue(
                            code="accepted_follow_up_no_progress",
                            severity="failure",
                            message=_FLAG_MESSAGES["accepted_follow_up_no_progress"],
                            event_indexes=(pending["index"], index),
                            evidence=(
                                f"deadline_seconds={NORMAL_TURN_PROGRESS_TIMEOUT}; elapsed_seconds={elapsed_text}",
                                _compact_evidence(str(pending["transcript"])),
                            ),
                        )
                    )
                    pending = None
                elif (
                    elapsed is not None
                    and elapsed > NORMAL_TURN_PROGRESS_TIMEOUT
                ) or kind in {"call.remote_stop", "call.ended"}:
                    elapsed_text = "unknown" if elapsed is None else f"{elapsed:.3f}"
                    issues.append(
                        VoiceEvaluationIssue(
                            code="accepted_follow_up_no_progress",
                            severity="failure",
                            message=_FLAG_MESSAGES["accepted_follow_up_no_progress"],
                            event_indexes=(pending["index"], index),
                            evidence=(
                                f"deadline_seconds={NORMAL_TURN_PROGRESS_TIMEOUT}; elapsed_seconds={elapsed_text}",
                                _compact_evidence(str(pending["transcript"])),
                            ),
                        )
                    )
                    pending = None

            if (
                kind == "patient.turn_processed"
                and event.get("workflow_before")
                in {"awaiting_follow_up_decision", "completed"}
                and event.get("workflow_after") == "active"
                and event.get("directive_kind") == "model_input"
            ):
                pending = {
                    "index": index,
                    "started_at": timestamp,
                    "transcript": event.get("transcript") or "",
                }
    return issues


def _application_gate_branch_issues(
    rows: list[Mapping[str, Any]],
) -> list[VoiceEvaluationIssue]:
    """Exact application replies and normal continuations are disjoint branches."""

    issues: list[VoiceEvaluationIssue] = []
    for run in _indexed_call_runs(rows):
        pending_exact_index: int | None = None
        for index, event in run:
            kind = event.get("event_type")
            if (
                kind == "patient.turn_processed"
                and event.get("directive_kind") == "say_exactly"
            ):
                pending_exact_index = index
                continue
            if pending_exact_index is None:
                continue
            if kind == "control.normal_turn_adopted_after_gate":
                issues.append(
                    VoiceEvaluationIssue(
                        code="application_gate_exact_reply_misclassified",
                        severity="failure",
                        message=_FLAG_MESSAGES[
                            "application_gate_exact_reply_misclassified"
                        ],
                        event_indexes=(pending_exact_index, index),
                        evidence=(
                            f"turn_epoch={event.get('turn_epoch', 'unknown')}",
                        ),
                    )
                )
                pending_exact_index = None
            elif kind in {
                "control.completed_turn_directive",
                "caller.turn_boundary_detected",
                "call.ended",
            }:
                pending_exact_index = None
    return issues


def _transcript_issues(rows: list[Mapping[str, Any]]) -> list[VoiceEvaluationIssue]:
    issues: list[VoiceEvaluationIssue] = []
    for run in _indexed_call_runs(rows):
        issues.extend(_transcript_issues_for_run(run))
    return issues


def _transcript_issues_for_run(
    indexed_rows: list[tuple[int, Mapping[str, Any]]],
) -> list[VoiceEvaluationIssue]:
    issues: list[VoiceEvaluationIssue] = []
    pending_write = False
    verified_write = False
    notification_context_resolved = False
    notification_delivery_pending = False
    application_close_pending = False
    expected_application_close: str | None = None
    identity_unlocked = False
    outbound_call = False
    outbound_identity_continuation_pending = False
    identity_prompt_indexes: list[int] = []
    confirmation_prompt_indexes: dict[str, list[int]] = {}
    exact_questions: dict[str, list[int]] = {}
    expected_exact_digest: str | None = None
    expected_exact_event_index: int | None = None
    expected_exact_at: datetime | None = None
    pending_identity_progress_index: int | None = None
    pending_identity_progress_at: datetime | None = None
    pending_progress_kind = "identity"
    identity_progress_may_prefix_next_turn = False
    pending_caller_close_index: int | None = None
    armed_confirmation_epoch: int | None = None
    confirmation_is_armed = False
    confirmation_delivery_protocol_seen = False
    pending_application_audio_index: int | None = None

    for index, event in indexed_rows:
        event_type = event.get("event_type")
        if event_type == "assistant.application_audio_render_started":
            pending_application_audio_index = index
        if event_type in {
            "assistant.application_audio_completed",
            "assistant.application_audio_interrupted",
            "assistant.application_audio_stale",
        }:
            pending_application_audio_index = None
        if event_type in {
            "assistant.application_audio_render_failed",
            "assistant.application_audio_timeout",
        }:
            issues.append(
                VoiceEvaluationIssue(
                    code="application_audio_delivery_failed",
                    severity="failure",
                    message=_FLAG_MESSAGES["application_audio_delivery_failed"],
                    event_indexes=(index,),
                    evidence=(str(event_type),),
                )
            )
            pending_application_audio_index = None
        if event_type == "call.ended" and pending_application_audio_index is not None:
            issues.append(
                VoiceEvaluationIssue(
                    code="application_audio_delivery_incomplete",
                    severity="failure",
                    message=_FLAG_MESSAGES["application_audio_delivery_incomplete"],
                    event_indexes=(pending_application_audio_index, index),
                )
            )
            pending_application_audio_index = None
        if (
            event_type == "confirmation.mutation_executed"
            and event.get("domain_status") == "conflict"
        ):
            proposal_id = str(event.get("proposal_id") or "unknown")
            tool_name = str(event.get("tool_name") or "appointment mutation")
            issues.append(
                VoiceEvaluationIssue(
                    code="confirmed_mutation_conflict",
                    severity="failure",
                    message=_FLAG_MESSAGES["confirmed_mutation_conflict"],
                    event_indexes=(index,),
                    evidence=(
                        f"proposal_id={proposal_id}; tool={tool_name}; domain_status=conflict",
                    ),
                )
            )
        if (
            event_type == "patient.turn_processed"
            and event.get("workflow_before") == "awaiting_follow_up_decision"
            and event.get("workflow_after") == "completed"
            and isinstance(event.get("transcript"), str)
            and _SUBSTANTIVE_FOLLOW_UP_REQUEST_PATTERN.search(
                str(event["transcript"])
            )
        ):
            issues.append(
                VoiceEvaluationIssue(
                    code="substantive_follow_up_request_discarded",
                    severity="failure",
                    message=_FLAG_MESSAGES[
                        "substantive_follow_up_request_discarded"
                    ],
                    event_indexes=(index,),
                    evidence=(_compact_evidence(str(event["transcript"])),),
                )
            )
        if event_type == "control.completed_turn_delivery_failed":
            issues.append(
                VoiceEvaluationIssue(
                    code="authoritative_directive_delivery_failed",
                    severity="failure",
                    message=_FLAG_MESSAGES["authoritative_directive_delivery_failed"],
                    event_indexes=(index,),
                    evidence=(str(event.get("error_class") or "unknown"),),
                )
            )
        if event_type == "control.application_gate_progress_appended":
            pending_identity_progress_index = index
            pending_identity_progress_at = _occurred_at(event)
            pending_progress_kind = str(event.get("gate_kind") or "identity")
        if (
            event_type == "assistant.gate_progress_audio_started"
            and pending_identity_progress_index is not None
        ):
            identity_progress_may_prefix_next_turn = True
            started_at = _occurred_at(event)
            if pending_identity_progress_at is not None and started_at is not None:
                latency_ms = int(
                    (started_at - pending_identity_progress_at).total_seconds() * 1000
                )
                if latency_ms > 2500:
                    progress_code = (
                        "confirmation_progress_update_late"
                        if pending_progress_kind == "confirmation"
                        else "identity_progress_update_late"
                    )
                    issues.append(
                        VoiceEvaluationIssue(
                            code=progress_code,
                            severity="failure",
                            message=_FLAG_MESSAGES[progress_code],
                            event_indexes=(pending_identity_progress_index, index),
                            evidence=(f"latency_ms={latency_ms}",),
                        )
                    )
            pending_identity_progress_index = None
            pending_identity_progress_at = None
        if event_type == "confirmation.proposal_prepared_for_delivery":
            confirmation_delivery_protocol_seen = True
            confirmation_is_armed = False
            armed_confirmation_epoch = None
        if event_type == "confirmation.proposal_armed":
            confirmation_is_armed = True
            value = event.get("after_turn_epoch")
            armed_confirmation_epoch = int(value) if isinstance(value, int) else None
        if (
            event_type == "confirmation.proposal_directive_appended"
            and isinstance(event.get("directive_digest"), str)
        ):
            expected_exact_digest = str(event["directive_digest"])
            expected_exact_event_index = index
            expected_exact_at = _occurred_at(event)
        if event_type == "confirmation.pre_delivery_continuation":
            confirmation_is_armed = False
            armed_confirmation_epoch = None
            expected_exact_digest = None
            expected_exact_event_index = None
            expected_exact_at = None
        if event_type == "confirmation.semantic_interpreted":
            value = event.get("turn_epoch")
            confirmation_epoch = int(value) if isinstance(value, int) else None
            if confirmation_delivery_protocol_seen and (
                not confirmation_is_armed or (
                armed_confirmation_epoch is not None
                and confirmation_epoch is not None
                and confirmation_epoch <= armed_confirmation_epoch
                )
            ):
                issues.append(
                    VoiceEvaluationIssue(
                        code="confirmation_before_proposal_armed",
                        severity="failure",
                        message=_FLAG_MESSAGES["confirmation_before_proposal_armed"],
                        event_indexes=(index,),
                        evidence=(
                            f"confirmation_turn_epoch={confirmation_epoch}; "
                            f"armed_after_epoch={armed_confirmation_epoch}",
                        ),
                    )
                )
        if event_type == "call.started":
            outbound_call = event.get("direction") == "outbound"
        if event_type == "workflow.transition":
            current = event.get("current")
            if current not in {"awaiting_intent", "awaiting_name_confirmation"}:
                identity_unlocked = True
            if (
                outbound_call
                and event.get("reason") == "name_confirmation_accepted"
            ):
                outbound_identity_continuation_pending = True
        if (
            event_type == "patient.turn_processed"
            and event.get("directive_kind") == "say_exactly"
            and isinstance(event.get("directive_digest"), str)
        ):
            expected_exact_digest = str(event["directive_digest"])
            expected_exact_event_index = index
            expected_exact_at = _occurred_at(event)
        if event_type == "tool.requested":
            outbound_identity_continuation_pending = False
        if (
            event_type in {"patient.turn_processed", "patient.turn_observed"}
            and event.get("workflow_before") == "active"
        ):
            outbound_identity_continuation_pending = False
            caller_transcript = event.get("transcript")
            if isinstance(caller_transcript, str) and any(
                pattern.search(caller_transcript.replace("’", "'"))
                for pattern in _CALLER_CLOSE_PATTERNS
            ):
                pending_caller_close_index = index
        if event_type == "tool.decision" and event.get("tool_name") in _WRITE_TOOLS:
            if event.get("domain_status") == "confirmation_required":
                pending_write = True
                verified_write = False
            elif event.get("domain_status") in {"verified", "proposed"}:
                pending_write = False
                verified_write = True
        if event_type == "notification.context_resolved":
            notification_context_resolved = True
            notification_delivery_pending = True
        if (
            event_type == "patient.turn_processed"
            and event.get("workflow_before") == "awaiting_follow_up_decision"
            and event.get("workflow_after") == "completed"
        ):
            application_close_pending = True
            expected_application_close = (
                "Thank you. Take care."
                if event.get("directive_kind") == "say_exactly"
                else "Understood. I won't take any further scheduling action."
            )
        if event_type != "assistant.turn":
            continue

        transcript = event.get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            continue

        exact_reply_at = _occurred_at(event)
        exact_reply_latency_seconds = (
            (exact_reply_at - expected_exact_at).total_seconds()
            if exact_reply_at is not None and expected_exact_at is not None
            else None
        )
        if (
            expected_exact_digest is not None
            and event.get("reason") == "call_stopped"
            and exact_reply_latency_seconds is not None
            and exact_reply_latency_seconds > 15
        ):
            evidence: tuple[str, ...] = (
                "Exact reply transcript was deferred until call termination; "
                f"latency_seconds={exact_reply_latency_seconds:.1f}.",
            )
            issues.append(
                VoiceEvaluationIssue(
                    code="application_reply_playback_unverified",
                    severity="failure",
                    message=_FLAG_MESSAGES["application_reply_playback_unverified"],
                    event_indexes=tuple(
                        value
                        for value in (expected_exact_event_index, index)
                        if value is not None
                    ),
                    evidence=evidence,
                )
            )

        if pending_caller_close_index is not None:
            if not any(
                pattern.search(transcript)
                for pattern in _ASSISTANT_TERMINAL_CLOSE_PATTERNS
            ):
                issues.append(
                    VoiceEvaluationIssue(
                        code="caller_close_not_honored",
                        severity="failure",
                        message=_FLAG_MESSAGES["caller_close_not_honored"],
                        event_indexes=(pending_caller_close_index, index),
                        evidence=(_compact_evidence(transcript),),
                    )
                )
            pending_caller_close_index = None

        if expected_exact_digest is not None:
            exact_candidate = transcript
            if identity_progress_may_prefix_next_turn:
                exact_candidate = _APPLICATION_PROGRESS_PREFIX.sub("", transcript, count=1)
                identity_progress_may_prefix_next_turn = False
                if not exact_candidate.strip():
                    continue
            actual_digest = spoken_contract_digest(exact_candidate)
            if actual_digest != expected_exact_digest:
                issues.append(
                    VoiceEvaluationIssue(
                        code="exact_application_reply_not_honored",
                        severity="failure",
                        message=_FLAG_MESSAGES["exact_application_reply_not_honored"],
                        event_indexes=tuple(
                            value
                            for value in (expected_exact_event_index, index)
                            if value is not None
                        ),
                        evidence=(_compact_evidence(exact_candidate),),
                    )
                )
            expected_exact_digest = None
            expected_exact_event_index = None
            expected_exact_at = None

        if identity_unlocked and any(
            pattern.search(transcript) for pattern in _IDENTITY_PENDING_PATTERNS
        ):
            issues.append(
                VoiceEvaluationIssue(
                    code="identity_state_contradiction",
                    severity="failure",
                    message=_FLAG_MESSAGES["identity_state_contradiction"],
                    event_indexes=(index,),
                    evidence=(_compact_evidence(transcript),),
                )
            )

        if (
            outbound_identity_continuation_pending
            and re.search(r"\bhow can i help\b", transcript, re.I)
        ):
            issues.append(
                VoiceEvaluationIssue(
                    code="outbound_post_identity_purpose_reset",
                    severity="failure",
                    message=_FLAG_MESSAGES["outbound_post_identity_purpose_reset"],
                    event_indexes=(index,),
                    evidence=(_compact_evidence(transcript),),
                )
            )

        if notification_delivery_pending:
            if re.search(r"\bhow can i help\b", transcript, re.I):
                issues.append(
                    VoiceEvaluationIssue(
                        code="notification_call_purpose_reset",
                        severity="failure",
                        message=_FLAG_MESSAGES["notification_call_purpose_reset"],
                        event_indexes=(index,),
                        evidence=("How can I help",),
                    )
                )
            notification_delivery_pending = False

        if application_close_pending:
            normalized_close = _normalize_text(transcript)
            normalized_expected_close = _normalize_text(
                expected_application_close or ""
            )
            if normalized_close != normalized_expected_close:
                issues.append(
                    VoiceEvaluationIssue(
                        code="application_reply_mixed_with_autonomous_output",
                        severity="observation",
                        message=_FLAG_MESSAGES[
                            "application_reply_mixed_with_autonomous_output"
                        ],
                        event_indexes=(index,),
                        evidence=(_compact_evidence(transcript),),
                    )
                )
            application_close_pending = False
            expected_application_close = None

        if not identity_unlocked and any(
            pattern.search(transcript) for pattern in _IDENTITY_PROMPT_PATTERNS
        ):
            identity_prompt_indexes.append(index)

        if _CONFIRMATION_PROMPT_PATTERN.search(transcript):
            kind = _confirmation_kind(transcript)
            confirmation_prompt_indexes.setdefault(kind, []).append(index)

        premature = next(
            (
                match.group(0)
                for pattern in _PREMATURE_COMMITMENT_PATTERNS
                if (match := pattern.search(transcript)) is not None
            ),
            None,
        )
        if premature and not verified_write:
            issues.append(
                VoiceEvaluationIssue(
                    code="premature_commitment_wording",
                    severity="observation",
                    message=(
                        "Assistant implied that a write was underway before the "
                        "write had been verified; proposal language should be used."
                    ),
                    event_indexes=(index,),
                    evidence=(_compact_evidence(premature),),
                )
            )

        success_claim = next(
            (
                match.group(0)
                for pattern in _SUCCESS_CLAIM_PATTERNS
                if (match := pattern.search(transcript)) is not None
            ),
            None,
        )
        if success_claim and (
            pending_write or not (verified_write or notification_context_resolved)
        ):
            issues.append(
                VoiceEvaluationIssue(
                    code="unverified_success_claim",
                    severity="failure",
                    message=(
                        "Assistant claimed a scheduling mutation succeeded before "
                        "a verified write result was recorded."
                    ),
                    event_indexes=(index,),
                    evidence=(_compact_evidence(success_claim),),
                )
            )

        if "\ufffd" in transcript:
            issues.append(
                VoiceEvaluationIssue(
                    code="transcript_encoding_corruption",
                    severity="observation",
                    message="Assistant transcript contains replacement characters.",
                    event_indexes=(index,),
                )
            )

        repeated = _repeated_ngram(transcript)
        if repeated:
            issues.append(
                VoiceEvaluationIssue(
                    code="repeated_assistant_phrase",
                    severity="observation",
                    message="Assistant repeated a long phrase inside one spoken turn.",
                    event_indexes=(index,),
                    evidence=(repeated,),
                )
            )

        for question in _questions(transcript):
            exact_questions.setdefault(question, []).append(index)

        if (
            event.get("reason") == "call_stopped"
            and not re.search(r"[.!?]['\"]?\s*$", transcript.strip())
        ):
            issues.append(
                VoiceEvaluationIssue(
                    code="assistant_utterance_truncated",
                    severity="observation",
                    message="Final assistant transcript ends mid-utterance at call stop.",
                    event_indexes=(index,),
                    evidence=(_compact_evidence(transcript[-100:]),),
                )
            )

    if pending_identity_progress_index is not None:
        progress_code = (
            "confirmation_progress_update_missing"
            if pending_progress_kind == "confirmation"
            else "identity_progress_update_missing"
        )
        issues.append(
            VoiceEvaluationIssue(
                code=progress_code,
                severity="failure",
                message=_FLAG_MESSAGES[progress_code],
                event_indexes=(pending_identity_progress_index,),
            )
        )

    if pending_caller_close_index is not None:
        issues.append(
            VoiceEvaluationIssue(
                code="caller_close_not_honored",
                severity="failure",
                message=_FLAG_MESSAGES["caller_close_not_honored"],
                event_indexes=(pending_caller_close_index,),
                evidence=("No closing assistant turn was recorded.",),
            )
        )

    if len(identity_prompt_indexes) >= 2:
        issues.append(
            VoiceEvaluationIssue(
                code="repeated_identity_prompt",
                severity="failure",
                message="Assistant asked for identity confirmation more than once before unlock.",
                event_indexes=tuple(identity_prompt_indexes),
            )
        )

    for kind, indexes in confirmation_prompt_indexes.items():
        if len(indexes) >= 3:
            issues.append(
                VoiceEvaluationIssue(
                    code="repeated_confirmation_prompt",
                    severity="failure",
                    message=(
                        f"Assistant repeatedly asked for {kind} confirmation without "
                        "completing the write."
                    ),
                    event_indexes=tuple(indexes),
                )
            )
        elif len(indexes) == 2:
            issues.append(
                VoiceEvaluationIssue(
                    code="repeated_confirmation_prompt",
                    severity="observation",
                    message=(
                        f"Assistant asked for {kind} confirmation twice; this may be "
                        "necessary recovery, but should be reviewed for conversational repetition."
                    ),
                    event_indexes=tuple(indexes),
                )
            )

    duplicate_question_indexes = sorted(
        {
            index
            for indexes in exact_questions.values()
            if len(set(indexes)) >= 2
            for index in indexes
        }
    )
    if duplicate_question_indexes:
        duplicate_questions = tuple(
            question
            for question, indexes in exact_questions.items()
            if len(set(indexes)) >= 2
        )
        issues.append(
            VoiceEvaluationIssue(
                code="repeated_assistant_question",
                severity="observation",
                message="Assistant repeated the same substantive question across turns.",
                event_indexes=tuple(duplicate_question_indexes),
                evidence=duplicate_questions[:3],
            )
        )

    return issues


def _occurred_at(event: Mapping[str, Any]) -> datetime | None:
    value = event.get("occurred_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _questions(transcript: str) -> tuple[str, ...]:
    values: list[str] = []
    for candidate in re.findall(r"(?:^|[.!])\s*([^?]{12,}\?)", transcript):
        normalized = _normalize_text(candidate[:-1])
        if len(normalized.split()) >= 5:
            values.append(normalized)
    return tuple(values)


def _confirmation_kind(transcript: str) -> str:
    normalized = _normalize_text(transcript)
    if re.search(r"\bcancel(?:lation|ation|led|ed)?\b", normalized):
        return "cancellation"
    if re.search(r"\breschedul(?:e|ed|ing)\b", normalized):
        return "reschedule"
    if re.search(r"\b(?:book|booking|appointment request|submit that request)\b", normalized):
        return "booking"
    return "scheduling"


def _normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _repeated_ngram(transcript: str, *, size: int = 10) -> str | None:
    words = re.findall(r"[a-z0-9]+", transcript.casefold())
    positions: dict[tuple[str, ...], int] = {}
    for index in range(len(words) - size + 1):
        ngram = tuple(words[index : index + size])
        previous = positions.get(ngram)
        if previous is not None and index - previous >= size:
            return " ".join(ngram)
        positions.setdefault(ngram, index)
    return None


def _indexed_call_runs(
    rows: list[Mapping[str, Any]],
) -> list[list[tuple[int, Mapping[str, Any]]]]:
    """Split calls without allowing one call's text to contaminate another."""

    runs: list[list[tuple[int, Mapping[str, Any]]]] = []
    prelude: list[tuple[int, Mapping[str, Any]]] = []
    current: list[tuple[int, Mapping[str, Any]]] | None = None
    for index, event in enumerate(rows):
        event_type = event.get("event_type")
        if event_type == "call.started":
            if current:
                runs.append(current)
            current = [*prelude, (index, event)]
            prelude = []
            continue
        if current is not None:
            current.append((index, event))
            if event_type == "call.ended":
                runs.append(current)
                current = None
            continue
        prelude.append((index, event))
    if current:
        runs.append(current)
    if prelude:
        runs.append(prelude)
    return runs


def _compact_evidence(value: str, *, limit: int = 180) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _deduplicate_issues(
    issues: list[VoiceEvaluationIssue],
) -> list[VoiceEvaluationIssue]:
    grouped: dict[tuple[str, Severity], VoiceEvaluationIssue] = {}
    order: list[tuple[str, Severity]] = []
    for issue in issues:
        key = (issue.code, issue.severity)
        if key not in grouped:
            grouped[key] = issue
            order.append(key)
            continue
        previous = grouped[key]
        grouped[key] = VoiceEvaluationIssue(
            code=previous.code,
            severity=previous.severity,
            message=previous.message,
            event_indexes=tuple(
                dict.fromkeys((*previous.event_indexes, *issue.event_indexes))
            ),
            evidence=tuple(dict.fromkeys((*previous.evidence, *issue.evidence))),
        )
    return [grouped[key] for key in order]


def _metrics(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    caller_turns = sum(event.get("event_type") == "caller.turn" for event in rows)
    return {
        "calls": sum(event.get("event_type") == "call.started" for event in rows),
        "assistant_turns": sum(
            event.get("event_type") == "assistant.turn" for event in rows
        ),
        "patient_turns": caller_turns
        or sum(event.get("event_type") == "patient.turn_processed" for event in rows),
        "tool_requests": sum(
            event.get("event_type") == "tool.requested" for event in rows
        ),
        "write_requests": sum(
            event.get("event_type") == "tool.requested"
            and event.get("tool_name") in _WRITE_TOOLS
            for event in rows
        ),
        "verified_writes": sum(
            event.get("event_type") == "tool.decision"
            and event.get("tool_name") in _WRITE_TOOLS
            and event.get("domain_status") in {"verified", "proposed"}
            for event in rows
        ),
    }


__all__ = [
    "VoiceEvaluationIssue",
    "VoiceEvaluationResult",
    "evaluate_voice_events",
    "render_voice_evaluation",
]
