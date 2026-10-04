## Verification

Rode run, oude code (de drie bronbestanden op HEAD teruggezet, nieuwe tests ongewijzigd):
`python3 -m pytest tests/test_health_headless_d6.py -q` -> 8 failed, 3 passed. Alle faals zijn gedrag, geen ontbrekend symbool:
- `test_only_receipt_processor_running_is_ok`: `assert 'fail' == 'ok'` (was degraded)
- `test_five_thousand_rejected_rows_stay_under_bound`: `system_health is 1160706 bytes`, `assert 1160706 < 8192`
- reason/details_counts/failing-tests: `KeyError` op velden die het oude gedrag niet levert (`reason`, `details_counts`, `failing`)
- `test_locked_db_reads_degraded_with_reason` en `test_index_carries_the_db_reason`: `assert 'locked' in ''` (gelockte DB: degraded zonder reden)

Groene run, nieuwe code:
`python3 -m pytest tests/test_health_headless_d6.py -q` -> 11 passed.
Buren: `python3 -m pytest tests/test_architecture_doc_drift.py tests/test_beacon_reader_expectations.py tests/test_beacon_reader_register.py tests/test_beacon_register.py tests/test_daemon_register.py tests/test_generate_daemon_liveness_md.py tests/test_health_beacon.py tests/test_ledger_health.py tests/test_live_requires_measured_health.py tests/test_receipt_conversion_rejection_beacon.py tests/test_sessionstart_hook_beacon_health.py tests/test_system_health_aggregation_contract.py tests/test_system_health_monotonic.py tests/test_t0_index_split.py tests/test_t0_state_health.py tests/test_build_t0_state_freshness.py tests/test_build_t0_brief_output.py tests/test_build_t0_state_launchd_liveness.py tests/test_future_state_reconciliation.py tests/test_health_headless_d6.py -q` -> 543 passed, 4 failed. Twee (aggregatie-contract, `db_reason` als kale Name) heb ik gerepareerd (daarna 80 passed op aggregatie + d6 + t0_index_split + monotonic). De twee overige, `test_future_state_reconciliation.py::TestWriteFailureReflectedBeforePersist` (TypeError `track_freshness` in een lambda), falen ook op de ongewijzigde HEAD.
