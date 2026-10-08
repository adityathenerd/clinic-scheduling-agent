# GPT-Live vs Realtime: Cost and Value Analysis

Status: Decision analysis complete; selected path implemented
Last updated: 2026-10-08
Scope: OpenAI inference cost and architectural value for the clinic-scheduling submission

## Decision and outcome

The selected GPT-Live/Sol-low path and Twilio transport were subsequently
implemented and exercised in live outbound calls. The original go/no-go analysis
below remains useful as the evidence and fallback record.

Prefer **GPT-Live with a GPT-6 Sol backend at `reasoning.effort: "low"`** for the first implementation attempt. Keep **OpenAI Realtime as the bounded model-layer fallback** if the Live integration does not clear a 60-minute connectivity, delegation, interruption, and traceability spike. Treat telephony separately: the current Twilio free trial blocks `<Stream>`, so the existing account must pass a capability check before Twilio is accepted as the demo transport.

This reverses the earlier default. The reason is not that GPT-Live is cheaper per call—it usually is not. The reason is that its incremental cost is small in absolute terms for a five-minute call, while it creates the exact boundary the project wants:

```text
GPT-Live
  owns listening, speaking, overlap, interruption, and conversational pacing

GPT-6 Sol backend
  owns task reasoning and tool selection within the workflow

Application control plane
  owns identity, permissions, confirmation, idempotency, state, and recovery

Fake scheduler
  owns authoritative appointment state
```

The submission is judged on safety, agent design, evaluation quality, and a closed improvement loop. GPT-Live makes those responsibilities more legible than placing both conversation and task reasoning in one Realtime session.

## Current official prices

As of 2026-10-05, OpenAI lists:

| Component | Standard price |
|---|---:|
| `gpt-live-1` voice session | $0.05 per active session minute |
| `gpt-realtime-2.1` audio input | $32 per 1M tokens |
| `gpt-realtime-2.1` audio output | $64 per 1M tokens |
| `gpt-realtime-2.1-mini` audio input | $10 per 1M tokens |
| `gpt-realtime-2.1-mini` audio output | $20 per 1M tokens |
| `gpt-6-luna` backend input | $0.10 per 1M tokens |
| `gpt-6-luna` backend output | $0.50 per 1M tokens |
| `gpt-6-sol` backend input | $2 per 1M tokens |
| `gpt-6-sol` backend output | $10 per 1M tokens |

GPT-Live is billed for the complete active session: patient speech, assistant speech, silence, and time waiting for backend work. It is billed per second rather than rounded to whole minutes. Backend model and tool usage is separate.

Realtime is billed when responses are created. OpenAI documents user audio at approximately one token per 100 ms and assistant audio at approximately one token per 50 ms. Empty audio filtered by VAD does not add input audio tokens. The entire conversation is input to later responses, so later turns can cost more; prompt caching and truncation affect the result.

