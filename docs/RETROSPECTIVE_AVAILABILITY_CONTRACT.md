# Retrospective: Availability Retrieval Regression

## Outcome

The October 8 voice run failed to retrieve rescheduling options even though the
calendar contained matching slots. The defect should have been isolated in the
first debugging turn by tracing the tool contract end to end.

## What happened

The caller asked for later times. The backend successfully called
`get_appointment` and `check_eligibility`, then said it could not retrieve
availability. It never called `search_slots`.

`search_slots` had been tightened to require a canonical `location_id` and warned
the model not to substitute a patient-facing address. `get_appointment`, however,
returned the spoken location but omitted the canonical `location_id`. The database
and calendar were healthy; the exposed producer/consumer contract was incomplete.

## Why the first diagnosis was slower than it should have been

The initial investigation was at risk of focusing on prompt adherence and model
tool selection before comparing the exact `get_appointment` payload with the
`search_slots` schema. That ordering invites whack-a-mole prompt patches around a
deterministic interface defect.

## Correct first-turn debugging sequence

1. Preserve the failing caller turn and identify its delegation.
2. List every tool requested in that delegation; note the missing expected tool.
3. Compare the last successful tool output with the required inputs of the missing
   downstream tool.
4. Verify calendar data independently, without treating its presence as proof that
   the agent can reach it.
5. Execute the producer-to-consumer chain directly against the same fixture.
6. Repair the boundary, add a regression test, and make the evaluator reject the
   misleading spoken failure when no retrieval attempt occurred.

## Changes made

- `get_appointment` now returns canonical `appointment_type_id`, `provider_id`, and
  `location_id` alongside patient-facing details.
- A runtime regression test reads an appointment and uses only its returned
  canonical fields to retrieve the Friday/Saturday slots.
- Backend instructions require a same-delegation `search_slots` call for every
  availability question and prohibit unsupported retrieval-failure claims.
- The transcript harness now raises the hard failure
  `availability_failure_without_search` when the assistant claims it could not
  retrieve availability without a recorded slot-search attempt.

## Prevention rule

For agent/tool regressions, contract tracing precedes prompt editing. Every tool
handoff must have a test proving that the upstream result contains all identifiers
required by the downstream schema, and every failure claim must be grounded in a
corresponding structured operation event.
