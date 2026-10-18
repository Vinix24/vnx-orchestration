# VNX Orchestration System - Complete Architecture

**Status**: Active
**Last Updated**: 2026-10-18
**Owner**: T-MANAGER
**Purpose**: Single source of truth for VNX system architecture, components, and data flow.

This document does not stamp a product version. The running version is `VERSION`
(repo root); a number copied here would drift the day the repo cuts a release.

---

## Table of Contents
1. [System Overview](#system-overview)
2. [Terminal Architecture](#terminal-architecture)
3. [Core Components](#core-components)
4. [Data Flow](#data-flow)
5. [File Formats](#file-formats)
6. [Process Management](#process-management)
7. [Intelligence Systems](#intelligence-systems)
8. [Open Items System](#open-items-system)
9. [Future-State Reconciliation: Open-Item → Track → Dispatch](#future-state-reconciliation-open-item--track--dispatch)
10. [Staging Workflow](#staging-workflow)
11. [Review Seats](#review-seats)
12. [Multi-Provider Dispatch](#multi-provider-dispatch)
13. [Unified Dashboard](#unified-dashboard)
14. [Demo & Distribution](#demo--distribution)

---

## System Overview

VNX is a file-based orchestration system enabling parallel development across multiple Claude Code terminals with centralized T0 orchestration brain.

### Core Principles
- **File-Based Communication**: NDJSON receipts + Markdown dispatches
- **Deliverable-Based Governance**: T0 is sole authority for declaring work done; workers attach evidence, receipt processor tracks but does not close
- **Native Skill Architecture**: skills invoke natively (`/skill-name` in Claude Code, `$skill-name` in Codex, `@skill-name` in Gemini) instead of a compiled prompt template
- **Multi-Provider Dispatch**: every provider runs as a CLI subprocess, never an imported SDK (`no-anthropic-sdk` in `scripts/lib/providers/provider_constraints.yaml`); full lane map, takeover chain and the `vnx providers` switch in `docs/core/PROVIDER_LANES.md`
- **Project-Scoped Process Isolation**: `VNX_KILL_SCOPE` prevents cross-project process interference
- **Singleton Process Enforcement**: Bulletproof duplicate prevention
- **Progressive Intelligence**: Token-efficient context aggregation
- **Quality Advisory Pipeline**: Automatic file size/complexity warnings on every completion
- **Track-Agnostic Workers**: T1-T3 are role labels on ephemeral, headless build-worker dispatches, not persistent terminals; T0 dispatches the next role to the lane the door selects
- **Multi-Model Coordination**: T0 runs Opus 5.5 (`t0-opus-only`, a floor); T1-T3 build-workers default to Sonnet (`workers-kimi-pinned`, advisory — a dispatch spec's own model wins)
- **Review Seats**: every seat of the project's review stack is filled by its own reviewer, and a rejection by any review peer blocks the merge (see *Review Seats*)
- **Git Worktree Isolation**: every headless dispatch runs in its own worktree under `.vnx-data/worktrees/`, provenance in every receipt

### Worktree Model

Each headless dispatch gets its own isolated git worktree under
`.vnx-data/worktrees/dispatch-<id>/`, cut from `origin/main` by
`scripts/lib/dispatch_worktree_isolation.py` and torn down when the dispatch ends.
For operator-driven feature work, `vnx new-worktree <name>` creates a git worktree
and bootstraps it in one step: an isolated `.vnx-data/` with an intelligence
snapshot, skills, terminals, hooks, settings and the plan files
(`scripts/commands/new_worktree.sh`).

**Commands**:
- `vnx new-worktree <name>` -- creates git worktree + full bootstrap in one step
- `vnx merge-preflight <name>` -- governance GO/NO-GO verdict
- `vnx finish-worktree <name>` -- governance-gated closure with intelligence merge-back

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    VNX ORCHESTRATION SYSTEM                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │  T0 (BRAIN)  │  │ T1 (worker)  │  │ T2 (worker)  │         │
│  │ Claude Opus  │  │Sonnet(default│  │Sonnet(default│         │
│  │ Read-Only    │  │  Full R/W    │  │  Full R/W    │         │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘         │
│         │                  │                  │                  │
│         │   ┌──────────────┴──────────────────┘                │
│         │   │          ┌──────────────┐                         │
│         │   │          │ T3 (worker)  │                         │
│         │   │          │Sonnet(default│                         │
│         │   │          │ Any Task     │                         │
│         │   │          └──────┬───────┘                         │
│         │   │                  │                                 │
│         ▼   ▼                  ▼                                 │
│  ┌──────────────────────────────────────┐                       │
│  │        FILE-BASED MESSAGE BUS        │                       │
│  │  • Dispatches: .md (.vnx-data/)      │                       │
│  │  • Receipts: .ndjson (state/)        │                       │
│  │  • Reports: .md (unified_reports/)   │                       │
│  │  • Quality: sidecar + advisory       │                       │
│  └──────────────────────────────────────┘                       │
│         │                                                        │
│         ▼                                                        │
│  ┌──────────────────────────────────────┐                       │
│  │     ORCHESTRATION PROCESSES          │                       │
│  │  • Single-entry dispatch door        │                       │
│  │  • Receipt Processor (delivery)      │                       │
│  │  • T0 Brief Generator (Snapshot)     │                       │
│  │  • Quality Advisory (File analysis)  │                       │
│  │  • Supervisor (Health monitoring)    │                       │
│  │  • Queue Popup Watcher (Dispatch UI) │                       │
│  │  • Dashboard Server (serve_dashboard)│                       │
│  │  • Nightly Intel Pipeline (02:00)    │                       │
│  └──────────────────────────────────────┘                       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

T1-T3 are role labels, not persistent panes: each build-worker dispatch is an
ephemeral `claude -p` run in its own isolated worktree (see *Terminal
Architecture* below), routed through the single-entry dispatch door. The
legacy Dispatcher/Smart Tap process pair still exists for the older PR-queue
path (Core Components §1) but is not the primary route.

---

## Terminal Architecture

### Terminal Specifications

T0 is the one persistent terminal: an interactive orchestration session that plans
work and reviews receipts, never writes code itself. T1-T3 are **role labels on
dispatches, not persistent panes** — a build-worker dispatch is an ephemeral
`claude -p` run in its own isolated worktree (`dispatch_envelope.run_envelope_headless_plan`),
torn down when the dispatch completes. There is no standing T1/T2/T3 process to
inspect between dispatches.

| Role | Nature | Model | Permissions | Purpose |
|------|--------|-------|-------------|---------|
| **T0** | Persistent, interactive | Opus 5.5 (`t0-opus-only`, a floor) | Read-only | Plans dispatches, reviews receipts, advances gates — never writes code |
| **T1 / T2 / T3** | Ephemeral, headless, one per dispatch | Sonnet by default (`workers-kimi-pinned`, advisory — a dispatch spec's own model wins) | Scoped R/W per role (`docs/operations/WORKER_PERMISSIONS.md`) | Any task dispatched by T0, routed through the lane the door selects |

**Multi-Provider Support**: a dispatch's `provider` field, not a per-terminal flag,
selects the CLI: `claude` (the only Claude lane, `claude-headless`), `codex`,
`gemini`, `kimi`, `glm-harness`, or `deepseek-harness`. Full routing rules:
`docs/core/PROVIDER_LANES.md`. Skills are synced to `~/.claude/skills/`,
`~/.codex/skills/`, and `.gemini/skills/` during `vnx init`.

### Terminal Status Detection

`terminal_state.json` is built from receipts and events: a dispatch or acknowledgement
event marks a terminal `working`, a completion event `idle`, a failure or timeout event
`blocked`. When the primary view is stale (180 seconds), the fallback reconciler
`scripts/lib/terminal_state_reconciler.py` rebuilds it from receipts, tmux activity
and process health, and only rewrites the file in repair mode.

### Attention Model (`terminal_state.json`)

Each terminal entry carries three attention fields:

| Field | Type | Description |
|-------|------|-------------|
| `needs_human` | bool | True when operator action is required |
| `attention` | object or null | Details when `needs_human=true` |
| `context_usage_pct` | int or null | Context window fill % (from session logs) |

**`attention` object structure**:
```json
{
  "type": "context-pressure",
  "reason": "T2 context window at 87% capacity",
  "since": "2026-10-18T09:30:00+00:00",
  "jump_target": "T2"
}
```

**Attention triggers** (`scripts/lib/canonical_state_views.py`, computed per worker
terminal, in priority order):
- `blocked`: the terminal status is blocked
- `stale`: the terminal is `working` but has reported nothing for more than 180 seconds
  (`STALE_WORKING_SECONDS`)
- `context-pressure`: `context_usage_pct` is above 80 (`CONTEXT_PRESSURE_THRESHOLD`)

**`vnx jump` command** (`scripts/commands/jump.sh`):
```bash
vnx jump T2              # Switch tmux focus to T2
vnx jump --attention     # Focus the highest-priority attention terminal
```
The dashboard "Jump" action calls `POST /api/jump/{terminal}` which executes `vnx jump`.

---

## Core Components

### 1. Dispatcher (`dispatcher_minimal.sh`) — the PR-queue path only

**Purpose**: Native skill activation and instruction routing for the older
PR-queue path (`FEATURE_PLAN.md` → `staging/` → `queue/` → `pending/*.md`,
see *Staging Workflow* below). The single-entry dispatch door
(`scripts/lib/dispatch_cli.py`) is the primary, canonical path for every other
dispatch; this daemon only scans the markdown files the PR-queue path
produces. Check current liveness before relying on it — it is a supervised
daemon (`scripts/vnx_supervisor_simple.sh`) but is commonly absent between
runs (`docs/core/DAEMON_LIVENESS.md` is the generated, re-checkable source,
not this doc).

**Functionality**:
- Maps dispatch roles to native Claude Code skills
- Hybrid dispatch through tmux (`scripts/lib/dispatch_deliver.sh`): skill via `send-keys` (triggers slash-command detection) + instruction via `paste-buffer`
- No template compilation needed (skills load via `/skill-name args` invocation)
- Multi-provider skill invocation: `/skill-name` (Claude), `$skill-name` (Codex), `@skill-name` (Gemini)
- PR-ID included in dispatch prompt for receipt correlation
- Rich footer with "Expected Outputs" guidelines and report metadata template

**Receipt Footer**:
- Task Completion Guidelines section
- Report Metadata block (parsed by receipt processor)
- Expected Outputs section (implementation summary, files modified, testing evidence, open items)
- Report write path: `$VNX_DATA_DIR/unified_reports/`

### 2. Heartbeat ACK Monitor (`heartbeat_ack_monitor.py`)

**Purpose**: Acknowledgment receipt processing and timeout management

**Functionality**:
- Monitors for `task_ack` receipts
- Tracks acknowledgment timestamps
- Manages timeout detection
- Updates dispatch status

### 3. Receipt Processor (`receipt_processor.sh`) — Primary

**Purpose**: Parse new markdown reports into receipts, attach evidence to open items, and append to `t0_receipts.ndjson`.

**Functionality**:
- Monitors `$VNX_DATA_DIR/unified_reports/*.md` (monitor mode with time filtering)
- Uses `report_parser.py` to generate a compact JSON receipt
- Attaches evidence to tracked open items via PR-ID (does NOT close items or complete PRs)
- Appends receipts to `state/t0_receipts.ndjson` (production receipt log)
- Delivery to T0 is not a push: `VNX_RECEIPT_T0_PUSH=0` is the default
  (ADR-035 §5.3). T0 reads the dispatches it still has to decide on from
  `open_outcomes` (`t0_index.json`, or `scripts/receipt_query.py open-outcomes`)
  and closes each with `receipt_query.py decide` (DISPATCH_RULES §13). Setting
  the flag restores the tmux buffer-paste push.
- Includes flood protection + singleton enforcement

**Governance**: Receipt processor is evidence-only. T0 reviews evidence, closes satisfied open items, and completes PRs when all blockers/warnings are resolved.

### 4. Report Parser (`report_parser.py`)

**Purpose**: Extract a structured receipt from a worker markdown report

**Functionality**:
- Parses `$VNX_DATA_DIR/unified_reports/*.md`
- Normalizes metadata, tags, metrics, recommendations
- Produces compact JSON for `t0_receipts.ndjson`

**Note**: `report_watcher.sh` exists but production receipt ingestion is handled by `receipt_processor.sh`.

### 5. Context Rotation Hooks

**Purpose**: Context-rotation automation for long-running T0 sessions.

**Hooks actually wired** (see the generated *Hooks Wired* table further down,
which is the checkable source): `t0_context_guard.py` (PreToolUse/Stop/UserPromptSubmit),
`session_stop_rotation.py` (Stop). T0's own rotation flow, the mechanism
that actually ships, is `docs/operations/T0_CONTEXT_ROTATION.md`
(`scripts/hooks/t0_context_guard.py`, `scripts/t0_rotate_spawn.sh`). `vnx guard install|check`
registers the guard at user level and reports where it runs (`vnx_cli/commands/guard.py`);
the setup steps are in `docs/core/00_GETTING_STARTED.md`. A separate
worker-side rotation design (`vnx_context_monitor.sh` / `vnx_handover_detector.sh`
/ `vnx_rotate.sh`) is documented in the archived
`docs/_archive/core/technical/CONTEXT_ROTATION_SYSTEM.md`; those hook scripts
are not present in the tree and are not wired into `.claude/settings.json`.

**Receipts**:
- `context_rotation` receipts are **informational only**. T0 does not need to act on these receipts unless paired with a human decision or explicit dispatch.

### 6. T0 Intelligence Aggregator (`t0_intelligence_aggregator.py`)

**Purpose**: Context aggregation for T0 orchestration

**Functionality**:
- Imported by `unified_state_manager.py` (a supervised daemon), not a process of its own
- Receipt correlation and warnings
- Terminal insights and patterns
- Tag-based report lookup
- Rolling window, archived after 1000 events (`ARCHIVE_THRESHOLD`)

**Output**: `state/t0_intelligence.ndjson`

### 7. VNX Supervisor (`vnx_supervisor_simple.sh`)

**Purpose**: Process health monitoring and auto-restart

**Functionality**:
- Monitors all core processes
- Auto-restart on failure
- PID tracking in `state/pids/`
- Health checks every 5 seconds (`CHECK_INTERVAL`)

### 8. Dashboard Generator (`generate_valid_dashboard.sh`)

**Purpose**: Real-time system metrics visualization

**Functionality**:
- Updates every 2 seconds
- Terminal status aggregation
- Process health tracking
- Queue depth monitoring
- Dispatch throughput counted from `dispatcher.log` (the `successRate` and `avgProcessTime` fields are fixed strings, not measurements)

**Output**: `state/dashboard_status.json`

---

## Data Flow

### Complete Orchestration Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                       ORCHESTRATION LOOP                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. T0 Creates Dispatch                                         │
│     └─► Stages a bundle in dispatches/pending/<id>/              │
│                                                                  │
│  2. Human Promotes Dispatch                                      │
│     ├─► Operator reviews the staged bundle                      │
│     └─► `vnx dispatch <id>` fires it (approval gate)             │
│                                                                  │
│  3. The Single-Entry Door Routes the Lane                       │
│     ├─► validate → compile_plan → permit → execute               │
│     ├─► Assembles: skill body + intelligence + instruction        │
│     │    + report-contract footer                                │
│     ├─► claude → claude-headless lane (ephemeral worktree)        │
│     └─► kimi/glm/deepseek → provider_dispatch.py lane             │
│                                                                  │
│  4. Worker Executes Task                                        │
│     ├─► Performs the requested work in its isolated worktree    │
│     └─► Writes a markdown report in unified_reports/            │
│                                                                  │
│  5. Receipt Processor Handles Report                            │
│     ├─► Detects new report in unified_reports/                 │
│     ├─► Parses structured data via report_parser.py            │
│     ├─► Attaches evidence to open items (does NOT close)       │
│     └─► Appends to t0_receipts.ndjson                          │
│                                                                  │
│  6. Govern                                                      │
│     └─► phantom-guard checks the receipt against the diff       │
│                                                                  │
│  7. Intelligence Aggregator Updates Context                     │
│     └─► Consolidates system state into t0_intelligence.ndjson   │
│                                                                  │
│  8. T0 Reviews                                                  │
│     ├─► Sees the dispatch in open_outcomes until it decides     │
│     │    (DISPATCH_RULES §13)                                    │
│     ├─► Reads review-gate evidence and advances gates           │
│     └─► Creates the next dispatch [Loop Continues]              │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

The heartbeat ACK monitor (`heartbeat_ack_monitor.py`) is a supervised daemon that
processes acknowledgement events and timeouts for lanes that emit them. The full
end-to-end picture, with the governance step in detail, is
`docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md`.

---

## File Formats

Each format has one owner document. This section names where it lives.

- **Dispatch.** A staged bundle in the central pending dir:
  `<data_dir>/dispatches/pending/<dispatch-id>/instruction.md` plus
  `dispatch-spec.json`. The fields and the staging flow are in
  `docs/core/DISPATCH_RULES.md` section 12. The older PR-queue path writes markdown
  dispatches (see *Staging Workflow* below).
- **Receipt.** One NDJSON line per record in `state/t0_receipts.ndjson`. The shape is in
  `docs/core/11_RECEIPT_FORMAT.md`; the receipt v2 decision is ADR-035.
- **Report.** Markdown at `$VNX_DATA_DIR/unified_reports/<dispatch-id>.md` with the
  headings `## Summary`, `## Changes`, `## Verification`, `## Open Items` and a
  Dispatch-ID, Model and Provider identity block. The contract is
  `scripts/lib/report_body_contract.py`, summarised in `DISPATCH_RULES.md` section 8.
  A session field in the report lets cost tracking resolve the session transcript
  (see COST_TRACKING_GUIDE.md).
- **Intelligence stream.** `state/t0_intelligence.ndjson`, written through the T0
  intelligence aggregator (Core Components 6).

---

## Process Management

### Singleton Enforcement

**Mechanism**: PID files in `.vnx-data/pids/`
- Each process creates `{name}.pid` on start
- Checks for existing PID before starting
- Validates process is actually running
- Cleans up stale PID files

**Core Processes** (managed by supervisor): one `{name}.pid` per daemon that
`start_all()` starts. The list is the generated *Supervised Components* section
below, never a copy of it here. The supervisor itself is tracked by
`vnx_supervisor.pid`.

### Project-Scoped Process Isolation

**Problem**: `vnx_proc_find_pids_by_fingerprint()` used bare script names in `grep -F`, matching processes from all VNX projects system-wide.

**Solution**: `VNX_KILL_SCOPE` environment variable scopes process kills to the current project:
```bash
# When set, adds project-path filter before fingerprint grep
export VNX_KILL_SCOPE="$scripts_dir"  # e.g. /path/to/project/scripts

# Scoped kill: only kills processes containing BOTH the project path AND the fingerprint
ps -axo pid=,command= | grep -F "$VNX_KILL_SCOPE" | grep -F "$fingerprint" | ...
```

**Callers**: `vnx_kill_all_orchestration()` in `bin/vnx` exports VNX_KILL_SCOPE before the fingerprint loop and unsets it after.

### Process Cleanup (`vnx_kill_all_orchestration`)

**Purpose**: Full process cleanup on `vnx stop` or `vnx start` (restart).

**Fingerprints killed**: the script names in the `for fp in ...` loop of
`vnx_kill_all_orchestration()` in `bin/vnx`. It is a superset of the supervised daemons:
it also covers scripts that earlier versions started, so a leftover process from an
older install is stopped too.

Also cleans orphan `fswatch` processes watching `.vnx-data/`.

### Health Monitoring

**Supervisor Checks**:
- Interval: 10 seconds
- Action: Auto-restart on failure
- Logging: `logs/supervisor.log`
- Alerts: Process restart notifications

**Dashboard Updates**:
- Interval: 2 seconds
- Metrics: Process health, queue depth, terminal status
- Output: `state/dashboard_status.json`

### Terminal State Initialization

On `vnx start`, the system:
1. Writes initial `terminal_state.json` with all terminals as `idle`
2. Cleans tmux global environment (removes stale VNX vars from previous projects)
3. Sets session-level tmux env vars (3-layer tmux isolation)
4. Per-pane shell cleanup: unsets + re-exports correct VNX vars before launching CLI

---

## Intelligence Systems

Full technical reference (schema, tuning, testing): `docs/core/technical/INTELLIGENCE_SYSTEM.md`.
This section stays short to avoid drifting out of sync with that doc; it lists
what ships and where the evidence for "it actually runs" lives.

- **Pattern Matching Engine** — `quality_intelligence.db` (SQLite, FTS5),
  `pattern_usage` + `tag_combinations` tables, offer/adoption tracking appended
  to `state/intelligence_usage.ndjson` (G-L7 audit). Written by
  `scripts/gather_intelligence.py` (`record_pattern_offer`,
  `record_adoption_from_receipt`). Verify liveness directly rather than trust a
  checkmark here: `sqlite3 "$VNX_STATE_DIR/quality_intelligence.db" "SELECT COUNT(*) FROM pattern_usage; SELECT COUNT(*) FROM tag_combinations;"`.
- **Intelligence Injection** — happens at dispatch assembly in the lane, not in a hook:
  `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` sections 4 and 7. Reviewer roles
  are not enriched. `userpromptsubmit_worker_intelligence_inject.sh` exists on disk but no
  hook references it (see below).
- **Quality Digest** (`build_t0_quality_digest.py`) — 3-section append-only
  NDJSON to `state/` (G-L6): Operational Defects, Prompt/Config Tuning,
  Governance Health, each recommendation carrying `evidence_ids` (G-L2).
- **Nightly Intelligence Pipeline** (`nightly_intelligence_pipeline.sh`, cron at 04:00
  through `scripts/install_nightly_crons.sh`) — phases run in dependency order: schema
  init, code-quality scan and snippet/doc extraction, conversation analysis,
  session-dispatch linking, event analysis and pattern extraction, the learning cycle
  (`learning_loop.py run`), memory and dream consolidation, weekly digest, stale-edit
  scan, session brief, governance aggregation, suggested edits, the quality digest
  (`build_t0_quality_digest.py`) and `generate_t0_recommendations.py`. A failing phase is
  logged and does not block the next. The authoritative order is the `run_phase` list in
  the script.
- **T0 Intelligence Aggregator** — receipt correlation, warning detection, terminal
  insights and tag-based report lookup; rolling window archived after 1000 events.
- **State Manager Integration** — `state/unified_state.ndjson`, 5-second
  cycle in `unified_state_manager.py` over dispatches + receipts + terminal status, feeding the
  aggregator above.
- **Governance Measurement System** — SPC-based quality scoring
  (`scripts/lib/cqs_calculator.py` per-dispatch CQS; `scripts/governance_aggregator.py`
  nightly FPY/rework/SPC), tables `governance_metrics` / `spc_control_limits` /
  `spc_alerts` in `quality_intelligence.db` (schema: `schemas/quality_intelligence.sql`).
  Verify liveness rather than trust a date stamp:
  `sqlite3 "$VNX_STATE_DIR/quality_intelligence.db" "SELECT COUNT(*), MAX(computed_at) FROM governance_metrics;"`.
  First-Pass Yield and rework rate as a published percentage are not
  measurement-ready yet — see the technical reference before citing either as a fact.
- **Deterministic Gates** — three-tier verification: contract blocks (machine-checkable
  success criteria in the dispatch) → lightweight verification (`verify_claims.py`,
  post-receipt) → pre-merge gate (`vnx gate-check --pr <PR-ID>`, pytest/AST/artifact/shell-syntax).
  Results in `.vnx-data/state/gate_results/<PR-ID>.json`, per-check GO/HOLD.

---

## File System Layout

```
project-root/                        # this repo's own layout — no .claude/vnx-system/ prefix
├── bin/vnx                          # operator + automation CLI entry point
├── scripts/                         # Active orchestration scripts
│   ├── lib/dispatch_cli.py          # the single-entry dispatch door
│   ├── dispatcher_minimal.sh        # legacy PR-queue-path dispatcher (see Core Components §1)
│   ├── receipt_processor.sh         # Receipt processing, evidence-only
│   ├── report_parser.py
│   ├── append_receipt.py            # Receipt + quality sidecar writer
│   ├── generate_t0_recommendations.py
│   ├── vnx_supervisor_simple.sh
│   ├── pr_queue_manager.py          # PR queue + staging workflow
│   ├── gather_intelligence.py       # Intelligence aggregation
│   ├── learning_loop.py             # Adoption signals, pending_rules queue
│   ├── tag_intelligence.py          # Pairwise/triple tag subsets
│   ├── build_t0_quality_digest.py   # 3-section NDJSON digest
│   ├── check_intelligence_health.py # Intelligence health check
│   ├── gate_runner.py               # Deterministic gate execution
│   ├── review_gate_manager.py       # Review-gate policy execution
│   ├── commands/                    # Extracted CLI command files
│   │   ├── jump.sh                  # vnx jump <terminal> | --attention
│   │   ├── start.sh
│   │   ├── stop.sh
│   │   ├── doctor.sh
│   │   ├── new_worktree.sh
│   │   ├── merge_preflight.sh
│   │   ├── finish_worktree.sh
│   │   ├── recover.sh
│   │   └── headless.sh
│   └── lib/                         # Shared libraries
│       ├── vnx_paths.sh             # Path resolver (cross-project guard)
│       ├── process_lifecycle.sh     # PID-safe process control
│       ├── runtime_core.py          # Runtime state machine core
│       ├── dispatch_router.py       # Dispatch routing logic
│       ├── subprocess_adapter.py    # Headless subprocess delivery
│       └── subprocess_dispatch.py   # Subprocess dispatch orchestration
│
├── skills/                          # Canonical native skills
│   ├── skills.yaml                  # Skill registry
│   └── {skill-name}/SKILL.md        # Per-skill docs + references
│                                     # (synced into .claude/skills/, ~/.codex/skills/, .gemini/skills/ by `vnx init`)
│
├── templates/terminals/             # T0-T3 agent templates
├── schemas/                         # Quality intelligence SQL schema
├── docs/                            # This documentation tree
│
├── .vnx-data/                       # Runtime data (gitignored)
│   ├── state/                       # State files
│   │   ├── t0_receipts.ndjson       # Production receipts
│   │   ├── t0_brief.json            # T0 decision snapshot
│   │   ├── terminal_state.json      # Terminal status + attention model
│   │   ├── pr_queue_state.yaml      # PR queue tracking
│   │   ├── quality_intelligence.db  # Quality patterns DB
│   │   ├── intelligence_usage.ndjson # Append-only pattern offer/adoption audit (G-L7)
│   │   ├── t0_quality_digest.ndjson # 3-section quality digest, append-only (G-L6)
│   │   ├── t0_recommendations.json  # Structured recommendations (max 5 pending — G-L8)
│   │   ├── pending_rules.json       # Pending constraint updates awaiting approval (G-L1)
│   │   ├── intelligence_health.json # Intelligence pipeline health check
│   │   ├── open_items.json          # Open items registry
│   │   └── dashboard_status.json    # Real-time metrics
│   │
│   ├── dispatches/                  # Task dispatches
│   │   ├── pending/                 # Door bundles (<id>/dispatch-spec.json) and promoted *.md dispatches
│   │   ├── staging/                 # PR-queue proposals, written by pr_queue_manager.py init-feature
│   │   ├── queue/                   # Promoted PR-queue dispatches awaiting the popup watcher
│   │   ├── active/                  # In progress
│   │   └── completed/              # Finished
│   │
│   ├── unified_reports/             # Markdown reports
│   ├── logs/                        # System logs
│   ├── pids/                        # Process PID files
│   └── locks/                       # Singleton locks
│
├── dashboard/                       # Operator dashboard (git-tracked)
│   ├── index.html                   # Vanilla HTML/JS UI (no build toolchain)
│   └── serve_dashboard.py           # Python stdlib HTTP server (port 4173)
│
├── .vnx/                            # VNX config (gitignored)
│   └── config.yml                   # Project-level VNX config
│
└── .claude/terminals/               # Terminal workspaces
    ├── T0/CLAUDE.md
    ├── T1/CLAUDE.md
    ├── T2/CLAUDE.md
    └── T3/CLAUDE.md
```

---

## Current System Status

### Supervised Components (generated — do not hand-edit; regenerate with `python3 scripts/generate_architecture_doc.py --write`)

Every row below is a daemon `vnx_supervisor_simple.sh`'s `start_all()` actually
starts, read live via `scripts/lib/daemon_register.py` (shared with
`docs/core/DAEMON_LIVENESS.md` — one register, not two). A daemon renamed or
removed in `start_all()` fails this file's generation instead of silently
going stale here.

Running/absent state is deliberately **not** stored in this file — it
changes by the minute, and a committed "Active" checkmark is exactly the
claim that drifted (measured 2026-08-30: none of the supervised processes was
running while every item of the old checklist still said "Active"). Check current
liveness with `bash scripts/vnx_supervisor_simple.sh status` or
`python3 -c "import sys; sys.path.insert(0,'scripts/lib'); import daemon_register as d; print(d.measure_daemon_liveness())"`.

<!-- BEGIN GENERATED: supervised-components -->
- Dispatcher (native skills, multi-provider dispatch). — `dispatcher_minimal.sh` — `scripts/vnx_supervisor_simple.sh:198`
- Smart Tap (JSON/Markdown auto-translation). — `smart_tap_json_translator.sh` — `scripts/vnx_supervisor_simple.sh:204`
- Receipt Processor (report -> receipt -> T0 delivery, adoption tracking). — `receipt_processor.sh` — `scripts/vnx_supervisor_simple.sh:205`
- Heartbeat ACK Monitor (ACK processing + timeout tracking). — `heartbeat_ack_monitor.py` — `scripts/vnx_supervisor_simple.sh:210`
- Queue Watcher (dispatch review popup; falls back to auto-accept when VNX_QUEUE_POPUP_ENABLED=0). — `queue_popup_watcher.sh` or `queue_auto_accept.sh` (conditional) — `scripts/vnx_supervisor_simple.sh:212`
- Dashboard Generator (real-time metrics -> dashboard_status.json). — `generate_valid_dashboard.sh` — `scripts/vnx_supervisor_simple.sh:217`
- Unified State Manager (state consolidation, 5s cycle). — `unified_state_manager.py` — `scripts/vnx_supervisor_simple.sh:218`
- Intelligence Daemon (real-time intelligence updates). — `intelligence_daemon.py` — `scripts/vnx_supervisor_simple.sh:219`
- Recommendations Engine (T0 dispatch suggestions, max 5 pending). — `recommendations_engine_daemon.sh` — `scripts/vnx_supervisor_simple.sh:220`
<!-- END GENERATED: supervised-components -->

### Hooks Wired in `.claude/settings.json` (generated — do not hand-edit; regenerate with `python3 scripts/generate_architecture_doc.py --write`)

<!-- BEGIN GENERATED: hooks -->
- **PreToolUse**: `pretooluse_block_raw_claude_spawn.sh`, `pretooluse_block_subagent.sh`, `t0_context_guard.py`
- **SessionEnd**: `session_reconcile_cleanup.sh`, `build_current_state.py`, `build_doc_indexes.py`
- **SessionStart**: `session_reconcile_autoclose.sh`, `build_t0_state_hook.sh`, `tmux_signal_session_ready.sh`, `path_parity_check.sh`, `monitor_tripwire.sh`, `sessionstart.sh`, `hookpin_check.sh`
- **Stop**: `stop_report_hook.sh`, `tmux_signal_stop_receipt.sh`, `session_stop_rotation.py`, `t0_context_guard.py`
- **UserPromptSubmit**: `tmux_signal_prompt_received.sh`, `t0_context_guard.py`
<!-- END GENERATED: hooks -->

`scripts/userpromptsubmit_worker_intelligence_inject.sh` is intentionally
absent above: it exists on disk but no hook event in `.claude/settings.json`
references it (verified 2026-08-30 — `grep -c
userpromptsubmit_worker_intelligence_inject .claude/settings.json` is 0; its
only other reference in the tree is its own test,
`tests/test_learning_feature.py`). It is dead code or a pending wiring
change, not an active component — the previous "Worker Intelligence
Injection" checklist entry was wrong.

### Not Continuously Supervised

These are real components but are neither `start_all()` daemons nor
`.claude/settings.json` hooks, so the two generated sections above correctly
omit them. Listed by hand — not generated — so they don't silently
disappear from this document:

- **VNX Supervisor** — the process that runs `start_all()` itself
  (`scripts/vnx_supervisor_simple.sh`); tracked by its own PID file
  (`$VNX_PIDS_DIR/vnx_supervisor.pid`), not a `start_all()` entry.
- **Quality Advisory Pipeline** — `scripts/lib/quality_advisory.py`,
  imported and invoked inline by `scripts/append_receipt.py` on every
  receipt write. A feature of Receipt Processor, not an independent process.
- **PR Queue Manager** — `scripts/pr_queue_manager.py`, invoked on demand
  via `bin/vnx`; not a persistent daemon.
- **Operator Dashboard** — `dashboard/serve_dashboard.py`, launched via
  `dashboard/launch-dashboard.sh`; not part of `start_all()` or `start.sh`.
- **Nightly Intelligence Pipeline** — `scripts/nightly_intelligence_pipeline.sh`,
  cron-scheduled (`scripts/install_nightly_crons.sh:28`, `0 4 * * *`), not
  supervisor-managed.

### Deprecated Components (not started by supervisor)
- ACK Dispatcher V2 — replaced by `heartbeat_ack_monitor.py`; the old script is
  not tracked in the repo, superseded rather than archived
- Report Watcher (`report_watcher.sh`) — replaced by Receipt Processor; the
  file is still tracked but production receipt ingestion runs through
  `receipt_processor.sh`
- Receipt Notifier — replaced by Receipt Processor, which handles parsing,
  appending, and delivery in one process; the old script is not tracked in
  the repo, superseded rather than archived
- Dispatcher V7 — reference only (see `docs/_archive/core/technical/DISPATCHER_SYSTEM.md`, archived)

### Terminal / Role Status
- **T0**: Opus 5.5, persistent orchestrator, read-only
- **T1 / T2 / T3**: role labels on ephemeral headless build-worker dispatches,
  Sonnet by default (see *Terminal Architecture* above)

---

## Open Items System

### Purpose
Provides T0 with deterministic, token-light tracking of blockers, warnings, and deferred work across all dispatches and PRs.

### Components
- **State Files**:
  - `state/open_items.json` - Source of truth
  - `state/open_items_digest.json` - Pre-computed summary
  - `state/open_items.md` - Human-readable view
  - `state/open_items_audit.jsonl` - Audit log

### Governance Model
- **T0 is sole authority** for declaring work done (closing open items, completing PRs)
- **Workers** attach evidence by including `PR-ID` in their reports
- **Receipt processor** attaches evidence to open items but does NOT close them or complete PRs
- **Severity classification**: blocker (must close before PR complete), warn (should close), info (nice to have)

### Integration Points
1. **T0 Brief**: Includes `open_items_summary` with counts and top blockers
2. **Recommendations Engine**: Adds `BLOCKER_OPEN_ITEM` and `OPEN_ITEMS_SUMMARY` types
3. **Unified Reports**: Workers add unfinished items in `## Open Items` section
4. **PR Workflow**: T0 must resolve all blockers before completing PRs
5. **Evidence Pipeline**: Receipt processor attaches evidence; T0 reviews and closes

### Decision Flow
```
[Before PR Promotion]
    ↓
Check open items digest
    ↓
[Blockers exist?]
    ├─ YES → Resolve (close/defer/wontfix)
    └─ NO → Can promote PR
```

---

## Future-State Reconciliation: Open-Item → Track → Dispatch

The future state (the track layer + roadmap autopilot) only earns trust if it
mirrors reality without a human re-stating it. The future-state
reconciliation batch (PRD kept in the local, gitignored `claudedocs/` scratch
space -- not part of the shipped repo, so it has no citable in-tree path)
makes that linkage automatic and tenant-safe. Three pieces: a **lifecycle**
(open-item → track → dispatch), a **loop** (the autopilot tick), and a
**multi-tenancy model** (ADR-007 composite keys).

### The lifecycle

Open items are the source of blockers and follow-up work (see *Open Items
System* above). Tracks are the planning unit for forward-state features.
Dispatches are the executable work. The reconciliation keeps the three in sync:

```
open_items.json  ──(bridge)──▶  track_open_items  ──(reconciler)──▶  tracks.derived_status
   (source of truth)              (per-track links)                   (computed state)
        ▲                                                                    │
        │                          dispatches (terminal? PR merged?) ────────┘
```

1. **Bridge** (`scripts/import_open_items_to_tracks.py`, PR-C #862). Reads the
   on-disk open-items store, resolves each item's current target track, and
   keeps `track_open_items` in sync. It is a **thin orchestrator over the
   single-writer primitives** `tracks.link_open_item` / `tracks.unlink_open_item`
   (decision D1): it owns no `track_open_items` SQL of its own. The whole run is
   one `BEGIN IMMEDIATE` transaction — the read-then-write window is serialized
   (TOCTOU closed) and the mutations are atomic (a failure anywhere rolls the run
   back). It fails loud on an absent/unreadable/wrong-shape source, requires the
   migration 0030 resolution schema (`resolved_at` / `resolution_reason`) and
   fails closed on a pre-0030 DB, and is idempotent. All access is
   `(track_id, project_id)`-scoped (ADR-007).

2. **Reconciler** (`scripts/lib/track_reconciler.py`). After the bridge syncs
   the links, the reconciler recomputes each track's `derived_status` from the
   links, the dependency graph, and the track's dispatches/PR — never an LLM.

3. **Event semantics (D3, honest).** Each `track_open_items` mutation has a
   matching ADR-005 NDJSON ledger event, but under the implemented D3 posture
   those events are emitted **after** the DB commit. The DB is authoritative;
   events are **at-most-once, never orphaned**; a post-commit emit failure is
   logged loudly, is non-fatal (the reconciler re-derives status from the rows),
   and surfaces as CLI exit 4. Exactly-once via a transactional outbox is
   deferred (#867). See ADR-005.

### The derived_status rule (precise)

`track_reconciler._compute_derived_status` evaluates the conditions in order and
returns the first that applies. A track is **`done` only when all of these hold**:

- it has **zero unresolved blocking open-items** — no `track_open_items` row with
  `link_type = 'blocks'` and `resolved_at IS NULL`; and
- **every blocking dependency track is `done`** (each `hard` `track_dependencies`
  edge points at a track whose phase is `done`; `soft` and `overlap` edges are
  advice and never block, and an unknown kind counts as `hard`, fail-closed and
  logged as a warning); and
- **all of its dispatches are in terminal states** —
  `{completed, expired, dead_letter}`; and
- **if it has a linked PR, that PR is confirmed merged** — via a `pr_merged`
  coordination event on one of the track's dispatches. A track with **no
  dispatches** is `done` only when its `pr_ref` is in the merged set (historical
  tracks); a track with no PR and all dispatches terminal is `done` outright.

Otherwise the status is one of:

- **`blocked`** — an unresolved blocking open-item exists, or a dependency track
  is not `done`;
- **`in_progress`** — a dispatch is still in flight, or all dispatches are
  terminal but a linked PR is not yet confirmed merged;
- **`queued`** — only planned/proposed dispatches (or none) and no merged PR.

This rule is deterministic. It is the truth the optional PM-gate automation
(#873) sits on top of: deterministic closes auto-apply, judgment cases
escalate to a human gate.

### The autopilot loop (the tick)

`RoadmapManager.autopilot_tick()` (`scripts/roadmap_manager.py`, PR-D #871) runs
the lifecycle on every tick, **under the `VNX_ROADMAP_AUTOPILOT=1` gate**:

```
autopilot_tick()                       [gate: VNX_ROADMAP_AUTOPILOT=1]
  ├─ track sync:
  │    ├─ bridge: import_open_items_to_tracks()   # sync track_open_items
  │    └─ reconcile_tracks()                       # synchronous; recompute derived_status
  │         └─ status != ok ?  → return {status: "degraded",
  │                                       reason: "track_sync_failed"}   ◀── STOP. No advance.
  └─ (sync ok) → dispatch the next feature step / advance the roadmap
```

The bridge runs immediately **before** the synchronous reconcile; reconcile
failure is surfaced in the tick result. The downstream advance is **gated on a
clean sync**: if the track sync fails, the tick returns `degraded` and refuses to
dispatch a feature step or advance on stale state. The reconcile pass emits its
governance receipt, so the gated path is auditable in the ledger.

### Multi-tenancy: ADR-007 composite keys

All of the above is tenant-scoped. The `dispatches` table was brought into the
ADR-007 pattern (PR-A1 #859): a schema-preserving, in-place,
crash-safe 12-step rebuild swaps any uniqueness keyed solely on `dispatch_id`
for a composite `UNIQUE(dispatch_id, project_id)`, and removes every single-key
variant (inline, table-level, standalone, partial, and `lower(dispatch_id)`
expression indexes). Tenant `project_id` is resolved **fail-closed** from a
precedence chain (resolved DB path → `.vnx-project-id` marker → `VNX_PROJECT_ID`);
conflicting or unknown sources abort, existing NULL/empty/conflicting values
abort before any mutation, and there is **never a silent `vnx-dev` default**.
`build_t0_state` reads canonical tracks and `track_open_items` only with a
`WHERE project_id = ?` predicate, degrading to an explicit `tenant_unavailable`
flag (empty rows) rather than leaking cross-tenant data (PR-B #863). See
ADR-007 and `docs/MIGRATION_GUIDE.md` for the operator runbook.

### Open PRs in the T0 state

`build_t0_state` carries a `pr_queue` section (schema pr_queue/1.1) with one row per open PR: `head_sha`, `mergeable`, `merge_state` and a `ci` object. CI on the head is judged by the merge door's own judge (`check_ci_run_for_head`), not by a rule of the builder. A failed `gh` read gives `available: false` with a reason, and `t0_index.json` (schema t0_index/1.3) then shows `queue.open_prs: null` with `pr_queue_unavailable` instead of `0`. The row keys, the `ci.state` values, the measure cap of 8 and the 12-second deadline are in `docs/core/STATE_FABRIC.md`, "Current: `pr_queue`, the open PRs".

---

## Staging Workflow

Two staging paths exist in the tree. The single-entry door is the canonical
one. The PR-queue path is older and still live: the roadmap autopilot drives
it and the daemon dispatcher still consumes what it produces.

### The door: a staged bundle in the central pending dir

`vnx dispatch <dispatch-id>` is the one entry that decides the lane. It reads a
staged bundle from the central data dir, not from the repo:

```
<data_dir>/dispatches/pending/<dispatch-id>/
├── instruction.md
└── dispatch-spec.json
```

The staged bundle is the staging gate (ADR-006). `vnx dispatch stage ...`
writes it and never fires. `vnx dispatch <dispatch-id> --dry-run` prints the
compiled plan and permit and spawns nothing. `vnx dispatch <dispatch-id>`
fires. The door then picks the lane: `claude_headless` for claude,
`provider_dispatch.py` for kimi, glm and deepseek, `subprocess_dispatch.py` for
a terminal-pinned single-worker PR. No popup sits in this path.

The ruleset lives in one place, `docs/core/DISPATCH_RULES.md`: lane selection
in §5, provider routing in §8, the staging flow in §12. This document does not
repeat them. Decision records:
`docs/governance/decisions/ADR-024-single-entry-door-default-dispatch-lane.md`
(the door as default lane) and
`docs/governance/decisions/ADR-025-raw-file-dispatch-deprecation.md` (the
markdown path below).

### The PR-queue path: FEATURE_PLAN.md to markdown dispatches

`scripts/pr_queue_manager.py` turns a `FEATURE_PLAN.md` into markdown
dispatches under `dispatches/`. Its verbs are `init-feature`, `staging-list`,
`show`, `patch`, `promote` and `reject`.

```
FEATURE_PLAN.md → init-feature → staging/ → promote → queue/ → pending/ → dispatcher_minimal.sh
                  (all PRs)      (proposals) (1 PR)    (popup)  (*.md)
```

- `init-feature` writes one file per PR into `staging/`. Nothing in `staging/`
  is delivered.
- `promote <dispatch-id>` refuses while the PR's dependencies are incomplete.
  `--force` skips that check.
- A promoted file lands in `queue/` for the popup watcher, where accepting it
  moves it to `pending/`. With `VNX_QUEUE_POPUP_ENABLED=0` it goes straight to
  `pending/`, and `queue_auto_accept.sh` does the same for files already in
  `queue/`.
- `dispatcher_minimal.sh` scans `pending/*.md`. That markdown path does not go
  through the door. ADR-025 deprecates its raw-file form and names the daemon
  path as a structural-consolidation follow-up.
- The roadmap autopilot (`RoadmapManager.run_feature_step` in
  `scripts/roadmap_manager.py`, ticked under `VNX_ROADMAP_AUTOPILOT=1`) calls
  `create_dispatch_from_pr` and then `promote_dispatch` for the next
  dependency-ready PR. This path is not T0-only.

**CLI Commands**:
```bash
# Generate all PR dispatches to staging/
python scripts/pr_queue_manager.py init-feature FEATURE_PLAN.md

# Review staging with dependency status
python scripts/pr_queue_manager.py staging-list

# Promote one PR (to queue/, or to pending/ when the popup is disabled)
python scripts/pr_queue_manager.py promote <dispatch-id>
```

**State Management**: `pr_queue_state.yaml` under the project's resolved
`VNX_STATE_DIR` (`PRQueueManager.vnx_state_dir` in `scripts/pr_queue_manager.py`)
-- the per-project central store (ADR-026), not a fixed repo-relative path.
- Tracks completed PRs, in-progress PR, execution order
- Dependency validation during promotion
- Evidence attachment via receipt processor (T0 reviews and completes PRs)

### Staging Notification

The recommendations engine (`scripts/generate_t0_recommendations.py`,
`check_staging_dispatches`) emits a `STAGING_READY` recommendation for each new
file in `staging/`, with the `show`, `patch`, `promote` and `reject` commands
attached. `state/staging_seen.json` records which file versions it already
announced, so an unchanged file raises one recommendation.

### T0 Decision Tree
```
STAGING_READY recommendation (new file in staging/)
    ↓
[Review dispatch?]
    ├─ YES → `pr_queue_manager.py show <id>`
    │    ↓
    │ [Needs changes?]
    │    ├─ YES → `pr_queue_manager.py patch <id> --set key=value`
    │    └─ NO → Continue
    │    ↓
    │ [Check dependencies?]
    │    ↓
    │ `pr_queue_manager.py staging-list` (shows ready vs waiting)
    │    ↓
    │ [Approve?]
    │    ├─ YES → `pr_queue_manager.py promote <id>` → queue/ (popup) or pending/ (popup disabled)
    │    └─ NO → `pr_queue_manager.py reject <id> --reason "X"`
    │
    └─ NO → Ignore (stays in staging)
```

**Reference**: `docs/core/DISPATCH_RULES.md` is the enforced dispatch ruleset.

---

## Review Seats

A review seat is an entry of the project's review stack (`VNX_DEFAULT_REVIEW_STACK`,
default `codex_gate,kimi_gate`) that names a review peer and is not optional
(`scripts/lib/review_seats.py`). `ci_gate` checks CI, `wiring_gate` is not a review
peer and `claude_github_optional` is optional by name, so none of the three is a seat.
An unknown or retired gate name in a stack is refused by name, never dropped quietly.

Each seat gets its own reviewer: one reviewer gate fills at most one seat of a request.
When a seat's reviewer is out, the next gate in the takeover chain reads it
(`docs/core/PROVIDER_LANES.md`, "Takeover chain"). With the default chain and both
subscription reviewers out, deepseek reads one seat and the other seat ends as
`chain_exhausted`.

The merge side asks one module which record fills which seat on a PR head:
`review_seat_evaluation.evaluate_review_seats`. The merge door (`scripts/pr_merge.py`),
the `vnx-gate/review` summary check, `vnx pr-ready`, track closure and
`t0_gate_enforcement` all read it, so no evaluator derives a seat of its own. A seat is
filled by a completed record on this exact head with zero blocking findings and complete
evidence. Blocking always blocks: a FAIL verdict or a blocking finding from any review
peer on the head, in its JSON or in its report on disk, refuses the merge whatever the
other seats say. A missing or failed reviewer is absence, not a vote (ADR-037): the seat
stays unfilled and neither signs nor rejects.

Reviewer prompts carry no repo map, skill or intelligence
(`DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md`, section 4). The merge-door checks are in
`DISPATCH_RULES.md` sections 2.1 and 2.2, the evidence rules in
`docs/core/45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md`, the reviewer split in ADR-008. The
seat rule and the per-seat reviewer are on main, not yet released.

---

## Multi-Provider Dispatch

### Dispatch lanes and provider routing

VNX drives every provider as a CLI subprocess and never imports a vendor SDK
(the `no-anthropic-sdk` constraint enforces this in CI). The hard guard-rails on
which provider may serve which lane live in one machine-readable SSOT:
`scripts/lib/providers/provider_constraints.yaml`.

Claude runs on one lane, with a terminal-pinned variant:

- **claude-headless** (`dispatch_envelope.run_envelope_headless_plan`) — the only
  Claude worker lane since the tmux-spawn lane was removed on 2026-09-18.
  `claude -p` in an isolated worktree, on the subscription.
- **claude-subprocess** (`scripts/lib/subprocess_dispatch.py`) — the
  terminal-pinned `claude -p` lane, opt-in per terminal
  (`VNX_ADAPTER_T{n}=subprocess`).

Non-Claude providers (codex, gemini, kimi, deepseek-harness, litellm sub-providers,
local-gemma) route through `scripts/lib/provider_dispatch.py`. Kimi is CLI-OAuth
only (`kimi-via-cli-only`); GLM (glm-5.2 default, plus glm-5.3/glm-5.3-flash — see
`deprecated-glm-models` in `provider_constraints.yaml`) routes via OpenRouter (`zai-via-openrouter-only`);
DeepSeek runs through the Claude harness with an own key (`deepseek-harness-subscription-blocked`
gates the subscription variant). The operator turns a provider family on or off with
`vnx providers` (on main, not yet released). Full lane map, takeover chain and switch:
`docs/core/PROVIDER_LANES.md`; dispatch decision rules: `docs/core/DISPATCH_RULES.md`.

**Single-entry door (merged, default-ON).** Lane selection runs behind
one entry point (`scripts/lib/dispatch_cli.py`): validate → snapshot →
compile_plan → permit → execute. It is the default lane as of 2026-06-24
(ADR-024; `VNX_SINGLE_ENTRY_DISPATCH` resolves on via
`scripts/lib/dispatch_flags.py` `_DEFAULT_ENABLED = True`; `VNX_DISPATCH_LEGACY=1`
is the absolute per-terminal rollback). The door normalizes GLM to the harness
lane, applies the single-source routing predicate, and runs a phantom-guard that
rejects evidence-free GATE-GREEN receipts — all on `main`.

### Provider lane map

The per-lane table (auth, transport, primary use, maturity) lives in one place
now: `docs/core/PROVIDER_LANES.md`. It replaces a capability matrix that used
to live here and drifted (naming Gemini a review-capable "Experimental"
provider after it was retired as a reviewer, and omitting kimi/glm-harness/
deepseek-harness entirely).

### Skill Sync

During `vnx init`, skills are synced to all provider directories:
- `~/.claude/skills/` — Claude Code (user-level)
- `.claude/skills/` — Claude Code (project-level)
- `~/.codex/skills/` — Codex CLI
- `.gemini/skills/` — Gemini CLI

Each skill has a `SKILL.md` with YAML frontmatter (required for Codex CLI discovery) and a `references/` directory mapping to project files.

### Tmux Environment Isolation (3-Layer Fix)

**Problem**: tmux server global environment carries stale VNX variables from previously launched projects.

**Solution** (3 layers):
1. **Session-level tmux env**: `set-environment -t` overrides global env
2. **Per-pane shell cleanup**: unset all 11 VNX vars + re-export correct values before launching CLI
3. **Popup queue cleanup**: expanded from 5 to 11 vars with re-export

**Cross-project contamination guard** (`vnx_paths.sh`):
- Detects when `PROJECT_ROOT` doesn't match the script's location
- Unsets `VNX_DATA_DIR`, `VNX_STATE_DIR`, `VNX_DISPATCH_DIR` to prevent data writes to wrong project

### Path Resolution

`scripts/lib/vnx_paths.sh` provides dynamic path resolution:
- `_resolve_node_path()` -- finds node via VNX_NODE_PATH > nvm > system PATH
- `_resolve_venv_path()` -- finds Python venv in project or main worktree
- `_resolve_project_root()` -- git-based resolution with worktree awareness
- Cross-project contamination guard: validates inherited VNX_HOME matches computed default

---

## Unified Dashboard

### Architecture

The VNX operator dashboard is a projection over `.vnx-data/state/`.

**Stack**:
- Server: `dashboard/serve_dashboard.py`, a Python stdlib HTTP server with API modules
  under `dashboard/api_*.py`. It reads `PORT` from the environment and falls back to
  4173. `dashboard/launch-dashboard.sh` sets `PORT` to 4174 by default, because the
  Next.js proxy targets that port.
- Frontend: `dashboard/index.html` (vanilla HTML/JS, no build step) and the token-usage
  app under `dashboard/token-dashboard/` (Next.js, its own `npm` build; see its README).
- State source: `.vnx-data/state/` files, polled from the page (a 7-second refresh in
  `index.html`).

### Write actions

Reads are `GET`. The mutating `POST` routes are guarded against cross-site requests
(a token is required on a non-loopback bind) and are limited to:

| Route | Effect |
|-------|--------|
| `/api/jump/{terminal}` | tmux focus switch (`vnx jump`) |
| `/api/restart-process`, `/api/unlock-terminal` | restart a supervised process, clear a terminal lock |
| `/api/conversations/resume` | validate a session and return its resume command |
| `/api/operator/*` actions (session start/stop, terminal attach, projections refresh, reconcile, open-item inspect, gate toggle, config set) | operator controls |
| `/api/intelligence/proposals/*` | apply or accept an intelligence proposal |

No route creates or promotes a dispatch. The route table is the `do_GET`/`do_POST`
handlers in `dashboard/serve_dashboard.py`; read-only groups are `/api/health`,
`/api/events`, `/api/token-stats`, `/api/conversations`, `/api/dispatches` and
`/api/operator/*`.

### Startup

```bash
# Start the dashboard server (PORT overrides the default)
bash dashboard/launch-dashboard.sh
```

---

## Demo & Distribution

### VNX CLI (`bin/vnx`)

**Commands**:
```bash
vnx init              # Initialize VNX in a project (terminals, skills, hooks, quality DB)
vnx start             # Launch tmux session with all terminals
vnx stop              # Stop all orchestration processes
vnx doctor            # Health check (tools, dirs, templates, path hygiene)
vnx update            # Pull latest VNX from GitHub remote (.vnx-origin)
vnx cost-report       # Token usage and cost metrics
vnx jump <terminal>   # Switch tmux focus to terminal (or --attention for highest-priority)
vnx analyze-sessions  # Populate session analytics from Claude Code JSONL logs
vnx analyze-sessions --dry-run  # Diagnose session discovery without writing
```

### Command Loader Architecture

`bin/vnx` acts as a thin dispatcher. New commands are loaded from `scripts/commands/<name>.sh` via the `_load_command()` function, which sources the file and calls `cmd_<name>()`. This keeps the main script stable while allowing commands to be added independently.

Extracted commands: `start`, `stop`, `doctor`, `regen-settings`, `new-worktree`, `merge-preflight`, `finish-worktree`, `recover`, `registry`, `status`, `ps`, `cleanup`, `restart`, `jump`.

### Project Configuration

### Settings Patch Management

`settings.json` is patch-managed, not wholly VNX-owned:
- **VNX owns**: `hooks`, `env.VNX_*`, baseline `permissions.allow/deny`
- **Project owns**: extra `env` keys, `permissions.ask`, `additionalDirectories`
- **Merge semantics**: `allow/deny` use union with deny-over-allow precedence

Commands: `vnx regen-settings --merge` (update VNX keys) | `--full` (first-time init) | `--validate` (check structure).

**`config.env`** (project-level, sourced by `vnx start`):
```bash
VNX_PROVIDER=claude_code       # T0 provider (claude_code/codex_cli/gemini_cli)
VNX_MODEL=sonnet               # Default build-worker model — provider_constraints.yaml pins the actual floor/default
VNX_T1_PROVIDER=codex_cli      # T1 role can use a different provider
```

**`config.yml`** (`.vnx/config.yml`, gitignored): written by `vnx init`, real keys only:
```yaml
project_root: "/path/to/project"
project_id: "my-project"
vnx_data_dir: "/path/to/resolved/data/root"
```
The installed VNX version is a separate one-line file, `.vnx-version`, not a
key in `config.yml` — `vnx init --set-version <ver>` is the only thing allowed
to rewrite it once it exists.

### Demo Setup (retired)

The "demo/" LeadFlow SaaS project generator (demo/setup_demo.sh, 923 lines,
plus demo/FEATURE_PLAN.md and the demo/dry-run* replay fixtures) was
deleted repo-wide in #193 (2026-04-08, "clean public docs structure and move
private docs out of repo"). No replacement exists. This section previously
described it as a current component; nothing since has regenerated it.

### Quality Advisory Pipeline

**On every completion**, `append_receipt.py` generates a quality sidecar:
```json
{
  "decision": "approve_with_followup",
  "risk_score": 0.35,
  "findings": [
    {"severity": "warn", "file": "lead_scoring_engine.py", "message": "File exceeds 500 lines (555)"}
  ]
}
```

**T0 receives** quality advisory signal with top-10 findings (severity, file, symbol, message).

**Thresholds** (Python files):
- Warning: 500 lines
- Blocker: 800 lines

---

**Document Status**: Active. The consolidation/self-learning loop ships but is opt-in and currently dormant: existing patterns inject into dispatch context, but the pool does not grow on its own yet.
**Last Major Update**: 2026-10-18 (checked against `main`: review seats, provider switch pointer, corrected attention model, supervisor interval, nightly pipeline, dashboard routes). Prior: 2026-09-27 (docs-refresh sweep: single-entry door as the primary dispatch path, ephemeral headless workers, receipt pull instead of tmux push, removed hardcoded version and token-reduction claims).
**Dashboard**: Vanilla HTML/JS + Python HTTP server, plus a Next.js token-usage app (guarded write actions, attention model)
**Governance Model**: Deliverable-based (T0 sole authority, evidence tracking, no auto-completion, G-L1–G-L8 enforced)
**Maintainer**: T-MANAGER (VNX Orchestration Expert)
