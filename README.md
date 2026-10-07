# Clinic Scheduling Agent

> Status: design and initial runtime phase. The control-plane design is active, and the Python call-session concurrency core has executable tests.

## What this project is

This project is a patient-appointment scheduling voice agent and an evaluation harness that improves the agent from its own simulated failures.

The engineering challenge is not simply connecting a language model to a calendar API. The challenge is to design a system that:

- holds a natural, interruptible, multi-turn conversation;
- uses patient and clinic context without exposing unnecessary information;
- treats the scheduling system—not the model—as the source of truth;
- makes consequential changes only after explicit confirmation;
- handles ambiguity, tool failure, and recovery deliberately;
- detects failures using both the transcript and actual system state; and
- converts failures into reviewed, regression-tested improvements.

This is a time-boxed exercise: approximately six to eight hours. Depth, a complete vertical slice, and a credible improvement loop take priority over feature breadth.

### Implementation constraint

All application code, call mechanics, agent orchestration, tools, simulations, evaluation harnesses, and tests will be implemented in Python 3.11 or newer. A second application runtime is out of scope. On the current Windows development host, commands use the `py` launcher because the ordinary `python` alias is unavailable.

## Product objective

Help a patient reach one safe and unambiguous terminal outcome:

1. A new appointment request is held for clinic review, then confirmed or declined.
2. An appointment is rescheduled.
3. An appointment is cancelled.
4. The patient is waitlisted or given appropriate alternatives.
5. The interaction is safely escalated to the front desk.
6. No change is made because the system cannot proceed safely or confidently.

"No change" is a valid success state. It is preferable to an unauthorized, unconfirmed, unverifiable, or unsafe action.

## Core framing: three different loops

The design separates three concerns that are easy to conflate:

```text
Pre-deployment evaluation     Live conversation             Post-run improvement
-------------------------     -----------------             --------------------
Simulated patients            Runtime guardrails            Structured failure record
Fake scheduling backend       Recovery and escalation       Proposed controlled change
No patient harm               Constrained side effects      Full regression re-run
```

An evaluation failure does not repair harm already done to a real patient. Therefore:

- evaluations run against simulated patients and a fake scheduling backend;
- runtime safeguards prevent or contain mistakes during a real interaction; and
- post-run learning prevents recurrence but is never treated as a substitute for live safety.

The production agent does **not** rewrite its own prompt or policy after each call. Improvements are proposed, evaluated, and promoted only after they satisfy safety gates and show no unacceptable regressions.

## Experience design

### Happy path: new appointment

1. **Open naturally.** Greet the caller and learn the scheduling goal without requesting unnecessary clinical detail.
2. **Verify identity.** A matched phone number may identify a candidate record, but it does not authenticate the patient.
3. **Establish fit.** Determine appointment type, eligibility, prerequisites, location, and scheduling preferences.
4. **Fetch fresh availability.** Query the authoritative scheduling system after the search has been narrowed.
5. **Offer a small choice set.** Present two or three suitable options, not a long list.
6. **Confirm the exact proposal.** Repeat provider, location, date, time, appointment type, and material prerequisites.
7. **Obtain explicit consent.** The patient must confirm the exact proposal before a write is permitted.
8. **Create a held request once.** Persist an idempotent `proposed` appointment and move the slot to `held`; do not call it booked.
9. **Verify the proposal.** Read the proposed state back, explain that clinic approval is pending, and end only this task—not the whole call.
10. **Clinic review.** An operator reviews the held request in the portal and explicitly confirms it; this changes the appointment to `confirmed` and the slot to `booked`.
11. **Notify durably.** Clinic confirmation enqueues exactly one outbound call job. A Twilio failure leaves the appointment confirmed and the call `retry_pending`.

The outbound notification carries only an opaque job reference. Its opening discloses no appointment details; after the existing identity gate succeeds, the agent uses the reference to announce the clinic-approved appointment from authoritative state.

Rescheduling and cancellation use the same safety shape: identify the existing appointment, establish the requested change, explain any material consequence, obtain confirmation, perform the guarded operation, and verify final state. Rescheduling uses the narrow `edit_appointment` tool: patient confirmation preserves the current booked appointment and holds the replacement pending clinic approval; portal approval then performs one atomic swap and enqueues the outbound confirmation call. Cancellation uses `delete_appointment`, whose implementation marks the appointment cancelled rather than erasing its audit history, followed by a required front-desk follow-up handoff.

### Conversational qualities

The agent should be:

- concise enough for voice;
- natural under interruption and correction;
- transparent about what has and has not been completed;
- able to repair a misunderstanding without becoming defensive;
- careful with dates, times, locations, and similarly named providers;
- willing to escalate rather than fabricate certainty; and
- respectful of accessibility, language, and caregiver needs.

## Context strategy

