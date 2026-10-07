# Evaluation Harness Rigour Audit

Date: 2026-10-06  
Scope: agent, tool, scheduler, concurrency, and safety boundaries without a new
carrier call or external model request.

## Outcome

The reinforced build is promotion-eligible in the credential-free harness:

- 10/10 behavioral scenarios passed.
- 0 critical and 0 major gate failures.
- 12/12 known-bad negative controls were detected, one for every evaluator gate.
- The versioned baseline failed 1/10 scenarios with two critical failures.
- The reinforced comparison repaired that scenario with zero prior-pass regressions.
- 260 repository tests passed; Python compilation passed.
- A repeated reinforced run produced the same SHA-256 report hash:
  `83BA10CF19D39CC3B62726D4A7EAE1846C832FE0E379AAB14B4254B09E38111E`.

## Captured transcript results

| Log | Gate result | Hard failures | Observations | Verified writes |
|---|---:|---:|---:|---:|
| V04 confirmation-loop baseline | FAIL, exit 1 | 3 | 3 | 0 |
| V05 latest smooth call | PASS_WITH_OBSERVATIONS, exit 0 | 0 | 2 | 1 |

V04 was rejected for a repeated confirmation cycle, three repeated confirmation
prompts, and an incomplete workflow at hangup. It also surfaced three premature
commitment phrases, an in-turn repeated phrase, and truncated final speech.

V05 preserved its valid outcome but no longer passes silently. It surfaces the
pre-verification phrase “I'm putting that change through” and the presence of two
confirmation questions as non-blocking observations. The write itself was
proposal-bound, verified once, and followed by a truthful read-back.

## Boundary coverage

The composed E2E suite covers privacy before identity, public FAQ access,
semantic confirmation outcomes, exact argument binding, stale slot and
appointment versions, duplicate writes, timeout ambiguity, urgency and human
preemption, post-completion tool access, competing parallel sessions, and a
verified reschedule.

Those tests exposed two production defects during this pass:

1. A timeout after a committed write was being presented as a definite failure.
   It now surfaces as `UNKNOWN`, requiring reconciliation and preventing a blind
   retry.
2. An eager repeated write after an unclear semantic confirmation could replace
   the pending proposal. It is now rejected while the original proposal remains
   immutable.

## Commands

```powershell
py -3.11 -m unittest discover -s tests -q
py -3.11 -m evals.runner --profile baseline --output reports/eval-baseline.json
py -3.11 -m evals.runner --profile reinforced --compare reports/eval-baseline.json --output reports/eval-after.json
py -3.11 -m clinic_agent --evaluate-voice-log --voice-log data/voice-v04.jsonl
py -3.11 -m clinic_agent --evaluate-voice-log --voice-log data/voice-v05.jsonl
```

## Honest limits

- The transcript evaluator is deterministic and conservative. It cannot judge
  audio quality, prosody, or semantic paraphrases outside its patterns.
- Semantic identity and confirmation model behavior is stubbed in the
  credential-free E2E suite; production-model classification still needs
  contract and live acceptance evidence.
- The concurrency test exercises isolated async sessions over the locked
  in-memory store, not multi-process SQLite contention.
- After a write timeout the control plane safely refuses replay, but automatic
  authoritative reconciliation is not implemented. The backend contract is
  separately proven through idempotency lookup and safe replay.
- The dashboard session projection can remain `active` after the voice workflow
  has completed; appointment truth and audit state are authoritative meanwhile.
