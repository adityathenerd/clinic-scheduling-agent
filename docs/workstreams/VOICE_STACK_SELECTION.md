# Voice Stack Selection

Status: Workstream recommendation complete  
Last updated: 2026-10-05  
Decision owner: voice-stack workstream  
Decision horizon: six-to-eight-hour submission build, not production procurement

## Recommendation

Use **GPT-Live with a GPT-6 Sol backend at `reasoning.effort: "low"`** for the first submission implementation attempt, entirely in **Python 3.11+**. Keep scheduling policy, authorization gates, writes, reconciliation, and final-state verification in the application-owned control plane.

Run one **60-minute GPT-Live spike** against the existing control-plane contract. The live model owns listening, speaking, and turn-taking while delegating business reasoning and tools to Sol. Start the spike with a Twilio account-capability gate: current Twilio free trials explicitly block the `<Stream>` verb required by the Media Streams bridge. Continue only if the existing account can open a minimal stream and the full spike then proves call connection, interruption, delegation, confirmation-bound tools, and traceability. Otherwise, keep the same voice and control-plane design but move the demo transport to a local or browser audio path; use OpenAI Realtime only if the OpenAI Live integration itself fails.

If Hindi, Hinglish, or another Indian language becomes a hard demo requirement, use **Sarvam Voice Agents** as the fallback. Use **ElevenLabs Agents** only if the priority changes to the fastest managed telephony and polished voice experience. Do not select **Rumik Silk** as the primary stack; it is primarily a TTS component, not an equivalent end-to-end reasoning and tool-use platform.

```text
Preferred path

Twilio, only after the Stream capability gate
   -> voice-engine adapter
   -> GPT-Live conversation frontend
   -> GPT-6 Sol delegated backend
   -> application-owned authorization/control plane
   -> guarded tool gateway
   -> fake scheduler

Bounded fallback

Telephony adapter with a proven streaming transport
   -> OpenAI Realtime
   -> application-owned control plane
   -> guarded tool gateway
   -> fake scheduler
```

The voice provider receives no scheduler credentials and cannot authorize a mutation. The write boundary remains responsible for identity, exact confirmation, fresh state, idempotency, and read-after-write verification.

## Decision drivers

The choice is optimized for this exercise, in this order:

1. Preserve deterministic safety and tool authority.
2. Finish a measurable before-and-after improvement loop.
3. Reuse proven transport work and minimize new failure surfaces.
4. Maintain natural interruption and low audible latency.
5. Keep traces sufficient to explain failures.
6. Support the languages required by the actual demonstration.
7. Avoid components whose only benefit is speculative.

This is not a general ranking of voice vendors. A production clinic procurement would require measured call quality, security review, legal terms, regional deployment checks, load tests, support evaluation, and a real cost model.

## Python 3.11 implementation constraint

The repository has one runtime: Python 3.11 or newer. A provider that requires a Node or TypeScript application is unsuitable for this build, even if it has a stronger browser quickstart. On the current Windows host, commands must use the `py` launcher because the `python` alias is unavailable.

| Option | Python path | Implication for this project |
|---|---|---|
| OpenAI Realtime | Official OpenAI Python SDK supports Realtime WebSocket connections with audio and function calling | Best-supported path and closest to RiderPal's existing Python implementation |
| GPT-Live | Official OpenAI Python API exposes Live session creation and connection; the WebSocket guide includes a Python server-side path | Viable in Python, but the delegation and event lifecycle still need a new adapter and spike |
| Sarvam Voice Agents | Official `sarvam-conv-ai-sdk` provides an async typed Python client; Python 3.11 exceeds its documented 3.9+ prerequisite | Viable without adding a JavaScript runtime; avoid optional PyAudio when using telephony/headless audio |
| ElevenLabs Agents | Official `elevenlabs` Python SDK supports conversations and signed connections | Viable, though default local audio extras can introduce PortAudio/PyAudio setup; telephony should use headless streaming |
| Rumik Silk | The official Pipecat integration is a Python package with offline tests and examples | Language-compatible with the repository, but it still introduces Pipecat plus external STT and LLM dependencies |

