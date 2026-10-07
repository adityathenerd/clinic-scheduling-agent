# Appointment Harness

This package is a deterministic synthetic scheduling backend for the clinic agent and evaluation suite. It is a separate artifact from the conversational agent: it owns appointment truth, versions, idempotency, deterministic failures, and audit events, but it does not own prompts, consent inference, or conversational policy.

## Run deterministic scenarios

The minimally viable frontend is a dependency-free Python terminal dashboard:

```text
py -3.11 -m appointment_harness --scenario overview
py -3.11 -m appointment_harness --scenario book
py -3.11 -m appointment_harness --scenario reschedule
py -3.11 -m appointment_harness --scenario cancel
py -3.11 -m appointment_harness --scenario commit-timeout
py -3.11 -m appointment_harness --scenario follow-up-outage
```

Add `--json` for machine-readable output. Every scenario starts from the same seeded fixture, uses a fixed clock, and prints its append-only audit trail.

## Run its tests

```text
py -3.11 -m unittest discover -s tests/harness_backend -v
```

## Run the persisted browser dashboard

The live agent persists authoritative synthetic clinic state to SQLite and append-oriented conversation events to TinyDB. Start the read-only dashboard:

```text
py -3.11 -m appointment_harness.web
```

Open `http://127.0.0.1:8000`. Use `/api/snapshot` for JSON. The dashboard binds to localhost, masks patient phone numbers, and refreshes every three seconds.

## Boundary

```text
Agent / evaluator / terminal frontend
                 |
                 v
        AppointmentHarness service
          |       |        |
          v       v        v
    Versioned   Fault    Append-only
      store    injector    audit
          |
          v
   SQLite clinic snapshot

Conversation adapter --> TinyDB event documents
```

The Python control plane supplies server-owned session, proposal, confirmation, version, and idempotency references. Model-facing tool schemas expose only domain arguments. The harness revalidates ownership and authoritative state at the protected boundary.

## Supported behavior

- identity-bound synthetic sessions;
- availability searches with versioned snapshots;
- current appointment reads;
- create, edit/reschedule, and delete/cancel;
- soft cancellation that retains audit history;
- required post-cancellation front-desk follow-up;
- accepted, pending, and failed follow-up outcomes;
- optimistic appointment and slot versions;
- one-time confirmed proposal references;
- idempotent writes and reconciliation lookup;
- final-state verification;
- deterministic fixed time and JSON fixtures;
- append-only structured audit events; and
- thread-safe mutations for parallel evaluation calls;
- durable SQLite reopen/recovery for clinic entities and workflow sessions;
- active proxy-authority records and general front-desk handoffs; and
- TinyDB turn, transition, and tool-event documents with metadata redaction.

## Deterministic faults

Faults are scheduled by operation, boundary, kind, and occurrence:

```python
Fault(
    operation="create_appointment",
    point=FaultPoint.AFTER_COMMIT,
    kind=FaultKind.TIMEOUT,
    occurrence=1,
)
```

Implemented fault classes cover read timeout, pre-commit failure, slot race, stale availability, commit-then-timeout, final-read conflict, duplicated requests through idempotency replay, and follow-up outage.

## Frontend decision

The terminal frontend is the initial choice because it:

- stays inside the Python 3.11-only constraint;
- adds no dependency, build step, or second server;
- runs identically offline for reviewers;
- exposes the authoritative state and audit trail clearly; and
- uses the same service API as the agent and eval harness.

The browser dashboard is a read-only adapter over the persisted stores, not a second scheduling path. It is intentionally small: it demonstrates state and trace visibility without adding a frontend framework or mutation surface.

## Risk-to-test map

| Risk | Cheapest reliable test |
|---|---|
| Unauthorized patient access | Service contract test |
| Stale slot or appointment version | Unit/contract test |
| Duplicate mutation | Idempotency test |
| Commit succeeds but response is lost | Fault-injected integration test plus lookup |
| Reschedule loses replacement slot | Atomicity test preserving original appointment |
| Cancellation erases history | State and audit assertion |
| Follow-up outage undoes cancellation | Integration test across both operations |
| Parallel booking double-writes one slot | Threaded service test |
| Sensitive identity answers enter logs | Audit canary assertion |

## Deliberate non-goals

- real patient data;
- external HTTP, managed database, or EHR integration;
- model-based decisions;
- production authentication;
- clinical routing policy; and
- autonomous state repair.
