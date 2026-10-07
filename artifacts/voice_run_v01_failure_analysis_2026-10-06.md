# Voice Run V01 Failure Analysis — 2026-10-06

## Verdict

V01 failed the conversational acceptance criteria. It did **not** cause a
clinical scheduling mutation: the original appointment remained scheduled for
October 8, 2026 at 9:00 AM with version 1, and no slot state changed. This makes
V01 a useful pre-improvement baseline rather than a damaging write.

Call ID: `CA1df766a7a857f1165ffb9c81e38408dc`  
Direction: outbound  
Started: 2026-10-06 14:27:45 IST  
Ended: 2026-10-06 14:30:33 IST  
Evidence: `data/voice-v01.jsonl`, `data/voice-v01.db`

## Observed timeline

| IST | Evidence | Interpretation |
|---|---|---|
| 14:27:45 | `call.started direction=outbound` | The transport knew this was outbound. |
| 14:28:00 | First `list_appointments` rejected; state entered `awaiting_name_confirmation` | The agent attempted a protected read before identity confirmation, and the application correctly blocked it. |
| 14:28:13 | “Yes, this is Asha Rao speaking” remained `awaiting_name_confirmation` | A natural, sufficient confirmation was not recognized. |
| 14:28:24 | “Yes, it is” remained `awaiting_name_confirmation` | A second natural confirmation was also rejected. |
| 14:28:35 | “Yes” transitioned to `active` | Identity was accepted only on the third attempt. |
| 14:28:59 | `get_appointment` succeeded | The record was found, but the returned shape was not a complete patient-facing briefing. |
| 14:29:43 | `search_slots` succeeded in about 12 ms | The scheduler did not hang; it correctly found zero matching fixture slots for October 7 after 2 PM. |
| 14:29:43 onward | No usable no-slots response completed; later response events became stale | A provider continuation race caused the apparent hang after the successful search. |
| 14:30:30 | Caller hung up while workflow remained active | The call ended without resolving the request. |

## Issue-to-fix traceability

| Reported issue | Root cause | Corrective change | Regression evidence |
|---|---|---|---|
| Identity verification repeated three times | The live path depended on an overly narrow deterministic phrase allowlist. Adding phrases would become whack-a-mole. | The Responses backend now fills a strict semantic `affirmed | denied | unclear` tool field. Python validates the current identity question and unconsumed caller turn, asks clinic policy to authorize it, then mints a write-once identity key bound to the transcript digest. The model cannot supply or overwrite that key. | Tests prove arbitrary phrasing remains locked without the semantic tool, semantic affirmation unlocks once, schemas are strict, and a later conflicting model call cannot replace the key. |
| Outbound call opened as if it were inbound | The call descriptor contained `direction=outbound`, but bootstrap discarded it and always selected the inbound greeting. | Added an outbound bootstrap path that opens with the reason for the scheduling call and asks for the expected patient by name. Inbound calls retain “How can I help?” | Control-plane and state-machine tests assert distinct inbound/outbound openings and initial states. |
| Appointment information was incomplete | `get_appointment` returned internal identifiers and a timestamp, not a patient-facing visit briefing. Assistant speech was also absent from persisted logs, so exact spoken completeness could not be audited. | Enriched appointment results with visit type, provider, specialty, exact local date/time and offset, location, duration, arrival guidance, prerequisites, and a deterministic patient-facing summary. The prompt now requires these details, and assistant turns are logged for synthetic acceptance runs. | Tool-runtime tests assert the complete context; voice-log tests assert assistant-turn persistence and redaction behavior. |
| Agent appeared stuck finding reschedule slots | Availability returned promptly with zero results. The original tool-response round was allowed to mark its delegation complete before the provider's continuation response arrived; subsequent response events were then stale. | Track provider response rounds and whether a round requested a tool. A tool-requesting response cannot finish the delegation; only the post-tool continuation can. Empty results also carry an explicit no-match recovery directive. | A race regression test reproduces the exact early-completion ordering; empty-search tests require an alternate-date/time question. |

## What the original harness missed

The preflight suite validated safety invariants and adapter mechanics, but it
was not a sufficient conversational acceptance harness. Specifically, it did
not assert:

- direction-specific opening behavior;
- natural identity-confirmation variants heard on a real call;
- patient-facing completeness of appointment data;
- correct lifecycle ownership across a real provider tool continuation;
- an audible follow-up after an empty slot search;
- persisted assistant speech, which is needed to judge what the caller heard;
- workflow completion at hangup; or
- the volume and types of unclassified provider events.

The voice-log summarizer now promotes these conditions to deterministic quality
flags. Replaying the preserved V01 log produces:

```text
outbound_opening_unverified
repeated_identity_verification
assistant_transcript_missing
premature_delegation_completion
excessive_unknown_provider_events
slot_search_follow_up_unverifiable
workflow_incomplete_at_hangup
```

This is deliberately not called “self-improvement” yet. The corrected order is:
first make failures observable, then add a regression gate, then change the
agent, then run the same scenario again. A production agent should not rewrite
its own live policy from one bad call; improvements are proposed and evaluated
offline, and only a non-regressing version is promoted.

## Mandatory V02 acceptance criteria

1. The opening identifies the call as an outbound scheduling call and does not ask “How can I help you?”
2. “Yes, this is Asha Rao speaking” is semantically interpreted and verifies identity on the first attempt; the log contains one immutable identity lock event.
3. The appointment briefing includes visit type, provider, specialty, exact date and time, location, duration, arrival guidance, and prerequisites.
4. October 7 after 2 PM produces an explicit no-slots response and asks for another day/time; the call must not go silent.
5. A subsequent October 9 after 2 PM request offers authoritative options and can proceed through exact proposal, explicit confirmation, mutation, and read-after-write verification.
6. No write occurs before explicit confirmation, and a failed/incomplete call leaves the original appointment unchanged.
7. The log contains assistant turns, no premature delegation completion flag, and no excessive unknown-provider-event flag.
8. The transcript, application decisions, and final database state agree.

## Verification baseline after the fix

```text
py -3.11 -m unittest discover -s tests -q
Ran 191 tests in 3.227s
OK

py -3.11 -m compileall -q clinic_agent appointment_harness tests
exit code 0
```
