# Mission Plan

Status: Active  
Last updated: 2026-10-05  
Current phase: Control-plane implementation and bounded voice integration

## TL;DR

- **Objective:** Build a focused clinic scheduling agent and an evaluation loop that demonstrates measurable, regression-safe improvement.
- **Present state:** The Python-only architecture and all workstream recommendations are integrated. The primary voice path is GPT-Live delegating to GPT-6 Sol at low reasoning effort; call mechanics and mutation contracts have 45 passing tests.
- **Primary uncertainty:** The synthetic clinic policy, identity model, and approved urgent-handoff policy remain open. The selected GPT-Live path still needs a bounded integration proof.
- **Immediate next action:** Freeze the minimum clinic policies, implement the typed scheduling state machine and fake backend, then run the 60-minute GPT-Live spike against those stable contracts.

## Objective and success

### Objective

Produce a runnable weekend-scale repository, a concise design note, and a demoable before/after evaluation loop suitable for the 2care AI software-engineering exercise.

### Success criteria

- One command runs a realistic multi-turn agent interaction.
- One command runs the evaluation and improvement loop.
- The suite covers happy paths, edge cases, hard gates, and recovery.
- At least one baseline failure becomes a structured improvement.
- The rerun improves the target score without regressing prior passes or violating critical gates.
- Decisions and tradeoffs are legible to reviewers.
- A short recording can demonstrate the entire loop.

## Context

The user's prior RiderPal project used an OpenAI realtime voice stack and preloaded delivery context to reduce call latency. Inspection of the public repository shows Twilio Media Streams, not Plivo, connected directly to an OpenAI Realtime WebSocket. The new project adapts the proven transport and context-hydration ideas to clinic scheduling, where privacy, identity, changing availability, and consequential writes require stricter authority boundaries.

## Scope

### In scope

- Design-first documentation.
- Book, reschedule, and cancel vertical slices sharing one guarded mutation framework.
- Explicit workflow and tool invariants.
- Synthetic patient scenarios and fake scheduling state.
- Transcript, trace, and final-state evaluation.
- One demonstrated improvement cycle.

### Non-goals

- Real patient data.
- Production clinical advice.
- Full EHR integration.
- Broad specialty coverage.
- Autonomous production self-modification.

## Decisions already made

| Decision | Rationale | Date | Revisit when |
|---|---|---|---|
| Design from patient outcomes backward | UX and terminal states should determine architecture | 2026-10-04 | Core scenarios expose a missing outcome |
| Separate offline learning from runtime safety | Post-run learning cannot undo live harm | 2026-10-04 | Never at principle level |
| Preload stable context; live-check volatile state | Balances voice latency with correctness | 2026-10-04 | Real integration exposes different consistency guarantees |
| Use GPT-Live for spoken interaction and GPT-6 Sol with low reasoning effort for delegated reasoning | Separates responsive conversation from deeper tool-oriented reasoning while staying within one provider stack | 2026-10-05 | The bounded spike misses safety or latency gates |
| Keep the Python control plane as sole authority for permissions, state, tools, and final claims | Models may propose; only deterministic application guards may authorize consequential behavior | 2026-10-05 | Never at principle level |
| Use a small explicit Python state machine first | The workflow is bounded and legibility matters more than framework breadth | 2026-10-05 | Concrete complexity makes a framework demonstrably simpler |
| Use Twilio first behind a transport adapter; keep `gpt-realtime-2.1` as voice fallback | RiderPal proves Twilio mechanics and the fallback preserves the same application contracts | 2026-10-05 | GPT-Live passes the spike or Twilio access blocks the demo |
| Defer Terra; compare Luna only after stable safety parity | Terra is a weaker cost/value fit and premature model benchmarking threatens the weekend scope | 2026-10-05 | Sol-low passes all gates but materially misses the cost target |
| Add `edit_appointment` for rescheduling and `delete_appointment` for cancellation, followed by front-desk follow-up | Explicit user decision; distinct mutations need distinct guards and audit trails | 2026-10-05 | Clinic integration exposes different domain operations |
| Treat safety violations as hard gates | Aggregate scores can hide unacceptable failures | 2026-10-04 | Clinic policy changes |
| Optimize for one complete vertical slice | Six-to-eight-hour ceiling rewards depth and closure | 2026-10-04 | Required slice is complete early |
| Treat RiderPal as reference transport, not reusable domain architecture | It proves the audio relay but combines transport, prompt, live context, and startup | 2026-10-04 | A focused extraction shows direct reuse is lower risk |
| Keep telephony provider behind an adapter | Recalled Plivo implementation and public Twilio implementation differ | 2026-10-04 | Final demo provider is selected |
| Use one actor per call with supervisor-owned lifecycle | Serializes mutable session state while allowing bounded I/O concurrency | 2026-10-05 | Load or runtime evidence shows a simpler reducer is sufficient |
| Use Python 3.11+ for all implementation | Explicit user constraint; one runtime reduces setup and debugging surface | 2026-10-05 | Only an explicit user reversal changes this constraint |
| Demonstrate inbound voice and defer full outbound mechanics | The task requires real conversation, not both call directions | 2026-10-05 | Submission rubric or demo need requires outbound |

## Assumptions and unresolved questions

