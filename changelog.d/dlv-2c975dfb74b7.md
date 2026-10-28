- Cost ledger (K4b): the conversation analyzer's `claude` and `deepseek-harness` lanes and the dream
  cycle's kimi call now write one ledger line per call (role `intelligence`, ADR-043). The analyzer
  books through `harness_run_cost.record_classifier_run` with producer `conversation_analyzer`; the
  dream cycle books through `record_kimi_run` with producer `dream`. `record_classifier_run` gains a
  `producer` keyword (default `classifier`) so the two intelligence producers stay apart.
- Cost ledger (K4b): the line follows the call, never the hope. Both analyzer lanes pass a fresh
  `--session-id <uuid4>`, so the tokens come from that call's own transcript, and the model is the one
  the CLI reports (`model`/`modelUsage`), falling back to `VNX_ANALYZER_CLAUDE_MODEL` (default
  `sonnet`) only when the child named none. A child that never started books
  `cost_source=unavailable:spawn_refused` with five confirmed zeros (`missing_cli`, or the
  deepseek-harness pre-spawn statuses `key_missing`/`balance_low`/`balance_unavailable` and an unsafe
  harness config dir); a call that ran and left no transcript books `unknown:no_transcript`, never a
  zero. dream's kimi stream carries no usage accounting, so it books `unknown:kimi_cli_no_usage`.
- Cost ledger (K4b): ollama is not booked. A local model is neither `metered` nor `subscription`, and a
  third billing class is out of scope here; the analyzer's local lane writes no line.
- Cost ledger (K4b): a refused ledger write never changes the analysis or the consolidation result —
  the booker logs its WARNING and the caller continues with the outcome it already has.
