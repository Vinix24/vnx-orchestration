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
- `.claude/skills/` — Symlinked to `.vnx/skills/` (8 agent skills).

### Workflow
1. T0 creates a dispatch in `.vnx-data/dispatches/pending/`
2. Human promotes dispatch (approval gate)
3. Workers (T1/T2/T3) execute their assigned tracks
4. Workers write reports to `$VNX_DATA_DIR/unified_reports/`
5. Receipt processor generates NDJSON audit trail
6. T0 reviews receipts and advances quality gates

### Rules
- Every change goes through a dispatch. No cowboy commits.
- PRs are small (150-300 lines) and independently deployable.
- `.vnx-data/` is runtime state — never commit it.
- Read your terminal's CLAUDE.md for role-specific instructions.

For full documentation: `.vnx/docs/`
