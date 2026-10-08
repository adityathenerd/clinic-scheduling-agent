# Call Mechanics

Status: Implemented; retained as the call-mechanics decision record
Last updated: 2026-10-08

The direct Twilio/OpenAI path, per-call actor lifecycle, interruption handling,
bounded workers, and structured event logging are implemented and covered by the
current repository test suite. Historical recommendations below are preserved.

Implementation constraint: Python 3.11+ only. Call actors, adapters, simulations, and tests must not introduce a second application runtime. On the current Windows host, use the `py` launcher.

## Recommendation

Build a small clean adapter using RiderPal's proven Twilio streaming pattern, not a copy of `main.py`. The committed path is **Twilio -> `gpt-live-1` -> Responses delegation -> `gpt-6-sol` with `reasoning.effort: "low"`**, entirely in one Python 3.11+ process. “Sol light” means Sol at low reasoning effort; it is not a separate model.

OpenAI Realtime 2.1 remains a bounded fallback behind the same `VoiceEngine` contract if a 60-minute GPT-Live spike cannot prove connectivity, delegation, interruption, guarded tools, and trace attribution. Do not add a Node or TypeScript relay. GPT-5.6 Terra is rejected for this path: its documented $2 input/$12 output per million tokens is not better than GPT-6 Sol at $2/$10 and it offers no measured capability advantage. GPT-6 Luna remains a later cost challenger after the safety suite passes.

Keep four in-process boundaries:

```text
TwilioAdapter <-> CallSessionActor <-> VoiceEngine facade
                                          |          |
                                          |          +-- DelegatedBackend
                                          |              Responses -> GPT-6 Sol (low)
                                          +-- LiveFrontend
                                              gpt-live-1 speech/turn-taking
                         |
                         v
                  AgentControlPlane -> guarded tools -> scheduler truth
```

These are modules and protocols, not services. `VoiceEngine` preserves one provider-neutral application boundary while its two internal responsibilities remain explicit: `LiveFrontend` owns full-duplex conversation and `DelegatedBackend` owns task reasoning lifecycle. Neither owns scheduler credentials, write authorization, or appointment truth.

### Concurrency model

Use a lightweight supervisor plus one actor per call/session:

```text
CallSupervisor
  +-- CallActor(call-A) -> serialized event queue -> owned CallState
  +-- CallActor(call-B) -> serialized event queue -> owned CallState
  +-- bounded I/O workers -> typed results returned to the owning actor
```

Each `CallActor` exclusively owns its mutable call state and processes one event at a time. Network, model, and tool I/O may run concurrently, but workers do not mutate session state directly; they return typed results to the actor's queue. This prevents interleaved callbacks from corrupting confirmation, interruption, or close state without introducing a distributed actor system.

The supervisor owns actor creation, lookup, deduplication, idle expiry, and removal. It does not own patient workflow state. An actor failure should affect one call only.

For the weekend demo:

- Evaluate the scheduling core without telephony.
- Add one inbound Twilio call as the end-to-end demonstration.
- Keep outbound calls deferred.
- Use native G.711 mu-law end to end when the selected voice engine supports it.
- Support real interruption, not merely simultaneous speech.

## Why RiderPal should be extracted, not reused wholesale

RiderPal proves:

- Twilio bidirectional streaming works.
- A direct realtime WebSocket relay is feasible.
- G.711 mu-law avoids a transcoding component.
- Concurrent incoming and outgoing audio loops are sufficient for a prototype.
- Pre-call context hydration reduces conversational latency.

Its current implementation also has material gaps:

- Telephony, prompt construction, route fetching, logging, configuration, and process startup share one module.
- External Maps calls occur during module initialization.
- The outbound call is initiated before the local server starts, creating a connection race.
- No `mark` or `clear` handling exists, so buffered speech cannot be reliably interrupted.
- The model conversation is not truncated to what the caller actually heard after interruption.
- No bounded queues, backpressure, task cancellation, or explicit stop lifecycle exists.
- Session configuration and events may expose context through logs.
- Request-signature validation is absent.
- Configuration and a credential were committed directly in the repository.

