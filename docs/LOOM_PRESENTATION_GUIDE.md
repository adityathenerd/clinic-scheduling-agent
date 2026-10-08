# Loom Presentation Guide — Clinic Scheduling Agent

Target length: **7–9 minutes**  
Audience: 2care AI engineering reviewers  
Primary outcome: demonstrate judgment, runtime safety, and a measurable
failure-to-improvement loop—not the amount of plumbing.

## The one idea to land

> Models may propose, but deterministic Python code authorizes. The scheduler is
> the source of truth, and an improvement is promoted only when the same suite
> shows the target failure repaired without regressing earlier passes.

Everything in the walkthrough should support that sentence.

## What to have open before recording

Arrange these in this order so the recording feels deliberate:

1. The successful call recording for
   `CA76484e894f7609c0bc6809808a96852c`.
2. The clinic portal at `http://127.0.0.1:8000/`.
3. `README.md`, near **Core framing: three different loops**.
4. `artifacts/voice_run_v10_gate_branch_before_after_2026-10-08.md`.
5. Commit `5ee215c`, or its actor/evaluator diff.
6. `reports/eval-baseline.md`.
7. `reports/eval-reinforced-latest.md`.
8. `docs/DESIGN_NOTE.md`.
9. A PowerShell terminal opened at the repository root.

Repository root:

```powershell
cd C:\Users\ASUS\Desktop\clinic-scheduling-agent
```

Do not show `.env`, API keys, Twilio credentials, the full phone number, or raw
unredacted JSONL. Hide desktop notifications before starting.

## Recording structure at a glance

| Time | Screen | Point to establish |
|---:|---|---|
| 0:00–0:35 | README / title | This is an authority and learning system, not an API wrapper |
| 0:35–2:45 | Successful call recording + portal | The agent holds a real multi-turn conversation while Python owns consequential state |
| 2:45–3:35 | Architecture / three loops | Runtime protection and post-run learning are different jobs |
| 3:35–4:45 | V10 failed/passing call report | A real voice-state defect became a named invariant and passed in the next call |
| 4:45–6:45 | Baseline and reinforced eval commands/reports | Formal score moves from 10/11 to 11/11 with no prior-pass regression |
| 6:45–7:45 | Harness components | Explain what produces evidence and why transcript-only judging is insufficient |
| 7:45–8:30 | Design note / close | State production limits, AI contribution, and personal judgment |

If you need a shorter recording, omit the detailed V10 commit diff—not the
baseline/reinforced comparison.

---

## 0:00–0:35 — Open with the engineering judgment

Show the README title and three-loop diagram.

Say:

> This is a patient appointment scheduling voice agent, but the core of the
> project is not the call connection. The design rule is that models may propose
> actions, while deterministic Python code alone authorizes identity, state
> transitions, tools, consent, writes, and final claims. The scheduler—not the
> transcript—is the source of truth.

> I separated runtime protection from post-run learning because evaluating a bad
> call afterward cannot undo patient harm. Runtime gates prevent unsafe actions;
> the evaluation harness turns failures into reviewed improvements before they
> are promoted.

Do not begin with a list of frameworks. The architecture matters because of the
failure modes it controls.

---

## 0:35–2:45 — Play and annotate the successful call

Play the recording for call:

```text
CA76484e894f7609c0bc6809808a96852c
October 8, 2026 at 16:59:19 IST
Harness result: PASS — 0 hard failures, 0 observations
```

Let the recording remain the primary audio. Pause only at the transitions below
and add short commentary.

### Moment 1 — Direction-aware opening

Point out:

> This is an outbound call, so the system already knows its purpose. It does not
> reset into a generic “How can I help?” opening.

### Moment 2 — Identity verification

Point out:

> The phone number only selects a candidate record; it is not authorization.
> Protected appointment details and scheduling tools remain locked until the
> identity gate completes. The semantic classifier records evidence, but Python
> mints the immutable authorization state.

### Moment 3 — Current appointment and fresh availability

Point out:

> Slow-changing clinic context can be preloaded for latency, but appointment
> state and doctor availability are fetched live because they are volatile and
> consequential.

If the portal is visible, show the calendar and the current appointment. Mention
that every block has an explicit start and end time; empty visual space does not
imply availability.

### Moment 4 — Exact reschedule proposal and confirmation

Point out:

> Conversation can remain natural, but consent is bound to an exact normalized
> proposal. A changed date, time, provider, location, appointment type, or
> consequence invalidates the old consent.

> Patient confirmation does not immediately overwrite the booked appointment.
> It holds the replacement for clinic review. Portal approval performs one
> atomic swap and enqueues one outbound confirmation call.

### Moment 5 — FAQ after the scheduling task

Point out:

> Completing one task does not terminate the whole conversation. The state
> machine explicitly offers follow-up help, returns to active conversation for a
> clinic FAQ, and then accepts “that’s all” as a valid no-change terminal path.

### Moment 6 — Clean close

Point out:

