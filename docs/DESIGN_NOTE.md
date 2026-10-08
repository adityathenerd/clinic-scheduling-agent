# Design Note — Clinic Scheduling Agent

The governing design choice is that **models may propose actions, but deterministic application code alone authorizes them**. GPT-Live owns natural spoken interaction, while application-rendered audio owns consequential protocol messages with a Twilio playback acknowledgement. GPT-6 Sol proposes bounded reasoning and typed tool calls, and a Python control plane owns identity, workflow state, confirmation, permissions, mutations, stale-work suppression, and final claims. The scheduling backend—not the transcript or model—is the source of truth.

## Key choices and why

I separated offline evaluation, live protection, and reviewed post-run improvement because learning after a call cannot undo patient harm. Runtime safety therefore lives in an explicit state machine and narrow tools. Protected reads and writes remain locked until caller authority is established. Every write also requires fresh authoritative state, an exact active proposal, explicit consent bound to that proposal, an idempotency key, and read-after-write verification. Corrections advance the workflow epoch and invalidate older consent and delegated work.

The mutation surface is intentionally small: create, reschedule, cancel, and escalate. Patient-side create/reschedule operations produce a held proposal pending clinic approval; they are never described as booked. Cancellation preserves the audit record and creates a front-desk follow-up. “No change” and human handoff are valid outcomes when the system cannot proceed safely.

SQLite owns authoritative clinic state; TinyDB/JSONL owns observational conversation and runtime events. They join only through an opaque session identifier, so a transcript cannot become a second appointment authority.

Exact workflow speech is also treated as a side effect requiring verification. The application renders trusted wording to PCMU/8 kHz, suppresses autonomous Live output during playback, and advances delivery only on Twilio’s final mark. GPT-Live receives the spoken turn as quiet context and resumes when the caller next speaks. Render failure, missing playback acknowledgement, barge-in, and late completion each have explicit state and tests. This boundary was introduced after a real call showed that a Live instruction acknowledgement proved context acceptance but not that the caller heard the response.

## Improvement loop and result

The harness evaluates conversation, structured tool/state trace, and final scheduler state. Transcript-only judging cannot prove identity, correct final state, idempotency, or fresh availability, so safety rules are hard promotion gates and conversation quality is scored separately.

The versioned baseline (`seed=42`, suite `2026-10-07.2`) passed **10/11 scenarios** but had **two critical failures**: after a slot correction, stale confirmation was accepted and success was reported without final verification. Promotion was blocked, while all **16/16 evaluator negative controls** were detected.

That failure became a structured artifact containing the violated invariant, trace evidence, root-cause layer, change, and regression case. I reinforced the write boundary with a one-time confirmation token bound to a normalized proposal digest. Changing provider, location, date, time, appointment type, or material consequence invalidates consent; success remains unavailable until final state is re-read. The reinforced run passed **11/11 scenarios**, with **zero critical or major failures**, **16/16 negative controls detected**, and **no regression among the ten prior passes**. This narrow code-level fix is stronger than another prompt instruction.

## One production change

I would replace the deliberately low-assurance demo identity check with a clinic-approved patient/proxy verification service using minimum-necessary factors, explicit proxy authority, rate limits, audited access, and safe human recovery before any protected record is disclosed. The existing boundary permits that upgrade without giving authority to the model.

## AI use and judgment

AI helped brainstorm scenarios, draft prompts and fixtures, generate implementation candidates, classify failures, and propose reinforcements. My judgment set the safety invariants, severity and promotion rules, data-ownership boundaries, and the ban on autonomous production self-modification. I also rejected a prompt-only repair: consent binding, idempotency, stale-result rejection, and final verification belong in deterministic code because they must hold even when the model is confidently wrong.
