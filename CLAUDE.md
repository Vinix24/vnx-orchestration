<!-- VNX:BEGIN BOOTSTRAP -->
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
<!-- VNX:END BOOTSTRAP -->

<important if="working on schemas/migrations">
ADR-007 binding: every new central-DB table requires composite UNIQUE/PK over project_id.
See `docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md`.
T0 must cite this explicitly in review-gate prompts.
</important>

<important if="working on review-gates or codex/kimi/gemini providers">
Per CC-COMMUNITY-SYNTHESIS-2026-05-29.md: codex for strict diff-mode, kimi for synthesis/operational angle.
Parallel review pattern proven 3x. Raw vs gate-routed dispatch = different audit trail — audit concern applies.
</important>

<important if="working on dispatch infrastructure or subprocess adapter">
Wave 6 elastic pool shipped 2026-05-16 (ADR-018, 9 PRs). Use `bin/vnx pool {status,scale,config,reap}`.
Backward-compat: terminal-pin via subprocess_dispatch.py still works.
SubprocessAdapter path: `scripts/lib/subprocess_adapter.py` + `scripts/lib/subprocess_dispatch.py`.
Single dispatch entry is the door (`vnx dispatch`): decision-tree enforced in code + side-door blocking.
Dispatch mechanics, lanes, and failure modes: `docs/core/DISPATCH_RULES.md`.
</important>

<important if="working on receipt processor or governance/audit trail">
GOV-1/2/3 receipt-gap: raw `claude -p` bypasses receipts. rc9 shipped cheap-lane fixes.
Self-learning loop is dormant. Receipt processor must be running for audit trail integrity.
`.vnx-data/` is runtime state — never commit it.
</important>

<important if="working on tmux delivery or session hooks">
Hard rule: Enter ALWAYS as a separate tmux keystroke — combined send-keys misses delivery.
Leaseless lane live on main (#663+#664). Known bugs: timestamp drift, env-not-inherited.
</important>

## Path Resolution

All scripts must resolve project root via helper libraries — never hardcode paths or rely solely on env vars. Python: `scripts/lib/project_root.py`. Bash: `scripts/lib/vnx_resolve_root.sh`. Background: issue #225.

## Event Streams

`.vnx-data/events/T{n}.ndjson` is a **per-dispatch ring buffer**, not a long-running log. At the end of each subprocess-adapter dispatch, the live file is archived to `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson` and truncated to 0 bytes. If you're debugging "the live file is empty", look in the archive directory instead.

Only subprocess-routed terminals produce this stream. TmuxAdapter-routed terminals (T0 default; T2/T3 unless `VNX_ADAPTER_T{n}=subprocess`) produce no per-terminal NDJSON.

## Supervisor Mode

Set per-project to enable unified supervisor (auto-respawn, lease sweep, runtime supervision):

```
VNX_SUPERVISOR_MODE=unified   # opt in to supervisor
```

Default (unset or `legacy`): no behavior change.

When enabled:
- Dispatcher prelude ticks `lease_sweep` every 30s
- Dispatcher prelude ticks `runtime_supervise` every 60s
- Recommend wrapping daemons via `dispatcher_supervisor.sh` and `receipt_processor_supervisor.sh`

See `docs/operations/UNIFIED_SUPERVISOR.md` for full guide.

<!-- Local maintainer overrides (optional, gitignored): machine-specific VNX notes live in
     ~/.claude/vnx-local.md; repo-local private notes in CLAUDE.local.md. Both load after this
     file and win on conflict. Keep secrets and absolute local paths out of this tracked file. -->
@~/.claude/vnx-local.md