Sources: [OpenAI pricing](https://developers.openai.com/api/docs/pricing) and [voice cost optimization](https://developers.openai.com/api/docs/guides/voice-latency-cost).

## Five-minute call model

The comparison needs an explicit conversation shape. This model assumes:

- five minutes from session start to closure;
- patient speaks for 45% of elapsed time: 2.25 minutes;
- assistant speaks for 35%: 1.75 minutes;
- silence, overlap, and tool wait consume 20%: one minute;
- the GPT-Live backend consumes 8,000 uncached input tokens and 1,000 output tokens in total;
- short-context Standard pricing;
- no paid hosted tools;
- telephony, scheduler infrastructure, and custom-service costs excluded because they are common to both options; and
- no prompt-cache discount.

These are planning assumptions, not measured usage.

### GPT-Live calculation

```text
Voice frontend = 5 minutes × $0.05                     = $0.2500

Luna backend   = 8,000 × $0.10 / 1M
               + 1,000 × $0.50 / 1M                  = $0.0013

Sol backend    = 8,000 × $2.00 / 1M
               + 1,000 × $10.00 / 1M                 = $0.0260
```

| GPT-Live configuration | Estimated five-minute cost |
|---|---:|
| GPT-Live + GPT-6 Luna | $0.2513 |
| GPT-Live + GPT-6 Sol | $0.2760 |

Moving from Luna to Sol adds about **2.47 cents per five-minute call**, or roughly **9.8%** of the Live-plus-Luna total. The voice session, not backend reasoning, dominates the bill.

### Realtime calculation

```text
Patient audio tokens   = 2.25 minutes × 600 tokens/minute  = 1,350
Assistant audio tokens = 1.75 minutes × 1,200 tokens/minute = 2,100

Realtime 2.1 audio floor = 1,350 × $32 / 1M
                         + 2,100 × $64 / 1M                = $0.1776

Realtime 2.1 mini floor  = 1,350 × $10 / 1M
                         + 2,100 × $20 / 1M                = $0.0555
```

These are **marginal audio lower bounds**, not complete session estimates. They omit text tokens, special tokens, optional transcription, and repeated conversation-history input across turns. Automatic caching can offset part of that history cost. A reliable Realtime estimate must come from measured usage logs for the intended prompt and tools.

## Direct comparison

| Configuration | Five-minute planning cost | Relative to full Realtime audio floor | What the number omits |
|---|---:|---:|---|
| Realtime 2.1 mini | $0.0555 floor | 0.31x | History/text and the cost of lower reliability |
| Realtime 2.1 | $0.1776 floor | 1.00x | History/text, optional transcription, cache effects |
| GPT-Live + Luna | $0.2513 | 1.41x | Custom service and telephony costs |
| GPT-Live + Sol | $0.2760 | 1.55x | Custom service and telephony costs |

At 1,000 five-minute calls, the planning totals are:

| Configuration | Approximate inference cost |
|---|---:|
| Realtime 2.1 audio floor | $177.60 plus context/text |
| GPT-Live + Luna | $251.30 |
| GPT-Live + Sol | $276.00 |

Against the deliberately conservative Realtime floor, the GPT-Live premium is approximately **$73.70 per 1,000 calls with Luna** or **$98.40 with Sol**. Actual Realtime context charges reduce that gap; idle GPT-Live time widens it.

## Sensitivity: what changes the answer

### Silence and tool-wait time

GPT-Live charges for active session time even during silence and backend work. Realtime VAD can avoid charging empty input audio. Long holds, slow tools, or failure to close sessions promptly make GPT-Live less attractive.

Control:

- preload stable context;
- keep scheduler reads fast;
- speak a short acknowledgment only when useful;
- close completed sessions immediately;
- measure backend time separately; and
- do not mistake “I am checking” latency for time to the answer the patient needs.

### Conversation verbosity

Realtime output audio is the largest part of its marginal speech cost in this model. Shorter assistant turns reduce both price and patient frustration. GPT-Live's duration pricing also rewards concise calls, although it does not distinguish silence from speech.

### Realtime conversation history

Realtime sends conversation context into later responses. Long, many-turn calls can materially exceed the raw-audio floor. Stable instructions improve cacheability; aggressive conversation edits or frequent prompt changes can reduce it.

### Backend model

The backend choice barely affects the total GPT-Live cost at this workload. Optimizing from Sol to Luna saves only about 2.47 cents per representative call. It should happen only after Luna matches Sol on:

- tool-argument correctness;
- correction handling;
- hard-gate compliance;
- timeout and reconciliation behavior; and
- final scheduler state.

### Mini Realtime

Realtime mini is dramatically cheaper, but OpenAI notes a tradeoff in instruction following and function calling. It is not an equivalent comparator until it passes the complete safety suite. A critical booking error cannot be justified by a lower token bill.

## Cost versus value

### Value GPT-Live adds

| Value | Why it matters here |
|---|---|
| Meaningful separation of concerns | Speaking behavior and task policy change for different reasons and can be evaluated independently |
| Stronger reasoning without replacing the voice layer | The backend can use Sol now and a cheaper model later without rewriting telephony or turn-taking |
| Full-duplex conversation | The patient can interrupt or correct the agent while backend work is in progress |
| Application-owned authorization | The application still checks permissions, confirmation, and records before protected actions |
| Cleaner evaluation evidence | Frontend timing, delegation, backend reasoning, tools, and final state can be measured as separate stages |
| Better improvement targeting | A failed run can be classified as conversation, delegation, workflow, tool-contract, or scheduler failure |
| Reduced prompt conflict | Short conversational instructions remain in Live; detailed business rules remain in the backend/control plane |
| Reusable non-voice core | The same backend and eval harness can run in text mode without the live voice frontend |

OpenAI's guidance describes exactly this split: GPT-Live handles spoken interaction, a separate backend reasons and uses tools, and the application controls permissions and business records. It also specifically advises waiting for backend results before confirming a booking. See [GPT-Live architecture](https://developers.openai.com/api/docs/guides/voice-agents), [delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation), and [GPT-Live prompting](https://developers.openai.com/api/docs/guides/live-prompting).

### Costs and risks GPT-Live adds

| Cost or risk | Control |
|---|---|
| Two model lifecycles instead of one | Use Responses delegation first; keep one Python process and one OpenAI SDK |
| Delegated work may outlive a correction | Track delegation IDs; cancel or invalidate stale work; tool boundary rejects stale proposals |
| Acknowledgment may hide backend latency | Measure first useful result separately from first audio |
| Session duration charges include silence | Preload, bound tool time, close promptly, and report duration |
| Newer integration than RiderPal's Realtime bridge | Enforce a 60-minute spike and retain the Realtime adapter |
| More event types and state joins | Normalize both providers behind the existing `VoiceEngine` and event contracts |
| Frontend can speak while task state changes | Spoken success is permitted only after backend verification; playback and application state are asserted separately |

## Break-even framing

Using the five-minute assumptions, GPT-Live + Sol costs at most about **9.84 cents more per call** than the full Realtime raw-audio floor. GPT-Live + Luna costs about **7.37 cents more**.

That gives a transparent operational break-even test:

- If Live prevents one manual correction, avoidable escalation, or repeat call per 100 calls, the avoided incident needs to be worth at least **$9.84 with Sol** or **$7.37 with Luna** to recover the conservative premium.
- If it prevents one such incident per 50 calls, the threshold falls to **$4.92 with Sol** or **$3.69 with Luna**.

No claim is made that GPT-Live will produce that improvement. The evaluation harness must measure task success, hard-gate failures, correction recovery, and final state. The point is that the economic question is testable and the inference premium is small compared with many forms of human rework.

For the take-home itself, expected volume is negligible. The relevant return is stronger architectural evidence: the demo visibly separates natural voice behavior, backend reasoning, deterministic authorization, and state verification.

## Recommended configuration

### First implementation

- Python 3.11+ only.
- `gpt-live-1` voice frontend over a server-side WebSocket suitable for telephony audio.
- Responses delegation to the documented `gpt-6-sol` Live backend at the lowest reasoning effort that passes the suite.
- Application-owned custom functions and control plane.
- Fake scheduler as the authoritative state store.
- Same JSONL event schema used by the non-voice harness.
- Twilio transport behind the existing `TelephonyAdapter` boundary.

Sol is preferred for the submission because the absolute premium over Luna is only about 2.47 cents in the planning call, while instruction adherence and reasoning are part of what the exercise is assessing. Luna becomes a later optimization candidate after measured parity.

### Why “Sol light” means Sol at low effort

There is no separate “light” Sol model in the current API. The supported control is `reasoning.effort`. GPT-6 Sol supports `none`, `low`, `medium`, `high`, `xhigh`, and `max`; `medium` is the model default. OpenAI describes `low` as appropriate for tool use, multi-step execution, and customer-support or chat-assistant workflows while optimizing speed and cost.

Use:

```python
"model": "gpt-6-sol",
"reasoning": {"effort": "low"}
```

Do not use `none` initially. Appointment correction, exact confirmation, tool sequencing, and unknown-write recovery benefit from some reasoning. The eval suite may later prove that selected read-only turns can safely use Luna or Sol with `none`, but the first configuration should remain uniform and explainable.

See [reasoning effort guidance](https://developers.openai.com/api/docs/guides/reasoning) and [GPT-Live delegation settings](https://developers.openai.com/api/docs/guides/live-delegation).

### Terra assessment

`gpt-5.6-terra` is technically available through the Live endpoint and supports reasoning and function calling. It is not the preferred backend:

| Backend | Input / 1M | Output / 1M | Positioning | Decision |
|---|---:|---:|---|---|
| GPT-6 Luna | $0.10 | $0.50 | Efficient focused/high-volume work | Later cost challenger |
| GPT-6 Sol | $2.00 | $10.00 | Complex agentic workflows | Primary at low effort |
| GPT-5.6 Terra | $2.00 | $12.00 | Older balance tier, roughly earlier-family mini | Reject for this build |

Terra has no price advantage over Sol for input and costs more for output. It also weakens the story by selecting an older model without a measured capability benefit. Retain it only as an experimental benchmark if account-specific latency proves materially better; do not add it to the implementation path.

See [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra), [GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol), and [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna).

### Fallback

Use `gpt-realtime-2.1`, not mini, if the GPT-Live spike fails. Preserve all application-owned guards. Consider mini only after it independently passes every critical scenario.

## Preserved sixty-minute go/no-go spike

The first ten minutes are a transport gate. Check whether the existing Twilio account can execute a minimal `<Connect><Stream>` call with already-provisioned, verified numbers. Current trial documentation blocks `<Stream>` even though it allows limited inbound and outbound calling, and purchasing a number is documented as upgrade-only. Do not purchase or upgrade anything as part of the spike. If streaming is blocked, switch the demo to synthetic or browser audio and continue evaluating GPT-Live; switching to Realtime would not remove the same Twilio transport restriction.

GPT-Live remains the primary path only if the Python spike proves:

1. A server-side Live WebSocket connects and streams telephone-compatible audio.
2. An appointment request delegates to the Sol backend.
3. The backend requests the application-owned slot-search tool.
4. A patient correction invalidates stale delegated work.
5. Exact confirmation is required before the write tool succeeds.
6. The final scheduler state is verified before Live announces success.
7. Barge-in stops or supersedes stale spoken output.
8. Events identify session time, delegation latency, tool time, first useful result, and playback.

If any foundational item is still unresolved at 60 minutes, switch to the existing Realtime path. This protects the weekend ceiling without discarding the stronger target architecture.

## Measurement plan

The before/after provider comparison should report:

```text
API cost per call
API cost per successful call
call duration
backend tokens and model
task pass rate
critical failures
unnecessary delegations
tool argument accuracy
duplicate writes
time to first audio
time to first useful answer
interruption recovery
final scheduler-state match
human or fallback rate
```

Provider choice is accepted on **cost per safe successful task**, not the cheapest nominal minute.

## Sources and limits

- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [Voice usage and cost optimization](https://developers.openai.com/api/docs/guides/voice-latency-cost)
- [GPT-Live getting started](https://developers.openai.com/api/docs/guides/live)
- [GPT-Live delegation](https://developers.openai.com/api/docs/guides/live-delegation)
- [GPT-Live prompting](https://developers.openai.com/api/docs/guides/live-prompting)
- [Voice-agent architecture comparison](https://developers.openai.com/api/docs/guides/voice-agents)
- [Migration from Realtime to GPT-Live](https://developers.openai.com/api/docs/guides/live-migration)
- [Current GPT-6 model guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [GPT-6 Sol model and pricing](https://developers.openai.com/api/docs/models/gpt-6-sol)
- [Twilio trial Voice restrictions](https://www.twilio.com/docs/usage/trials/try-out-voice)
- [Twilio error 21404: phone-number purchase requires an upgraded account](https://www.twilio.com/docs/api/errors/21404)
- [RiderPal outbound Twilio Media Streams implementation](https://github.com/adityathenerd/riderpal-ai/blob/main/main.py)

Prices, models, and API behavior can change. Recalculate from current official pricing before any production estimate. The figures above are scenario estimates, not invoices or guarantees.
