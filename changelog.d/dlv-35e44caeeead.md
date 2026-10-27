- Pool route (T-D0): the elastic pool now starts `pool_worker_runner` by default, so a pool worker delivers through the single-entry door; `VNX_SINGLE_ENTRY_DISPATCH=0` (or `VNX_DISPATCH_LEGACY=1`) still starts the legacy `subprocess_dispatch` worker.
  The pool worker owns its terminal lease: it renews `terminal_leases.last_heartbeat_at` while it runs and releases the row at its end.
  ADR-042 besluit 6 is amended accordingly; `terminal_leases` stays a pool table with `project_id` in its key (ADR-007).