A clean adapter is likely less work than safely untangling this coupling.

## Call lifecycle

1. Receive an inbound webhook and validate the provider signature.
2. Deduplicate using the provider call identifier.
3. Create an opaque internal call identifier.
4. Start minimum-necessary context hydration concurrently.
5. Return the provider's connect/stream response and establish the media WebSocket.
6. Accept connected/start events; record the stream identifier and negotiated codec.
7. Connect the GPT-Live frontend over a server-side WebSocket and emit `live.session_started`.
8. Configure conversational instructions, VAD, and hydrated context; keep detailed workflow policy in the Sol backend and application control plane.
9. Disclose that the caller is speaking with an AI assistant.
10. Run bounded incoming-audio, outgoing-audio, and control-event loops.
11. When Live delegates, correlate every backend response and tool request with a `delegation_id` and control-plane `state_revision`.
12. Route guarded tool requests to the control plane; the frontend and backend receive no scheduler credentials.
13. On spoken interruption, clear queued Twilio audio, cancel the active spoken response, and truncate unheard output without automatically cancelling backend work.
14. On a semantic correction, superseding request, or escalation, invalidate the active delegation and explicitly cancel it; stale output may neither invoke a new mutation nor be spoken.
15. Permit an already-authorized write that crossed the commit boundary to finish or reconcile. Its scheduler result updates authoritative application state, but the stale delegation receives no result and cannot announce success.
16. Verify final scheduler state before Live may say that a booking, reschedule, or cancellation succeeded.
17. On normal completion, verify that no tool operation remains unresolved, close the Live session promptly, and emit `live.session_closed` and `call.ended` once.

