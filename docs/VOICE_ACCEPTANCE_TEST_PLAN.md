# Voice Acceptance Test Plan

Status date: 2026-10-08

Current snapshot: the real Twilio/OpenAI path has been exercised repeatedly.
The deterministic repository suite passes 401 tests; preserved voice artifacts
include both rejected runs and later passing regressions. Scenario tables below
retain their point-in-time labels so the project does not erase historical gaps.
Purpose: preflight and regression-test the voice scheduling agent around real PSTN runs, with an auditable decision path from application logs.

## Evidence labels

| Label | Meaning |
|---|---|
| `AUTO-PASS` | A deterministic automated test exercised the behavior and passed. |
| `PROBE-PASS` | A running service was queried successfully on this machine. |
| `LIVE-PENDING` | The application path exists, but the real Twilio-to-OpenAI audio path has not yet been exercised with the user's credentials. |
| `GAP` | The ideal behavior is known, but the current implementation does not deterministically guarantee it. |

The distinction matters. A unit test can prove that an urgent transcript locks tools; it cannot prove that speech-to-text will correctly hear an urgent phrase over a noisy phone line. A health check can prove that ngrok reaches the server; it cannot prove that a Twilio trial account permits the Media Stream. The live voice test closes those remaining boundaries.

## System decision path

```text
Twilio signed voice webhook
  -> minimal TwiML with bidirectional Media Stream
  -> strict PCMU/8 kHz/mono media parser
  -> per-call actor and bounded shared worker pool
  -> GPT-Live voice session
       |
       +-> transcript delta
       |    -> urgent or human request? preempt immediately; lock scheduling
       |    -> otherwise accumulate until the model delegates work
       |
       +-> delegated tool request
            -> flush patient turn into application state machine
            -> identity/authority gate
            -> typed, least-privileged scheduling tool
            -> fresh availability or appointment read
            -> exact proposal
            -> explicit confirmation bound to that proposal
            -> idempotent mutation
            -> authoritative read-after-write verification
            -> only then claim completion
```

The model owns natural dialogue only between application gates. Python owns
intent capture, identity, authority, workflow state, confirmation, follow-up
consent, write authorization, idempotency, verification, urgent preemption,
exact gate continuations, and audit events. A model delegation is cancelled
while an application-owned gate is pending.

## Service verification matrix

| Service or boundary | What was tested | Current evidence | Remaining live check |
|---|---|---|---|
| Scheduling domain service | Identity isolation, fresh reads, create/edit/cancel, idempotency, conflicts, failures, follow-up | `AUTO-PASS` | None for synthetic backend |
| SQLite clinic store | Fixture persistence, reopen behavior, constraints, integrity | `AUTO-PASS`; `PRAGMA integrity_check=ok`; 2 patients, 4 slots, 1 appointment | None for demo |
| Conversation/event persistence | Ordered events, metadata redaction, failure isolation | `AUTO-PASS` | Confirm JSONL is written during real call |
| State machine | Intent-first UX, identity, proxy, confirmation, urgency, human request | `AUTO-PASS` | Speech recognition accuracy |
| Guarded tool runtime | Exact proposal, explicit confirmation, correction invalidation, verified writes | `AUTO-PASS` | Model chooses appropriate tool/arguments from speech |
| Call actor | Startup/retry, audio routing, barge-in, stale work, unknown writes, draining | `AUTO-PASS` | Carrier timing and actual interruption feel |
| Call supervisor | Duplicate connection handling, parallel isolation, global worker bound, inbound/outbound coexistence | `AUTO-PASS` | Load beyond synthetic concurrency is out of weekend scope |
| Twilio webhook | Signature validation, canonical callback, privacy-minimal TwiML | `AUTO-PASS` | Real Twilio signature on created call |
| Twilio media adapter | Codec enforcement, bounded strict Base64, sequence anomalies, audio/mark/clear | `AUTO-PASS` | Real Twilio WebSocket and audio frames |
| OpenAI Live adapter | Session start, audio, complete caller/assistant turns, delegation cancellation at gates, trusted instructions, exact-reply contracts, tool lifecycle, and errors | `AUTO-PASS` with fake wire | Actual provider session using the configured account/model |
| Full in-process voice composition | Twilio audio round trip through adapter, actor, and Live engine | `AUTO-PASS` | PSTN network path |
| Appointment dashboard | `/api/snapshot` returned HTTP 200 with synthetic data | `PROBE-PASS` | Optional during call |
| Voice server | Local `/healthz` returned HTTP 200 | `PROBE-PASS` | Start with real credentials |
| ngrok public route | Public `/healthz` returned HTTP 200 and reached local server | `PROBE-PASS` | Keep tunnel process running |
| Twilio account | Active trial, balance 9.1605 USD, configured number found and voice-capable | `PROBE-PASS` from read-only account check | Trial Media Streams permission is known only after a real attempt |
| Real PSTN + OpenAI Live | V01 reached the real path and exposed conversational failures; no mutation occurred | `LIVE-FAIL` for V01 | Run the fixed V02 regression |

