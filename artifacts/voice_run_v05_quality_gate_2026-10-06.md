# V05 Voice Run Quality Gate

Date: 2026-10-06  
Call: `CAe0762b627a6eda0d7695a2a6de36f5da`  
Evidence: `data/voice-v05.jsonl`, `data/voice-v05.db`  
Verdict: **PASS WITH OBSERVATIONS**

This is a live outbound rescheduling run followed by public clinic FAQ questions.
The run passed every safety and scheduling-integrity gate. The earlier summary
logger reported `anomalies=0` and `quality_flags=none`; the stricter deterministic
transcript evaluator added in the subsequent harness pass reports
`PASS_WITH_OBSERVATIONS`, with zero hard failures and two observations.

## Gate results

| Gate | Evidence | Result |
|---|---|---|
| Direction-aware opening | Outbound purpose stated; no generic inbound opening | PASS |
| Privacy before disclosure | Appointment was disclosed only after identity moved to `active` | PASS |
| Identity liveness | One semantic affirmation and one `name_confirmation_accepted` transition | PASS |
| Complete appointment context | Type, provider, date, time, location, duration, arrival guidance, and unverified prerequisite were stated | PASS |
| Fresh availability | Canonical search returned four live Monday/Tuesday slots | PASS |
| Exact proposal | Old and replacement appointment details were read before mutation | PASS |
| Explicit confirmation | One proposal-bound semantic decision: `confirmed`, `authorized_for_turn=true` | PASS |
| No confirmation loop | Zero `patient_correction_or_non_confirmation` transitions and no repeated proposal | PASS |
| Guarded mutation | First write returned `confirmation_required`; second returned `verified` | PASS |
| Read-after-write truth | `confirmation_pending -> completed` occurred only after verified edit | PASS |
| Authoritative state | Appointment version 2 is October 13 at 3:00 PM; new slot booked; old slot available | PASS |
| Idempotent audit trail | Exactly one `confirmation.recorded` and one `appointment.updated` | PASS |
| Parking FAQ | Answer came through the clinic FAQ tool | PASS |
| Medicine boundary | No prescription was promised; caller was routed to clinician/front desk for determination and documentation | PASS |
| Transport integrity | Zero voice failures, media gaps, unknown outcomes, or unknown provider events | PASS |

## Transcript observations

1. The call was materially smoother than V04. Identity was accepted once, the
   availability options were correct, one confirmation completed the reschedule,
   and the verified result was read back accurately.
2. Before confirmation, Mira said, “I'm putting that change through,” and then
   asked for confirmation. No success was claimed and no write occurred, but
   “preparing that change” would distinguish proposal from submission more cleanly.
3. The final verified read-back was complete but included both “submitting” and
   “It's set.” A tighter response could lead directly with the verified outcome.
4. The transcript evaluator counted two scheduling-confirmation questions. This
   did not become a loop—the second question was the application-owned exact
   proposal and the next answer completed one verified write—but it remains a
   conversational repetition observation.

Machine gate command and result:

```text
py -3.11 -m clinic_agent --evaluate-voice-log --voice-log data/voice-v05.jsonl
# exit 0: PASS_WITH_OBSERVATIONS; 0 hard failures; 2 observations
```

## Observability limitation

The structured voice log ends in workflow state `completed`, while the dashboard's
persisted session summary still shows `active`. Appointment truth and the audit log
are correct, so this did not affect the patient outcome, but the operational session
projection is stale and should not be used as the sole completion signal.

The current log also cannot measure response-onset latency for every turn. It records
tool and completed transcript timing, but not a per-turn first-audio marker. Audio
quality, prosody, and perceived latency therefore still require listening review or
additional instrumentation.

## Final decision

Accept this run as the first live passing rescheduling regression for the fixed
confirmation loop. Preserve V04 as the failing baseline and V05 as the passing
after-run. Track the pre-confirmation wording and stale session projection as
non-blocking follow-up work; neither invalidates the verified appointment outcome.
