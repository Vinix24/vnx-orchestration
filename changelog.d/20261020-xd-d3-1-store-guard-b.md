- **Store-guard tests hold under xdist (D3-1).** Two tests swapped `vnx_paths`
  for later tests in the same process. `test_resolve_project_id_with_env_override`
  reloaded it and `test_append_receipt_stamps_project_id_from_env` re-imported it
  after a `sys.modules` pop. Either one left a second `TestIsolationGuardError`
  class, so the isolation-guard tests in `test_test_runner_store_guard.py` and
  `test_receipt_mirror_isolation.py` failed depending on worker order. The reload
  is gone, the re-import puts the original modules back, and the guard tests
  look up the guard class when they assert instead of at import. The guard itself
  is unchanged.