Twilio supports `media`, `mark`, and `clear` messages for bidirectional streams. `mark` identifies audio that has played; `clear` empties queued audio. Both are needed for correct barge-in behavior. See [Twilio WebSocket messages](https://www.twilio.com/docs/voice/media-streams/websocket-messages).

## Interruption and stale-work invariants

When caller speech starts while the assistant is speaking:

1. Stop producing new assistant audio.
2. Send `clear` to the telephony provider.
3. Cancel the active Live spoken response.
4. Determine the last played timestamp using playback marks.
5. Truncate the assistant conversation item to that timestamp.
6. Resume listening while any valid backend delegation follows its explicit finish-or-cancel policy.

An acoustic interruption is not necessarily a correction; “yes,” a cough, or an overlapping acknowledgment must not discard valid reasoning. A `caller.correction`, superseding intent, or `escalation.requested` event does invalidate the active delegation. New tool requests from an invalid delegation fail closed. In-flight read work may finish for cleanup but its result is suppressed. An authorized write that has already crossed the application commit boundary must finish or reconcile by idempotency key; it is not rolled back by pretending the backend was cancelled.

Twilio playback marks are the source of truth for what the caller heard. GPT-Live generation state and Sol backend progress are separate facts. The actor tracks all three independently so neither a generated-but-unheard sentence nor a completed-but-stale backend result enters conversation state.

## Explicit interfaces

```python
class TelephonyAdapter(Protocol):
    def events(self) -> AsyncIterator[TelephonyEvent]: ...
    async def send_audio(self, chunk: AudioChunk) -> None: ...
    async def clear_playback(self) -> None: ...
    async def mark_playback(self, label: str) -> None: ...
    async def transfer(self, destination: str) -> None: ...
    async def close(self, reason: str) -> None: ...


class LiveFrontend(Protocol):
    async def connect(self, config: VoiceSessionConfig) -> None: ...
    async def push_audio(self, chunk: AudioChunk) -> None: ...
    async def cancel_response(self) -> None: ...
    async def truncate_response(self, item_id: str, audio_end_ms: int) -> None: ...
    async def close(self) -> None: ...


class DelegatedBackend(Protocol):
    async def cancel_delegation(self, delegation_id: str, reason: str) -> None: ...
    async def submit_tool_result(self, result: ToolResult) -> None: ...


class VoiceEngine(LiveFrontend, DelegatedBackend, Protocol):
    """One application facade; Realtime implements delegation cancellation as a no-op."""


class AgentControlPlane(Protocol):
    async def bootstrap(self, call: CallDescriptor) -> VoiceSessionConfig: ...
    async def handle_tool_intent(self, intent: ToolIntent) -> ToolResult: ...
    async def reconcile(self, operation_id: str) -> ToolResult: ...
```

Events the control plane can rely on:

- `call.started`
- `stream.started`
- `voice.connected`
- `live.session_started`
- `delegation.created`
- `backend.response_started`
- `backend.tool_requested`
- `backend.tool_completed`
- `delegation.completed`
- `delegation.cancelled`
- `delegation.stale`
- `live.commentary_appended`
- `live.session_closed`
- `caller.speech_started`
- `caller.speech_stopped`
- `assistant.first_audio`
- `assistant.playback_marked`
- `assistant.interrupted`
- `tool.requested`
- `tool.completed`
- `tool.outcome_unknown`
- `stream.stopped`
- `call.ended`

## Failure semantics

| Failure | Required behavior |
|---|---|
| Duplicate webhook | Return the existing call session |
| Media sequence gap | Record it; continue if decoding remains valid |
| Voice connection fails before greeting | One bounded retry, then human fallback or end |
| Voice connection fails mid-call | Do not silently reconstruct context; explain and transfer or end |
| Caller interrupts spoken output | Clear Twilio playback and cancel/truncate Live output; backend continues unless semantics invalidate it |
| Caller corrects or escalates | Cancel and invalidate the active delegation; reject later tool requests and suppress stale results |
| Stale read finishes | Record completion for latency/cleanup; do not apply it to active workflow state or speak it |
| Authorized write is already in flight when superseded | Let it settle, verify/reconcile by idempotency key, and suppress stale narration |
| Live attempts to announce success before verified state | Reject/suppress the utterance; only verified application state may unlock success language |
| Read tool times out | One safe retry may be allowed |
| Write tool times out | Never blindly retry; enter `reconciliation_required` |
| Caller hangs up before confirmation | Perform no mutation |
| Caller hangs up during an authorized write | Let the operation settle and reconcile by idempotency key |
| One streaming task exits | Cancel sibling tasks and close the session once |
| Outbound audio queue grows | Apply backpressure; do not allow unbounded memory growth |
| Unknown provider event | Record safely and ignore unless it invalidates session integrity |

## Latency targets

These are prototype instrumentation targets, not vendor claims:

- Cached session bootstrap: under 300 ms.
- End of caller speech to first assistant audio for a no-tool turn: target under 1.2 seconds.
- Barge-in detection to stopped playback: target under 300 ms.
- Any tool delay over roughly 1.5 seconds: provide a truthful short acknowledgment.
- Never reduce a confirmation or verification step to improve latency.

Measure distinct clocks rather than one blended “response latency”:

- first audio: Live begins producing an audible response;
- first useful result: the first content that advances the patient's task;
- delegation latency: `delegation.created` to backend result;
- tool latency: guarded request to typed completion;
- playback timing: Twilio send/mark/clear and caller-heard cursor; and
- active Live seconds: session start to prompt closure, including backend wait and silence.

GPT-Live bills active session duration, including silence and backend wait. Close completed sessions promptly and never treat an early filler acknowledgment as the useful result.

Start with server VAD. Tune threshold and silence duration from recorded synthetic tests. Semantic VAD may sound more natural but can intentionally wait longer, so it should be evaluated rather than assumed.

## Blast radius

| Component | Owns | Failure blast radius |
|---|---|---|
| Telephony adapter | Webhooks, stream protocol, codec, playback buffer | Current call only |
| Live frontend | Speech, VAD, turn-taking, commentary, generated audio | Current conversation; no scheduler mutation authority |
| Delegated backend | Sol response/delegation lifecycle and tool proposals | One delegation; no direct scheduler credentials or write authority |
| Voice engine facade | Normalized Live or Realtime events and fallback compatibility | Current call; application control-plane contract remains stable |
| Call session | Lifecycle, queues, cancellation, timing | One call |
| Agent control plane | Instructions, state machine, tool intent | One patient workflow |
| Scheduling tool gateway | Authorized reads and writes | Appointment state; highest side-effect risk |
| Context hydrator | Minimum-necessary session context | Privacy and answer relevance |
| Event sink | Sanitized lifecycle and latency records | Observability and privacy; must never block calls |

## Privacy and observability

- Validate Twilio's request signature for HTTP and WebSocket entry points. See [Twilio security guidance](https://www.twilio.com/docs/usage/security).
- Do not log audio payloads, complete prompts, phone numbers, patient details, credentials, or complete provider messages.
- Use opaque internal identifiers and sanitized event metadata.
- Record monotonic timestamps for latency calculations.
- Do not record raw audio by default.
- A phone number may select a candidate record but must not authenticate the caller.

## Local simulation

Implement `FakeTelephonyAdapter` with:

- deterministic call/start/media/stop events;
- synthetic media chunks or text-backed turns;
- programmable disconnects;
- duplicate starts;
- delayed or missing marks;
- caller interruption during assistant playback;
- a bounded fake clock; and
- no provider credentials.

The evaluation harness should normally drive the agent beneath the audio boundary. A separate contract test should verify that telephony events map correctly onto the same call-session events.

## Boundary and concurrency tests

Use contract, unit, and concurrency tests at every boundary:

- two or more simultaneous calls never share state, confirmation, audio marks, or tool results;
- events for one call are applied in actor order even when I/O completes out of order;
- duplicate webhooks resolve to the existing actor;
- barge-in clears playback, cancels generation, and truncates unheard output once;
- barge-in does not cancel backend work merely because speech overlapped;
- correction and escalation invalidate the active delegation;
- stale delegated tool requests fail before execution;
- an in-flight stale read result is recorded but never returned to speech;
- every Live, delegation, backend, tool, and playback stage has an attributable timestamp;
- disconnect cancels sibling tasks and releases resources;
- unknown write outcomes enter reconciliation rather than blind retry;
- repeated stop/disconnect/error signals close the session exactly once;
- late worker results cannot mutate a closed actor;
- queue bounds and backpressure prevent one slow call from exhausting the process; and
- an actor crash or timeout leaves other calls unaffected.

Use Python's `unittest` or `pytest` for the executable specification, depending on whether the project accepts one test dependency. Prefer `unittest` initially when it keeps the vertical slice dependency-light. Python 3.11 is present through the Windows `py` launcher, while the ordinary `python` command is unavailable. The call adapter, control plane, and harness must remain in Python.

## Workstream decision

Choose inbound Twilio calling for the primary voice demonstration with GPT-Live and delegated Sol-low as the committed path. The company task does not require both directions. Inbound avoids outbound-consent and dialing complexity while directly exercising identity, scheduling, confirmation, and escalation. Preserve `direction` in `CallDescriptor` and test outbound bootstrap semantics at the contract level, but defer full outbound telephony unless the core loop finishes early.

### Sixty-minute GPT-Live spike gate

Promote GPT-Live only when one traced call proves all of the following:

1. Twilio connects to a server-side `gpt-live-1` WebSocket in the Python process.
2. A booking request delegates to GPT-6 Sol with `reasoning.effort: "low"`.
3. The backend requests the guarded slot-read tool.
4. A caller correction supersedes the old delegation, whose late result is neither applied nor spoken.
5. Exact confirmation gates the write tool.
6. Read-after-write scheduler state is verified before spoken success.
7. Barge-in clears caller-heard playback correctly.
8. Trace events attribute frontend, delegation, backend, tool, and playback stages and expose all six latency clocks above.

If a foundational item remains unproven at 60 minutes, switch the adapter to OpenAI Realtime 2.1 without changing the actor, control plane, tool gateway, or harness scenarios.

## Known unknowns

- Whether reviewers require a public telephone call or accept the recorded inbound Twilio demonstration.
- Whether `gpt-live-1` preserves telephone-compatible audio end to end without a conversion step in the current account/region.
- Required human-transfer destination and behavior.
- Prototype identity-verification policy.
- Acceptable pause before a spoken hold acknowledgment.
- Whether the recalled Plivo implementation exists separately from the public Twilio version.
- Whether interruption behavior remains reliable across accents, noise, and carrier conditions.

## Unknown-unknown controls

Unknown unknowns cannot be enumerated honestly. Contain them through:

- strict event schemas with safe handling of unknown events;
- bounded queues and timeouts;
- idempotent lifecycle and write operations;
- fail-closed mutation authorization;
- chaos scenarios for disconnect, duplication, delay, and reordering;
- adapter contract tests; and
- one-call blast-radius containment.

No deployment, credentials, real call, or external mutation was performed.

## Async worker and actor model

The backend uses one lightweight actor per call and a bounded shared worker pool:

```text
CallSupervisor
  +-- CallSessionActor(call A) -- serialized mailbox, owns A's mutable state
  +-- CallSessionActor(call B) -- serialized mailbox, owns B's mutable state
  +-- CallSessionActor(call C) -- serialized mailbox, owns C's mutable state
             |
             +-- AsyncWorkerPool -- bounded tool/provider I/O only
```

The actor processes state transitions sequentially. Slow reads and writes execute in workers and return typed completion events to the owning actor. This permits barge-in, hangup, and provider-control events to be processed while a tool is still in flight. No worker may mutate session state directly.

The supervisor deduplicates provider call identifiers and contains failure to one actor. A caller hangup closes the transports once, but an already-authorized write worker is allowed to settle so its outcome can be recorded or marked for reconciliation.

## Executable call-mechanics tests

The standard-library test suite is intentionally provider-free and covers:

- inbound and outbound descriptor contracts;
- bounded mailbox overflow and terminal behavior;
- bounded worker concurrency and recovery after worker failure;
- voice connection retry and fallback transfer;
- caller audio forwarding;
- real barge-in semantics: clear, cancel, and truncate to played audio;
- Live/delegated stage normalization and monotonic timestamps;
- interruption without accidental backend cancellation;
- correction/escalation invalidation and stale-result suppression;
- unknown provider events;
- observability failure isolation and sensitive-field redaction;
- duplicate tool-operation suppression;
- non-blocking event processing while tools run;
- unknown write outcomes and reconciliation-required state;
- caller hangup during an authorized write;
- close-once behavior;
- duplicate webhook deduplication;
- parallel call isolation;
- concurrent inbound and outbound session ownership; and
- one-call failure containment.

Run them with:

```powershell
py -3.11 -m unittest discover -s tests -v
```

The suite currently contains 40 passing tests. The demo remains bounded to inbound calling. Outbound is represented and concurrency-tested at the session contract so adding a dialer later does not require redesigning state ownership.

## Primary references

- [GPT-Live](https://developers.openai.com/api/docs/guides/live)
- [GPT-Live delegation](https://developers.openai.com/api/docs/guides/live-delegation)
- [Realtime-to-Live migration](https://developers.openai.com/api/docs/guides/live-migration)
- [Server-side voice WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets)
- [Voice latency and cost](https://developers.openai.com/api/docs/guides/voice-latency-cost)
- [Twilio Media Streams messages](https://www.twilio.com/docs/voice/media-streams/websocket-messages)