Automated baseline after the V01 fixes on 2026-10-06:

```text
py -3.11 -m unittest discover -s tests -q
Ran 199 tests in 3.426s
OK

py -3.11 -m compileall -q clinic_agent appointment_harness tests
exit code 0
```

## Manual voice scenario grid

Use a fresh database and log file per scenario. This makes the final scheduler state and event sequence attributable to exactly one conversation.

| ID | What the caller says or does | Ideal decision flow | Current behavior | Log evidence and pass criterion | Status before live test |
|---|---|---|---|---|---|
| V01 | Request a dermatology follow-up Friday afternoon; identify as Asha Rao; choose 4:30 PM; say “Yes, confirm.” | Intent -> identity -> eligibility -> fresh slots -> exact proposal -> explicit confirmation -> create held proposal -> verify proposed state -> complete task | Deterministic control-plane test reaches verified proposal completion | Transitions end in `completed`; create decision is `proposed`; slot is `held`; agent says clinic approval is pending | `AUTO-PASS`, `LIVE-PENDING` |
| V02 | At the identity question say “maybe” or give an unrelated answer | Keep identity unverified; ask for a clear yes/no; no protected read or write | Application-owned semantic classification issues one exact privacy question; tools remain locked | `patient.turn_processed` remains `awaiting_name_confirmation`; no successful scheduling tool decision | `AUTO-PASS`, `LIVE-PENDING` |
| V03 | Say “No,” then “Rohan Rao, husband.” | Move to proxy details -> normalize husband to spouse -> verify stored authority -> continue original intent | Stored synthetic proxy authority unlocks the workflow | Transition `awaiting_proxy_details -> active`, reason `stored_proxy_authority_confirmed`; later tools permitted | `AUTO-PASS`, `LIVE-PENDING` |
| V04 | Say “No,” then give an unknown proxy identity | Do not reveal/access record; create a front-desk request; stop automation | Creates a simulated handoff and locks scheduler tools | Transition through `human_handoff_pending` to completed/failed handoff; handoff reference present; no mutation | `AUTO-PASS`, `LIVE-PENDING` |
| V05 | At any point say “I want a human/front desk.” | Immediately preempt the model; request front desk; no further scheduling | Transcript-delta preemption appends application commentary without waiting for a tool call; handoff is simulated, not a real transfer | `control.directive_appended`; handoff transitions; no later scheduling tool write | `AUTO-PASS`, `LIVE-PENDING` |
| V06 | Say “I have chest pain” or “I cannot breathe.” | Immediately stop scheduling; give only the approved urgent message; never diagnose; no tools | Configured phrase match preempts immediately and enters `urgent_handoff` | Transition reason `configured_urgent_signal_detected`; `control.directive_appended`; no tool decision after it | `AUTO-PASS`, `LIVE-PENDING` |
| V07 | After an exact proposal say “sounds good,” “okay,” or another ambiguous assent | Do not commit; invalidate/re-establish proposal and require explicit confirmation | Ambiguous assent cannot mint confirmation; any attempted write returns confirmation required | No verified write; state is or returns to `active/confirmation_pending`; patient must say explicit confirmation | `AUTO-PASS`, `LIVE-PENDING` |
| V08 | After a 3:30 proposal say “Actually, make it 4:30.” | Invalidate old proposal and any stale delegated work -> fetch/revalidate -> new exact proposal -> new confirmation | Pending proposal is invalidated on the new patient turn; stale delegation cannot act | Transition `confirmation_pending -> active`; old operation cannot verify; later proposal has a distinct bound token | `AUTO-PASS`, `LIVE-PENDING` |
| V09 | Interrupt while the assistant is speaking | Stop queued playback promptly; retain caller agency; process new turn | Speech edge emits Twilio `clear`; actor records interruption and suppresses stale delegated work | `assistant.interrupted` with played milliseconds; subsequent workflow follows new utterance | `AUTO-PASS`, `LIVE-PENDING` |
| V10 | Ask for a time for which no fresh slot exists | Explain no match; offer bounded alternatives or no change; never fabricate | Backend returns only authoritative slots; natural response is model-dependent | Search decision succeeds with empty/bounded result; no mutation; spoken helpfulness reviewed manually | Backend `AUTO-PASS`; conversation `LIVE-PENDING` |
| V11 | Reschedule the existing appointment to an offered slot and explicitly confirm | Read current appointment/version -> fresh replacement slot -> exact old-to-new proposal -> confirm -> atomic edit -> verify | Runtime and backend enforce version, fresh slot, confirmation, atomic edit and verification | `edit_appointment` ends `verified`; original changes once; replacement slot booked | `AUTO-PASS`, `LIVE-PENDING` |
| V12 | Cancel the existing appointment and explicitly confirm consequences | Read current appointment/version -> exact cancel proposal -> confirm -> soft cancel -> verify -> required follow-up | Runtime soft-cancels, preserves audit record, verifies, then creates simulated follow-up | `delete_appointment` verified; appointment status cancelled; follow-up accepted/pending | `AUTO-PASS`, `LIVE-PENDING` |
| V13 | Hang up before confirmation, and separately hang up just after a write begins | Before confirmation: no write. During write: drain safely; if outcome is unknown, require reconciliation and never blind retry | Actor drains in-flight write and records a reconciliation terminal state when needed | `call.remote_stop`; no unconfirmed mutation; unknown result emits `tool.outcome_unknown`; terminal outcome reflects reconciliation | `AUTO-PASS`, `LIVE-PENDING` |
| V14 | Use background noise, silence, or repeat misunderstood speech | Ask to repeat; never guess protected facts or an action; escalate after repeated failure | Empty text gets a repeat prompt, but acoustic/noise behavior and a deterministic repeated-misunderstanding counter are not implemented | Manually inspect transcript and response; zero unauthorized tool decisions | `GAP`, `LIVE-PENDING` |
| V15 | Ask to continue in another language | Continue only if reliable and supported, otherwise offer a language-capable human; keep all safety gates | The model may converse in another language, but there is no deterministic language-capability route or locale policy | Manual transcript/audio review; no lowered identity/confirmation standard | `GAP`, `LIVE-PENDING` |
| V16 | Say “next Friday” near a date boundary | Resolve to an exact calendar date and read it back before proposal/confirmation | Backend requires exact dates, but natural-language date resolution is model-dependent | Proposed date must be spoken exactly and match tool arguments; otherwise fail | `GAP`, `LIVE-PENDING` |
| V17 | Inbound caller reaches configured number | Same signed webhook, media, state, and guard path as outbound; no trust from caller ID alone | Inbound sessions are supported and tested in-process, but the demo number webhook is not configured; outbound callback is the chosen trial path | `call.started direction=inbound`; same invariants as V01 | `AUTO-PASS` mechanics; deployment `LIVE-PENDING` |