| Item | Type | Confidence | Impact if wrong | How to resolve |
|---|---|---:|---|---|
| Constitution governs agent behavior, tool authority, evals, and change control | Assumption | Medium | High | User confirmation |
| Prototype can use synthetic patient and clinic data | Assumption | High | High | Confirm before implementation |
| Phone match is not sufficient authentication | Assumption | High | Medium | Select simulated verification policy |
| A custom state machine remains clearer than LangGraph for the initial slice | Assumption | High | Medium | Reassess after the executable graph exists |
| Inbound booking is the primary demo | Assumption | Medium | Medium | Confirm desired call direction |
| Telephony remains outside the deterministic source-of-truth eval core | Decision | High | Medium | Add a voice/delegation overlay rather than replacing deterministic tests |
| The public Twilio code may differ from the recalled Plivo version | Question | — | Medium | User clarification or link to the Plivo version |
| GPT-Live delegation will satisfy the required event and cancellation semantics within 60 minutes | Assumption | Medium | Medium | Run the bounded spike; fall back immediately if a gate fails |

## Workstreams and dependencies

| Workstream | Desired outcome | Dependencies | Completion signal |
|---|---|---|---|
| Product contract | Happy path, outcomes, edges, hard gates | User scope choices | Scenario catalog approved |
| Constitution | Stable governing rules | Meaning and scope confirmed | v1.0 accepted |
| Architecture | Minimal components and contracts | Product contract | State and tool interfaces defined |
| Evaluation | Deterministic and qualitative rubric | Constitution and scenarios | Baseline score reproducible |
| Improvement loop | Failure-to-change-to-regression cycle | Evaluation harness | Before/after run passes gates |
| Submission | README, design note, recording | Runnable project | Checklist complete |
| Call mechanics | Minimal telephony and streaming boundary | Control-plane interfaces | `workstreams/CALL_MECHANICS.md` recommendation |
| Voice-stack selection | GPT-Live/Sol-low recommendation, fallback, and cost model | Call and control requirements | Decision integrated; bounded spike remains |

## Now / Next / Later

### Now

- Freeze minimum synthetic clinic, identity, and urgent-handoff policies.
- Convert the control-plane draft into typed schemas and executable transitions.
- Implement the deterministic fake scheduler and source-of-truth eval path for create, edit, delete/cancel, and post-cancellation follow-up.

### Next

- Run the 60-minute GPT-Live → Sol-low delegation spike against a stub read tool and the real event contracts.
- Keep GPT-Live if every integration gate passes; otherwise switch the `VoiceEngine` adapter to `gpt-realtime-2.1`.
- Build the baseline failure, controlled improvement artifact, and full regression rerun.

### Later

- Connect the proven control plane to the Twilio adapter for one inbound demonstration.
- Record the walkthrough.
- Tighten the one-page design note.

## Risks and controls

| Risk | Likelihood | Impact | Early signal | Mitigation |
|---|---:|---:|---|---|
| Too much framework plumbing | Medium | High | No end-to-end run early | Use the explicit state machine and one Python process first |
| Prompt-only safety | Medium | High | Model occasionally crosses a hard gate | Enforce permissions at workflow and tool boundaries |
| Transcript judge misses real errors | High | High | Fluent transcript with wrong database state | Assert tool trace and final state deterministically |
| Improvement overfits one scenario | Medium | High | Target passes while previous cases fail | Full regression suite and severity gates |
| Voice integration consumes the weekend | Medium | Medium | GPT-Live spike exceeds 60 minutes or lacks required event control | Stop at the time box and use the Realtime adapter fallback |
| Stale delegated work survives a correction or escalation | Medium | High | Old result is spoken or triggers a write | Epoch-bind every delegation; cancel when possible and always suppress stale results locally |
| Twilio trial blocks `<Stream>` or `<ConversationRelay>` | High on a current free trial | Medium | Minimal media-stream attempt is stripped or rejected | Spend only 10 minutes checking; use synthetic/browser audio and preserve the adapter; never spend without authorization |

## Completion criteria for this phase

- README reflects the conversation and original exercise.
- Constitution draft has a clear definition and amendment model.
- User confirms or corrects the constitution's scope.
- Open assumptions are narrowed enough to begin implementation.

## Cold-pickup handoff

- **Last completed action:** Added typed edit/cancel/follow-up contracts to the GPT-Live → Sol-low architecture; all 45 tests pass.
- **Current working state:** Voice roles, fallback, delegation invalidation, and eval implications are fixed; clinic policy and scheduling control-plane implementation remain next.
- **Exact next action:** Select the synthetic clinic and identity policy, then encode the guarded scheduling state and tool contracts in Python.
- **Open these files first:** `README.md`, `docs/IMPLEMENTATION_PLAN.md`, `docs/AGENT_CONTROL_PLANE.md`, `docs/AGENT_CONSTITUTION.md`, `docs/MISSION_PLAN.md`.
- **Do not repeat:** The distinction among offline evaluation, live protection, and post-run improvement is already settled.
- **Context that must not be lost:** RyderPal's preloaded-context insight is retained, but live state and sensitive actions require stricter validation.
- **Active intent thread and why now:** Produce a high-judgment, weekend-scale submission for 2care AI.
- **Next intent checkpoint:** After the first reproducible baseline evaluation.

## Change log

| Date | What changed | Why it changed |
|---|---|---|
| 2026-10-04 | Project and initial design documents created | Preserve decisions and establish an implementation-ready direction |
| 2026-10-04 | RiderPal repository inspected and reuse boundary documented | Replace remembered architecture with repository evidence |
| 2026-10-05 | Uncertainty matrix, workstream boundaries, and initial control plane added | Parallelize safely without fragmenting system ownership |
| 2026-10-05 | Python-only constraint applied and call-mechanics core verified with 34 tests | Keep one runtime and make concurrency assumptions executable |
| 2026-10-05 | Committed GPT-Live → GPT-6 Sol-low, Twilio adapter, Realtime fallback, and explicit state machine | Consolidate the runtime while keeping permissions and state deterministic |
| 2026-10-05 | Delegation lifecycle and stale-result safety propagated across control plane, call mechanics, and harness | Prevent late model work from becoming speech or side effects after the caller changes course |
