# Agent Control Plane

Status: Implemented prototype; retained as the authoritative control-plane contract
Owner: Primary design chat
Last updated: 2026-10-08

Implementation constraint: Python 3.11+ only. On the current Windows host, use the `py` launcher.

## Purpose

This document defines the stable core of the clinic scheduling agent: instruction precedence, permitted tools, decision logic, state ownership, transition guards, and failure behavior. Voice and telephony are adapters around this core.

## Boundary model

```text
Caller / simulator
      |
      v
Twilio adapter / text adapter  ---- transport events ----> Event recorder
      |
      v
GPT-Live conversation frontend ---- Responses delegation ----> GPT-6 Sol (low)
      |                                                          |
      +---------------- typed events / proposals -----------------+
                                 |
                                 v
Python agent control plane <---- structured context ---- Context hydrator
      |
      +---- guarded reads/writes ----> Scheduling gateway ----> Fake or real backend
      |
      +---- escalation request ------> Human-handoff adapter
```

The runtime has three distinct authorities:

- **GPT-Live** manages spoken turn-taking, interruption, clarification, and concise delivery.
- **GPT-6 Sol at low reasoning effort** performs delegated reasoning and proposes typed workflow or tool actions.
- **The Python control plane** is the sole authority for workflow state, permissions, confirmation binding, tool execution, stale-work rejection, and final-state claims.

Neither model can grant itself tool authority. The scheduling gateway executes only operations permitted by the control plane, and the scheduling backend—not either model—owns appointment truth. GPT-Live must wait for verified application state before saying availability is current or an appointment action succeeded.

## Runtime ownership and concurrency

The process uses one lightweight actor per call/session. The actor exclusively owns the mutable `SessionState` and serializes all patient turns, model events, tool results, interruption signals, and disconnect events through one queue.

I/O workers may operate concurrently but return typed immutable results. They may not mutate session state or call scheduling tools outside the actor's guarded command path. A small supervisor creates, locates, expires, and removes actors; it never owns clinical or appointment state.

Required concurrency invariants:

- no state crosses call identifiers;
- one call's events are reduced serially;
- late results cannot mutate a terminal session;
- late delegated results cannot mutate or be spoken after their workflow epoch becomes stale;
- duplicate lifecycle events are idempotent;
- a session closes once;
- write results always return to the actor that issued the correlated command; and
- one failed or overloaded call cannot block or corrupt another.

## Instruction precedence

Higher levels cannot be overridden by lower levels:

1. **Agent constitution:** safety, privacy, authority, truthful state, and promotion rules.
2. **Clinic policy pack:** supported appointment types, identity policy, prerequisites, escalation destinations, and approved urgent-language policy.
3. **Tool contracts and workflow guards:** machine-enforced preconditions and schemas.
4. **Session context:** minimum-necessary patient, referral, provider, and clinic context.
5. **Current authoritative tool results:** availability, appointment state, and mutation outcomes.
6. **Patient request and preferences:** the caller's goal, corrections, constraints, and consent.
7. **Conversational style:** tone, brevity, and phrasing.

A patient utterance may change intent or preferences but cannot waive identity, privacy, consent, or tool guards.

## Proposed minimum tool surface

The first vertical slice should expose the fewest tools that close the booking loop.

| Tool | Class | Purpose | Required guard | Side effect |
|---|---|---|---|---|
| `interpret_identity_response` | Semantic/control | Classify the latest answer as `affirmed`, `denied`, or `unclear` using a strict Responses tool schema | Active identity question and unconsumed caller turn | Records semantic evidence only; cannot authorize itself |
| `search_clinic_faqs` | Public read | Search source-labelled clinic information about insurance, services, providers, visit logistics, location, and medicines | Administrative questions only; no identity required | None |
| `verify_identity` | Policy/read | Verify synthetic identity factors and caller authority | Candidate record exists; attempt limit not exceeded | Records verification result only |
| `check_eligibility` | Read | Check appointment type, provider fit, and administrative prerequisites | Identity verified; intent minimally specified | None |
| `search_slots` | Read | Return fresh slots matching bounded preferences | Identity verified; eligibility known | None |
| `get_appointment` | Read | Load the authoritative current appointment and version | Identity and caller authority verified; appointment reference available | None |
| `create_appointment` | Write | Commit the exact confirmed appointment | Fresh slot; exact confirmation token; idempotency key | Creates appointment |
| `edit_appointment` | Write | Reschedule one appointment to the exact confirmed replacement slot | Current appointment/version; fresh replacement slot; exact confirmation token; idempotency key | Updates appointment |
| `delete_appointment` | Write | Cancel one appointment while retaining its audit record | Current appointment/version; exact cancellation confirmation; idempotency key | Marks appointment cancelled |
| `escalate_to_front_desk` | Handoff | Create a structured handoff request | Escalation reason and minimum context | Handoff request |