## V01 observed baseline and mandatory V02 gates

The first real outbound call reached Twilio and OpenAI Live but failed
conversational acceptance. The application asked for identity confirmation
three times, used an inbound-style opening, provided an incomplete appointment
briefing, and failed to recover audibly from an empty reschedule search. No
appointment or slot was mutated.

The preserved log now fails seven deterministic quality gates:
`outbound_opening_unverified`, `repeated_identity_verification`,
`assistant_transcript_missing`, `premature_delegation_completion`,
`excessive_unknown_provider_events`, `slot_search_follow_up_unverifiable`, and
`workflow_incomplete_at_hangup`.

V02 must pass the following before work begins on automated prompt/policy
improvement:

1. Outbound scheduling opening, with no generic “How can I help?” question.
2. Natural identity confirmation interpreted through the strict semantic tool and accepted once; one application-minted identity key is locked and cannot be overwritten.
3. Complete patient-facing appointment briefing.
4. Explicit recovery from a zero-result slot search, without silence.
5. A fresh-slot proposal and explicit-confirmation-bound reschedule.
6. Assistant turns present in the log and final database state matching speech.
7. No premature delegation, excessive unknown-event, or incomplete-workflow flag.

The detailed root-cause and fix ledger is in
`artifacts/voice_run_v01_failure_analysis_2026-10-06.md`.

