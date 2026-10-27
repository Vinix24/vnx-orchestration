- Track closure (track `slop-refactor-panel-review`, S3): `close_track_if_done` in
  `scripts/lib/track_reconciler_closure.py` hands its three close gates to `_terminal_gate` and its
  phase walk to `_walk_to_done`, with the order of checks and every value written to the returned
  payload unchanged. The function drops from 165 to 147 raw lines and from 77 to 62 executable
  lines; two seam tests pin each gate's full payload and the walk's `path`/`declared_phase`/`error`
  contract, and `test_close_track_if_done_after_s3` holds the 150/70 ratchet.
