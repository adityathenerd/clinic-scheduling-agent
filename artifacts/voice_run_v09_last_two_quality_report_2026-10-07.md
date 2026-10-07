# Voice V09 — Last Two Calls Quality Report

Date: 2026-10-07  
Source: `data/voice-v09.jsonl`

This report preserves the deterministic harness result for the two calls immediately following the active-turn stall. Calls were segmented from `call.started` through `call.ended` while retaining the pre-start workflow events required to evaluate the outbound opening and application-owned speech contracts. The matching JSON report is `reports/voice-v09-last-two-2026-10-07.json`.

## Results

| Call | Journey | Harness result | Material evidence |
|---|---|---|---|
| `CA33e7fedf68afdcd23fd58d92deba507c` | Existing appointment lookup, next-day slot search, reschedule request held for clinic approval, clean close | **Fail** — one grouped hard failure with two occurrences | Four tool requests, one verified proposed/held write, no loop; Live omitted the required timezone wording from the exact proposal and held-request readback |
| `CA9a57165730293eb074e8fe50d3502a21` | Clinic-approval outbound confirmation, identity verification, approved appointment readback, clean close | **Pass** | No issues; no tool call was required because the approved appointment context was preloaded and resolved by the application |

## Why the first call sounded successful but failed the gate

The patient-selected provider, date, local time, location, and clinic-approval status were all communicated correctly. The omitted phrase was the rendered `UTC+05:30` timezone suffix. That omission did not change the patient's apparent understanding, but it violated an application-owned `say_exactly` contract twice. The harness therefore correctly kept the run promotion-ineligible rather than treating semantic similarity as exact delivery.

## Response-quality observations outside the deterministic result

- “I'll check that” delayed the answer instead of leading with the next appointment.
- “Okay, I'm on it” could imply action before explicit confirmation.
- “Here is the exact proposed rescheduling” was mechanical and repeated appointment details.
- The held-request response repeated “confirmed” and “not confirmed.”
- The outbound confirmation read a long inventory and exposed an internal-style UTC offset.

These were transcript-review findings, not deterministic harness observations. This distinction is important: the safety harness should remain conservative and reproducible, while naturalness requires a separate rubric or curated application-owned copy.

## Reinforcement applied after the run

- Clinic identity changed to **2care Clinic**.
- Patient-facing location changed to **2care Clinic, Koramangala, Bengaluru** while retaining the legacy internal `downtown` identifier for database and replay compatibility.
- Appointment values are converted to India's fixed timezone and spoken as **IST**; raw UTC offsets are no longer patient-facing.
- Appointment answers lead with the answer.
- Proposal wording describes the request and approval consequence naturally before asking for yes/no confirmation.
- Held-request and clinic-confirmation readbacks are shorter and distinguish clearly between the current appointment, the held request, and clinic approval.

## Reproducibility

The exact call fixtures are preserved at:

- `tests/fixtures/voice_v09_reschedule_timezone_omission.jsonl`
- `tests/fixtures/voice_v09_clinic_confirmation_pass.jsonl`

They are exercised by `tests/runtime/test_normal_turn_liveness.py`, which asserts that the first call retains both exact-reply failures and that the second remains clean.
