- Added: the spool replay of the cost ledger (ADR-043 §6, K2a-3b): `cost_ledger.replay_spool` and `vnx costs replay [--json]`
  (script, pip CLI and `bin/vnx`) bring spooled lines back into the ledger, one spool file per exclusive hold of the lock
  file, at most `VNX_COST_LEDGER_REPLAY_BATCH` files in `VNX_COST_LEDGER_REPLAY_BUDGET_SEC` seconds. Under the lock file
  the ledger `flock` is taken with `LOCK_NB` as well, bounded by what is left of the wait and the budget, so a writer of an
  older fabric version cannot hold the replay, and every current writer behind it. A file is deleted only after the
  ledger is fsynced, also when nothing was appended, and each of its records reads back as its own complete line, so a
  killed replay leaves it and writes no line twice.
  Exit 0 done, 1 an unreadable spool directory or a write failure, 3 a lock busy.
  - A second execution under one `run_id` (an f2bef053 writer booked an attempt again while it waited in the spool) is
    booked under the next free attempt with `renumbered_from` and a `cost_ledger_renumbered` receipt.
  - A spool file that is not one run's goes to `provider_costs.spool/quarantine/` (OI-2243), each one a file of the
    batch and the budget; its receipt is booked before the move, and a move that failed after it is finished without a
    second receipt. It counts in the new `LedgerTotals.spool_quarantined`; it no longer blocks `record_next_attempt`.
  - The aggregation (OI-2244, OI-2241): a spooled record already in the ledger is no backlog, any unreadable line makes
    the total incomplete, a reader of a store it may not write reads without creating the lock file
    (`unlocked_read`), and the text says `1 run wacht op replay`.
