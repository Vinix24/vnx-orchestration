## Verification
Green: `python3 -m pytest tests/test_receipt_outcome.py tests/test_active_drain_outcome.py tests/test_weekly_digest_outcome_classifier.py tests/test_recent_receipts_outcome.py tests/test_contract_invalid_window.py tests/test_outcome_vocab_sync.py tests/test_dispatcher_drain_lifecycle.py tests/test_build_t0_state_pytest_noise_filter.py tests/test_receipt_t0_view_filter.py -q` -> 178 passed, 3 failed. The 3 failures (`test_dispatch_paths_written_when_provided`, `test_classify_completion_no_dead_letter_on_failure`, `test_transient_fail_then_success_only_completed`, AttributeError `_apply_runtime_overrides`) fail identically on main 892d64a0 (run in the main checkout).

Red (new/changed tests against the c337feea sources, same command on test_receipt_outcome, test_active_drain_outcome, test_weekly_digest_outcome_classifier, test_recent_receipts_outcome): 12 failed, 59 passed, all behavioural, e.g.
- (a) `{'d1': 'success'} == {'d1': 'investigate'}`; drain: `{'d1': 'completed'} == {'d1': 'skipped'}`
- (b) `{'d1': 'failure'} == {}` (frozen contract_invalid marked failure); `'reject' == 'accept'`
- (c) digest `(2, 1, 1, 0) == (1, 1, 0, 0)` and `(0, 0, 0, 0) == (1, 0, 1, 0)`; recent receipts row `[...] == []`
ADR-007: every new test carries a second project with a colliding dispatch id.
