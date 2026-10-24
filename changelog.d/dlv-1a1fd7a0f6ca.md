- xdist cutover rule (track `ci-profile-a-xdist`, D3-9): the throwaway git repos the tests build no longer spawn a
  detached `git maintenance run --auto --quiet --detach` child. `git commit` ends with `run_auto_maintenance()`,
  which starts that child unless `maintenance.auto` is false or `gc.auto` is 0; the child creates and removes
  `.git/objects/maintenance.lock` a few milliseconds later, and a `TemporaryDirectory` deleting the repo inside
  that window failed VNX CI run 39971696706 with `OSError: [Errno 39] Directory not empty: 'objects'`
  (`tests/test_attest_d2.py::TestDiffBinding::test_diff_bind_forged_manifest_fails`, serial Profile A only), which
  blocked the D4 cutover rule. The cleanup failure is not papered over (no `ignore_cleanup_errors`, no retry):
  the new shared helper `tests/git_maintenance_guard.py` writes both settings into a repo's own config, and
  `tests/conftest.py` injects them once per session as git command-scope config, so every repo-creating helper in
  `tests/` — including helpers added later, and the production git commands these tests drive — never starts the
  child. `tests/test_git_maintenance_guard.py` pins the helper, its fail-closed reader and the absence of the
  spawned child in git's own trace.
