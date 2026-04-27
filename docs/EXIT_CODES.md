# VNX Exit Codes

## Headless Run Failure Classes

The VNX headless pipeline uses named failure classes (not numeric bands).
Source of truth: `scripts/lib/exit_classifier.py`.

| Class | Retryable | Meaning | Operator action |
|-------|-----------|---------|-----------------|
| `SUCCESS` | No | Exit code 0, no error conditions | Check output artifact for results |
| `TIMEOUT` | Yes | Subprocess exceeded time limit | Increase `VNX_HEADLESS_TIMEOUT` or simplify the prompt |
| `TOOL_FAIL` | Yes | Transient tool/network error | Retry is safe — check stderr for details |
| `INFRA_FAIL` | Yes | Binary missing, OOM, or infrastructure problem | Check CLI binary install or system resources |
| `NO_OUTPUT` | Yes | Subprocess produced no output (hung or crashed silently) | Check for deadlocks or oversized prompts |
| `INTERRUPTED` | No | Terminated by a signal (SIGTERM, SIGKILL, etc.) | Check for resource limits or manual termination |
| `PROMPT_ERR` | No | Prompt rejected by the CLI | Review and fix the prompt before retrying |
| `UNKNOWN` | No | Unrecognised exit condition | Check exit code and stderr for clues |

## Numeric Exit Codes (CLI scripts)

Operational scripts (`scripts/commands/`) use conventional numeric exit codes:

- `0` — Success
- `1` — General failure (see stderr)
- `2` — Misuse / invalid arguments

## Notes

- Scripts should default to JSON output for machine parsing.
- Add `--human` to emit human-readable output for operators.
- Keep dispatch/receipt formats unchanged when adding JSON-first outputs.
- The headless failure class is stored in `.vnx-data/state/headless_runs/` as `failure_class` for operator inspection.
