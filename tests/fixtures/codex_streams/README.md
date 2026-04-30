# Codex Stream Parser Version Fixtures

This directory contains captured (or synthesized) NDJSON output samples from `codex exec`,
one fixture file per known CLI version and scenario.

## How to capture a new version's output

When a new codex CLI version ships:

1. Run the gate against a known PR with `--json` output enabled and capture stdout:
   ```bash
   codex exec --json "$(cat prompt.md)" > tests/fixtures/codex_streams/v0.X-success.json
   ```

2. Capture a failure scenario (e.g. with a deliberately broken PR):
   ```bash
   codex exec --json "$(cat prompt_failing.md)" > tests/fixtures/codex_streams/v0.X-failure.json
   ```

3. Add corresponding test cases in `tests/test_codex_parser_versions.py` (see the existing
   `v0.118` test class as a template).

4. Update the stub file for this version (e.g. `v0.X-stub.json`) into a real fixture.

## File naming

- `vX.Y-success.json` — gate passed, empty findings
- `vX.Y-failure.json` — gate failed, structured findings
- `vX.Y-rate-limit.json` — CLI returned a rate-limit error
- `vX.Y-blocking-findings.json` — critical/security findings present
- `vX.Y-stub.json` — placeholder for a version not yet captured in production

## Fixture format

Each file contains NDJSON (one JSON object per line). The parser (`scripts/lib/codex_parser.py`)
handles these event types emitted by codex CLI:

- `{"type": "agent_message", "text": "..."}` — top-level text event
- `{"item": {"type": "agent_message", "text": "..."}}` — item-wrapped event
- `{"type": "error", "message": "..."}` — error or rate-limit event
- `{"type": "token_usage", "input_tokens": N, "output_tokens": M}` — billing metadata

The verdict JSON block (embedded in agent_message text) must contain `verdict` and/or `findings` keys.

## Version history

| Version | Status | Notes |
|---------|--------|-------|
| 0.118   | captured (synthesized) | Primary version at dispatch time (2026-04-30) |
| 0.119   | stub | Ship when CLI updates |
| 0.120   | stub | Ship when CLI updates |
