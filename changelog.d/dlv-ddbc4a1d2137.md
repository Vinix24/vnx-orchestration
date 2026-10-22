- Golf 1, C5 (track `golf-1-opruimen`): removed `scripts/cleanup_stale_vnx_sessions.sh`, a manual tool for the removed
  tmux lane with no caller in repo, runtime or fleet, and `build_process_gone_failure_report` from
  `scripts/lib/worker_heartbeat.py`, which only a test called. `worker_heartbeat.py` stays for `kimi_spawn.py`.
  Both are on `scripts/ci/retired_paths.txt`.