Optional only if the fake backend models them honestly:

- `hold_slot` for a short-lived transactional hold; and
- `send_confirmation` after final-state verification.

The system should not expose one generic “manage appointment” tool. Narrow tools make authorization, evaluation, and blast radius legible.

`search_clinic_faqs` is deliberately separate from patient and scheduling
state. It can answer public questions before identity verification, but it
cannot read a patient record or grant tool authority. Returned entries carry a
source label, effective date, and review date. Provider-directory information
may describe credentials and joining dates; actual appointment times still
come only from `search_slots`. Service descriptions must not become
personalized treatment advice, and medicine-policy answers must not become
dosing, interaction, substitution, or stopping advice.

### Semantic identity lock

The real voice path does not use an expanding phrase allowlist to understand
identity answers. While the state is `IDENTITY_PENDING`, the Responses backend
must call `interpret_identity_response` with a strict enum:

```text
decision = affirmed | denied | unclear
```

That call is an interpretation of the caller's latest turn, not an identity
credential. The Python control plane validates the current state and unconsumed
turn, asks the clinic-policy backend to authorize the low-assurance demo check,
then mints an opaque `identity_confirmation_key`. It also stores a SHA-256
digest of the exact transcript turn as evidence. The model receives the key as
a result but no tool accepts a replacement key from the model.

Within a call, the key is write-once. A repeated or conflicting semantic tool
call is rejected and cannot overwrite it. Production persistence should enforce
the same invariant with a unique `(session_id, confirmation_kind)` insert; an
identity dispute should append a revocation/recovery event rather than mutate
the original confirmation record.

`delete_appointment` is named to match the requested tool surface, but it performs a domain cancellation rather than hard-deleting the record. After the cancelled state is verified, the control plane must invoke `escalate_to_front_desk` with reason `post_cancellation_follow_up`. A failed handoff does not roll the cancellation back; it produces `CANCELLED_FOLLOW_UP_PENDING` and a truthful patient explanation.

### Mutation tool contracts

`edit_appointment` accepts:

```text
session_id
patient_id
appointment_id
expected_appointment_version
replacement_slot_id
availability_snapshot_id
proposal_id
confirmation_token
idempotency_key
```

It returns a typed outcome: `updated`, `conflict`, `rejected`, or `unknown`. An update must be atomic: if the replacement slot cannot be committed, the original appointment remains unchanged. Success is provisional until `get_appointment` returns the expected new version and replacement slot.

`delete_appointment` accepts:

```text
session_id
patient_id
appointment_id
expected_appointment_version
proposal_id
confirmation_token
idempotency_key
```

It returns `cancelled`, `conflict`, `rejected`, or `unknown`. `cancelled` retains the appointment identifier, prior details, cancellation timestamp, actor, and mutation correlation in the audit record. Success is provisional until `get_appointment` returns `status=cancelled` at the expected new version.

The post-cancellation `escalate_to_front_desk` call contains only:

```text
session_id
patient_id
cancelled_appointment_id
reason = post_cancellation_follow_up
follow_up_topics[]
idempotency_key
```

It must not include the full transcript. Its typed outcome is `accepted`, `pending`, or `failed`; both `pending` and `failed` map to the visible workflow state `CANCELLED_FOLLOW_UP_PENDING` until reconciled.

Shared mutation invariants:

