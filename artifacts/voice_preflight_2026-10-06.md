# Voice Preflight Evidence — 2026-10-06

## Verdict

The deterministic application, provider adapters, voice composition, persistence, and public HTTP route passed preflight. The first controlled PSTN-to-OpenAI Live call was subsequently run and exposed conversational failures documented in `voice_run_v01_failure_analysis_2026-10-06.md`; this file remains the pre-call baseline.

## Observed evidence

| Check | Result |
|---|---|
| Full test suite at preflight | 174 tests passed in 3.835 seconds |
| Full test suite after V01 fixes and semantic identity lock | 191 tests passed in 3.227 seconds |
| Python compilation | `clinic_agent`, `appointment_harness`, and `tests` compiled with exit code 0 |
| SQLite integrity | `ok` |
| Fixture snapshot | 2 patients, 4 slots, 1 appointment |
| Dashboard API | HTTP 200 from `/api/snapshot` |
| Local voice health | HTTP 200, `{"status":"ok"}` |
| Public ngrok health | HTTP 200, `{"status":"ok"}` |
| Port 8001 after preflight | Free; dummy-credential server stopped cleanly |
| ngrok | Active, public origin forwards to `http://localhost:8001` |
| Twilio account probe | Active Trial, 9.1605 USD, configured number found and voice-capable |
| Real external call | Not placed during preflight; V01 was run later and retained as a failing baseline |

The health probe used dummy provider values only to verify application composition and routing. It did not open a carrier call or OpenAI session, and those values were not persisted.

## Commands and results

```text
py -3.11 -m unittest discover -s tests -q
----------------------------------------------------------------------
Ran 174 tests in 3.835s
OK
```

```text
py -3.11 -m compileall -q clinic_agent appointment_harness tests
exit code 0
```

```text
GET http://127.0.0.1:8001/healthz
200 {"status":"ok"}

GET https://6eda-2401-4900-892e-a6c0-d899-3a24-cde5-a6de.ngrok-free.app/healthz
200 {"status":"ok"}
```

## Test architecture added for the live run

- Privacy-aware JSONL voice event logs with timestamps, call/session identity, workflow state, tool decisions, mutations, anomalies, and terminal outcome.
- Transcript content redacted by default; explicit synthetic-demo opt-in; secrets always redacted.
- A CLI summary that renders the high-signal decision timeline without needing credentials.
- Immediate urgent-symptom and human-request preemption before a model tool call.
- Application-owned state-transition and tool-decision events.
- A deterministic full voice-control-plane booking test through verified completion.
- Failure isolation when one telemetry sink is unavailable.
- Fixed project-root `.env` loading with process-environment precedence and an
  explicit, non-echoing migration command for credential-bearing shells.

See `docs/VOICE_ACCEPTANCE_TEST_PLAN.md` for the service matrix, live scenario grid, expected logs, commands, and known gaps.
