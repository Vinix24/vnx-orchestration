- Review gates, GE-3 (track `gate-engine-consolidation`): the overwrite guard now sees every module that writes a
  `review_gates/results/` record. `gate_result_parser.record_claude_github_result`,
  `gate_result_parser._validate_and_persist_result` (the `record-result` CLI) and
  `gate_request_handler._persist_claude_github_files` wrote their record with a bare `write_text`; all three now
  route through `gate_recorder.write_result_guarded` (slot lock, overwrite guard, atomic write), keeping each
  record's path and content shape. A `record-result` for the same PR, gate and head that would downgrade a
  decided, evidenced verdict is refused instead of erasing it, and books no receipt; on a new head it lands as
  before. `tests/test_oi1472_residual_guard_writers.py` now opens both modules, so a future raw writer fails CI.
  Routing must not book one outcome twice: `write_result_guarded` gained `emit_outcome_receipt=False`, used by
  the two callers that already emit their own richer, contract-linked `review_gate_result` receipt — that
  gate-specific receipt stays, because it carries findings and (for the Claude GitHub path) the head read off
  the contract request, which the result record itself does not carry.
