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
- `.vnx/skills/` — 8 agent skills (planner, architect, backend-developer, etc.)

### Workflow
1. T0 creates a dispatch → human promotes it (approval gate)
2. Dispatcher sends the dispatch into the worker's conversation (T1/T2/T3)
3. Workers execute their assigned task based on what they received
4. Workers write reports to `$VNX_DATA_DIR/unified_reports/`
5. Receipt processor generates NDJSON audit trail
6. T0 reviews receipts and advances quality gates

**Workers**: You receive your dispatch in-conversation. Do NOT look in `.vnx-data/dispatches/` — that directory is managed by the system.

### Rules
- Every change goes through a dispatch. No cowboy commits.
- PRs are small (150-300 lines) and independently deployable.
- `.vnx-data/` is runtime state — never commit it.
- Stay within assigned dispatch scope.

For full documentation: `.vnx/docs/`
