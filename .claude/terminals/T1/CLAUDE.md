# T1 Worker Terminal (Track A)

Purpose: implementation-heavy tasks.

## Startup
1. Your dispatch arrives in this conversation. Execute scoped implementation work based on the dispatch.
2. Write status/update report to `.vnx-data/unified_reports/`

## load-dispatch Activation (PR-3 Runtime Core)
When your first message is `load-dispatch <dispatch-id>`, read the dispatch bundle from disk:
```
python scripts/load_dispatch.py --dispatch-id <dispatch-id>
```
This prints the skill command and full prompt. Execute those instructions as your task.
The bundle is at `.vnx-data/dispatches/<dispatch-id>/bundle.json` and `.../prompt.txt`.

## Open Items in Reports (MANDATORY)
Your report MUST always include an `## Open Items` section — even when empty.

## Key Context
- This project modifies VNX itself (bash/python orchestration system)
- Main CLI: `bin/vnx` (~2700 lines bash)
- Path resolution: `scripts/lib/vnx_paths.sh`
- All shell changes must pass `bash -n`
- Backward compatibility with existing commands is mandatory
