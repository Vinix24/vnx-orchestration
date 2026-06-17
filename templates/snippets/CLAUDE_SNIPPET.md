## VNX Governance System

This project uses **VNX Glass Box Governance** for multi-agent orchestration.

### How It Works
VNX coordinates work across 4 terminals (T0-T3) with human gates at every step:
- **T0** (Orchestrator): Plans work, creates dispatches, reviews results. Does NOT write code.
- **T1** (Track A): Primary implementation — components, pages, features.
- **T2** (Track B): Testing, integration, validation.
- **T3** (Track C): Code review, security, performance analysis.

### Key Paths
- `.vnx/` — VNX system (skills, scripts, templates, docs). Do not modify.
- `.vnx-data/` — Runtime state (dispatches, receipts, logs). Do not commit.
- `.claude/terminals/T0-T3/CLAUDE.md` — Terminal-specific instructions.
- `.claude/skills/` — Agent skills (copied from the shipped template at init; after init this is the source of truth — edit skills here directly).

### Workflow
1. T0 creates a dispatch in `.vnx-data/dispatches/pending/`
2. Human promotes dispatch (approval gate)
3. Workers (T1/T2/T3) execute their assigned tracks
4. Interactive workers write reports to `$VNX_DATA_DIR/unified_reports/`
5. Headless review gates write normalized reports to `$VNX_DATA_DIR/unified_reports/headless/` and structured results to `.vnx-data/state/review_gates/results/`
6. Receipt processor generates NDJSON audit trail
7. T0 reviews receipts, review-gate evidence, and closure state before advancing quality gates

### Rules
- Every change goes through a dispatch. No cowboy commits.
- PRs are small (150-300 lines) and independently deployable.
- `.vnx-data/` is runtime state — never commit it.
- Read your terminal's CLAUDE.md for role-specific instructions.
- Required headless review gates are not complete until both the result record and the normalized headless report exist.

### Mandatory Report Contract

**Every agent and worker MUST write a unified report on completing any task.**

This is how work enters the governed audit trail:
```
report on disk → receipt processor → t0_receipts.ndjson
```

Without a report, your work has no receipt and is invisible to governance.

Write to: `$VNX_DATA_DIR/unified_reports/<dispatch-id>.md`

Your report MUST contain these exact headings (aliases accepted):

| Required | Accepted aliases |
|---|---|
| `## Summary` | — |
| `## Changes` | `## Files Modified`, `## Work Completed` |
| `## Verification` | `## Test Results`, `## Evidence`, `## Tests` |
| `## Open Items` | — |

`## Summary` must be at least 50 non-whitespace characters. `## Open Items` may contain "None" explicitly. Include your dispatch ID as a plain-text or bold field (e.g. `Dispatch-ID: 20260601-213416-myfeature`). Full contract: `scripts/lib/report_body_contract.py`.

### Dispatch lanes

Two lanes ship on main; T0 picks per task. Full decision rule, provider strings, concurrency, and failure modes live in **`docs/core/DISPATCH_RULES.md`** (tmux-spawn lane detail: `docs/operations/TMUX_SPAWN_LANE.md`).

- **`scripts/lib/tmux_interactive_dispatch.py`** (default) — leaseless ephemeral, isolated worktree per dispatch, drives an interactive `claude` worker on the subscription. Use for parallel/independent feature work.
- **`scripts/lib/subprocess_dispatch.py`** — terminal-pinned (Wave 5 smart-context, lease, triple-gate). Opt in per terminal with `VNX_ADAPTER_T{n}=subprocess`. Use for single-worker PRs that benefit from prior-round findings, or work expected to run >30 min. **No Anthropic SDK** — only `subprocess.Popen(["claude", ...])`.

For full documentation: `.vnx/docs/`
