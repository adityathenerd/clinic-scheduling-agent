# Call Mechanics Test Matrix

Status: Executable GPT-Live/delegation contract core complete  
Last updated: 2026-10-05

## Test philosophy

Tests target observable contracts and safety invariants rather than provider SDK internals. Each call has one state-owning actor. A shared bounded worker pool performs slow I/O and returns typed events; workers never mutate call state directly.

The suite uses only Python 3.11 standard-library components so a reviewer can run it without credentials, network access, or dependency installation.

```powershell
py -3.11 -m unittest discover -s tests -v
```

## Risk-to-test matrix

| Boundary or risk | Unit/contract behavior | Situations covered | Pass condition |
|---|---|---|---|
| Call descriptor | Direction and required metadata | Inbound without destination; outbound with and without destination | Invalid outbound descriptors fail before session creation |
| Actor mailbox | Bounded serialized delivery | FIFO, overflow, work after close | Order preserved; overflow fails fast; terminal actor rejects work |
| Worker pool | Global bounded concurrency | Concurrent success, worker exception, later work | Active workers never exceed configured bound; one failure does not stall queue |
| Session bootstrap | Voice connection and context configuration | First-attempt success, transient failure, retry exhaustion | One retry only; exhaustion transfers or safely fails and closes once |
| Inbound audio | Media sequence integrity | Normal chunks, gaps, duplicates, out-of-order chunks | Gaps observed; duplicates not forwarded; valid later audio continues |
| Outbound audio | Playback tracking | Audio send and playback mark | Exact item/timestamp mark emitted after audio |
| Barge-in | Caller interrupts assistant | Marked audio, no mark, repeated speech-start event | Buffer cleared, response cancelled, model item truncated exactly once |
| Frontend/backend separation | Spoken interruption while a Sol delegation is active | Live output active; backend request still valid | Playback stops, but delegation is not cancelled merely due to overlap |
| Delegation invalidation | Caller corrects or requests escalation | Active delegation and late completion | Backend cancel requested once; completion is classified stale and rejected |
| Stale tool request | Invalid delegation requests a guarded tool | Read request after correction/escalation | Tool never executes and `delegation.stale` is emitted |
| Stale in-flight result | Read began before a correction | Worker completes after invalidation | Completion remains observable but is not returned to Live or spoken |
| Stage attribution | Live, delegation, backend, tool, commentary, playback, close | Normal and stale flows | Stable event names plus monotonic timestamps distinguish every stage |
| Tool dispatch | I/O outside actor loop | Slow read while caller interrupts | Actor processes control events while worker remains blocked |
| Tool idempotency | Duplicate operation identifier | Pending duplicate and completed duplicate | Control plane executes operation once |
| Read failure | Safe typed error | Timeout or exception | Failed read result returned; no reconciliation state |
| Write failure | Unknown side effect | Timeout before caller hangup and after caller hangup | Never retried blindly; terminal state requires reconciliation |
| Tool correlation | Result belongs to request | Mismatched operation identifier | Fails closed; write becomes unknown, not successful |
| Caller hangup | Lifecycle during I/O | No operation; authorized write in flight | Transports close once; authorized write settles; result recorded |
| Voice failure | Provider loss | Idle call and write in flight | Current call transfers/fails; in-flight write still drains safely |
| Telemetry | Non-blocking and private | Sink exception, nested sensitive fields | Call continues; sensitive values are redacted |
| Unknown provider events | Forward compatibility | Unrecognized event kind | Event observed without mutating state or crashing call |
| Webhook deduplication | One actor per provider call | Simultaneous duplicate delivery | Both requests receive the same actor; bootstrap runs once |
| Conflicting duplicate | Same call ID, different metadata | Inbound/outbound conflict | Request is rejected rather than attached to wrong state |
| Parallel session isolation | Per-call ownership | 24 simultaneous calls | Audio and failures stay within their owning call |
| Shared backend pressure | Cross-call worker limit | 12 calls submit work together | Only configured number of workers run simultaneously |
| Direction coexistence | Future outbound compatibility | Inbound and outbound sessions together | Direction reaches bootstrap without shared-state collision |
| Supervisor shutdown | Cleanup | Multiple live actors | Every actor closes once; no session retains mutable work |

## Deliberately deferred provider contract tests

The following should be added with the selected Twilio and GPT-Live adapters. Realtime 2.1 must pass the same facade contract if the bounded fallback is activated.

### Telephony adapter

- validates authentic and forged webhook/WebSocket signatures;
- maps connected, start, media, DTMF, mark, stop, and unknown events;
- rejects a stream identifier belonging to another call;
- preserves G.711 mu-law bytes without headers or accidental transcoding;
- emits clear and mark messages with the active stream identifier;
- handles duplicate callbacks and provider retry delivery; and
- maps disconnect and transfer failures to typed session events.

### Voice-engine adapter

- maps audio deltas and response identifiers correctly;
- maps speech-start and speech-stop events;
- cancels and truncates exactly the response currently playing;
- maps `live.session_started`, `delegation.created`, `backend.response_started`, `backend.tool_requested`, `backend.tool_completed`, `delegation.completed`, `delegation.cancelled`, `delegation.stale`, `live.commentary_appended`, and `live.session_closed`;
- correlates delegated work by `delegation_id` and control-plane `state_revision`;
- cancels semantic stale work without treating ordinary acoustic overlap as a correction;
- keeps caller-heard Twilio playback marks separate from generated Live audio and Sol backend progress;
- maps tool calls and tool results without argument mutation;
- distinguishes connection failure from unknown tool outcome;
- handles provider rate-limit and session-expiry events; and
- redacts provider payloads from ordinary logs.

### Small integration tests

- provider media event -> actor -> voice-engine audio input;
- voice audio -> actor -> provider media plus playback mark;
- caller interruption -> provider clear plus voice cancel/truncate;
- tool request -> control-plane guard -> result returned to voice engine; and
- parallel provider streams remain isolated by call and stream identifiers.

## GPT-Live spike acceptance test

The 60-minute integration spike passes only when one trace proves: Twilio connectivity; Sol-low delegation; a guarded availability read; correction superseding stale work; exact confirmation before the write; read-after-write verification before spoken success; working Twilio `clear`/`mark` barge-in; and attributable frontend, backend, tool, playback, and close timings. A missing foundational item triggers the Realtime 2.1 fallback rather than extending the spike.

## Scope decision

The company task asks for a real multi-turn voice conversation but does not require both inbound and outbound calling. The submission demonstrates inbound calling. Outbound is kept in the descriptor and concurrency contracts, but actual dialing, consent, and campaign mechanics remain outside the weekend slice unless the core evaluation loop is already complete.