- the selected appointment belongs to the verified patient or authorized caller;
- the supplied appointment version matches the authoritative current version;
- the one-time confirmation token matches the operation-specific proposal digest;
- replaying an idempotency key cannot apply a second mutation or create a second handoff;
- any material patient correction invalidates the proposal and confirmation; and
- a model can propose arguments, but only the Python guard may issue the tool command.

## Session state

The control plane owns a typed session record rather than relying on conversational memory alone.

```text
session_id
call_id
candidate_patient_id
identity_status
identity_confirmation_key
identity_evidence_digest
caller_authority
correction_epoch
active_delegation_id
delegation_status
backend_response_id
intent
appointment_type
selected_appointment_id
selected_appointment_version
eligibility_status
prerequisite_status
preferences
availability_snapshot_id
proposed_slot
active_proposal_id
proposal_version
confirmation_token
mutation_idempotency_key
committed_appointment_id
verification_status
follow_up_handoff_id
follow_up_status
escalation_reason
terminal_outcome
```

Sensitive values should be minimized or tokenized in traces. Every field records provenance: patient statement, preloaded context, clinic policy, or authoritative tool result.

### Delegation correlation record

Every backend job is issued with an immutable correlation envelope:

```text
session_id
delegation_id
backend_response_id
correction_epoch
workflow_state
active_proposal_id
proposal_version
confirmation_token_id (if any)
requested_capability
issued_at_monotonic
```

A result is actionable only when the session is still nonterminal and its epoch, proposal, and authorization references match current state. Otherwise the result is classified as `stale`, retained for audit/reconciliation, and barred from tools and patient-facing speech.

## Primary state machine

```text
START
  -> CONTEXT_READY
  -> IDENTITY_PENDING
  -> IDENTITY_VERIFIED
  -> INTENT_KNOWN
  -> ELIGIBILITY_KNOWN
  -> PREFERENCES_KNOWN
  -> AVAILABILITY_FRESH
  -> SLOT_PROPOSED
  -> CONFIRMATION_PENDING
  -> ACTION_AUTHORIZED
  -> MUTATION_SUBMITTED
  -> FINAL_STATE_VERIFIED
  -> COMPLETED
```

Rescheduling and cancellation reuse the same guarded spine with operation-specific branches:

```text
INTENT_KNOWN(reschedule)
  -> EXISTING_APPOINTMENT_VERIFIED
  -> PREFERENCES_KNOWN
  -> AVAILABILITY_FRESH
  -> SLOT_PROPOSED
  -> ACTION_AUTHORIZED
  -> EDIT_SUBMITTED
  -> FINAL_STATE_VERIFIED
  -> COMPLETED

INTENT_KNOWN(cancel)
  -> EXISTING_APPOINTMENT_VERIFIED
  -> CANCELLATION_PROPOSED
  -> ACTION_AUTHORIZED
  -> DELETE_SUBMITTED
  -> CANCELLATION_VERIFIED
  -> FOLLOW_UP_HANDOFF_SUBMITTED
  -> CANCELLED_WITH_FOLLOW_UP
```

Valid terminal branches from multiple states:

```text
ESCALATED
NO_CHANGE
URGENT_HANDOFF
CANCELLED_WITH_FOLLOW_UP
CANCELLED_FOLLOW_UP_PENDING
CALL_ENDED
```

`CALL_ENDED` is not automatically success or failure. The harness determines whether the system left a committed or uncertain mutation and whether recovery is required.

## Transition guards

