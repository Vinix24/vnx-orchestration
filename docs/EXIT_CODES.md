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

### Open-item → track bridge (`scripts/import_open_items_to_tracks.py`)

The bridge (PR-C, #862) defines contract-bound exit codes so an operator or the
autopilot can distinguish failure classes. Source of truth: the `EXIT_*`
constants in `scripts/import_open_items_to_tracks.py`.

| Code | Constant | Meaning | Operator action |
|------|----------|---------|-----------------|
| `0` | `EXIT_OK` | Bridge run committed; all ledger events emitted | None |
| `1` | `EXIT_GENERIC_ERROR` | Unexpected error (see stderr) | Inspect stderr; safe to re-run (idempotent) |
| `3` | `EXIT_SOURCE_MISSING` | The open-items source is absent, unreadable, or structurally invalid (wrong shape) | Restore/repair `open_items.json`; the bridge fails loud rather than treat a missing source as "close every link" |
| `4` | `EXIT_LEDGER_FAILURE` | The DB mutation **committed**, but a post-commit ADR-005 ledger event failed to emit (D3 at-most-once) | Non-fatal: the DB is authoritative and the reconciler re-derives `derived_status`. Investigate the ledger writer; re-running is safe |
| `5` | `EXIT_SCHEMA_PRECONDITION` | The migration 0030 resolution schema (`resolved_at` / `resolution_reason`) is absent (pre-0030 DB) | Run the migration to ≥ 0030, then re-run the bridge. The bridge never reports success on a pre-0030 DB |
| `6` | `EXIT_DB_ERROR` | A DB-layer failure (locked/malformed/SQLite error) — distinct from a ledger failure | Check DB health and locks (`integrity_check`); re-run after resolving |

Exit `4` is the deliberate D3 tradeoff: events are emitted **after** the commit,
so a ledger-emit failure leaves the DB correct (at-most-once, never orphaned) and
the missing event is reconcile-compensated. Exactly-once via a transactional
outbox is deferred to 1.x (#867). See ADR-005 and `docs/MIGRATION_GUIDE.md`.

### Intelligence corpus quarantine (`scripts/vnx_intelligence_corpus_quarantine.py`)

The quarantine tool defines five contract-bound exit codes so an operator can tell
a refusal from an unwinding failure. Source of truth: the `return` statements in
`main` (`scripts/vnx_intelligence_corpus_quarantine.py:110-115`); policy in
`docs/governance/decisions/ADR-070-intelligence-corpus-quarantine.md`; the operator
view in `docs/operations/INTELLIGENCE_CORPUS_QUARANTINE.md`.

| Code | Meaning | Operator action |
|------|---------|-----------------|
| `0` | Done | Continue with the next step |
| `1` | Unexpected error, or a failed audit line in `verify` (nothing was committed); see stderr | Read stderr, fix the cause, re-run the step |
| `2` | Misuse: an argument is missing or `--out` does not exist | Correct the command; nothing was measured |
| `3` | Refused: the permit, the decoy check, the invariant or the manifest does not hold — **nothing changed** | Read the reason (stderr and the `refused` audit line); repair with `baseline --reuse-snapshot` or a new `quarantine` |
| `4` | After the commit or the seal: the after-check, the `VACUUM`, an audit line or a rehearsal verdict that is not fully true failed — the corpus is gone and the artifact is the restore path | Do not clean up; read the error and restore from the artifact |

Exit `3` is a refusal before the first write. Exit `4` is never "try again": the
store is already changed or the artifact is already sealed.

## Notes

- Scripts should default to JSON output for machine parsing.
- Add `--human` to emit human-readable output for operators.
- Keep dispatch/receipt formats unchanged when adding JSON-first outputs.
- The headless failure class is stored in `.vnx-data/state/headless_runs/` as `failure_class` for operator inspection.