> The call ends only after the patient indicates there is nothing else. No
> additional mutation is required for a successful outcome.

After the call finishes, briefly show the portal event and appointment status.

---

## 2:45–3:35 — Explain the three loops

Show the README diagram.

Say:

> There are three separate loops.

> First, pre-deployment evaluation runs difficult scenarios against synthetic
> patients and a fake scheduler without risking a real patient. Second, live
> protection enforces identity, authorization, confirmation, idempotency, and
> read-after-write verification during the call. Third, post-run improvement
> converts a failure into a structured change, reruns the same suite, and only
> promotes it if safety gates pass and prior behavior does not regress.

> The production agent never rewrites its own prompt or policy immediately after
> one call. Improvements are reviewed and tested before promotion.

This answers the apparent paradox in “self-improving”: the system learns from a
failure without treating a harmed caller as the repair mechanism.

---

## 3:35–4:45 — Show a real voice failure becoming a guard

Open:

```text
artifacts/voice_run_v10_gate_branch_before_after_2026-10-08.md
```

### Before

Call:

```text
CA68a2a0030819bb606a2061ab54ecd2bf
October 8, 2026 at 16:55:07 IST
```

Say:

> The evaluator rejected this call with three hard failures: the exact
> application response was misclassified as normal conversation, application
> audio delivery never completed, and the workflow remained incomplete at
> hangup.

> The root cause was not bad wording. After identity verification moved the
> workflow to ACTIVE, two branches claimed the turn: the application still owned
> an exact reply, while the actor also created a normal-conversation liveness
> path.

### Structured reinforcement

Show commit `5ee215c`.

Say:

> The fix changed the branch predicate from state-only to state plus directive
> ownership. ACTIVE plus a `say_exactly` directive remains application-owned.
> Only a model-owned continuation enters normal conversation. I also added a
> named evaluator rule and regression tests, so this exact failure cannot become
> invisible again.

### After

Say:

> The next call, four minutes later, was `CA76484…`, the recording you just saw.
> It passed with zero failures and zero observations. The following clinic
> confirmation call also passed.

This live pair supports the story, but the versioned deterministic suite below
is the formal promotion gate.

---

## 4:45–6:45 — Run the formal improvement loop

### Step 1 — Deliberate baseline

Run:

```powershell
py -3.11 -m evals.runner --profile baseline
```

Expected result:

```text
passed=10/11
critical_failures=2
negative_controls=16/16
promotion_eligible=false
```

The process exits with code `2`. Explain that this is intentional: a critical
evaluation failure must fail automation rather than merely lower an average.

Open `reports/eval-baseline.md` and say:

> The failing scenario changes an already proposed slot. The baseline incorrectly
> reuses stale confirmation and can report success without authoritative final
> verification. Ten other scenarios pass, but two critical failures block
> promotion.

### Step 2 — Explain the structured improvement

Show the confirmation-token/write-boundary implementation or the relevant part
of `docs/DESIGN_NOTE.md`.

Say:

> The reinforcement is a control-plane change, not prompt decoration. A one-time
> token is bound to the normalized active proposal. Any material correction
> invalidates it. The write also uses an idempotency key and success is withheld
> until the scheduler is read back at the expected version and state.

The structured failure artifact identifies:

- failed gate;
- severity;
- evidence from the trace and final state;
- owning boundary;
- proposed invariant change;
- regression scenarios; and
- promotion conditions.

### Step 3 — Rerun the same suite

Run:

```powershell
py -3.11 -m evals.runner --profile reinforced --compare reports/eval-baseline.json
```

Expected result:

```text
passed=11/11
critical_failures=0
major_failures=0
negative_controls=16/16
prior_pass_regressions=[]
promotion_eligible=true
```

Say:

> The target failure is repaired, the ten previously passing scenarios still
> pass, and every known-bad negative control is still rejected. That closes the
> loop: failure, structured reinforcement, same-suite rerun, measurable score
> improvement, and no prior-pass regression.

Optionally show the full verification:

```powershell
py -3.11 -m unittest discover -s tests
```

Expected result:

```text
Ran 401 tests
OK
```

Do not spend recording time reading test names. The number supports breadth; the
behavioral evidence is the important part.

---

## 6:45–7:45 — Explain the harness components

Use this component map:

```text
Versioned scenario
    │
    ├── scripted patient acts
    ├── initial scheduler state
    ├── permitted information and tools
    ├── injected fault or adversarial condition
    └── expected terminal invariants
            │
            ▼
Production control-plane components
            │
            ▼
Structured evidence recorder
    ├── conversation turns
    ├── workflow transitions
    ├── tool requests and decisions
    ├── confirmation/proposal bindings
    ├── mutations and idempotency outcomes
    └── authoritative final scheduler state
            │
            ▼
Deterministic evaluator
    ├── critical hard gates
    ├── major behavioral gates
    └── non-blocking quality observations
            │
            ▼
Comparator and promotion decision
    ├── repaired failures
    ├── prior-pass regressions
    ├── negative-control detection
    └── promotion eligible / blocked
```

