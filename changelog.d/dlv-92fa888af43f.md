- Golf 1 (track `golf-1-opruimen`): `scripts/ci/retired_paths.txt` becomes one fragment per cluster under
  `scripts/ci/retired_paths.d/`, named after the dispatch, so parallel clusters merge in any order instead
  of conflicting on the shared file. Every fragment holds its cluster's block unchanged; the header (the
  three line kinds and the rule that a line stays) moved to `scripts/ci/retired_paths.d/README.md`.
  `scripts/ci/check_retired_paths.py` reads every `*.txt` fragment in sorted order as one list, refuses a
  line that appears in two fragments (naming both files), and treats an empty or missing directory as an
  error. `--list` still reads a single file and `--list-dir` reads a directory, which is the default. C4a
  gains its two helper entries (`tests/test_worker_permission_relay.py::_ScanArgs` and `::_make_git_repo`)
  and C5 gains the `Source:` line its neighbours carry. The old single file is itself listed as retired.