| Transition | Required evidence | Failure branch |
|---|---|---|
| `CONTEXT_READY -> IDENTITY_PENDING` | Candidate context loaded without disclosure | Continue with generic greeting if no candidate exists |
| `IDENTITY_PENDING -> IDENTITY_VERIFIED` | Clinic-approved identity factors and caller authority | Retry within limit, then `NO_CHANGE` or `ESCALATED` |
| `IDENTITY_VERIFIED -> INTENT_KNOWN` | Administrative scheduling intent captured | Clarify or escalate unsupported request |
| `INTENT_KNOWN -> ELIGIBILITY_KNOWN` | Supported appointment type and prerequisites evaluated | `ESCALATED` if policy requires judgment |
| `ELIGIBILITY_KNOWN -> PREFERENCES_KNOWN` | Enough constraints to search efficiently | Ask one bounded preference question |
| `PREFERENCES_KNOWN -> AVAILABILITY_FRESH` | Live search result tied to query and timestamp/version | Retry read or escalate on unavailable backend |
| `AVAILABILITY_FRESH -> SLOT_PROPOSED` | Slot satisfies eligibility and preferences | Offer alternatives or waitlist path |
| `SLOT_PROPOSED -> ACTION_AUTHORIZED` | Exact explicit confirmation bound to unchanged proposal | Stay pending; changed proposal invalidates confirmation |
| `ACTION_AUTHORIZED -> MUTATION_SUBMITTED` | Valid confirmation token and idempotency key | Block write and record guard failure |
| `MUTATION_SUBMITTED -> FINAL_STATE_VERIFIED` | Authoritative read matches confirmed intent | Re-read, reconcile, or escalate unknown result |
| `FINAL_STATE_VERIFIED -> COMPLETED` | Patient receives truthful summary | End successfully |
| `INTENT_KNOWN(reschedule) -> EXISTING_APPOINTMENT_VERIFIED` | Authoritative appointment belongs to the verified patient; version captured | Clarify selection, make no change, or escalate |
| `ACTION_AUTHORIZED -> EDIT_SUBMITTED` | Confirmation binds current appointment/version and fresh replacement slot | Reject stale appointment or slot; re-read and reconfirm |
| `EDIT_SUBMITTED -> FINAL_STATE_VERIFIED` | Authoritative appointment matches the confirmed replacement slot | Reconcile by idempotency key; never claim success early |
| `INTENT_KNOWN(cancel) -> EXISTING_APPOINTMENT_VERIFIED` | Authoritative appointment belongs to the verified patient; version captured | Clarify selection, make no change, or escalate |
| `EXISTING_APPOINTMENT_VERIFIED -> CANCELLATION_PROPOSED` | Exact appointment and material cancellation consequences stated | Remain pending until understood |
| `ACTION_AUTHORIZED -> DELETE_SUBMITTED` | Confirmation binds appointment/version and cancellation consequences | Reject stale version or mismatched token |
| `DELETE_SUBMITTED -> CANCELLATION_VERIFIED` | Authoritative state is cancelled and audit record remains | Reconcile by idempotency key; never repeat blindly |
| `CANCELLATION_VERIFIED -> FOLLOW_UP_HANDOFF_SUBMITTED` | Minimum-necessary follow-up payload created | Record follow-up pending; cancellation remains valid |
| `FOLLOW_UP_HANDOFF_SUBMITTED -> CANCELLED_WITH_FOLLOW_UP` | Handoff receipt is recorded | Use `CANCELLED_FOLLOW_UP_PENDING` if the handoff fails |

## Decision graph

At every patient turn or significant tool event, evaluate in this order:

1. **Potential urgency?** Stop scheduling dialogue and follow the approved urgent-handoff policy.
2. **Human requested?** Escalate without pressuring the caller to continue automation.
3. **Unsupported clinical judgment requested?** State the administrative boundary and escalate if needed.
4. **Identity or caller authority insufficient for the next disclosure/action?** Verify or make no change.
5. **Intent unclear or changed?** Clarify and invalidate downstream proposals or confirmation as necessary.
6. **Existing appointment mutation?** Fetch and version the exact appointment before discussing a reschedule or cancellation.
7. **Eligibility or prerequisites unresolved?** Check policy; escalate rather than infer.
8. **Preferences insufficient?** Ask the highest-information scheduling question.
9. **Availability stale or absent?** Fetch fresh authoritative slots for create or edit.
10. **No acceptable slot?** Offer bounded alternatives, waitlist, or front-desk handoff.
11. **Exact proposal not confirmed?** Read back material details and consequences, then request confirmation.
12. **Write permitted?** Execute the operation-specific tool once with version and idempotency protection.
13. **Final state verified?** Continue only from authoritative state; otherwise reconcile or escalate.
14. **Cancellation verified?** Create the required front-desk follow-up handoff, then explain whether it was accepted or remains pending.