Describe the components in this order:

### 1. Scenario catalog

Each scenario defines the starting state, patient behavior, allowed information,
fault injection, expected tool trace, and expected terminal state. The catalog
includes verified rescheduling, correction after confirmation, unauthorized
callers, urgency, explicit human requests, malformed tools, parallel duplicate
writes, timeout-after-commit, slot races, and follow-on tasks.

### 2. Scripted patient and fault injection

The patient side is controlled enough to reproduce difficult turns. The fake
scheduler can inject conflicts, stale versions, timeouts, duplicate requests,
and slots being taken concurrently.

### 3. Real control-plane components

The harness does not evaluate a toy alternate policy. Scenarios exercise the
same state machine, guarded tool runtime, confirmation binding, scheduler
contracts, and idempotency behavior used by the agent.

### 4. Structured recorder

The recorder captures ordered evidence. This matters because a fluent transcript
cannot prove whether the caller was authorized, the intended slot was mutated,
one retry caused two writes, or the database matches what the assistant claimed.

### 5. Deterministic evaluator

Critical failures are hard gates, not weighted deductions. Examples include
disclosure before identity, mutation without authority, stale confirmation,
duplicate mutation, blind retry after unknown outcome, missed urgency, incomplete
workflow, or patient/clinic approval being bypassed.

Conversational issues such as awkward repetition can remain observations unless
they indicate a loop, stalled workflow, or incorrect outcome.

### 6. Negative controls

The suite contains known-bad evidence for every evaluator gate. A trustworthy
evaluator must reject those controls. Otherwise a green report may only prove
that the judge passes everything.

### 7. Comparator and promotion gate

The comparator checks more than the new total. It identifies the repaired
scenario and confirms that scenarios which passed before still pass. Promotion
requires zero critical/major failures, successful negative controls, and no
unacceptable prior-pass regression.

### Voice-log evaluator

Real PSTN calls add a second input surface:

```powershell
py -3.11 -m clinic_agent --evaluate-voice-log --voice-log data/voice-demo.jsonl
```

It checks call-specific evidence such as repeated identity/confirmation cycles,
exact speech delivery, liveness gaps, incomplete workflows, clinic-approval
bypass, and unverified writes. It shares the same principle—structured evidence
over transcript fluency—but it does not replace the controlled scenario suite.

---

## 7:45–8:30 — Close with production judgment and AI disclosure

Open `docs/DESIGN_NOTE.md`.

Say:

> For a real clinic, the first change is not another model or more features. I
> would replace the low-assurance demo name check with clinic-approved patient
> and proxy verification, connect a real EHR/scheduling sandbox behind the same
> gateway, add a transactional outbox and staffed escalation, and validate noisy
> audio, languages, disconnects, and concurrent calls under production
> observability.

> AI helped accelerate repository inspection, scenario generation, implementation
> drafts, debugging hypotheses, and documentation. My judgment overrode it where
> safety and architecture mattered: separating runtime protection from learning,
> keeping Python as the sole write authority, using explicit state rather than a
> free-running agent loop, treating critical failures as hard gates, and refusing
> immediate autonomous self-modification after a live call.

Finish with:

> The result is intentionally narrow: one complete, inspectable scheduling system
> whose failures become reproducible tests and whose improvements must earn
> promotion without breaking what already worked.

Stop there. Do not end with a long feature list.

---

## Presenter cheat sheet

If you lose your place, return to these five points:

1. **Models propose; Python authorizes.**
2. **The scheduler is truth, not the transcript.**
3. **Runtime gates prevent harm; post-run evaluation cannot undo harm.**
4. **Baseline 10/11 with two critical failures → reinforced 11/11 with none.**
5. **Sixteen negative controls and zero prior-pass regressions make the score credible.**

## Commands in one block

```powershell
# Optional deterministic agent conversation
py -3.11 -m clinic_agent --mock-call

# Expected to fail with exit code 2: 10/11 and two critical failures
py -3.11 -m evals.runner --profile baseline

# Expected to pass: 11/11, 16/16 negative controls, no prior-pass regression
py -3.11 -m evals.runner --profile reinforced --compare reports/eval-baseline.json

# Full regression suite
py -3.11 -m unittest discover -s tests

# Evaluate a fresh captured voice log
py -3.11 -m clinic_agent --evaluate-voice-log --voice-log data/voice-demo.jsonl
```

## Final pre-recording checklist

- [ ] The repository is accessible to the reviewer.
- [ ] The successful call recording is queued at the beginning.
- [ ] The call does not expose a full phone number or credentials.
- [ ] The terminal starts in the repository root.
- [ ] The baseline report exists before running the comparison.
- [ ] You explain that baseline exit code `2` is expected.
- [ ] You show the repaired scenario and no-regression comparison.
- [ ] You mention the evaluator's transcript-only limitations.
- [ ] You state the production identity limitation clearly.
- [ ] You add the final Loom URL to the Google Form.
- [ ] You review the form and submit it yourself.

