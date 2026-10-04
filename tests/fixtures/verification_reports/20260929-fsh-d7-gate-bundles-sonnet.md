## Verification

Red run on the old code (`dispatch_cleanup.py` and `gate_artifacts.py` restored from HEAD, signatures intact):
`python3 -m pytest tests/test_gate_bundle_archive.py -q` -> 9 failed, 8 passed. Failures are behavioural:
- `test_finished_gate_leaves_no_bundle_in_pending` and `test_failed_gate_moves_the_bundle_to_failed_and_keeps_it`: `assert not True where True = exists()` on the pending bundle dir (a finished gate leaves the map behind).
- 6 cleanup tests: "the cleanup does not list the final_prompt-only bundle at all" (old code skips it).
- `test_invariant_every_planned_move_...`: `assert set() == {'proven-gate-0','proven-gate-1','proven-gate-2'}` (the old cleanup plans no moves).

Green run on the new code:
`python3 -m pytest tests/test_gate_bundle_archive.py tests/test_dispatch_cleanup.py tests/test_oi1443_prompt_pinned_on_record.py tests/test_gate_artifacts_atomicity.py tests/test_gate_artifacts_findings_by_contract.py tests/test_gate_artifacts_register.py tests/test_gate_artifacts_verdict_block.py tests/test_receipt_schema.py -q` -> 102 passed.
Neighbours: `python3 -m pytest tests/test_gate_recorder*.py tests/test_gate_runner*.py tests/test_final_prompt*.py -q` -> 174 passed.

Per "Klaar"-punt: pending map left after a finished gate (red -> green: two archive tests); `gate_artifacts` still finds the sha (`test_record_still_carries_the_prompt_sha_after_the_move`, green); cleanup skipped final_prompt-only bundle, now moves it with proof (red -> green); invariant on the dry-run plus "bundle without result stays" and the ADR-007 collision test (red -> green).
