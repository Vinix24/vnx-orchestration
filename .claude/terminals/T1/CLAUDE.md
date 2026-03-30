# T1 Worker Terminal (Track A)

Purpose: implementation-heavy tasks. For this feature, expect concrete Python/runtime changes that make headless runs inspectable.

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

- what changed
- exact commands you ran
- exact test files and totals you ran
- whether you tested PTY-backed, non-PTY, or synthetic paths
- known limitations or unresolved runtime gaps
- `## Open Items` section, even when empty

Do NOT:

- invent totals
- say "tests passed" without naming the command
- say "done" if you left follow-up work or ambiguity
- claim a PR or feature is closure-ready; only T0 can declare governance completion
- claim headless behavior is operationally proven if you only exercised unit paths

## Project Rules

- This project modifies VNX itself
- Main CLI: `bin/vnx`
- All shell changes must pass `bash -n`
- Backward compatibility with existing commands is mandatory unless dispatch says otherwise
- Path handling must work in both main repo and worktree contexts where relevant

## Evidence Standard

- If you changed code, say which files changed
- If you ran tests, include the exact command
- If you could not run something, say that explicitly
- If headless logging, heartbeat, or exit classification is involved, say exactly which evidence path you verified
- Distinguish clearly between local verification and any remote GitHub/CI checks you did not perform
