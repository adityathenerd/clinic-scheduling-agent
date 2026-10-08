# Uncertainty Matrix and Parallel Workstreams

Status: Historical planning snapshot; resolved decisions are implemented
Last updated: 2026-10-08

This matrix is preserved as the uncertainty map used before implementation. For
the current system state, use `README.md`, `docs/MISSION_PLAN.md`, the saved
evaluation reports, and the dated voice-run artifacts. Unresolved production
risks remain valid unless a later document explicitly closes them.

## Purpose

This matrix separates established decisions from unresolved questions, tacit knowledge, and residual uncertainty. It exists to:

- prevent assumptions from being presented as facts;
- assign parallel work without duplicating effort;
- expose dependencies early;
- focus the weekend on high-information decisions; and
- give the final submission an honest account of what was known, tested, inferred, and deferred.

The quadrants are mutable. New evidence should move an item to the appropriate quadrant and record why.

## Definitions

| Quadrant | Operational definition |
|---|---|
| Known knowns | Facts, requirements, or decisions supported by the assignment, repository evidence, or an explicit user decision |
| Known unknowns | Questions already identified whose answers could change the design or implementation |
| Unknown knowns | Relevant knowledge that probably already exists in code, documentation, experience, or stakeholders but has not yet been surfaced or formalized |
| Unknown unknowns | Failure modes and constraints not yet identified; these cannot honestly be enumerated, so the project defines discovery mechanisms and residual-risk zones |

## Known knowns

| Established item | Evidence | Design implication | Owner |
|---|---|---|---|
| The exercise is capped at roughly six to eight hours | Company task | Optimize for one closed vertical slice, not feature breadth | Control plane |
| The agent must hold a real multi-turn conversation and use tools | Company task | A scripted single-turn demo is insufficient | Control plane |
| The harness must cover difficult cases and show a before/after improvement | Company task | Baseline failure, reinforcement, rerun, and regression check are required | Evaluation Harness |
| A runnable repository, short recording, and one-page design note are required | Company task | Commands and demo story are first-class acceptance criteria | Control plane |
| Offline learning cannot repair harm already caused in a live call | Design decision | Separate evaluation, runtime protection, and post-run improvement loops | Control plane |
| The scheduling backend is authoritative for appointment state | Design decision | Preloaded context may guide dialogue but cannot authorize a mutation | Control plane |
| Stable context can be prefetched; volatile or consequential state must be refreshed | RyderPal insight plus clinic risk analysis | Hydrate policies and likely context, but live-check final availability and writes | Call Mechanics / Control plane |
| A matching phone number identifies a candidate record but does not authenticate the caller | Privacy decision | Do not disclose or mutate until identity and authority pass | Control plane |
| Consequential actions require exact, explicit confirmation | Constitution draft | Bind confirmation to provider, location, date, time, and appointment type | Control plane |
| Every write must be idempotent and read back before success is claimed | Architecture decision | Tool contracts need idempotency and authoritative verification | Control plane / Harness |
| Escalation and no-change are legitimate successful outcomes | Experience decision | Completion pressure must not reward unsafe guessing | Control plane / Harness |
| Transcript-only evaluation is insufficient | Evaluation decision | Inspect tool trace, workflow transitions, and final backend state | Evaluation Harness |
| Critical safety failures are hard gates, not weighted deductions | Constitution draft | One privacy or authorization failure blocks promotion | Evaluation Harness |
| Production self-modification is out of scope | Safety decision | Improvements are proposed, reviewed, versioned, rerun, and reversible | Evaluation Harness / Control plane |
| The public RiderPal repository uses Twilio Media Streams and OpenAI Realtime | Repository inspection | Treat telephony as an adapter; do not assume Plivo is canonical | Call Mechanics |
| RiderPal preloads context into a single session instruction and has no domain tool workflow | Repository inspection | Reuse the transport pattern, not the application architecture | Call Mechanics / Control plane |
| Simpler systems with fewer runtime boundaries have a smaller, more legible blast radius | Explicit user principle | Every component must justify the failure boundary it creates | All workstreams |
| This chat owns agent instructions, tools, decision graph, and state machine | Explicit user allocation | Other chats return bounded inputs; integration decisions remain here | Control plane |
| The company task requires a real multi-turn voice conversation but not both inbound and outbound telephony | Assignment reading and workstream review | Demonstrate inbound; preserve direction in contracts and defer full outbound | Call Mechanics |
| Backend session concurrency should use a lightweight supervisor plus one state-owning actor per call | Explicit user decision | Serialize per-call events while keeping bounded I/O concurrent | Call Mechanics / Control plane |
| All implementation will use Python 3.11+ | Explicit user constraint | Call mechanics, control plane, tools, harness, tests, and adapters must share one Python runtime | All workstreams |
| Python 3.11 is available through `py`, not the ordinary `python` command, on the current Windows host | Local runtime verification | Local commands and setup instructions must account for the launcher while remaining portable | All workstreams |
| The primary voice path is Twilio → `gpt-live-1` → Responses delegation → `gpt-6-sol` with low reasoning effort | Explicit user decision plus provider research | Live handles speech, Sol proposes reasoning/tool actions, and the Python application retains authority | Voice / Call Mechanics / Control plane |
| “Sol light” means `gpt-6-sol` configured with low reasoning effort | Official model capability plus explicit decision | Record model and effort separately; do not invent a model alias | Voice / Harness |
| The GPT-Live integration gets a 60-minute validation spike, with `gpt-realtime-2.1` behind the same `VoiceEngine` contract as fallback | Architecture decision | Protects the weekend time box without redesigning the control plane | Voice / Call Mechanics |
| Terra is not part of the weekend benchmark; Luna may be a later cost challenger only after safety parity | Cost/capability decision | Do not optimize token price before measuring safe-task success | Voice / Harness |
| A small explicit Python state machine is the initial orchestration mechanism | Simplicity and blast-radius principle | LangGraph is deferred until concrete complexity justifies it | Control plane |
| The initial mutation surface includes `create_appointment`, `edit_appointment`, and `delete_appointment` | Explicit user decision | Booking, rescheduling, and cancellation receive separate typed guards and traces | Control plane / Harness |
| Cancellation requires a front-desk follow-up handoff after authoritative cancellation verification | Explicit user decision | Cancellation and follow-up are separate observable outcomes; handoff failure must not undo the cancellation | Control plane / Harness |

