- Review gates (track `gate-engine-consolidation`, GE-4a, ADR-046): `scripts/lib/gate_outcome.py` adds `GateOutcome`
  (one of `pass`, `fail`, `unavailable`, `not_executable`) and `commit_gate_outcome`, the one writer of a gate's
  result record and its `review_gate_result` receipt. Under the slot lock it writes the record atomically, then the
  receipt. A failed receipt append leaves the record with `receipt_pending: true`, and the next commit on that slot
  books it once through the existing idempotency key. Each landed record carries `attempt_seq`, counting up within
  the slot. `record_terminal_result` and `record_not_executable` route through it with unchanged signatures and
  return values; `record_terminal_result` now refuses an in-flight status (`pending`, `running`) before it writes.
