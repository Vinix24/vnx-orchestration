- Golf 1, C1 (track `golf-1-opruimen`): removed dead code without a caller in repo, runtime or fleet:
  `scripts/dispatch_broker_cli.py`, `scripts/vulture_whitelist.py`, `scripts/update_pane_mapping.sh`,
  `scripts/reconcile_terminal_state.py`, the compat shims `pane_manager_v2.sh`, `smart_tap_v7_json_translator.sh`,
  `unified_state_manager_v2.py` and `dispatcher_v8_minimal.sh`, and the two tests of the `unified_state_manager_v2.py`
  shim. The libraries behind them (`dispatch_broker.py`, `terminal_state_reconciler.py`, `unified_state_manager.py`,
  `pane_manager.sh`, `smart_tap_json_translator.sh`, `dispatcher_minimal.sh`) stay. The dead-code check runs vulture
  on `scripts/` without the whitelist, which had no executable lines; the field-test runner lost its always-`None`
  `scoring_worktree` cleanup.
  - New CI check `scripts/ci/check_retired_paths.py` with the list `scripts/ci/retired_paths.txt`: a retired path,
    `path::symbol` or `env:NAME` fails CI when it comes back or when anything still calls it in an executable way
    (import, `-m`, a command in shell, yaml, json, plist or a settings template, an env read). Comments, docstrings,
    markdown and frozen history do not count. Every later golf-1 cluster adds its removals to the list.
  - A tracked symlink counts when its target, resolved lexically against the link's directory (a dangling link
    too), is a retired path or lies under one; an absolute target inside the repo counts the same way.
  - For a `path::Class.method` entry the class stays importable: what fails is the retired method on the class or
    an instance of it (`K.method`, `K().method`, an instance bound from `K(...)`, `getattr`/`patch.object` on
    either, a `patch("module.K.method")` target). The docstring of the check lists the forms it does not follow.
  - A consumer that tracks a symlink to `scripts/dispatch_broker_cli.py` or `scripts/dispatcher_v8_minimal.sh`
    (mission-control does, via `scripts/bootstrap-mission-control-repo.sh`) keeps a dangling link after it pins
    this release; remove the link and its bootstrap line.