## Known unknowns

| Question | Why it matters | Priority | Resolution method | Owner / decision point |
|---|---|---:|---|---|
| What exact clinic, specialty, and appointment types does the prototype represent? | Determines prerequisites, routing, and safe language | High | Choose one synthetic clinic policy pack | Control plane before scenario freeze |
| What identity-verification method is sufficient for the demo? | Controls disclosure and mutation authority | High | Define synthetic policy and failure behavior | Control plane before tool contracts |
| Is simulated text sufficient for most evals while one inbound call proves real voice? | Changes demo scope without changing the core | Medium | Use text-driven deterministic suite plus one inbound voice path | Control plane after integration spike |
| Can the GPT-Live delegation path pass the 60-minute integration gates? | Determines whether the primary voice frontend or the Realtime fallback is used for the demo | High | Bounded spike with typed events, correction invalidation, tool guard, and verified-speech checks | Voice / Call Mechanics |
| What latency and interruption behavior appears only with real delegated audio? | Text/control-plane tests cannot reproduce playback and barge-in timing | Medium | One inbound voice scenario plus event-aligned timing trace | Call Mechanics / Harness |
| What exact tool schema does the scheduling backend expose? | Guards must be enforceable at tool boundaries | High | Define a fake scheduler contract first | Control plane / Harness |
| Does the backend support slot holds or atomic booking? | Determines race-condition behavior | Medium | Model both; choose simplest honest demo behavior | Control plane |
| What urgent-symptom policy and handoff language does the simulated clinic approve? | The agent must not invent clinical triage | High | Define an explicit synthetic escalation policy | Control plane before prompt finalization |
| How much patient context is minimum necessary? | Too little hurts completion; too much expands privacy risk | High | Field-by-field data minimization review | Control plane |
| Which initial failure best demonstrates meaningful improvement? | The demo must be visible, consequential, and repairable | High | Select after scenario and guard design | Evaluation Harness |
| How are candidate improvements generated and applied? | Prompt-only patching may be weak or irreproducible | High | Define typed patch categories and controlled promotion | Evaluation Harness |
| What regression threshold is acceptable for non-critical conversational metrics? | “No regressions” requires an operational definition | Medium | Define hard gates plus bounded soft-score tolerance | Evaluation Harness |
| Will the evaluator have network access and provider credentials? | Affects reproducibility for reviewers | High | Provide deterministic offline path; make live voice optional | Call Mechanics / Harness |
| What minimum payload and service expectation should post-cancellation follow-up use? | Determines privacy exposure and whether the patient can be promised a callback | High | Define a synthetic front-desk contract before implementing the handoff | Control plane |
| Should the constitution govern only runtime behavior or also contributor/coding-agent rules? | Changes the document's authority and scope | Medium | User confirmation | Control plane before constitution v1.0 |
| Is there a separate Plivo version of RiderPal? | May change reuse options but not the core boundary | Low | User clarification if relevant | Call Mechanics |

## Unknown knowns

These are not facts yet. They are likely sources of already-existing knowledge that should be mined before inventing new mechanisms.