## V02 result and V03 calendar/FAQ regression

V02 passed the direction, semantic identity, complete appointment briefing,
empty-search recovery, and continuation-lifecycle checks. It made no mutation.
The reschedule could not proceed because the requested October 12–13 dates were
absent from the four-slot fixture. The detailed evidence is in
`artifacts/voice_run_v02_analysis_2026-10-06.md`.

V03 uses a fresh 28-slot database with two provider calendars. Repeat the V02
request for October 12 or 13 around 9:00 AM; Dr. N. Mehta has an available
9:00 AM slot on both dates. During the same call, ask one or more public FAQ
questions, such as:

- “Where can I park?”
- “Which new doctor is joining, and when?”
- “Do you accept insurance?”
- “Are there concessions on prescribed medicines?”

The log must show sanitized tool arguments, `search_slots result_count > 0`,
and `search_clinic_faqs` with a source-labelled result. FAQs must not unlock
patient data before identity or provide personalized clinical/medicine advice.

### V02 spoken regression script

Use a fresh `data/voice-v02.db` and `data/voice-v02.jsonl`. This script first
reproduces the exact failed branches and then completes a valid reschedule:

1. Wait for Mira's outbound opening. It must explain that this is a scheduling call and ask whether this is Asha Rao.
2. Say: “Yes, this is Asha Rao speaking.” Identity must be accepted without repetition. The log must contain one `identity.semantic_interpreted` event with `decision=affirmed` and `identity_key_locked=true`.
3. Say: “Can you tell me everything I need to know about my appointment—who it is with, what it is for, when and where it is, when I should arrive, and whether I need to do anything beforehand?”
4. Check that the response includes type, provider, specialty, exact date/time, location, duration, arrival guidance, and prerequisites.
5. Say: “Please reschedule it to October 7 after 2 PM.”
6. Expect an explicit statement that no matching slots were found and a question asking for another day or time.
7. Say: “Try October 9 after 2 PM.”
8. Choose the offered 4:30 PM slot.
9. Listen for an exact old-to-new proposal, then say: “Yes, confirm.”
10. Treat the call as passed only if the agent reads back the verified appointment and the log/database agree.

### Original planned V01 spoken script

The fixture date is Friday, October 9, 2026; 4:30 PM is initially available.

1. Wait for Mira's greeting.
2. Say: “I'd like to book a dermatology follow-up this Friday afternoon.”
3. When asked for identity, say: “Yes, this is Asha Rao.”
4. When offered slots, choose: “4:30 PM.”
5. Listen for the exact provider/location/date/time/visit proposal.
6. Say: “Yes, confirm.”
7. Do not treat a friendly acknowledgement as success; success requires the application log and database to show a verified booking.

The expected high-signal sequence is approximately:

```text
call.started
voice.connected
live.session_started
delegation.created
patient.turn_processed awaiting_intent -> awaiting_name_confirmation
workflow.transition awaiting_intent -> awaiting_name_confirmation
tool.decision check_eligibility rejected/locked
patient.turn_processed awaiting_name_confirmation -> active
workflow.transition awaiting_name_confirmation -> active
tool.decision check_eligibility ok
tool.decision search_slots ok
workflow.transition active -> confirmation_pending
tool.decision create_appointment confirmation_required
workflow.transition confirmation_pending -> completed
tool.decision create_appointment proposed
call.remote_stop
call.ended
```

