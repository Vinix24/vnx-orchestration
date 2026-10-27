- Gate engine (track `gate-engine-consolidation`, GE-4c-1): the failure record (`gate_recorder.record_failure` and
  `record_failure_simple`), the not-executable request and both `gate_result_parser` writers (`record_result` and
  `record_claude_github_result`) now go through `gate_outcome.commit_gate_outcome`. One call writes the record with an
  `attempt_seq`, books at most one `review_gate_result` receipt under the slot lock, and ends its request in the same
  commit; a refused write books no receipt and leaves the request as it was. The two parser receipts are booked by the
  one emitter and keep every field they carried (findings, summary, residual risk, required reruns, the claude_github
  head off its request), passed as `receipt_fields`. Their `status` now uses the receipt vocabulary (`completed` /
  `failed`) with the raw word under `gate_status`, like every other gate receipt. Only a decided pass or fail books a
  receipt: a `record-result` for `unavailable`, `not_executable` or an in-flight status no longer adds one, and a
  status that is neither an outcome nor in flight is refused before anything is written. The unused
  `write_result_guarded(emit_outcome_receipt=...)` opt-out is gone.
  A write on the same head, status and decision that changes `blocking_findings`, `advisory_findings`,
  `required_reruns` or `residual_risk` is no repeat: it lands as the next attempt with its own receipt, and is
  refused (`DuplicateReceiptRefused`, naming the changed fields) when the receipt key would return that receipt as
  a duplicate of the one already booked for the record on the slot.