Safety and state integrity checks precede task progress. Conversational polish never changes the ordering.

## Invalidation rules

The following events invalidate previously derived state:

- Patient changes provider, location, appointment type, date, or time preference: invalidate proposed slot and confirmation.
- Patient correction: increment `correction_epoch`, cancel the current delegation when possible, and reject any later output tied to the old epoch.
- A newer proposal is created: increment `proposal_version`; invalidate older proposal references, confirmation, and dependent delegations.
- Patient asks for a human or triggers urgent handling: terminate the scheduling path and invalidate every in-flight scheduling delegation.
- Availability version changes or slot expires: invalidate proposal and confirmation.
- Identity or caller authority becomes disputed: invalidate all permission to disclose or write.
- Tool returns conflicting patient or appointment identifiers: halt mutations and escalate.
- Call reconnects without trusted session continuity: restore only server-owned state and reconfirm before writes.
- Policy pack changes mid-session: block affected transition and require reevaluation.

## Error and recovery behavior

| Failure | Required behavior |
|---|---|
| Read timeout | Retry within a bounded policy; never invent the result |
| Write rejected before commit | Explain that no change was made; refresh options |
| Write response lost or outcome unknown | Do not retry blindly; query by idempotency key or reconcile through authoritative read |
| Final read disagrees with confirmed intent | Stop, disclose the discrepancy, and offer a confirmed repair or escalation |
| Reschedule loses a slot race | Preserve the original appointment, invalidate confirmation, refresh alternatives, and reconfirm |
| Cancellation succeeds but follow-up handoff fails | Keep the verified cancellation; record `CANCELLED_FOLLOW_UP_PENDING` and state the follow-up uncertainty truthfully |
| Caller disconnects before write | No mutation |
| Caller disconnects after uncertain write | Record recovery-needed state; reconcile before any later claim |
| Repeated misunderstanding | Offer human handoff; do not loop indefinitely |
| Urgent symptom signal | Use only the approved handoff language; do not diagnose |
| Delegated response arrives after correction | Record as stale; do not speak it or execute its proposed tool call |
| Delegated response arrives after escalation or terminal outcome | Record for audit only; never mutate or resume automation |
| Delegation cannot be cancelled | Advance the epoch and suppress its eventual result locally |
| GPT-Live speaks an unverified success claim | Mark a critical runtime/eval failure and immediately correct the record if the call remains active |

## Observability contract

Each event should include:

- event type;
- session and scenario identifiers;
- frontend session, delegation, backend response, proposal, confirmation, and correction-epoch identifiers;
- previous and next workflow state;
- redacted input provenance;
- tool name and outcome class;
- guard decision and violated invariant, if any;
- latency by stage;
- model identifier and reasoning-effort configuration;
- idempotency correlation for writes; and
- terminal outcome.

Ordinary logs must not include raw credentials, full prompts, unrestricted transcripts, or unnecessary patient data.

## Decisions still required here

1. Select one synthetic clinic and appointment type.
2. Define the prototype identity-verification policy.
3. Define the approved urgent-handoff policy.
4. Define the minimum post-cancellation handoff payload and simulated follow-up expectation.
5. Decide whether a slot hold is real in the fake backend or omitted.
6. Convert this draft into typed schemas and an executable transition table.
7. Draft the runtime system instruction only after these policy decisions are fixed.

## Acceptance criteria for the control plane

- Every tool call is reachable only through an explicit guarded transition.
- Every consequential claim is grounded in current tool state.
- Every delegated result is correlated to the active workflow epoch before it can be spoken or acted upon.
- Confirmation is invalidated when material details change.
- Every partial failure has a defined recovery or terminal branch.
- The state machine can be exercised without telephony or a live model.
- The evaluation harness can assert transitions and invariants deterministically.
- Voice-provider replacement does not change clinic policy or tool authority.
- Parallel-call and event-reordering tests prove actor isolation and serialized state transitions.
- Corrections, human requests, and urgency escalation suppress stale in-flight backend work.