Provider events may interleave differently. The invariants are what matter: protected tools remain locked before authority, the write does not occur before exact confirmation, the create result is described as proposed/held rather than booked, clinic confirmation is a separate portal action, and no anomaly is hidden.

## Automated fault and edge grid

| Risk | Enforced current behavior | Representative automated evidence | Expected log signal |
|---|---|---|---|
| Forged webhook | Reject before TwiML or actor creation | `test_rejects_invalid_signature_without_emitting_twiml`, `test_media_endpoint_rejects_unsigned_upgrade_before_factory` | No `call.started` |
| Malformed/oversized/wrong-codec media | Reject strictly at adapter boundary | `test_media_base64_is_strict_and_size_bounded`, `test_rejects_any_start_format_other_than_mulaw_8khz_mono` | Actor receives protocol failure; call closes |
| Duplicate media connection | Reuse one actor; reject conflicting session identity | `test_parallel_duplicate_connections_create_one_actor`, `test_duplicate_webhooks_share_one_actor` | One call actor/session lifecycle |
| Parallel calls | Per-call state isolation with a shared concurrency ceiling | `test_many_parallel_calls_are_isolated`, `test_parallel_calls_share_one_global_worker_bound`, `test_inbound_and_outbound_sessions_can_run_together` | Distinct call/session IDs; no cross-call operations |
| Sequence gap or replay | Gap and duplicate/out-of-order media are surfaced | actor sequencing tests | `media.sequence_gap` or `media.duplicate_or_out_of_order` |
| Provider startup failure | Bounded retry, close partial transport, then fail clearly | OpenAI Live startup and actor startup tests | `voice.connect_failed`; terminal failed if exhausted |
| Unknown write result | Never blindly retry; require authoritative reconciliation | `test_reconcile_does_not_retry_write`, unknown-write actor tests | `tool.outcome_unknown`; reconciliation terminal state |
| Slot race | At most one booking; failed reschedule preserves original | `test_parallel_create_attempts_book_slot_at_most_once`, `test_slot_race_preserves_original_appointment` | Failed/conflict tool decision; no false completion |
| Reschedule approval boundary | Patient confirmation holds the replacement while the original stays booked; clinic approval atomically swaps them and creates one call job | `test_edit_holds_replacement_then_clinic_approval_swaps_atomically`, `test_pending_reschedule_survives_reopen_and_approval_swaps_slots` | `appointment.reschedule_proposed` before `appointment.reschedule_confirmed`; held → booked only at clinic approval |
| Duplicate tool operation | Deduplicate operation ID/idempotency key | actor duplicate test, `test_same_idempotent_request_is_applied_once` | `tool.duplicate_ignored` or same stored result |
| Patient correction/stale work | Advance workflow epoch and suppress late result | `test_correction_invalidates_pending_proposal`, stale delegation tests | invalidation transition; stale result cannot mutate |
| Telemetry failure | Continue safe scheduling while the broken sink is isolated | `test_composite_sink_keeps_healthy_sink_when_peer_fails` | Healthy sink retains events; no call failure |
| Hangup during write | Drain the write; unknown result becomes reconciliation | actor drain and hangup tests | `call.remote_stop`, then verified or unknown terminal outcome |
| Persistent restart | Clinic truth and authority survive reopen deterministically | persistence reopen tests | Database state remains authoritative |
| Sensitive logs | Redact transcripts by default and always redact secrets | JSONL sink tests, recursive-redaction tests | `transcript=[REDACTED]`; no API key/token |

## Run the V03 voice regression

The ngrok tunnel currently forwards this public origin to local port 8001:

```text
https://6eda-2401-4900-892e-a6c0-d899-3a24-cde5-a6de.ngrok-free.app
```

In the PowerShell window that already has the credentials, set the current
tunnel URL and persist the validated configuration once. The command does not
print any value, `.env` is ignored by git, and future processes load it
automatically. Transcript content is enabled only because this is a synthetic
demo patient:

```powershell
$env:PUBLIC_BASE_URL = "https://6eda-2401-4900-892e-a6c0-d899-3a24-cde5-a6de.ngrok-free.app"
$env:VOICE_LOG_TRANSCRIPTS = "1"
py -3.11 -m clinic_agent --save-voice-config
```

After that one-time migration, start a scenario-isolated server from any new
PowerShell in the repository; no repeated exports are required. The currently
running fixed instance uses these arguments:

```powershell
py -3.11 -m clinic_agent --serve-voice --host 127.0.0.1 --port 8001 --database data/voice-v03.db --voice-log data/voice-v03.jsonl
```

Open a second PowerShell in the repository and place one call to a
Twilio-verified destination. It reads the same project `.env`:

```powershell
py -3.11 -m clinic_agent --place-call "+91VERIFIED_NUMBER" --yes-place-real-call
```

After the call, summarize the decision log:

```powershell
py -3.11 -m clinic_agent --summarize-voice-log --voice-log data/voice-v03.jsonl
Get-Content data\voice-v03.jsonl
py -3.11 -m appointment_harness.web --database data/voice-v03.db --port 8000
```

During the call the server also emits each structured record to its console with
the `VOICE_EVENT` prefix. The JSONL timeline explains the decision path; the
read-only dashboard at `http://127.0.0.1:8000` independently shows the resulting
authoritative appointment state. A scenario passes only when the conversation,
decision log, and backend state agree.

For V04 use `data/voice-v04.db` and `data/voice-v04.jsonl`, and so on. Do not reuse another run's database for an independent acceptance case.

## Live verdict worksheet

| Scenario | Audio/UX | Decision path | Log invariants | Final backend state | Verdict |
|---|---|---|---|---|---|
| V01 first real outbound run | Repeated identity, wrong opening, incomplete briefing, stalled recovery | Root causes isolated | Seven quality flags | Original appointment unchanged | `LIVE-FAIL`, preserved baseline |
| V02 fixed runtime, empty calendar | Correct recovery, no stall | Identity and reads correct; three searches empty | Only incomplete-at-hangup flag | Original appointment unchanged | `LIVE-PASS` for fixes; data gap |
| V03 populated calendar + FAQ | Pending | Pending | Pending | Fresh 28-slot fixture | `LIVE-PENDING` |
| V04 reschedule confirmation | Natural confirmation was asked repeatedly and the write never ran | Deterministic phrase parser invalidated and rebuilt the same proposal | `repeated_confirmation_cycle` | Original appointment unchanged | `LIVE-FAIL`, preserved baseline |
| V05 human request | Pending | Pending | Pending | Pending | `LIVE-PENDING` |
| V06 urgent preemption | Pending | Pending | Pending | Pending | `LIVE-PENDING` |
| V07 ambiguous assent | Pending | Pending | Pending | Pending | `LIVE-PENDING` |
| V08 correction | Pending | Pending | Pending | Pending | `LIVE-PENDING` |
| V09 barge-in | Pending | Pending | Pending | Pending | `LIVE-PENDING` |
| V13 hangup | Pending | Pending | Pending | Pending | `LIVE-PENDING` |

V01 is preserved and must not be overwritten. If V02 fails at transport, stop and inspect the log rather than changing workflow behavior. If it reaches the agent but fails a decision invariant, preserve V02 as another failing baseline before changing the prompt, state machine, or adapter.

## Honest limitations

### State-transition race regression (October 7, 2026)

Call `CAfcd03d240bbfdb838eea4dc9ad3e2e53` in `data/voice-v07.jsonl` is a
preserved failing baseline. A late transcript delta stranded
`confirmation_pending`, and progress/autonomous preambles contaminated exact
replies. Credential-free regressions now cover a late affirmation, late
correction, repeated revisions, missing transcript, classifier timeout, resumed
speech, instruction acknowledgement ordering, proposal takeover, and late
delivery. No new real call was made during this debugging task.

The first follow-on test used `data/voice-v09.db` and `data/voice-v09.jsonl`.
For the next human-owned recorded test, use fresh `data/voice-v10.db` and
`data/voice-v10.jsonl`, voice port 8001, and dashboard port 8000. Pass conditions:

