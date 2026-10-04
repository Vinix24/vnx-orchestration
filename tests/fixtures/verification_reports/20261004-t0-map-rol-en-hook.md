## Verification

**Red run** on base 5dfaec5c (old role and old hook swapped in, new tests): `10 failed, 5 passed`. Failures are assertion failures on behaviour: R1 (17 unanchored hits, relative state read), R3 (old STOP text, no Read tool), R4 (path absent from first 1,024 bytes, no NOT FOUND line), and the role-contains-command check. No ImportError, TypeError or KeyError. (The two audit tests were added after the red run; they are green only with the audit change.)

Green run: `python3 -m pytest tests/test_t0_role_startmap.py -q` -> 17 passed.

Regression: `python3 -m pytest tests/test_t0_role_startmap.py tests/test_sessionstart_hook.py tests/test_sessionstart_hook_role_alarm.py tests/test_sessionstart_hook_central_store.py tests/test_sessionstart_hook_consumer_resolver.py tests/test_sessionstart_hook_dead_vars.py tests/test_role_orchestrator_sentinel.py tests/test_role_sync.py tests/test_t0_role_audit_static.py tests/test_worker_dispatch_standards.py tests/test_sessionstart_hook_state_freshness.py` -> 189 passed, 1 failed (my own new audit test, fixed afterwards, then 17 passed in its file). Before the audit fix, `tests/test_t0_role_audit_static.py` had 2 failures (SCRIPT-OUTSIDE-FABRIC), now green. `bash -n hooks/sessionstart.sh` -> ok. `python3 scripts/lib/t0_role_sources_audit.py . .` -> exit 0.

R2 (T0 folder vs project root, checkout under test, tmp HOME): ran `vnx_paths.py`, `validate_skill.py --list`, `t0_role_audit.sh`; same exit code in both folders. Left out: `build_t0_state.py`, `reconcile_queue_state.py --repair`, `open_items_manager.py`, `runtime_core_cli.py`, `receipt_query.py decide`, `bin/vnx dispatch|pool` (they write, repair or dispatch).

Ground-truth check: the old line 39 `cat .vnx-data/...` is gone; the other `.vnx-data/` mentions (policy and illustrative paths in lines 160, 246-248, 318, 337-339) are not cwd reads and were left alone.