Primary references: [OpenAI Python Realtime library](https://developers.openai.com/api/reference/python), [OpenAI Python Live API](https://developers.openai.com/api/reference/python/resources/live), [OpenAI voice WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets), [Sarvam Python voice SDK](https://docs.sarvam.ai/conversations/deploy/sdks/python), [ElevenLabs Python Agents SDK](https://elevenlabs.io/docs/eleven-agents/libraries/python), and [Rumik Pipecat integration](https://github.com/ira-rumik/pipecat-rumik).

Implementation consequences:

- Use `asyncio`, typed event models, and one Python process for the first vertical slice.
- Do not introduce a Node sidecar for WebSocket or telephony handling.
- Prefer the provider's Python SDK only when it reduces protocol work without hiding events needed by the harness.
- A direct Python WebSocket adapter remains acceptable when it produces a smaller, more observable boundary than an SDK.
- Keep microphone packages such as PyAudio out of the server dependency set unless a local microphone demo becomes necessary.
- Use `py -3.11 -m ...` in Windows instructions and scripts; keep module entry points portable for other hosts.

## Architectural distinction

The options are not interchangeable products.

| Option | Voice architecture | Where reasoning lives | What is consolidated |
|---|---|---|---|
| OpenAI Realtime | Native speech-to-speech | The realtime model, constrained by application tools | Speech understanding, dialogue, speech output, and tool selection in one session |
| GPT-Live | Full-duplex voice frontend plus delegated backend | A separate backend model, agent, or application workflow | Live conversation is consolidated; business reasoning and tools are deliberately separated |
| Sarvam Voice Agents | Managed ASR -> LLM -> TTS loop | Sarvam-managed reasoning and configured workflows | Indian-language speech, orchestration, telephony, testing, and analytics |
| ElevenLabs Agents | Managed STT -> selected LLM -> TTS loop | A built-in, selected, or custom LLM | Voice orchestration, telephony, tools, procedures, simulations, and monitoring |
| Rumik Silk | TTS service | Not provided | Expressive Indian-language speech generation; external STT, LLM, transport, and orchestration remain necessary |

OpenAI's current voice-agent guidance explicitly distinguishes GPT-Live, Realtime, and chained pipelines: GPT-Live uses a separate backend, Realtime handles speech, reasoning, and tools in one session, and a chained pipeline exposes each speech/text stage. See [OpenAI voice-agent architectures](https://developers.openai.com/api/docs/guides/voice-agents).

## Decision matrix

Scores are directional: 1 is weak for this submission and 5 is strong. They reflect documented capabilities plus implementation risk, not an independent benchmark. Vendor latency and benchmark claims remain hypotheses until measured with the same audio, scenario, transport, and backend.

| Option | Consolidation | Reasoning and instruction control | Tool and safety boundary | Latency and interruption | Telephony readiness | Indic/code-mix | Observability and evals | Privacy clarity for prototype | Weekend confidence |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| OpenAI Realtime | 4 | 5 | 5 | 5 | 4 | 3 | 5 | 4 | 5 |
| GPT-Live plus backend | 3 | 5 | 5 | 5 | 4 | 3 | 5 | 4 | 4 with the spike gate |
| Sarvam Voice Agents | 5 | 4 | 4 | 4 | 5 | 5 | 4 | 3 | 4 if access is ready |
| ElevenLabs Agents | 4 | 4 | 4 | 4 | 5 | 4 | 4 | 3 without enterprise controls | 4 |
| Rumik plus external stack | 1 | 1 | 2 | 4 for TTS only | 1 | 5 | 1 | 2 | 1 |

### Why GPT-Live now wins the value decision

- It creates a meaningful boundary between continuous spoken interaction and task reasoning.
- The backend model can be selected for instruction adherence without replacing the voice layer.
- The application still executes functions and owns permissions, confirmation, records, and side effects.
- Frontend timing, delegation, backend work, tools, and final state can be evaluated independently.
- The voice frontend dominates cost, so using Sol instead of Luna adds only a small absolute amount in the representative cost model.
- The same backend, fake scheduler, and evaluation harness can run without voice.

The tradeoff is additional orchestration and duration-based billing during silence or backend work. Realtime remains the safer delivery fallback because RiderPal already demonstrates that transport. Deterministic tool permissions remain mandatory in either architecture; neither prompt is an authorization boundary.

Detailed assumptions and calculations are in [GPT-Live vs Realtime: Cost and Value Analysis](VOICE_COST_VALUE_ANALYSIS.md).

## Provider findings

### OpenAI Realtime

OpenAI documents Realtime as native speech-to-speech with session state, tools, interruption handling, and WebRTC/WebSocket connectivity. A Realtime session can interpret audio, choose a tool, and respond in audio without an intermediate application-owned transcript. Function execution still occurs in the application, which is the correct place for scheduling authorization and records access.

This is the smallest change from RiderPal. Reuse is limited to the transport pattern; the clinical control plane, typed tools, event schema, and scheduler remain new and independently testable.

Current pricing is audio/text token based, so a headline per-minute comparison would be misleading. Measure cost per successful evaluated conversation instead. See [Realtime getting started](https://developers.openai.com/api/docs/guides/realtime), [Realtime tools](https://developers.openai.com/api/docs/guides/realtime-mcp), [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations), [Realtime evaluation guidance](https://developers.openai.com/cookbook/examples/realtime_eval_guide), and [OpenAI API pricing](https://developers.openai.com/api/docs/pricing).

### GPT-Live

GPT-Live is a separate architectural option, not merely another Realtime model. The live model listens, speaks, and decides when to delegate. The backend reasons, uses tools, and returns results. OpenAI's documentation recommends keeping conversational behavior in the live prompt and detailed business rules and tool workflows in the backend. The application still checks permissions, obtains confirmations, accesses records, and saves task progress.

That separation closely matches this project's constitution:

- GPT-Live owns natural conversation and full-duplex turn-taking.
- The backend owns the state machine and reasoning about the scheduling task.
- The application owns authorization and side effects.

The drawback is a new voice/backend boundary and migration work. The existing RiderPal Realtime relay is evidence for the general streaming pattern, but it is not proof that GPT-Live is a drop-in replacement. Backend work may also continue while the caller interrupts, so the application needs an explicit cancel-or-finish policy for delegated work.

Current official pricing bills the GPT-Live voice session per second at a listed per-minute rate, while backend model and tool usage is additional. See [GPT-Live getting started](https://developers.openai.com/api/docs/guides/live), [GPT-Live partner integrations](https://developers.openai.com/api/docs/guides/live-partner-integrations), [voice-agent architectures](https://developers.openai.com/api/docs/guides/voice-agents), and [OpenAI API pricing](https://developers.openai.com/api/docs/pricing).

### Sarvam Voice Agents

Sarvam is the strongest consolidated India-oriented candidate. Its documentation describes a real-time ASR-to-LLM-to-TTS loop, inbound and outbound telephony, tools, tests, analytics, eleven Indian languages plus English, and code-mixed speech. It explicitly lists appointment booking as a common use case and says the voice stack's models are self-hosted by Sarvam with Indian data residency.

The managed consolidation is valuable if Indian-language behavior is central. The tradeoff is less ownership of intermediate stages and platform-specific workflow behavior. Built-in simulation and judging are useful, but they do not replace the repository harness. A transcript can sound correct while the wrong appointment was written; the external harness must still assert tool order, authorization evidence, and final scheduler state.

Privacy and retention must be verified at the product and contract level before any PHI is used. Documentation or marketing claims do not establish that a particular account configuration satisfies clinic obligations. See [Sarvam Voice Agents overview](https://docs.sarvam.ai/conversations/overview), [runtime architecture](https://docs.sarvam.ai/conversations/build/run-time), [telephony](https://docs.sarvam.ai/conversations/deploy/telephony), [tests](https://docs.sarvam.ai/conversations/build/tests), and [Sarvam privacy policy](https://www.sarvam.ai/privacy-policy).

### ElevenLabs Agents

ElevenLabs offers a mature managed platform: telephony, configurable LLMs, tools, structured procedures, simulations, repeated tests, experiments, analytics, and OpenTelemetry traces. It also exposes a broad set of telephony integrations, including Twilio, Plivo, and Exotel.

Its strength is speed to a polished voice experience. Its weakness for this exercise is that core reasoning quality depends on the selected LLM, and a managed platform can hide some of the exact engineering judgment the assignment asks to demonstrate. The repository should retain its own fake scheduler, event trace, hard-gate assertions, and improvement artifact even if ElevenLabs supplies conversation simulations.

ElevenLabs documents HIPAA-eligible configurations using a BAA and Zero Retention Mode, but those are not automatic properties of an arbitrary prototype account and may constrain available features. The demo remains synthetic-only. See [ElevenAgents overview](https://elevenlabs.io/docs/eleven-agents/overview), [tools](https://elevenlabs.io/docs/eleven-agents/customization/tools), [structured procedures](https://elevenlabs.io/docs/eleven-agents/customization/procedures/structured-procedures), [agent testing](https://elevenlabs.io/docs/eleven-agents/customization/agent-testing), [OpenTelemetry traces](https://elevenlabs.io/docs/eleven-agents/customization/opentelemetry-traces), [telephony](https://elevenlabs.io/docs/eleven-agents/phone-numbers/sip-trunking), and [HIPAA guidance](https://elevenlabs.io/docs/eleven-agents/legal/hipaa).

### Rumik identity resolution

The product is [Rumik](https://rumik.ai/), and the relevant offering is **Silk**. Its public API is positioned as real-time text-to-speech for English and Indian languages. The official Pipecat repository implements Rumik as a TTS service; the example pipeline still needs other services for speech recognition, reasoning, and transport.

Rumik should therefore not be compared as if it were an end-to-end alternative to Realtime, GPT-Live, Sarvam Voice Agents, or ElevenLabs Agents. It is a plausible later voice-output substitution if Indian-language expressiveness becomes important. Selecting it now would add STT, LLM, orchestration, transport, credentials, monitoring, and failure boundaries—the opposite of the stated simplicity goal.

See [Silk API](https://rumik.ai/silk-api), [Silk research](https://rumik.ai/research/silk), [Rumik's Pipecat integration](https://github.com/ira-rumik/pipecat-rumik), and [Rumik privacy policy](https://rumik.ai/privacy-policy).

## Required system boundary

Provider selection must not change these invariants:

```text
Voice provider
  may: converse, extract intent, request a tool
  may not: hold scheduler credentials, authorize a write, declare unverified success

Control plane
  owns: workflow state, active proposal, confirmation binding, escalation policy

Tool gateway
  owns: validation, authorization checks, idempotency, timeout semantics

Scheduler
  owns: availability and appointment truth

Evaluation harness
  owns: deterministic assertions over transcript, events, tool trace, and final state
```

This boundary keeps the most consequential blast radius independent of the voice vendor. A provider can be replaced without rewriting the clinic's definition of an authorized booking.

## Validation spike

### Twilio account-capability gate

Current Twilio trial documentation permits inbound and outbound trial calls only with verified numbers, but its custom-TwiML allowlist explicitly blocks `<Stream>` and `<ConversationRelay>`. The critical question is therefore not whether a trial number can receive a call; it is whether this existing account is upgraded or grandfathered and can actually execute `<Connect><Stream>`.

Use the first ten minutes of the spike to:

1. Inspect account status and Active Numbers without changing the account.
2. Attempt one minimal `<Connect><Stream>` call using only already-provisioned and verified test numbers.
3. Record the exact provider response and stop if error `21404` or a trial-verb restriction appears.

No number purchase, account upgrade, or other spend is authorized by this plan. If streaming works, continue with Twilio. If it is blocked, do not debug GPT-Live through a transport Twilio has disabled: demonstrate the same adapter and control plane with synthetic audio or browser WebRTC, and leave telephony as an explicit deployment prerequisite. RiderPal's outbound-callback pattern does not bypass this restriction because it also depends on `<Connect><Stream>`.

### Time box

- Twilio capability detection: first 10 minutes, inside the total time box.
- GPT-Live: 60 minutes total maximum for transport capability, connection, delegation, tool, interruption, and trace evidence.
- OpenAI Realtime: use as the model-layer fallback if the Live API blocks; it does not solve a Twilio `<Stream>` restriction.
- Sarvam: evaluate only if Indian-language acceptance criteria are confirmed and account access is already available.
- Do not spend submission time integrating every provider.

### Fixed scenarios

Run each viable option against the same synthetic profile and fake scheduler:

1. Straightforward booking.
2. Ambiguous relative date such as “next Friday.”
3. Caller interrupts and corrects the chosen time.
4. Offered slot disappears before the write.
5. Caller gives assent before all material details are read back.
6. Write commits but its response times out.
7. Urgent symptom triggers approved escalation and no medical advice.
8. Hindi/Hinglish switching, only if multilingual behavior is in scope.

### Measurements

- Scenario pass rate.
- Critical or forbidden mutation count; required result is zero.
- Tool-name and normalized-argument accuracy.
- Final scheduler-state match.
- Duplicate-write count.
- Time to first audible response and first useful response, reported as measured p50/p95.
- Interruption-to-stopped-playback latency.
- Whether the corrected intent wins after barge-in.
- Entity accuracy for names, dates, times, and providers.
- Dropped or unrecoverable sessions.
- Trace completeness: can a failed run be explained without guessing?

### Promotion gates

GPT-Live remains the primary implementation only if:

- integration completes inside the spike time box;
- every tested consequential action has correct final state;
- no hard gate is violated;
- no duplicate write occurs under retry or unknown-outcome faults;
- interruption and correction work reliably;
- delegated work can be cancelled or reconciled deliberately;
- the event trace identifies frontend, backend, tool, and playback timing; and
- the control-plane and fake-scheduler interfaces remain unchanged.

Sarvam becomes the primary choice only if Indic/code-mix is a real acceptance criterion, provider access is ready, and product-specific privacy and tool behavior are acceptable. ElevenLabs becomes primary only if managed telephony is newly prioritized over ownership of the voice architecture.

## Cost interpretation

Published prices are not directly comparable:

- OpenAI Realtime is billed using audio and text tokens.
- GPT-Live bills the live frontend by session time; backend model and tools are additional.
- ElevenLabs platform minutes may exclude LLM and telephony costs.
- Sarvam publishes bundled Voice Agents pricing, subject to the current plan and terms.
- Rumik's cited public pricing is primarily for TTS, not the missing STT, reasoning, transport, and orchestration components.

The useful prototype metric is **cost per successful, safety-gate-clean evaluated conversation**, with transport and backend costs included.

## Privacy position

- Use synthetic identities, clinic data, and symptoms for the weekend build.
- Do not record raw calls by default.
- Do not log complete prompts, audio payloads, phone numbers, patient details, credentials, or unsanitized provider events.
- Treat vendor HIPAA, retention, and residency statements as configuration- and contract-dependent.
- Do not claim that selecting a vendor makes the application HIPAA-, DPDP-, or otherwise compliant.
- Before real patient use, verify the exact product, region, subprocessors, retention mode, BAA/DPA, deletion behavior, support access, and incident obligations.

OpenAI's API data-control documentation is at [Your data](https://developers.openai.com/api/docs/guides/your-data). Equivalent product-specific terms must be verified for any alternative provider.

## Assumptions and open decisions

| Item | Current assumption | Revisit when |
|---|---|---|
| Patient data | Synthetic only | Before any real patient pilot |
| Primary language | English is sufficient for the core evaluation | Demo requires Hindi/Hinglish or another language |
| Telephony | Twilio is preferred only if the existing account passes a minimal `<Stream>` capability check | Current trial restrictions block streaming, or a different already-available transport materially reduces implementation risk |
| Call direction | Inbound demonstration | Product scope requires outbound consent/dialing behavior |
| Human transfer | May be simulated in the evaluated core | A real front-desk destination is available |
| Availability | May be prefetched as a hint, always refreshed before action | Scheduler offers a stronger reservation contract |
| Framework | No LangGraph requirement | Executable transition logic becomes clearer with it |
| Provider judge | Advisory for conversational quality | Never authoritative for final-state safety |

## Rejected alternatives

### Build a modular STT plus LLM plus TTS pipeline now

Rejected for the weekend because it adds components, queues, credentials, billing surfaces, observability joins, and latency attribution work without proving a requirement that native speech-to-speech cannot meet. Revisit when intermediate transcript inspection, provider substitution, or language quality becomes a measured need.

### Move scheduling policy into the voice platform

Rejected because it would couple provider configuration to the clinic's authorization model and weaken deterministic evaluation. Platform procedures may assist conversation flow, but the application-owned tool gateway remains authoritative.

### Select the vendor with the best claimed latency

Rejected because vendor measurements are rarely comparable across transport, audio, prompts, tools, and geography. The spike measures useful audible latency and task correctness using a fixed local scenario.

### Integrate Rumik for the first slice

Rejected because TTS quality alone does not close the scheduling and improvement loop. It materially increases the number of systems that must be configured and debugged.

## Workstream decision

1. **Commit path:** Python 3.11+, GPT-Live, a GPT-6 Sol delegated backend at low reasoning effort, a transport adapter, application-owned authorization/control plane, guarded tools, and fake scheduler. Twilio is the preferred transport only after the account passes the `<Stream>` capability gate.
2. **Bounded fallbacks:** use local or browser audio when Twilio streaming is unavailable; use OpenAI Realtime when the Live model integration fails. Both preserve the same control-plane contract.
3. **Language-driven fallback:** Sarvam Voice Agents through its Python SDK.
4. **Managed-experience alternative:** ElevenLabs Agents through its Python SDK.
5. **Deferred component experiment:** Rumik Silk through its Python Pipecat integration as a later TTS option.

Backend alternatives are deliberately narrow. GPT-6 Luna is the cost challenger after the safety suite passes. GPT-5.6 Terra is not selected: its documented input price matches GPT-6 Sol, its output price is higher, and its older balance-tier positioning does not create a compensating architectural benefit.

No account was created, no credentials were requested, no call was placed, and no external service was deployed as part of this research.

Current deployment-gate sources: [Twilio trial Voice restrictions](https://www.twilio.com/docs/usage/trials/try-out-voice), [Twilio error 21404](https://www.twilio.com/docs/api/errors/21404), and [RiderPal's outbound `<Connect><Stream>` implementation](https://github.com/adityathenerd/riderpal-ai/blob/main/main.py).