| Likely hidden knowledge | Where it may exist | Why surface it | Extraction action | Owner |
|---|---|---|---|---|
| Lessons from the original RiderPal build about latency, VAD, interruptions, and provider behavior | User experience, unpushed code, other branches, demo recordings | Could avoid repeating integration mistakes | Short structured debrief; inspect additional artifacts only if supplied | Call Mechanics |
| Real front-desk exception patterns and escalation heuristics | 2care product behavior, clinic staff knowledge, public product material | Determines whether scenarios are realistic | Derive a bounded synthetic clinic model; label unsourced assumptions | Control plane / Harness |
| Provider-specific instruction-adherence and tool-calling limitations | Current vendor documentation and examples | May eliminate an apparently attractive stack | Primary-source research plus tiny spike | Voice Stack Selection |
| Existing reusable test or simulation patterns | Installed libraries, reference repositories, SDK examples | Could reduce implementation time | Inventory only what the chosen architecture needs | Evaluation Harness |
| Hidden assumptions embedded in RiderPal code | Source, configuration, and Git history | Prevents importing brittle coupling or secrets | Architecture and security inspection | Call Mechanics |
| The user's available accounts, credits, phone numbers, and regional access | User environment | Determines what can be demonstrated live | Ask only after a preferred stack is identified | Voice Stack Selection |
| Reviewer expectations beyond the literal task | Company context, role description, interview signals | Helps prioritize judgment signals | Treat as hypotheses; do not overfit without evidence | Control plane |
| Tacit interpretation of “self-improvement” | User and company language | Prevents mismatched demonstration | Keep controlled improvement definition explicit | Control plane / Harness |

## Unknown unknowns

A list of “unknown unknowns” becomes a list of known unknowns as soon as it is written. The honest artifact is therefore a map of residual-risk zones and mechanisms intended to discover surprises.

| Residual-risk zone | Discovery mechanism | Signal that creates a new known unknown | Owner |
|---|---|---|---|
| Voice recognition of names, dates, accents, code-switching, and noisy audio | Perturbed audio fixtures, paraphrases, interruption tests | Entity disagreement or repeated repair loop | Call Mechanics / Harness |
| Concurrency and partial failure | Fault injection at every write boundary; duplicate/reordered events | Final state differs from confirmed intent | Harness |
| Prompt or tool manipulation by a caller | Adversarial utterances and invalid tool-argument tests | Attempted policy bypass or sensitive disclosure | Harness / Control plane |
| Provider behavior changes | Adapter contract tests and pinned version/config records | Same scenario changes after provider update | Voice Stack Selection / Call Mechanics |
| Delegated work completes after the caller has corrected, escalated, or disconnected | Reordered-event and delayed-result fault injection | Old result is spoken or causes a write in a newer workflow epoch | Call Mechanics / Harness |
| Unmodeled clinic policy conflict | Scenario review against the synthetic policy pack | Agent reaches a state with no permitted transition | Control plane |
| Overfitting the visible evaluation suite | Holdout scenarios and metamorphic variants | Patch fixes one wording but fails equivalent intent | Harness |
| Observability itself leaking sensitive data | Log inspection with seeded canary fields | Canary appears in an unauthorized trace or report | Harness / Call Mechanics |
| Latency interactions across components | Per-stage timing and end-to-end percentile measurement | Tail latency breaks turn-taking despite acceptable averages | Call Mechanics |
| Human escalation failure | Simulated unavailable/failed handoff | Agent loops, invents success, or abandons caller | Call Mechanics / Control plane |

## Parallel workstreams

| Workstream | Owns | Must not own | Required return artifact | Completion signal |
|---|---|---|---|---|
| Control plane — this chat | Constitution, instruction hierarchy, tools, decision graph, state machine, integration decisions | Vendor-specific transport implementation | `AGENT_CONTROL_PLANE.md` plus shared decision updates | Executable contracts and invariants are ready for implementation |
| Call Mechanics | Telephony lifecycle, streaming, interruptions, latency, provider adapter, transport failures | Scheduling policy or eval scoring | `workstreams/CALL_MECHANICS.md` | Minimal call boundary and latency/failure design recommended |
| Evaluation Harness | Scenario model, fake backend, traces, assertions, judge, scoring, improvement promotion | Voice-provider selection | `workstreams/EVALUATION_HARNESS.md` | Reproducible before/after loop specified |
| Voice Stack Selection | Current provider evidence, tradeoffs, validation spike, stack recommendation | Clinic workflow design | `workstreams/VOICE_STACK_SELECTION.md` | One preferred stack and fallback justified |

## Integration contract for every workstream

Each parallel chat must return:

1. a recommendation;
2. evidence and assumptions;
3. rejected alternatives and the condition that would revive them;
4. explicit interfaces with the control plane;
5. failure behavior and blast radius;
6. the smallest validation step;
7. unresolved questions; and
8. no edits to shared decision documents until reintegration here.

Every workstream must use Python 3.11+ for implementation recommendations and examples. Introducing another application runtime requires an explicit reversal of this project constraint, not a local convenience choice.

## Current convergence priorities

1. Freeze one synthetic clinic policy and identity model.
2. Freeze the create, edit/reschedule, delete/cancel, and follow-up handoff schemas.
3. Make the state machine and hard guards executable without voice.
4. Align the harness event schema with state transitions and tool contracts.
5. Run the bounded GPT-Live spike against the stable `VoiceEngine` and control-plane contracts.
6. Demonstrate one meaningful failure, reinforcement, and full regression rerun.