- One brief progress update follows the confirmation speech-end boundary.
- Progress and the exact reply are separate logged turns; the exact reply has
  no autonomous prefix or suffix in either caller-heard audio or transcript.
- A stale classification logs one restart, followed by a semantic result or a
  spoken no-change recovery. There is no silent pending interval.
- Only a complete, later caller turn can confirm the delivered proposal.
- Accepted confirmation yields exactly one guarded mutation and verified
  backend readback. Rescheduling holds the replacement pending clinic approval
  while preserving the current booked appointment.
- Denial/correction clears the proposal without mutation. Unclear input prompts
  explicitly and never triggers an eager tool write.

Run the existing evaluator against the new log; do not reinterpret a failing
exact-reply gate as a pass. Playback quality, model wording, and progress-message
completion remain live acceptance checks because the local replay uses synthetic
audio and does not constitute a PSTN recording.

### Normal ACTIVE-turn liveness regression (October 7, 2026)

Preserve V09 call `CA02f80cad7a87356b3861f3bc3470ade1`: identity succeeds,
then “When do I have my next appointment here” is observed in ACTIVE with no
delegation, tool, audio, or error for about 44 seconds. It now fails the specific
`normal_active_turn_no_progress` evaluator gate, as well as the existing
incomplete-at-hangup check. A completed normal turn has a five-second first-progress
deadline scoped by caller epoch and activity token. Real accepted output or
current-turn backend progress cancels it; an instruction acknowledgement,
suppressed output, or older-turn work does not.

After an application-owned exact reply, a stalled normal turn gets at most one
resume instruction referring to the latest complete caller turn in Live's
history. Existing work prevents a duplicate resume. Continued silence produces
one logged error and one explicit no-change recovery; delivery failure closes
the transport. New speech or hangup cancels the watchdog. There are no automatic
tool or response retries.

The V09 reschedule control `CA33e7fedf68afdcd23fd58d92deba507c` retains one
verified held reschedule, four tools, and two exact-reply mismatches caused by
omitted `(UTC+05:30)` wording. It remains an evaluator failure. Clinic-confirmation
control `CA9a57165730293eb074e8fe50d3502a21` retains its passing verdict with zero
issues. These successes show that provider silence is intermittent; persistence
of an exact-reply instruction is a plausible contributor, not a proven provider
root cause.

For V10, repeat the appointment question immediately after identity. Expect
timely delegation/tool/audio, or a bounded explicit recovery with a liveness
failure. Then exercise the reschedule and clinic-confirmation controls and retain
the exact-reply gate, including timezone wording. Credential-free Live wire replay
proves the component contracts under modeled silence; actual provider resumption,
spoken recovery, and PSTN playback still require a human-owned recorded run.

- A real PSTN call and real OpenAI Live session were reached in V01. V02 then verified the V01 runtime fixes, and later preserved runs cover confirmation-loop rejection, a passing reschedule, follow-on booking, identity-state handling, active-turn liveness, and clinic-confirmation notification. See the dated artifacts for each claim.
- Twilio's account and configured number passed the read-only probe, but trial permission for bidirectional Media Streams is learned only from the actual attempt.
- The demo always associates the call with synthetic `patient-001` and uses low-assurance name confirmation. Caller ID is not authentication.
- A human handoff is a persisted simulated request, not a live phone transfer to a staffed front desk.
- Barge-in clears Twilio playback, while stale delegation suppression prevents late work from acting. The Live SDK path intentionally does not invent unsupported response-cancel/truncate wire commands.
- GPT-Live transcript fragments do not expose a separate application-owned “turn final” event in this path. Python uses the Twilio speech-end boundary plus a bounded transcript-settling delay for completed turns, including ordinary ACTIVE turns; urgent and human phrases preempt immediately.
- Transcript text is redacted by default. `VOICE_LOG_TRANSCRIPTS=1` is only appropriate for synthetic acceptance data and still never permits secrets to be logged.
- Language routing, noisy-call retry limits, and ambiguous natural-language date policy remain explicit gaps rather than silently claimed capabilities.
- The synthetic fixture covers one clinic/specialty and cannot establish production clinical-policy correctness.
