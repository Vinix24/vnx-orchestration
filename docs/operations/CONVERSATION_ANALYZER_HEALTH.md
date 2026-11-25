# Conversation analyzer health and failure causes

The nightly analyzer writes `health/conversation_analyzer.json` under the resolved
project data directory. Its details include `deep_attempts`, `deep_failures`, and
`failure_causes`. A run otherwise marked `ok` becomes `degraded` when more than half
of its attempted deep analyses fail (`deep_failure_majority`). The details then
include `dominant_cause`, selected by highest count, with alphabetical tie breaking.
Missing or malformed cause counts yield `failure cause unavailable`; they do not
clear the degraded state. Invalid attempt/failure counts produce
`deep_counts_invalid`.

The existing `provider_switched_off` and `sessions_due_none_analysed` rules keep
their meaning. Exactly half failing does not trigger the majority rule. A run
already marked `fail` keeps that status, including an entirely failed run reported
by the CLI.

For failed CLI children, the analyzer reads `result`, `errors`, and `subtype` from
stdout JSON before considering meaningful stderr. Known startup notices are not
treated as causes. Without a readable message it records the exit code and a
bounded stdout excerpt. DeepSeek harness diagnostics also retain the cached
preflight balance state. That snapshot is from the beginning of the process, not
a fresh balance measurement at failure time. A refused preflight records its
`key_missing`, `balance_low`, or `balance_unavailable` reason in the cause counters.
Unavailable, malformed, and non-finite balances refuse the spawn.

## Configuration

Analyzer, digest, receipt-classifier and Ollama settings resolve through `config_runtime`:
override > project (for project-scoped keys) > global > environment > default.
The analyzer strategy remains `ollama-only`; its plist still selects `deepseek-harness`
unless a stored global value or override wins. Registered values are read when used.
The nightly scripts use the repository's `vnx config get --raw`; an unavailable command
logs a warning and uses `ollama-only`, an empty digest recipient or classifier off.
SMTP stays on port 587 with the recipient as username; the password stays in env/keychain.
Ollama model defaults remain consumer-specific: analyzer `qwen2.5-coder:14b`, adapter/router
`gemma3:27b`, classifier `llama3.1:8b`. A configured model applies to all consumers.

## Measured incident: 21 November 2026

The 01:00–01:09 UTC run's first three DeepSeek calls failed; successes and failures
then interleaved. All 34 failed harness transcripts ended in `tool_use`; the 13
successful transcripts ended in `end_turn`. Preflight reported 11.24 USD, and the
47 DeepSeek ledger rows booked 0.06696735 USD in total, with complete token usage.
This was not a sequence of thirteen successes followed by an exhausted balance.

A replay through the same shared harness builders, with the original analyzer
prompt and `--max-turns 1`, returned exit code 1 and:

```json
{"is_error": true, "subtype": "error_max_turns", "terminal_reason": "max_turns",
 "errors": ["Reached maximum number of turns (1)"], "stop_reason": "tool_use"}
```

There was no `result` field, and stderr held only the unrecognized-model startup
notice. Ignoring `errors` and `subtype` hid the actual stop. The cause now reads
`error_max_turns: Reached maximum number of turns (1)`.

The cause is fixed by offering no tools: deep analysis needs one JSON text answer.
Both Claude-Max and DeepSeek-harness calls use `DEEP_ANALYSIS_PRINT_FLAGS` in
`deep_analyzer.py`: `--max-turns 1 --tools ""`. The empty argument disables all
built-in tools without increasing the turn limit. Both lanes also pass
`--mcp-config '{"mcpServers":{}}' --strict-mcp-config`, excluding inherited MCP
tools. The DeepSeek builder already supplied these MCP flags (including in the
failed replay); the Claude-Max call now supplies them too.

The analyzer's direct CLI path does not call `enforce_daily_budget`; that guard
lives in provider dispatch. No budget, balance threshold, turn limit, model,
provider, or schedule is changed. Failure diagnostics and degraded health remain
in place for other failed calls.
