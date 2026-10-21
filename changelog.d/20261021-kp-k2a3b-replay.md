- Added: the spool replay of the cost ledger (ADR-043 §6, K2a-3b): `cost_ledger.replay_spool` and `vnx costs replay [--json]`
  (script, pip CLI and `bin/vnx`) bring spooled lines back into the ledger, one spool file per exclusive hold of the lock
  file, at most `VNX_COST_LEDGER_REPLAY_BATCH` files in `VNX_COST_LEDGER_REPLAY_BUDGET_SEC` seconds. Under the lock file
  the ledger `flock` is taken with `LOCK_NB` as well, bounded by what is left of the wait and the budget, so a writer of an
  older fabric version cannot hold the replay, and every current writer behind it. The receipt the replay writes is
  bounded by the same deadline: `emit_governance_receipt` and `append_receipt_payload` take an optional `lock_timeout`
  (default unchanged), a bounded append queues its central mirror for the next unbounded one, and a receipt lock still
  taken makes the call `lock_busy` with the spool file intact. A file is deleted only after the ledger is fsynced, also
  when nothing was appended, and each of its records, written now or landed before, reads back as its own complete line
  with the same content, field by field, so a killed replay leaves it and writes no line twice. A ledger line with a
  spooled `record_id` and other content keeps the file, writes nothing for it and ends the call with
  `readback_mismatch`; the replay goes on with the next file.
  Exit 0 done, 1 an unreadable spool directory, a write failure or a mismatch, 3 a lock busy.
  - A second execution under one `run_id` (an f2bef053 writer booked an attempt again while it waited in the spool) is
    booked under the next free attempt with `renumbered_from` and a `cost_ledger_renumbered` receipt. A correction of a
    renumbered record follows it under the renumbered run id, with `supersedes` naming the renumbered line, also when it
    comes in a later call; the writer leaves such a correction to the replay (`landed == "spool"`).
  - A spool file that is not one run's goes to `provider_costs.spool/quarantine/` (OI-2243), each one a file of the
    batch and the budget; its receipt is booked before the move, and a move that failed after it is finished without a
    second receipt. It counts in the new `LedgerTotals.spool_quarantined`; it no longer blocks `record_next_attempt`.
  - The aggregation (OI-2244, OI-2241): a spooled record already in the ledger with the same content is no backlog, any
    unreadable line makes the total incomplete, a reader of a store it may not write reads without creating the lock
    file (`unlocked_read`), and the text says `1 run wacht op replay`.
