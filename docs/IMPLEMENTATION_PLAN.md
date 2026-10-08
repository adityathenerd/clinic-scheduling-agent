# Implementation Plan

Status: Implemented; retained as the build-sequence decision record
Last updated: 2026-10-08

Current verification: the planned Python-only vertical slice, Twilio/OpenAI voice
path, guarded scheduler, clinic portal, and evaluation loop are implemented. The
repository passed 401 tests on 2026-10-08. The phased plan below is preserved to
show sequencing and tradeoffs rather than rewritten as if it were the final system.

## Governing constraint

All application code, agent orchestration, call mechanics, adapters, tools, evaluation, and tests use Python 3.11+. Keep the first vertical slice in one process unless a measured failure requires another service boundary. On the current Windows host, use `py -3.11`.

## Committed architecture

```text
Twilio -> GPT-Live (`gpt-live-1`) -> Responses delegation
                                      |
                                      v
                          GPT-6 Sol (`gpt-6-sol`, low)
                                      |
                                      v
                         Python control plane + guarded tools
                                      |
                                      v
                            fake/real scheduling backend
```

GPT-Live owns spoken interaction. Sol proposes reasoning and tool actions. The Python application alone owns permissions, workflow state, confirmation binding, tool execution, stale-result suppression, reconciliation, and truth claims. `gpt-realtime-2.1` implements the same `VoiceEngine` contract if the primary path fails its bounded spike.

## Build order

### Phase 0 — Freeze the synthetic operating policy

Time box: 30 minutes.

Decide and version:

- one clinic and one appointment type;
- identity factors and retry limit;
- caller/proxy authority policy;
- administrative prerequisites;
- urgent-symptom words and approved handoff language;
- front-desk handoff destination; and
- cancellation follow-up payload and simulated front-desk response.

Exit gate: every hard decision needed by a tool guard exists in a policy fixture rather than in a model prompt.

### Phase 1 — Make the safe path executable without voice

Time box: 90 minutes.

Implement:

- typed `SessionState` and transition events;
- explicit transition reducer;
- proposal/version and correction-epoch invalidation;
- confirmation token bound to a normalized proposal digest;
- minimal read/write tool contracts for `create_appointment`, `edit_appointment`, `delete_appointment`, and `escalate_to_front_desk`;
- versioned in-memory scheduler with idempotency and fault injection; and
- deterministic text conversation runner.

Exit gate: a simulated caller can book, reschedule, and cancel an appointment; every final state is read back; cancellation creates a follow-up handoff or explicit pending state; and direct attempts to bypass identity or confirmation fail at the application boundary.

### Phase 2 — Establish the evaluation source of truth

Time box: 90 minutes.

Implement the initial seeded scenarios and JSONL trace assertions. Include happy booking, happy reschedule, happy cancellation with follow-up, ambiguous date, changed selection, slot race, unverified caller, urgent symptom, timeout after commit, and failed cancellation-follow-up handoff.

Create the deliberate baseline failure: a stale generic confirmation permits the wrong proposal. Capture a structured failure artifact, replace the boolean with a proposal-bound token, and rerun the entire suite.

Exit gate: the baseline fails for the intended reason; the reinforcement fixes it; all previously passing scenarios remain passing; zero critical gates fail.

### Phase 3 — Run the GPT-Live delegation spike

Hard time box: 60 minutes.

Use the existing Python call actor, supervisor, and provider-neutral interfaces.

Spend no more than the first 10 minutes on the transport gate:

1. Inspect the existing Twilio account status, Active Numbers, and verified callers.
2. Make one minimal `<Stream>` attempt using already provisioned numbers.
3. If the account is a restricted trial or streaming is blocked, switch the demo transport to synthetic or browser audio while preserving the Twilio adapter contract.
4. Do not purchase a number, upgrade an account, or incur spend without explicit user authorization.

Then connect GPT-Live to one safe read-only stub and the guarded control-plane interface.

The primary path passes only if the trace proves all of the following:

1. GPT-Live connects and creates a delegated Responses job using `gpt-6-sol` with low reasoning effort.
2. `delegation_id`, backend response ID, correction epoch, proposal/version, and confirmation reference remain correlated.
3. A correction while Sol is working cancels the job when supported and always suppresses stale output locally.
4. Barge-in stops playback without accidentally authorizing or cancelling unrelated backend work.
5. Tool arguments reach the Python guard; neither model can call the scheduler around it.
6. GPT-Live does not say availability is current or a booking succeeded before verified application state exists.
7. Duplicate and reordered events are idempotent.
8. The trace contains stage-level latency and model configuration.

If a Live-model gate remains unproven at 60 minutes, stop the spike and switch the `VoiceEngine` implementation to `gpt-realtime-2.1`. Do not use Realtime as a workaround for a blocked Twilio `<Stream>` verb; transport and model fallbacks are independent. Do not change the control plane, scheduler tools, or eval criteria to accommodate either fallback.

### Phase 4 — Add one inbound Twilio demonstration

Time box: 45 minutes.

If the transport gate passed, attach the chosen voice adapter to Twilio Media Streams using the existing call-mechanics boundary. Otherwise demonstrate the identical `VoiceEngine` contract using browser or synthetic audio and retain Twilio as a documented adapter. Keep media framing, reconnects, and playback marks outside the scheduling domain.

Exit gate: one inbound call reaches a safe terminal outcome and produces the same normalized trace shape as the text runner.

### Phase 5 — Package the submission

Time box: 45 minutes.

- Make the two README commands truthful and reproducible.
- Generate measured before/after reports.
- Write the one-page design note from the decision log.
- Record the Loom: conversation, failing eval, failure artifact, applied reinforcement, passing rerun, and regression result.
- State where AI helped and which safety/design judgments were explicitly human-owned.

## Parallel boundaries

Work may proceed concurrently only where ownership is unambiguous:

| Lane | May change | Must not change |
|---|---|---|
| Control plane | Policy schemas, reducer, guards, tool authority | Media/provider behavior |
| Call mechanics | Session lifecycle, transport, voice events, timing | Clinic policy or eval scoring |
| Evaluation | Scenarios, assertions, reporting, improvement artifacts | Runtime permission rules |
| Voice adapter | Provider protocol mapping and model configuration | Scheduling state or tool authorization |

All lanes share the event schema and Python contracts. A provider-specific event is normalized at the adapter boundary before the control plane or harness consumes it.

## Stop rules

- Do not add LangGraph unless the explicit reducer becomes measurably harder to understand or test.
- Do not add a second application language or runtime.
- Do not benchmark Terra during the weekend slice.
- Do not compare Luna until Sol-low passes the stable safety suite.
- Do not let a model judge decide facts available from state or traces.
- Do not extend the live voice spike beyond its time box by stealing time from the closed improvement loop.

## Repository-wide definition of done

- `py -3.11 -m clinic_agent` runs the interaction path documented in the README.
- `py -3.11 -m evals.runner --profile reinforced` runs the complete improvement loop.
- The deterministic suite passes with zero critical failures.
- The before/after report shows the targeted failure fixed and no prior-pass regression.
- Voice integration either passes every spike gate or uses the documented fallback.
- Every protected action can be traced from patient intent through proposal, confirmation, guard, mutation, and final verification.