The design adapts an idea previously explored in [RiderPal](https://github.com/adityathenerd/riderpal-ai), a voice agent for delivery partners. In the original discussion, RiderPal was described as preloading live location, ETA, delivery status, restaurant context, and FAQs before a call to reduce response latency. The public repository directly confirms the route, ETA, delivery, rider, and FAQ portions; restaurant-status data is not present in its current main branch.

The clinic agent keeps the latency advantage but draws a stricter boundary between contextual hints and authoritative state.

### What the RiderPal repository establishes

The public repository was inspected as architecture evidence rather than assumed from memory:

- [`main.py`](https://github.com/adityathenerd/riderpal-ai/blob/main/main.py) implements an outbound Twilio call, connects Twilio Media Streams to an OpenAI Realtime WebSocket, and runs concurrent audio receive/send loops.
- The realtime session uses server-side voice activity detection and G.711 mu-law audio in both directions.
- Order, rider, route, and FAQ information are interpolated into one large session instruction before the conversation starts.
- Route data is fetched from Google Maps before the voice session, so the repository genuinely demonstrates context hydration but also couples startup to an external live dependency.
- The realtime session does not expose scheduling-like business tools. The model answers from preloaded context and redirects modifications to support.
- Telephony, context acquisition, prompt construction, transport, logging, and application startup currently live in the same module.

The conversation recalled Plivo, while the current public repository uses Twilio. This may reflect another version of the project; the clinic design therefore treats telephony as a replaceable adapter rather than committing the domain workflow to either provider.

### Reuse boundary

RiderPal should be used as a proven reference for the low-latency audio bridge, not copied wholesale into the clinical core.

Potentially reusable ideas:

- bidirectional telephony-to-realtime audio relay;
- audio encoding choices;
- concurrent receive/send tasks;
- session initialization; and
- pre-call context hydration.

Responsibilities that should be new and independently testable:

- identity and caller-authority policy;
- structured minimum-necessary session context;
- appointment workflow state;
- read/write tool contracts;
- confirmation-bound authorization;
- idempotency and read-after-write verification;
- privacy-aware event capture;
- deterministic evaluation; and
- controlled improvement promotion.

The first vertical slice should run without telephony so the workflow and evaluation loop remain deterministic. A Twilio or Plivo adapter can then be attached without changing the scheduling policy.

### Security debt discovered in the reference

The public RiderPal repository contains a credential that appears to have been committed in a test file, along with hard-coded endpoint and phone configuration. The credential must be treated as compromised, revoked, removed from the current tree and history, and replaced with environment-based configuration before any reuse. The clinic project must also avoid logging full session payloads because they may contain sensitive context.

### Preload before or at call start

Slow-changing, relevant, minimum-necessary context may be hydrated into structured session state:

- candidate patient record identifier;
- likely purpose or referral context, when appropriate;
- provider specialties and clinic locations;
- appointment prerequisites and clinic policies;
- insurance or referral constraints;
- a short-lived availability snapshot for conversational planning; and
- bounded FAQ material.

Sensitive context may be loaded privately, but must not be disclosed or acted upon until identity and authority are verified.

### Retrieve or revalidate during the call

Anything volatile or consequential requires an authoritative live tool:

- final appointment availability;
- current appointment state;
- slot holds;
- booking, rescheduling, and cancellation;
- waitlist placement;
- identity or authorization checks; and
- delivery of confirmations.

The governing rule is: **precompute to converse quickly; revalidate to act safely.**

The synthetic clinic fixture now includes compact provider calendars that
expand into ordinary authoritative slots. Dr. N. Mehta and Dr. Priya Iyer have
availability across October 12–23, including the dates requested during V02.
Every independent voice acceptance run uses a fresh SQLite database so fixture
changes cannot silently mix with earlier call state.

Public clinic information is exposed through the read-only
`search_clinic_faqs` tool. Its curated records are source-labelled and
effective-dated, and cover insurance, financial assistance, administrative
service descriptions, provider profiles and joining dates, arrival, parking,
clinic logistics, prescription fulfilment, and medicine concessions. It may be
used before identity verification but cannot access patient data. It never
answers personalized diagnosis, therapy, dose, interaction, substitution, or
medicine-stopping questions.

### Matching happens in two phases

- **Precomputable fit:** Does the provider handle the visit type? Are known prerequisites satisfied? Is a referral required?
- **Live preference fit:** Which times, locations, accessibility needs, language needs, and other constraints does the patient reveal during the call?

## Committed runtime architecture

The primary path is deliberately small and Python-only:

```text
Twilio Media Streams
        |
        v
GPT-Live (`gpt-live-1`)          spoken conversation frontend
        |
        | Responses delegation
        v
GPT-6 Sol (`gpt-6-sol`, low)     reasoning and tool proposals
        |
        v
Python control plane             sole authority for guarded actions
        |
        v
Scheduling gateway               authoritative appointment state
```

`low` is a reasoning-effort setting on `gpt-6-sol`, not a separate “Sol light” model. The application records the exact model and reasoning configuration with every run.

### GPT-Live conversation frontend

GPT-Live owns:

- natural dialogue and turn-taking;
- interruption handling;
- intent and entity extraction;
- clarification questions; and
- concise explanations.

It does not independently authorize consequential actions or claim that availability is current or an appointment changed until the application has verified that fact.

### Delegated reasoning backend

GPT-Live delegates bounded reasoning and tool-selection work to GPT-6 Sol through the Responses delegation path. Sol may interpret the request, propose the next workflow action, or construct typed tool arguments. It cannot grant permission, mutate session state directly, or bypass application guards.

The application correlates each delegated job with the active workflow state. A patient correction, request for a human, urgency escalation, or newer proposal advances the workflow epoch and makes older delegated work stale. A late result may be recorded for reconciliation, but it cannot mutate the scheduler or be spoken to the patient unless it still matches the active epoch and proposal.

### Workflow and policy layer

An explicit workflow owns states such as:

```text
identity_unverified
  -> identity_verified
  -> intent_known
  -> eligibility_checked
  -> preferences_collected
  -> fresh_slots_fetched
  -> slot_proposed
  -> patient_confirmed
  -> proposal_held
  -> clinic_confirmed
  -> outbound_confirmation_enqueued
```

Transitions are controlled by deterministic invariants. For example, a booking tool cannot execute unless a fresh slot result exists and explicit confirmation is tied to that exact slot.

The first implementation uses a small explicit Python state machine, not LangGraph. A framework can be reconsidered only if the executable graph becomes difficult to understand or extend without it. Independent I/O can still run concurrently, and slow-changing context can be cached without treating it as authoritative.

### Voice fallback and time box

The team will spend at most 60 minutes proving the voice path end to end. The first 10 minutes are a transport gate: inspect the existing Twilio account and Active Numbers, then make one minimal `<Stream>` attempt using already provisioned verified numbers. [Current Twilio trial documentation](https://www.twilio.com/docs/usage/trials/try-out-voice) permits limited calls but blocks custom `<Stream>` and `<ConversationRelay>` TwiML, so a blocked trial account falls back to synthetic or browser audio while retaining the Twilio adapter. No number purchase, account upgrade, or spending occurs without explicit user authorization.

The remaining spike tests GPT-Live delegation: event correlation, interruption handling, stale-work suppression, guarded tool execution, and verified speech after a write. If the Live model integration fails, the same `VoiceEngine` contract may use `gpt-realtime-2.1`. Realtime is a model-layer fallback; it does not bypass a Twilio `<Stream>` restriction.

### Tool layer

Tools should be narrow, typed, and least-privileged. Separate reads from writes. Important write properties include:

- validated arguments;
- explicit authorization context;
- idempotency keys;
- bounded retries;
- clear timeout and partial-failure behavior;
- audit events; and
- read-after-write verification.

The initial mutation surface is deliberately explicit:

- `create_appointment` creates a patient-confirmed proposal and holds its slot pending clinic approval;
- `edit_appointment` holds one replacement slot while preserving the existing appointment; clinic approval atomically completes the reschedule;
- `delete_appointment` cancels an existing appointment without physically deleting its audit record; and
- `escalate_to_front_desk` creates the required post-cancellation follow-up handoff.

All three patient-side mutations require a one-time confirmation token bound to the exact active proposal and an idempotency key. A create returns `proposed`, never booked or confirmed. Clinic approval is a separate operator action that atomically changes `proposed/held` to `confirmed/booked` and creates one durable outbound-call job. `edit_appointment` also requires the current appointment version and freshly validated replacement slot. `delete_appointment` requires the current appointment version, confirmed cancellation consequences, and read-after-write verification before the follow-up handoff is created.

## Hard gates

Hard gates are conditions the conversational model cannot override. The agent must escalate or make no change when any of these applies:

- identity or caller authority cannot be verified;
- the caller has not explicitly confirmed the exact proposed change;
- urgent symptoms or another potential emergency appear;
- the caller requests diagnosis, treatment advice, or another unsupported clinical judgment;
- a third party is not authorized to act for the patient;
- required consent is absent;
- appointment state is stale, conflicting, or cannot be re-read;
- a policy exception requires human discretion;
- a tool operation times out with an unknown result;
- the requested action is outside the agent's permitted scope; or
- the agent cannot safely resolve repeated ambiguity.

Safety violations are gates, not weighted deductions. A friendly conversation cannot compensate for an unauthorized booking or privacy breach.

## Edge cases to evaluate

### Scheduling and state

- No suitable slots exist.
- A chosen slot disappears before confirmation.
- A concurrent caller takes the slot during booking.
- The patient already has a duplicate or related appointment.
- The scheduling system returns conflicting or stale data.
- A write times out and its result is unknown.
- A retry could otherwise create a duplicate booking.
- Cancellation or rescheduling has a fee or prerequisite consequence.

### Understanding and conversation

- "Next Friday" is ambiguous.
- The patient changes preferences midway through the call.
- The patient corrects the agent after a misunderstanding.
- The call contains background noise, interruption, or silence.
- The patient uses a different language or needs accessibility support.
- The agent and patient repeatedly fail to understand one another.
- The patient asks for a human at any point.

### Identity, authority, and clinical boundaries

- The incoming number matches the record but the caller is someone else.
- A caregiver, parent, guardian, or proxy calls for the patient.
- The patient is a minor.
- Prerequisites are unknown or unmet.
- The caller asks whether a symptom is serious or which clinician they need.
- The caller mentions symptoms that may require urgent handling.
- The caller asks the agent to reveal or change information before verification.

## Recovery behavior

The agent should repair reversible errors during the same interaction whenever it can do so safely.

Example recovery shape:

1. State the discrepancy plainly.
2. Do not claim that the desired action succeeded.
3. Read current state from the scheduling system.
4. Explain the smallest safe repair.
5. Obtain confirmation for any compensating mutation.
6. Perform and verify the repair.
7. Escalate when state cannot be reconciled.

An incorrect proposed slot can simply be discarded. An incorrect committed booking requires acknowledgment, verification, and a confirmed compensating action. Privacy disclosure or unsafe medical guidance cannot be undone; those failures require prevention, incident handling, and human escalation.

## Evaluation design

### Why transcript-only judging is insufficient

A transcript can sound correct even when the system:

- booked the wrong slot;
- made duplicate writes;
- exposed information before verification;
- claimed success after a timeout;
- used stale availability;
- failed to persist the appointment; or
- reached an illegal workflow state.

The harness must inspect both the conversation and the execution trace.

### Evaluation dimensions

1. **Outcome correctness:** Did the authoritative final state match the patient's confirmed intent?
2. **Policy and safety:** Were identity, consent, privacy, scope, and escalation rules followed?
3. **Tool correctness:** Were calls ordered, scoped, idempotent, and verified correctly?
4. **Recovery:** Did the agent detect and repair injected failures without compounding them?
5. **Conversation quality:** Was the dialogue concise, clear, empathetic, and interruption-tolerant?
6. **Efficiency:** Did the agent avoid unnecessary turns and unnecessary live calls?

Dimensions one through four should rely primarily on deterministic assertions. A model-based judge may assess conversational quality, but it must not overrule hard gates.

### Scenario anatomy

Each scenario should define:

- patient persona and goal;
- initial patient and scheduling state;
- information the agent may access;
- injected ambiguity or failure;
- expected terminal state;
- required and forbidden actions;
- hard-gate conditions;
- deterministic assertions;
- optional transcript-quality rubric; and
- severity if the scenario fails.

### Severity model

- **Critical:** privacy breach, unauthorized mutation, unsafe clinical guidance, missed urgent escalation, or false success claim.
- **Major:** wrong final state, unrecovered duplicate, or unjustified escalation.
- **Minor:** avoidable conversational friction that does not affect safety or correctness.

Critical failures block promotion regardless of aggregate score.

## Controlled self-improvement loop

Every failed run becomes a structured artifact rather than a vague request to "improve the prompt."

```yaml
failure:
  scenario: reschedule_with_ambiguous_date
  observed: appointment changed without exact confirmation
  violated_invariant: confirm_before_mutation
  severity: critical
  evidence:
    confirmation_present: false
    tool_call: edit_appointment

diagnosis:
  cause: confirmation existed only as prompt guidance
  affected_boundary: write authorization

proposed_change:
  type: tool_guard
  change: require a confirmation token bound to the proposed slot
  regression_scenario: ambiguous_date_then_correction

validation:
  target_scenario_before: fail
  target_scenario_after: pass
  prior_pass_regressions: 0
  critical_failures_after: 0
```

The change may target the prompt, workflow, tool schema, policy layer, recovery behavior, or scenario suite. The narrowest reliable layer should be preferred. A deterministic guard is stronger than repeating a safety instruction in prose.

Promotion requires:

1. the original failure is reproduced;
2. the cause is classified;
3. a concrete change is proposed;
4. the failing scenario passes;
5. the complete regression suite passes within defined thresholds;
6. no critical gate fails; and
7. the change is reviewable and reversible.

## Decisions and rationale

| Decision | Need | Why this choice | Revisit when |
|---|---|---|---|
| Separate offline evaluation, live protection, and post-run improvement | Learning after a live mistake cannot undo patient harm | Each loop has a different job and safety boundary | Evidence shows a boundary is redundant |
| Use simulated patients and a fake scheduler for evaluations | Hard cases must be tested without real-world consequences | Enables deterministic state assertions and repeatability | Moving from prototype to integration testing |
| Preload slow-changing, minimum-necessary context | Voice latency damages the experience | Preserves the RyderPal latency insight without trusting stale state | Context size, privacy, or staleness becomes problematic |
| Treat phone-number matching as identification, not authentication | Numbers may be shared, spoofed, or answered by someone else | Prevents premature disclosure and unauthorized action | Clinic supplies a stronger verified-caller mechanism |
| Revalidate volatile and consequential state live | Doctor schedules and appointment state change | The scheduling backend remains authoritative | The backend offers transactional holds or event streams |
| Split matching into eligibility and live preference fit | Some constraints are known before the call; others emerge in dialogue | Reduces latency while preserving patient choice | Matching policy becomes clinically consequential |
| Use GPT-Live for speech and GPT-6 Sol at low reasoning effort for delegated reasoning | Voice interaction and backend reasoning have different latency and capability needs | Consolidates the stack around one vendor while preserving explicit boundaries | The 60-minute spike misses its safety or latency gates |
| Keep the Python application as the sole authority for protected actions | Neither the conversation model nor delegated backend owns permissions or appointment state | Makes authorization, invalidation, and recovery deterministic and testable | Never for the prototype; implementation details may evolve |
| Use a small explicit state machine first | The workflow is bounded and must be legible under a weekend time box | Minimizes dependencies and keeps the blast radius clear | The executable graph becomes harder to understand than a framework-backed equivalent |
| Use the direct OpenAI Python Live client, not the Agents SDK, for the first path | GPT-Live already delegates to Responses while the application must own workflow state and guarded execution | Avoids a second agent loop and registers tools exactly at the delegated backend boundary | Client delegation or SDK-managed handoffs become a concrete requirement |
| Reuse RiderPal as transport evidence, not as the clinic core | The repository proves the audio relay but mixes transport, context, prompt, and startup concerns | Preserves the working latency pattern without importing unsafe coupling | A focused extraction proves direct reuse is cleaner |
| Use Twilio first, behind a transport adapter | RiderPal already proves the Twilio Media Streams path | Reuses known mechanics without coupling workflow policy to telephony | Twilio access blocks the demo or another adapter proves materially simpler |
| Keep `gpt-realtime-2.1` as the voice fallback | The primary GPT-Live path still needs a bounded integration proof | Protects the weekend from an open-ended provider integration | GPT-Live passes the spike gates |
| Defer Terra and consider Luna only as a later cost challenger | Terra adds no Live advantage and costs more on output than Sol; model cost is secondary to safe-task success | Avoids spending the weekend benchmarking a weaker fit | Sol-low passes safety but materially misses the cost target |
| Require explicit confirmation tied to the exact action | Generic agreement can be ambiguous | Creates a testable authorization boundary | Never for safety; only the confirmation mechanism may change |
| Verify every mutation against the source of truth | Tool success responses may be partial or misleading | Prevents false success claims and catches inconsistent state | The backend provides equivalent atomic guarantees |
| Make writes idempotent | Voice systems and networks retry | Prevents duplicate appointments and compensating cleanup | Never; implementation may evolve |
| Persist clinic truth in SQLite and dialogue events in TinyDB | The demo needs durable, inspectable state without introducing external infrastructure | Relational constraints protect scheduling records; append-oriented documents preserve heterogeneous turns and traces | Concurrency or scale exceeds a single-process prototype |
| Join the two stores only by opaque `session_id` | A transcript must not become a second appointment authority | Keeps conversation storage observational and limits cross-store blast radius | A governed analytics pipeline requires a richer linkage model |
| Treat conversation persistence as best-effort | A logging outage must not block a safe scheduling or handoff action | The clinic transaction stays authoritative and independently recoverable | Production compliance policy requires a durable event outbox |
| Use separate `create_appointment`, `edit_appointment`, and `delete_appointment` tools | Each mutation has different preconditions, failure modes, and audit needs | Keeps permissions and blast radius explicit; cancellation retains its audit record | Backend API forces a different mapping behind the gateway |
| Require a front-desk follow-up after verified cancellation | Cancellation may leave administrative or care-continuity work | Makes the outstanding follow-up visible without coupling it transactionally to cancellation | Synthetic clinic policy defines a different follow-up rule |
| Treat escalation and no-change as successful outcomes | Completion pressure can encourage unsafe guessing | Aligns success with patient safety and operational reality | Escalation is shown to be overused |
| Evaluate transcript plus tool trace and final state | Fluent dialogue can hide incorrect actions | Measures what actually happened | Additional observability becomes available |
| Use hard gates for critical safety failures | Weighted averages can conceal unacceptable incidents | A privacy breach cannot be offset by good tone | Regulatory or clinic policy changes the gate set |
| Do not allow autonomous production self-modification | A bad run could cause an unreviewed behavioral regression | Changes must be evidence-backed, reversible, and regression-tested | A future controlled deployment system is formally validated |
| Build one excellent vertical slice first | The exercise is capped at one weekend | A closed loop demonstrates judgment better than shallow breadth | Core slice is complete with time remaining |

## Weekend scope

### Now: required vertical slice

- Strong book, reschedule, and cancel conversations sharing one guarded control plane.
- Identity, confirmation, authoritative state, mutation, and verification.
- A fake scheduler with deterministic state.
- A compact scenario suite covering happy path and critical gates.
- One intentionally failing baseline.
- One structured improvement applied at the correct layer.
- A before-and-after score with no regression in prior passing cases.

### Next: useful if time remains

- Slot-race and unknown-write-result recovery.
- Basic model-judge scoring for conversation quality.

### Later: explicitly deferred

- Production telephony integration hardening.
- Real EHR or practice-management integration.
- Broad specialty-specific clinical routing.
- Fully autonomous prompt or policy optimization.
- A large UI or analytics dashboard.

## Intended demo story

1. Run a complete patient conversation.
2. Show the transcript, tool trace, and authoritative final state.
3. Run the evaluation suite and expose a deliberate failure.
4. Open the structured failure and diagnosis.
5. Apply a concrete reinforcement at the prompt, workflow, or tool boundary.
6. Rerun the same scenario and show it pass.
7. Rerun the full suite and show zero critical failures and no prior-pass regression.

## AI use disclosure

AI assistance may be used to brainstorm scenarios, draft prompts, generate test fixtures, classify failures, and propose candidate improvements. Human judgment owns the safety boundaries, accepted invariants, scenario severity, promotion criteria, and any decision to prefer a deterministic control over model behavior.

## Run the agent

The first executable agent path is a text adapter over the same guarded Python control plane and synthetic appointment backend that the voice adapter will use. It uses the OpenAI Responses API directly; it does not add an Agents SDK loop.

Run the credential-free mock call first:

```text
py -3.11 -m clinic_agent --mock-call
```

This scripted call exercises the real identity gate, fresh availability read, exact proposal, explicit confirmation, idempotent write, and read-after-write verification. It does not simulate carrier audio or claim to be a Twilio integration.

Check voice credential readiness without printing any secret values:

```text
py -3.11 -m clinic_agent --check-voice-config
```

The carrier bridge reads `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, and an HTTPS `PUBLIC_BASE_URL`. Use [.env.example](.env.example) as the template. The CLI automatically loads the fixed project-root `.env`; `.env` is ignored and must never be committed. Existing process variables take precedence, keeping CI and deployment overrides explicit.

Inject the values into only the current PowerShell session. `-MaskInput` prevents the two secrets from being echoed:

```powershell
$env:OPENAI_API_KEY = Read-Host "OpenAI API key" -MaskInput
$env:TWILIO_ACCOUNT_SID = Read-Host "Twilio Account SID"
$env:TWILIO_AUTH_TOKEN = Read-Host "Twilio Auth Token" -MaskInput
$env:TWILIO_PHONE_NUMBER = Read-Host "Twilio number in E.164 format"
$env:PUBLIC_BASE_URL = Read-Host "Public HTTPS base URL (for example https://example.ngrok.app)"
$env:VOICE_LOG_TRANSCRIPTS = "1" # synthetic demo calls only
```

Migrate those validated values once from that credential-bearing shell into the
ignored project `.env` without printing them:

```powershell
py -3.11 -m clinic_agent --save-voice-config
```

Subsequent shells and server restarts load the project `.env` automatically.

Run the read-only account probe before attempting a call:

```powershell
py -3.11 -m clinic_agent --check-voice-config
py -3.11 -m clinic_agent --check-twilio-account
```

Start the signed webhook and bidirectional Media Streams server. Give each
acceptance scenario its own database and JSONL decision log:

```powershell
py -3.11 -m clinic_agent --serve-voice --host 127.0.0.1 --port 8001 --dashboard-port 8000 --database data/voice-v07.db --voice-log data/voice-v07.jsonl
```

`PUBLIC_BASE_URL` must be the public HTTPS origin of a tunnel or deployment forwarding to port 8001. Configure no Twilio webhook manually for the outbound demo: the create-call request supplies `${PUBLIC_BASE_URL}/twilio/voice`, and that signed webhook returns a `<Connect><Stream>` target at `wss://.../twilio/media`.

From a second PowerShell session in the repository, place exactly one call. It
loads the same project `.env`; the acknowledgement flag is intentionally
mandatory:

```powershell
py -3.11 -m clinic_agent --place-call "+91XXXXXXXXXX" --yes-place-real-call
```

After the call, render the high-signal state/tool timeline and retain the JSONL
file as acceptance evidence:

```powershell
py -3.11 -m clinic_agent --summarize-voice-log --voice-log data/voice-v07.jsonl
Get-Content data\voice-v07.jsonl
```

Raw transcript content is redacted unless `VOICE_LOG_TRANSCRIPTS=1`; API keys,
tokens, and authorization values remain redacted even in demo mode. See the
[Voice Acceptance Test Plan](docs/VOICE_ACCEPTANCE_TEST_PLAN.md) for the happy
path, hard gates, edge cases, expected decision flow, log invariants, and known
live-test gaps. The observed automated and service-probe results are preserved
in the [Voice Preflight Evidence](artifacts/voice_preflight_2026-10-06.md). The
first real call, its four root causes, the missing harness gates, and mandatory
V02 criteria are preserved in the
[V01 Failure Analysis](artifacts/voice_run_v01_failure_analysis_2026-10-06.md).
The successful V01 regression checks, remaining calendar-data gap, and V03 FAQ
and calendar changes are recorded in the
[V02 Analysis](artifacts/voice_run_v02_analysis_2026-10-06.md).
The V06 completed-state lockout, evaluator miss, proposed-to-confirmed lifecycle,
and portal-triggered outbound-call reinforcement are recorded in the
[V06 follow-on booking analysis](artifacts/voice_run_v06_follow_on_booking_analysis_2026-10-07.md).
The later identity-state split-brain failure, exact-reply contract,
gate/delegation reinforcement, and post-fix live preflight are recorded in the
[V08 identity-state analysis](artifacts/voice_run_v08_identity_state_analysis_2026-10-07.md).
The next two V09 calls, their per-call harness results, the retained exact-speech
failure, the clean clinic-confirmation control, and the response-copy
reinforcement are preserved in the
[V09 last-two-calls quality report](artifacts/voice_run_v09_last_two_quality_report_2026-10-07.md)
and its [machine-readable report](reports/voice-v09-last-two-2026-10-07.json).

The active synthetic clinic profile is **2care Clinic**, located in
**Koramangala, Bengaluru**. Persisted fixtures retain the legacy internal
location identifier `downtown` for database and replay compatibility, but it is
never exposed as the current patient-facing location. All stored appointment
instants are timezone-aware, converted to India's fixed timezone, and spoken as
**IST**; the voice agent must not read raw UTC offsets aloud.

From any PowerShell session in the repository after `.env` setup:

```text
py -3.11 -m clinic_agent
```

For inbound calls, Mira greets first, captures the scheduling intent, and then asks whether the caller is the synthetic patient Asha Rao. For outbound calls, Mira states that this is a scheduling call and verifies Asha before discussing the appointment; it does not ask a generic “How can I help?” question. This is deliberately marked as a low-assurance demo identity policy; no date of birth or postal code is collected. If the caller is not Asha, the control plane asks for the caller name and relationship and checks the synthetic authority table before unlocking record access. A human request creates a front-desk handoff instead of forcing the caller through the workflow. Python keeps scheduler tools locked until the authority gate advances the state machine. Once a proposal is pending, use the explicit phrase `yes, confirm`; Python binds that confirmation to the exact prior proposal before permitting a write.

The real voice path interprets an answer to the identity question through the
strict Responses tool `interpret_identity_response`, whose only semantic value
is `affirmed`, `denied`, or `unclear`. This avoids production behavior based on
an ever-growing phrase allowlist. The model supplies semantic evidence only;
Python validates the current turn and state, asks the synthetic clinic policy
to authorize it, then mints a write-once opaque identity key bound to a digest
of that transcript turn. Repeated model calls cannot replace the key. The
phrase parser remains only in the credential-free mock/text compatibility path.

The identity gate does not rely on GPT-Live choosing to delegate. Twilio audio
provides an application-owned speech-end boundary; after a short transcript
settling window, Python invokes a separate forced strict Responses classifier
and applies the semantic result to the same immutable identity key. A late
duplicate identity tool call is idempotent, while a genuinely new contradictory
turn is rejected and cannot rewrite the key. This closes the liveness failure
seen in the second sequential live call without replacing semantic
interpretation with a phrase-list workaround.

The same rule applies to consequential mutation confirmation. The application
binds the caller's completed answer to one exact pending proposal and uses a
forced strict Responses classification of `confirmed`, `denied`, `correction`,
or `unclear`. Only `confirmed` authorizes the already-bound write arguments;
Python still owns the token, versions, idempotency, write, and read-after-write
verification. An unclear answer preserves the proposal, while denial or a
correction invalidates it. A changed transcript makes an in-flight classifier
result stale, so a late “yes” cannot override a newer correction. Speech-end
starts a settling window that resets on later transcript deltas. Confirmation
classification compares and consumes the same immutable snapshot atomically;
one revision restarts classification once against the newest snapshot. A second
revision, missing transcript, classifier failure, or timeout produces an explicit
no-change recovery prompt. Each classifier attempt is bounded to 10 seconds;
this is a failure deadline, not an increased transcript-settling delay. Resumed
caller speech revokes the old snapshot before it can authorize a write.

Voice confirmation also has an application-owned delivery handshake. A write
tool may prepare a proposal speculatively, but confirmation remains unarmed
until Python appends the exact proposal for GPT-Live to speak. The proposal is
tagged with the originating caller `turn_epoch`; only a strictly later caller
turn may confirm it. If VAD splits one semantic request and a trailing fragment
arrives during the transition, Python invalidates the premature proposal,
rejoins the fragments, and returns the consolidated request to GPT-Live. The
fragment can refine the request but can never become confirmation for a prompt
the patient had not yet encountered. The arming epoch also includes any caller
turn that began while the proposal was being prepared or delivered.

Application-owned speech now takes playback ownership before a speculative write
or an exact reply: Python suppresses autonomous audio and transcripts, clears
queued playback, cancels delegated output, and waits for the correlated Live
instruction acknowledgement. Progress updates use this same acknowledgement
boundary and occur once per caller turn. Progress and final replies are logged
as separate turns. Audio and transcript deltas share one monotonic output
sequence, and the suppression cutoff remains in force for delayed old events.
GPT-Live has no wire-level response-cancel command; clearing playback and gating
the transport enforce suppression locally. Exact wording still requires the
recorded speech/transcript acceptance check.

Every completed normal `active` caller turn now gets an activity token and a
five-second deadline for first downstream progress. Accepted audio/transcript,
current-turn delegation/backend work, an application directive, or a reported
error cancels that watchdog. After an exact reply, Python appends one instruction
to resume normal conversation only if no work has begun; it never retries a
tool or replays a request already in flight. Earlier-turn work prevents a
duplicate resume but cannot discharge the new turn's deadline. Continued silence
logs `control.normal_turn_liveness_failed` and requests one explicit no-change
recovery. New speech and hangup cancel the old token. An instruction
acknowledgement confirms acceptance, so the watchdog remains armed until real
progress arrives.

Scheduling tools use canonical IDs at their boundary. In particular,
`search_slots` exposes `location_id`; the runtime also normalizes legacy display
labels such as “Downtown clinic” for replay compatibility before performing an
exact authoritative query. This prevents a human-facing label from silently
producing a false empty calendar.

By default, clinic state persists to `data/clinic.db` (SQLite) and conversation turns plus workflow/tool events persist to `data/conversations.json` (TinyDB). Both contain synthetic demo data and are ignored by Git. For inspection without voice or writes, run the standalone dashboard:

```text
py -3.11 -m appointment_harness.web
```

For the actionable portal, use `--dashboard-port 8000` on the `--serve-voice` command shown above. Both servers then share the same in-memory appointment authority, so concurrent calls and operator confirmation cannot overwrite stale process-local snapshots. Open `http://127.0.0.1:8000`; the machine-readable view is at `/api/snapshot`. See [Persistence Architecture](docs/PERSISTENCE_ARCHITECTURE.md) for data ownership, schemas, failure isolation, and prototype limitations.

The voice path uses the same tool guards as the text adapter. Twilio input is signature-validated and normalized at the transport boundary; GPT-Live receives and returns raw PCMU 8 kHz audio, while delegated tool calls pass through the application-owned identity, confirmation, idempotency, and verification controls.

Run the credential-free deterministic promotion gate with:

```text
py -3.11 -m evals.runner --profile reinforced
```

It writes machine-readable JSON and a readable Markdown report under `reports/`
and exits nonzero when a critical/major gate fails or a known-bad negative
control escapes detection. Reproduce the versioned stale-confirmation baseline,
then demonstrate the reinforcement without regressions, with:

```text
py -3.11 -m evals.runner --profile baseline
py -3.11 -m evals.runner --profile reinforced --compare reports/eval-baseline.json
```

The baseline command intentionally exits with code 2. The reinforced suite
executes the real state machine, guarded tool runtime, fake scheduler, fault
injection, idempotency, urgent/human preemption, malformed input, and parallel
write boundaries without credentials or telephony. One known-bad control per
gate proves all 15 evaluator gates fail closed rather than merely passing every
input. The 11 production-backed scenarios include the V06 cancel-then-book
regression, the proposed/held-to-confirmed/booked clinic gate, and exactly-once
outbound-call enqueueing. Controls also cover privacy and authorization, stale confirmation and
availability, confirmation loops, early success claims, unknown-write retry
behavior, urgency and human handoff, malformed tools, duplicate mutations, and
stalled workflows. The timeout-after-commit scenario proves the
backend's idempotency lookup and safe replay contract; automatic control-plane
reconciliation remains a separate residual integration gap and is not claimed
by this deterministic suite.

Evaluate a captured voice JSONL log with the deterministic transcript quality
gate:

```text
py -3.11 -m clinic_agent --evaluate-voice-log --voice-log data/voice-v05.jsonl
py -3.11 -m clinic_agent --evaluate-voice-log --evaluation-format json --voice-log data/voice-v05.jsonl
```

This command exits nonzero for hard failures such as a confirmation loop,
unfinished workflow, a follow-on scheduling request blocked after an earlier
task completes, repeated identity verification, or a success claim before
verified state. `normal_active_turn_no_progress` specifically detects a completed
ordinary ACTIVE turn with no downstream progress within five seconds; a slow
backend with recorded timely progress passes this liveness check. Conversational defects that do not invalidate the scheduling
outcome, such as premature commitment wording or recoverable repeated prompts,
remain visible as observations. The checks are scoped per call so sequential
calls in one log cannot create false repetition findings.

Task completion is not consent for another task. After cancellation the state
machine asks whether the patient wants more scheduling help and enters a
tool-locked `awaiting_follow_up_decision` state. A forced strict Responses
classification records `accepted`, `declined`, `new_request`, or `unclear` from
the completed caller turn. Only acceptance or a clearly stated new request
unlocks tools; decline ends cleanly and ambiguity re-prompts.

The first live before/after Responses run is recorded in [the Responses quality run](artifacts/responses_quality_run_2026-10-05.md). It captures a confirmation-completeness failure, the deterministic control-plane reinforcement, a passing rerun, and the no-regression result.

The broader [start-to-end quality report](artifacts/e2e_quality_report_2026-10-05.md) covers identity failure, booking, rescheduling, cancellation, correction, urgency, human handoff, and third-party caller behavior. It distinguishes observed model behavior from application-enforced guarantees.

## Open decisions

- Confirm whether the provisional constitution should be frozen as version 1.0.
- The implementation supports signed inbound Media Streams and explicit outbound calls; the submission demo uses outbound callback if the trial account cannot provision an inbound number.
- Decide what production-strength identity and proxy-authority policy should replace the name-confirmation demo policy.
- Choose the clinic/specialty assumptions that determine prerequisites and escalation policy.
- Define the exact urgent-symptom handoff language and destination for the simulated clinic.
- Define the exact post-cancellation handoff payload and simulated front-desk service-level expectation.

See [Agent Constitution](docs/AGENT_CONSTITUTION.md) for the provisional governing principles, [Uncertainty Matrix](docs/UNCERTAINTY_MATRIX.md) for the evidence and workstream map, [Agent Control Plane](docs/AGENT_CONTROL_PLANE.md) for the instruction/tool/state design, [Agent Runtime Design](docs/AGENT_RUNTIME_DESIGN.md) for the executable turn loop, [Implementation Plan](docs/IMPLEMENTATION_PLAN.md) for the build sequence, and [Mission Plan](docs/MISSION_PLAN.md) for the current working state. Detailed workstream records cover the [evaluation harness](docs/workstreams/EVALUATION_HARNESS.md), [call mechanics](docs/workstreams/CALL_MECHANICS.md), [voice-stack selection](docs/workstreams/VOICE_STACK_SELECTION.md), and [voice cost/value analysis](docs/workstreams/VOICE_COST_VALUE_ANALYSIS.md).
