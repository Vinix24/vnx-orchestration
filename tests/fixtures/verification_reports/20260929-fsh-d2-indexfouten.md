## Verification

Rode run op de oude code (`git checkout HEAD -- scripts/build_t0_state.py`, dan `python3 -m pytest tests/test_t0_index_split.py -q`): `5 failed, 33 passed`. Falen op gedrag, o.a. `assert 0 == 2` (twee open PR's in pr_queue, index geeft 0) en `Left contains 1 more item: {'lease_expires': ...}`. De recent_receipts-test faalde ook (index gaf de drie oudste).

Groene run op de nieuwe code: `python3 -m pytest tests/test_t0_index_split.py tests/test_build_project_status.py tests/test_contract_invalid_ledger.py -q` -> `108 passed`.

Grep op hetzelfde patroon: geen tweede plek met `[-3:]` op receipts of `lease_expires` uit de index in `scripts/` (consument `build_project_status.py` leest `open_prs` als getal, ongewijzigd).
