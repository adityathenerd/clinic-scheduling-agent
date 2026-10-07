# Persistence Architecture

Status: implemented prototype  
Last updated: 2026-10-05  
Constraint: Python 3.11+ only

## Decision

Use SQLite for authoritative synthetic clinic state and TinyDB for append-oriented conversation documents. Link the stores only with an opaque `session_id`. The Python appointment harness remains the scheduling authority. The transcript cannot change an appointment; the localhost clinic portal may invoke one narrow, CSRF-token-protected proposal-confirmation command when explicitly enabled.

This split is deliberate:

- appointments, slots, patient/provider relationships, versions, proposals, idempotency, and handoffs require relational constraints and transactional writes;
- conversation turns, state transitions, and tool events evolve as heterogeneous documents and benefit from an append-oriented shape; and
- a conversation logging failure must not block or undo a scheduling or handoff operation.

SQLite and TinyDB are local, inspectable, Python-native, and require no service provisioning. That is appropriate for a one-weekend artifact and keeps the operational blast radius legible. PostgreSQL plus a managed document/event system would be the likely production direction, but would add deployment work without improving the core design proof.

## Ownership

```text
Patient / voice or text adapter
              |
              v
       Python state machine
          |           |
          |           +---- best-effort events ----> TinyDB
          v                                      conversation_events
 AppointmentHarness
          |
          +---- authoritative snapshot ----------> SQLite
                                                    clinic.db
```

SQLite owns patients, authorized callers, appointment types, providers, provider capabilities and locations, slots, appointments, workflow sessions, confirmed patient proposals, idempotency results, handoffs, durable outbound confirmation-call jobs, and the domain audit log.

TinyDB owns spoken/text turns and observational runtime events. It does not own identity status, appointment truth, authorization, or mutation outcomes. Sensitive metadata keys such as date of birth, postal code, secrets, API keys, and confirmation tokens are redacted before persistence. Transcript content is synthetic in this repository; production content would require a retention policy, encryption, access control, and stronger de-identification.

## Consistency and failure behavior

There is intentionally no distributed transaction across the two files.

1. Protected scheduling and handoff operations complete against the appointment harness and SQLite path.
2. Conversation events are recorded on a best-effort basis.
3. If TinyDB cannot open or append, the session prints a warning and continues through the guarded scheduling path.
4. The dashboard is read-only by default and tolerates a missing or temporarily unreadable conversation file.
5. With `clinic_agent --serve-voice --dashboard-port 8000`, the portal shares the voice process's one appointment harness. One modal-confirmed action changes `proposed/held` to `confirmed/booked`, creates exactly one durable call job, then attempts Twilio. Provider failure records `retry_pending` and never rolls back the appointment.

This means a transcript can be incomplete while clinic state remains correct. It must never mean the opposite. A production implementation that requires guaranteed event delivery should use a transactional outbox in the relational store and asynchronously project redacted events to the conversation system.

## Schema highlights

Relational records use stable identifiers and explicit foreign keys. Slots and appointments carry optimistic versions. New appointments move through `proposed/held` and `confirmed/booked`. Outbound confirmation calls use one unique job per appointment and record pending, placed, or retry-pending state. Availability snapshots persist their original presentation order. Cancellation is a soft state change, not a row deletion. Handoffs have a kind, reason, topics, status, and timestamp. Session rows expose the current workflow and authority mode without copying transcript content.

Each TinyDB document contains:

```text
session_id
sequence
event_type
role (for a turn)
content (for a turn)
payload (redacted metadata or trace)
occurred_at
```

The pair `(session_id, sequence)` provides deterministic replay order within one conversation.

## Current prototype limitation

The appointment harness keeps its tested domain model in memory during a process and durably synchronizes it to SQLite after service operations. Reopening reconstructs the domain model from SQLite. This preserves the established deterministic fault-injection and concurrency tests while making the demo persistent, but it is not yet a direct-SQL repository for multi-process writers.

Before production or concurrent service deployment, replace full snapshot synchronization with transactional, row-level repository methods; add database-enforced uniqueness for active slot allocation; use a migration tool; and introduce the outbox described above. Those changes can occur behind the existing `AppointmentHarness` contract without changing the model prompts or tool surface.

## Run and inspect

```text
py -3.11 -m clinic_agent --serve-voice --port 8001 --dashboard-port 8000
```

The default files are `data/clinic.db` and `data/conversations.json`. Open `http://127.0.0.1:8000` for the local dashboard or `/api/snapshot` for JSON. Run `py -3.11 -m appointment_harness.web` only for a separate inspection-only portal; an actionable portal must be co-hosted with the voice process so it does not operate on a stale in-memory snapshot. Runtime data is intentionally excluded from Git.
