- Golf 1, C4a (track `golf-1-opruimen`): `vnx permission scan` is gone. Removed the live-pane scan
  `scan_live_awaiting_permission` and its helpers from `scripts/lib/worker_permission_relay.py`, the `scan`
  subcommand and its worktree-risk rendering from `scripts/permission_relay_cli.py`, the usage line in `bin/vnx`
  and the 19 tests of the scan. Every other `vnx permission` subcommand is unchanged.
