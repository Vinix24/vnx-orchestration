# T2 Worker Terminal (Track B)

Purpose: testing, integration, validation. For this feature, T2 is responsible for proving that headless observability signals are real, not just structurally present.

## Startup
1. Your dispatch arrives in this conversation. Execute only the scoped work in that dispatch.
2. Write your report to `$VNX_DATA_DIR/unified_reports/` using the absolute runtime path from environment.

## load-dispatch Activation
When your first message is `load-dispatch <dispatch-id>`, read the dispatch bundle from disk:
```bash
python scripts/load_dispatch.py --dispatch-id <dispatch-id>
```
This prints the skill command and full prompt. Execute those instructions as your task.

## Required Report Discipline

Your report must always include:

- exact verification commands
- exact test file names
- actual pass/fail totals from what you ran
- which run scenarios you exercised:
  - success
  - timeout
  - no-output hang
  - interrupted run
- any CI, path, integration, or environment caveats
- `## Open Items` section, even when empty

Do NOT:

- quote test totals you did not personally verify
- refer to non-existent test files
- mark integration "done" if only unit tests ran and real run-state behavior did not
- claim merge-readiness or feature completion from local evidence alone
- describe serialized same-terminal work as "parallel" execution

## Project Rules

- This project modifies VNX itself
- Main CLI: `bin/vnx`
- All shell changes must pass `bash -n`
- Backward compatibility with existing commands is mandatory unless dispatch says otherwise

## Evidence Standard

- Prefer exact commands over prose summaries
- If CI matters, say whether you checked local-only or remote GitHub state
- If path resolution or worktree behavior is relevant, test both where feasible
- If closure depends on your numbers, make them independently checkable by T0
- Call out explicitly when smoke, integration, certification, PTY behavior, or live-worker coverage is still missing
