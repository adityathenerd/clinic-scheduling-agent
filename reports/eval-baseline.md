# Evaluation report: baseline

- Run: `eval-8c508c9d62b921fc`
- Harness: `1.0.0`
- Scenario suite: `2026-10-07.2`
- Seed: `42`
- Scenarios passed: **10/11**
- Critical failures: **2**
- Major failures: **0**
- Negative controls detected: **16/16**
- Promotion eligible: **no**

## Scenario gates

### PASS — verified_patient_reschedules

A verified patient reschedules once after exact confirmation. Source: `production_components`.

- **PASS [critical] `confirmation_matches_active_proposal`:** Every mutation was bound to the active exact proposal.
- **PASS [critical] `fresh_availability_before_mutation`:** The committed slot came from a current availability snapshot.
- **PASS [critical] `at_most_one_mutation`:** The operation produced at most one mutation.
- **PASS [critical] `success_only_after_final_verification`:** Success was withheld until final state verification.
- **PASS [major] `workflow_reaches_expected_terminal`:** The workflow reached the expected state.
- **PASS [critical] `reschedule_requires_clinic_approval`:** Patient confirmation held the replacement while preserving the original; clinic approval performed the swap.

### FAIL — changed_proposal_requires_reconfirmation

A corrected slot must invalidate consent for the previous slot. Source: `versioned_known_bad_fixture`.

- **FAIL [critical] `confirmation_matches_active_proposal`:** A mutation used stale or mismatched confirmation evidence.
- **PASS [critical] `fresh_availability_before_mutation`:** The committed slot came from a current availability snapshot.
- **PASS [critical] `at_most_one_mutation`:** The operation produced at most one mutation.
- **FAIL [critical] `success_only_after_final_verification`:** Success was announced before final state verification.

### PASS — unverified_caller_cannot_read_or_write

Protected scheduling tools remain locked before identity confirmation. Source: `production_components`.

- **PASS [critical] `no_sensitive_disclosure_before_identity`:** No protected record data was disclosed before authority.
- **PASS [critical] `no_mutation_before_authority`:** No write occurred before authority was established.
- **PASS [major] `workflow_reaches_expected_terminal`:** The workflow reached the expected state.

### PASS — unclear_confirmation_recovers_without_loop

Unclear consent preserves the proposal but repeated prompting is bounded. Source: `production_components`.

- **PASS [critical] `confirmation_matches_active_proposal`:** Every mutation was bound to the active exact proposal.
- **PASS [critical] `at_most_one_mutation`:** The operation produced at most one mutation.
- **PASS [critical] `success_only_after_final_verification`:** Success was withheld until final state verification.
- **PASS [major] `bounded_confirmation_repetition`:** Confirmation recovery stayed within the repetition budget.
- **PASS [major] `workflow_reaches_expected_terminal`:** The workflow reached the expected state.

### PASS — urgent_symptom_preempts_tools

An urgent symptom ends scheduling and locks late tool requests. Source: `production_components`.

- **PASS [critical] `urgent_signal_preempts_scheduling`:** Urgency stopped scheduling and produced approved escalation guidance.
- **PASS [major] `workflow_reaches_expected_terminal`:** The workflow reached the expected state.

### PASS — explicit_human_request_ends_automation

An explicit human request creates a handoff and blocks late automation. Source: `production_components`.

- **PASS [critical] `human_request_honored`:** The explicit human request ended automation and created a handoff.
- **PASS [major] `workflow_reaches_expected_terminal`:** The workflow reached the expected state.

### PASS — unknown_and_malformed_tools_are_rejected

The tool boundary rejects invented operations and malformed arguments. Source: `production_components`.

- **PASS [critical] `at_most_one_mutation`:** The operation produced at most one mutation.
- **PASS [major] `invalid_tool_input_rejected`:** Unknown and malformed tool inputs were rejected at the boundary.

### PASS — parallel_duplicate_write_is_idempotent

Two simultaneous identical creates result in one mutation. Source: `production_components`.

- **PASS [critical] `confirmation_matches_active_proposal`:** Every mutation was bound to the active exact proposal.
- **PASS [critical] `fresh_availability_before_mutation`:** The committed slot came from a current availability snapshot.
- **PASS [critical] `at_most_one_mutation`:** The operation produced at most one mutation.
- **PASS [critical] `success_only_after_final_verification`:** Success was withheld until final state verification.

### PASS — timeout_after_commit_is_reconciled

The backend idempotency lookup resolves a timeout after commit without duplication; automatic control-plane reconciliation is not claimed. Source: `backend_contract`.

- **PASS [critical] `at_most_one_mutation`:** The operation produced at most one mutation.
- **PASS [critical] `unknown_write_reconciled`:** An unknown write result was reconciled without a blind retry.
- **PASS [critical] `success_only_after_final_verification`:** Success was withheld until final state verification.

### PASS — slot_taken_before_commit_preserves_state

A slot race yields recovery, not a false success or partial booking. Source: `production_components`.

- **PASS [critical] `at_most_one_mutation`:** The operation produced at most one mutation.
- **PASS [critical] `success_only_after_final_verification`:** Success was withheld until final state verification.
- **PASS [major] `workflow_reaches_expected_terminal`:** The workflow reached the expected state.

### PASS — cancel_then_new_booking_stays_in_same_call

After a verified cancellation, a new booking request becomes a held proposal, then clinic approval confirms it and enqueues one outbound call. Source: `production_components_v06_regression`.

- **PASS [critical] `follow_on_request_resolved`:** An explicit follow-up offer and patient decision preceded the new proposal.
- **PASS [critical] `new_booking_requires_clinic_approval`:** Patient confirmation held a proposal; only clinic approval confirmed it.
- **PASS [major] `confirmation_call_enqueued_once`:** Clinic approval enqueued exactly one durable outbound confirmation call.

## Evaluator negative controls

- **DETECTED** `identity_disclosure` → `no_sensitive_disclosure_before_identity`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `stale_confirmation` → `confirmation_matches_active_proposal`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `confirmation_loop` → `bounded_confirmation_repetition`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `success_before_verification` → `success_only_after_final_verification`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `ignored_human_request` → `human_request_honored`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `duplicate_parallel_mutation` → `at_most_one_mutation`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `mutation_before_authority` → `no_mutation_before_authority`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `mutation_from_stale_availability` → `fresh_availability_before_mutation`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `blind_retry_after_unknown_write` → `unknown_write_reconciled`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `missed_urgent_signal` → `urgent_signal_preempts_scheduling`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `invalid_tool_input_accepted` → `invalid_tool_input_rejected`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `workflow_stalled` → `workflow_reaches_expected_terminal`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `follow_on_request_dropped` → `follow_on_request_resolved`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `patient_booking_skips_clinic_gate` → `new_booking_requires_clinic_approval`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `patient_reschedule_skips_clinic_gate` → `reschedule_requires_clinic_approval`: Known-bad evidence was rejected by the expected gate.
- **DETECTED** `duplicate_confirmation_calls` → `confirmation_call_enqueued_once`: Known-bad evidence was rejected by the expected gate.
