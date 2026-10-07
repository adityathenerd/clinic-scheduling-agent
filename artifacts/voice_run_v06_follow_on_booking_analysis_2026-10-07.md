# V06 follow-on booking failure and reinforcement

Date: 2026-10-07  
Evidence: `data/voice-v06.jsonl`

## Observed failure

The cancellation completed correctly. The caller then asked for a new appointment on the following Wednesday or Friday. The completed patient turn remained `completed -> completed`; the subsequent `check_eligibility` request was rejected only because protected tools were locked in `completed`. The assistant consequently said it could not continue scheduling.

The original deterministic transcript evaluator incorrectly passed this call. Its write-tool set named a nonexistent `cancel_appointment` tool instead of the runtime's `delete_appointment`, and it had no invariant for a follow-on scheduling request rejected after an earlier task completed.

## Root cause

`completed` represented two different concepts:

- the current scheduling task has reached a verified terminal state; and
- the phone call itself has ended.

The state machine treated both as the second meaning. This made a successful mutation permanently disable record tools for the rest of the call.

## Reinforcement

1. A completed cancellation enters `awaiting_follow_up_decision` and explicitly asks whether the patient wants help with another appointment. Protected tools stay locked while the application semantically classifies the completed response as `accepted`, `declined`, `new_request`, or `unclear`. Only `accepted` or a clearly stated `new_request` opens a new task; politeness, ambiguity, and decline do not.
2. New booking creation now persists an appointment as `proposed` and the selected slot as `held`; patient confirmation is not described as a booking confirmation.
3. A localhost clinic operator reviews the held proposal in a modal. Its explicit confirm action changes the appointment to `confirmed`, the slot to `booked`, and creates one durable outbound confirmation-call job.
4. The Twilio call attempt is downstream of the clinic commit. Failure records `retry_pending` and cannot roll back the appointment.
5. The transcript evaluator now recognizes `delete_appointment` and hard-fails `follow_on_request_blocked` on the original V06 log.

## Regression evidence

- Original V06 log: hard failure `follow_on_request_blocked`; one verified cancellation is now counted correctly.
- Production-backed scenario: `cancel_then_new_booking_stays_in_same_call` passes only when the trace contains both the offer and an explicit semantic decision.
- New gates: `follow_on_request_resolved`, `new_booking_requires_clinic_approval`, and `confirmation_call_enqueued_once`.
- Each new gate has a known-bad negative control.
- Full automated suite: 278 tests passing at the time this artifact was updated.

## Outbound privacy boundary and residual limitation

The outbound call uses the normal guarded voice entry point and carries only an opaque confirmation-job reference. The pre-verification opening says only that this is a scheduling matter. After identity is verified, the job reference supplies a purpose-scoped instruction to read the authoritative confirmed appointment and report its patient-facing details. No patient or appointment details are embedded in Twilio's callback URL or opening. A production worker would add a governed retry schedule; this prototype keeps retries explicit and visible rather than silently redialing.
