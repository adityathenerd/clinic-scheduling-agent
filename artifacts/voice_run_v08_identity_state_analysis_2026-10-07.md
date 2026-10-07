# Voice Run V08 Identity-State Analysis — 2026-10-07

## Verdict

The observed call was a safety pass but a conversational failure. Identity was
semantically affirmed once, the application transitioned to `active`, and the
immutable identity key was locked. The assistant then contradicted that state
by saying it was waiting for verification and reset the outbound callback to a
generic help prompt. No protected data was disclosed and no appointment tool or
mutation ran.

Call ID: `CA50980141a6a9fb71c30e12162eaa6d54`

## Root cause

The GPT-Live startup prompt contained a mutable workflow snapshot saying
identity was unverified. Python later advanced the authoritative state, but the
Live session retained the old snapshot. The transition was communicated with
paraphrasable commentary, leaving the model to reconcile contradictory state.

This was a split-brain control boundary:

- Python: `active`, identity key locked.
- Live startup instructions: `awaiting_name_confirmation`, key unset.
- Application-to-model handoff: free-form commentary rather than an exact
  gate continuation.

## Reinforcement applied

1. Removed mutable workflow state from GPT-Live startup instructions.
2. Made the latest application instruction authoritative over older
   conversational assumptions.
3. Made ordinary outbound identity success an exact, direction-aware callback
   continuation rather than a generic model prompt.
4. Routed every application directive through trusted instructions; exact
   replies are never sent as paraphrasable commentary.
5. Cancelled concurrent delegations while application-owned intent, identity,
   confirmation, and follow-up gates are pending.
6. Added complete caller-turn audit events independent of tool delegation.
7. Added normalized SHA-256 contracts for exact replies so the evaluator can
   compare what Python required with what the caller heard without logging the
   expected patient-facing text twice.
8. Added hard failures for post-lock identity contradictions, outbound-purpose
   resets, and violations of exact application replies.

## Before/after evaluation evidence

Replaying the preserved failed call now produces three actionable hard
failures:

```text
workflow_incomplete_at_hangup
identity_state_contradiction
outbound_post_identity_purpose_reset
```

Verification after reinforcement:

- Focused runtime, actor, and transcript-evaluator tests: 78/78 passed.
- Full repository tests: 307/307 passed.
- Deterministic reinforced evaluation: 11/11 scenarios passed.
- Negative controls: 16/16 detected.
- Promotion gate: eligible.
- Credential-free scheduling happy path: completed with a held proposal and
  verified proposed state.
- Local voice `/healthz`: HTTP 200.
- Public ngrok `/healthz`: HTTP 200.
- Dashboard snapshot: HTTP 200.
- Twilio account: active trial; configured number voice-capable.

## Live rerun status

The first post-fix call attempt, `CA764e6ddbe683d3046cd07c165d5a3e17`,
ended at Twilio with `status=no-answer`, `duration=0`, and no webhook or media
events. It is not counted as an agent run. The next answered call remains the
live acceptance candidate.

## Clean-run acceptance gates

The rerun passes only if all of the following hold:

1. Outbound opening is direction-aware and asks identity once.
2. The first clear affirmation locks identity once.
3. The next spoken turn exactly matches the application-owned callback
   continuation; it must not mention waiting for verification.
4. The callback purpose is preserved; no generic “How can I help?” reset.
5. One bounded scheduling task reaches a terminal state or an explicit safe
   no-change/handoff outcome.
6. Every caller turn and assistant turn is present in the log.
7. The deterministic transcript quality gate has zero hard failures and zero
   unexplained observations.
8. The database state matches the final spoken claim.
