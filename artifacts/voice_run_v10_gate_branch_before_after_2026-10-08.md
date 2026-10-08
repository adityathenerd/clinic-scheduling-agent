# V10 Exact-Gate Branch — Before/After Live Regression

Date: 2026-10-08  
Source: `data/voice-v10.jsonl`  
Fix commit: `5ee215c` (`Separate exact gate replies from normal turn liveness`)

## Outcome

Two consecutive outbound calls preserve a real harness-detected defect and its
post-fix regression:

| Run | Call | Started (IST) | Current harness result | Material evidence |
|---|---|---:|---:|---|
| Before | `CA68a2a0030819bb606a2061ab54ecd2bf` | 16:55:07 | **FAIL** — 3 hard failures | Exact application reply was adopted as normal conversation; final application audio was not verified; workflow remained incomplete at hangup |
| After | `CA76484e894f7609c0bc6809808a96852c` | 16:59:19 | **PASS** — 0 failures, 0 observations | Identity completed once; reschedule became a verified held-for-review request; a post-task pharmacy FAQ was answered; the call closed cleanly |

The next clinic-confirmation call,
`CA1dce3d3ec9b5bf8d646417a9174f83c5`, also passes with zero failures and zero
observations. It verifies that the operator-approved reschedule was announced
through the outbound notification path.

## What broke

After an application-owned gate classified the caller's identity response, the
workflow legitimately moved to `ACTIVE`. The actor used that destination state
as if it proved the turn belonged to normal model conversation. It therefore
adopted the same gate turn into the normal liveness branch even though the
control plane had returned a `say_exactly:` reply. Two mutually exclusive owners
then acted on one transition, producing stalled/incomplete exact speech.

The evaluator rejected the call with:

- `application_gate_exact_reply_misclassified`;
- `application_audio_delivery_incomplete`; and
- `workflow_incomplete_at_hangup`.

## Structured reinforcement

Commit `5ee215c` changed the branch predicate from state-only to state plus
directive ownership. A gate may end in `ACTIVE`, but a `say_exactly:` directive
remains application-owned and cannot create a normal-turn watchdog. Only a
model-owned continuation, or an accepted request without an exact reply, enters
normal conversation.

The change also added a deterministic evaluator gate and regression tests. This
is the improvement-loop pattern used throughout the repository:

1. preserve the failed trace;
2. encode the failure as a named machine-detectable invariant;
3. fix the smallest owning boundary;
4. rerun the live journey; and
5. rerun all repository and evaluator regressions.

## Reproduction

The public CLI evaluates the newest call in a log. The per-call results above
were produced by segmenting `data/voice-v10.jsonl` with
`clinic_agent.runtime.voice_log._call_runs` and applying
`clinic_agent.runtime.transcript_evaluator.evaluate_voice_events` independently
to each call. The source log is intentionally Git-ignored because voice logs may
contain phone numbers or transcript data; this redacted report is the durable
submission evidence.

Current results:

```text
CA68a2a0030819bb606a2061ab54ecd2bf
FAIL: 3 hard failures; 0 observations

CA76484e894f7609c0bc6809808a96852c
PASS: 0 hard failures; 0 observations

CA1dce3d3ec9b5bf8d646417a9174f83c5
PASS: 0 hard failures; 0 observations
```

