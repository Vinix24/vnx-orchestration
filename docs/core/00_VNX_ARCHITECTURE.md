# VNX Orchestration System - Complete Architecture

**Status**: Active
**Last Updated**: 2026-09-27
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
11. [Multi-Provider Dispatch](#multi-provider-dispatch)
12. [Unified Dashboard](#unified-dashboard)
13. [Demo & Distribution](#demo--distribution)

---

## System Overview

VNX is a file-based orchestration system enabling parallel development across multiple Claude Code terminals with centralized T0 orchestration brain.

### Core Principles
- **File-Based Communication**: NDJSON receipts + Markdown dispatches
- **Deliverable-Based Governance**: T0 is sole authority for declaring work done; workers attach evidence, receipt processor tracks but does not close
- **Native Skill Architecture**: skills invoke natively (`/skill-name` in Claude Code, `$skill-name` in Codex, `@skill-name` in Gemini) instead of a compiled prompt template
- **Multi-Provider Dispatch**: every provider runs as a CLI subprocess, never an imported SDK (`no-anthropic-sdk` in `scripts/lib/providers/provider_constraints.yaml`); full lane map in `docs/core/PROVIDER_LANES.md`
- **Project-Scoped Process Isolation**: `VNX_KILL_SCOPE` prevents cross-project process interference
- **Singleton Process Enforcement**: Bulletproof duplicate prevention
- **Progressive Intelligence**: Token-efficient context aggregation
- **Quality Advisory Pipeline**: Automatic file size/complexity warnings on every completion
- **Track-Agnostic Workers**: T1-T3 are role labels on ephemeral, headless build-worker dispatches, not persistent terminals; T0 dispatches the next role to the lane the door selects
- **Multi-Model Coordination**: T0 runs Opus 5.5 (`t0-opus-only`, a floor); T1-T3 build-workers default to Sonnet (`workers-kimi-pinned`, advisory — a dispatch spec's own model wins)
- **Git Worktree Isolation**: One worktree per feature plan; all agents share it, auto-commit per task, provenance in every receipt

### Worktree Model

VNX uses **one feature worktree per feature/fix** as the standard development model. Each worktree gets:
- Isolated `.vnx-data/` directory (not shared with main repo)
- Intelligence snapshot from main repo
- Full bootstrap: skills, terminals, hooks, settings

**Commands**:
- `vnx new-worktree <name>` -- creates git worktree + full bootstrap in one step
- `vnx merge-preflight <name>` -- governance GO/NO-GO verdict
- `vnx finish-worktree <name>` -- governance-gated closure with intelligence merge-back

> **Deprecated**: Per-terminal worktrees (`VNX_WORKTREES=true`) are deprecated since VNX V8.

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

**Multi-Signal Activity Detection**:
1. **Receipt-Based**: Last 5 receipts in `t0_receipts.ndjson` (primary)
2. **State-Based**: Between `task_ack` and `task_complete` receipts = working
3. **Log-Based**: Terminal log activity (future: post-completion conversations)

**Status Classifications**:
- `working`: Active task processing (receipt activity detected)
- `idle`: Available, no current tasks
- `offline`: Cannot determine status
- `missing`: Terminal pane not found

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
  "reason": "T2 context window at 87% capacity",
  "priority": "high",
  "action": "rotate_context"
}
```

**Attention triggers** (computed per terminal):
- `stale_working` — terminal marked working but no receipt update in >180s
- `context_pressure` — `context_usage_pct` > 80%
- `blocked` — terminal status is blocked/error/timeout

**`vnx jump` command** (`scripts/commands/jump.sh`):
```bash
vnx jump T2              # Switch tmux focus to T2
vnx jump --attention     # Focus the highest-priority attention terminal
```
The dashboard "Jump" button calls `POST /api/jump/{terminal}` which executes `vnx jump`.

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
- Hybrid dispatch: skill via `send-keys` (triggers slash-command detection) + instruction via `paste-buffer`
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
- Delivery to T0 defaults to pull, not push: `VNX_RECEIPT_T0_PUSH=0` is the
  default (ADR-035 §5.3), so T0 reads new receipts via `scripts/receipt_query.py pull`
  rather than having them pasted into its pane. Setting the flag restores the
  tmux buffer-paste push.
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

**Purpose**: Optional context-rotation automation for long-running sessions.

**Hooks actually wired** (see the generated *Hooks Wired* table further down,
which is the checkable source): `t0_context_guard.py` (PreToolUse/Stop/UserPromptSubmit),
`session_stop_rotation.py` (Stop). T0's own rotation flow — the mechanism
that actually ships — is `docs/operations/T0_CONTEXT_ROTATION.md`
(`scripts/hooks/t0_context_guard.py`, `scripts/t0_rotate_spawn.sh`). A separate
worker-side rotation design (`vnx_context_monitor.sh` / `vnx_handover_detector.sh`
/ `vnx_rotate.sh`) is documented in the archived
`docs/_archive/core/technical/CONTEXT_ROTATION_SYSTEM.md` — those hook scripts
are not present in the tree and are not wired into `.claude/settings.json`.

**Receipts**:
- `context_rotation` receipts are **informational only**. T0 does not need to act on these receipts unless paired with a human decision or explicit dispatch.

### 6. T0 Intelligence Aggregator (`t0_intelligence_aggregator.py`)

**Purpose**: Progressive context management for T0 orchestration

**Functionality**:
- Aggregates all system state into single NDJSON
- Progressive reading: 5 levels (1K → 20K+ tokens)
- Receipt correlation and warnings
- Terminal insights and patterns
- Tag-based report lookup

**Output**: `state/t0_intelligence.ndjson` (rolling window, last 1000 events)

### 7. VNX Supervisor (`vnx_supervisor_simple.sh`)

**Purpose**: Process health monitoring and auto-restart

**Functionality**:
- Monitors all core processes
- Auto-restart on failure
- PID tracking in `state/pids/`
- Health checks every 10 seconds

### 8. Dashboard Generator (`generate_valid_dashboard.sh`)

**Purpose**: Real-time system metrics visualization

**Functionality**:
- Updates every 2 seconds
- Terminal status aggregation
- Process health tracking
- Queue depth monitoring
- Performance metrics

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
│  5. Worker Receives Task                                        │
│     ├─► Loads the assembled prompt                               │
│     ├─► Sends ACK receipt (task_ack)                           │
│     └─► Begins execution                                        │
│                                                                  │
│  6. Heartbeat ACK Monitor Processes Acknowledgment              │
│     ├─► Detects task_ack receipt                               │
│     ├─► Updates dispatch status                                │
│     ├─► Starts timeout tracking                                │
│     └─► Updates terminal state                                  │
│                                                                  │
│  7. Worker Executes Task                                        │
│     ├─► Performs requested work                                │
│     ├─► Creates markdown report                                │
│     └─► Writes completion receipt (task_complete)              │
│                                                                  │
│  8. Receipt Processor Handles Report                            │
│     ├─► Detects new report in unified_reports/                 │
│     ├─► Parses structured data via report_parser.py            │
│     ├─► Attaches evidence to open items (does NOT close)       │
│     ├─► Appends to t0_receipts.ndjson                          │
│     └─► T0 pulls it (`receipt_query.py pull`, push is opt-in)   │
│                                                                  │
│  10. Intelligence Aggregator Updates Context                    │
│      ├─► Consolidates all system state                         │
│      ├─► Generates progressive context layers                  │
│      └─► Updates t0_intelligence.ndjson                         │
│                                                                  │
│  11. T0 Reviews Feedback                                        │
│      ├─► Reads progressive intelligence                        │
│      ├─► Assesses terminal status                              │
│      ├─► Makes routing decisions                               │
│      └─► Creates next manager block [Loop Continues]           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## File Formats

### 1. Dispatch Format (JSON/Markdown)

**JSON Dispatch** (`dispatches/queue/.json/{timestamp}-{track}.json`):
```json
{
  "dispatch_format": "json",
  "dispatch_id": "20250930-083312-58562bb1",
  "metadata": {
    "track": "C",
    "role": "architect",
    "workflow": "[[@.claude/skills/architect/SKILL.md]]",
    "gate": "validation",
    "priority": "P0",
    "cognition": "deep"
  },
  "title": "Investigate terminal status detection logic",
  "instructions": "Detailed task instructions...",
  "context_files": [
    "@scripts/generate_valid_dashboard.sh",
    "@state/terminal_status.ndjson"
  ],
  "constraints": [
    "Read-only investigation",
    "Document findings in report"
  ]
}
```

**Markdown Dispatch** (`dispatches/queue/{timestamp}-{track}.md`):
```markdown
# Task: Investigate terminal status detection logic

**Track**: C (T3 - Deep Investigation)
**Priority**: P0
**Cognition**: deep
**Role**: architect

## Instructions
Detailed task instructions...

## Context Files
- @scripts/generate_valid_dashboard.sh
- @state/terminal_status.ndjson

## Constraints
- Read-only investigation
- Document findings in report
```

### 2. Receipt Format (NDJSON)

**ACK Receipt** (`task_ack`):
```json
{
  "event_type": "task_ack",
  "dispatch_id": "20250930-083312-58562bb1",
  "track": "C",
  "terminal": "T3",
  "timestamp": "2025-09-30T08:33:15Z",
  "model": "opus",
  "estimated_duration": "15m"
}
```

**Completion Receipt** (`task_complete`):
```json
{
  "event_type": "task_complete",
  "dispatch_id": "20250930-083312-58562bb1",
  "track": "C",
  "terminal": "T3",
  "timestamp": "2025-09-30T08:48:22Z",
  "status": "success",
  "summary": "Completed terminal status investigation",
  "report_path": "reports/C/20250930-083312-investigation-report.md",
  "metrics": {
    "duration_seconds": 907,
    "lines_changed": 0,
    "files_modified": 0
  }
}
```

### 3. Intelligence Format (NDJSON)

**Unified Intelligence** (`state/t0_intelligence.ndjson`):
```json
{
  "event_type": "task_complete",
  "dispatch_id": "20250930-083312-58562bb1",
  "track": "C",
  "terminal": "T3",
  "timestamp": "2025-09-30T08:48:22Z",
  "status": "success",
  "summary": "Terminal status uses receipt-based detection",
  "report_path": "reports/C/20250930-083312-investigation-report.md",
  "tags": ["terminal", "status", "monitoring"]
}
```

**Progressive Reading Levels**:
1. **Quick (1K tokens)**: Last 10 events
2. **Standard (3K tokens)**: Last 25 events
3. **Detailed (5K tokens)**: Last 50 events + terminal insights
4. **Full context (10K tokens)**: Last 100 events + patterns + warnings
5. **Full (20K+ tokens)**: Last 200 events + complete context

### 4. Report Format (Markdown)

**Structured Report** (`reports/{track}/{timestamp}-{title}.md`):
```markdown
# Investigation Report: Terminal Status Detection

**Dispatch ID**: 20250930-083312-58562bb1
**PR-ID**: PR-3
**Session**: a1b2c3d4-e5f6-7890-abcd-ef1234567890
**Track**: C
**Terminal**: T3
**Gate**: investigation
**Timestamp**: 2025-09-30T08:48:22Z
**Status**: success
**Confidence**: 0.95

## Summary
Terminal status is determined by receipt-based activity detection.

## Findings
1. Status script checks last 5 receipts in t0_receipts.ndjson
2. Track B and C show "working" due to shadow receipts
3. Heartbeat system correctly detects activity

## Recommendations
- Add log-based activity monitoring
- Enhance post-completion conversation detection
- Document multi-signal detection strategy
```

**Note**: Session field enables cost tracking via session transcript resolution (see COST_TRACKING_GUIDE.md)

---

## Process Management

### Singleton Enforcement

**Mechanism**: PID files in `.vnx-data/pids/`
- Each process creates `{name}.pid` on start
- Checks for existing PID before starting
- Validates process is actually running
- Cleans up stale PID files

**Core Processes** (managed by supervisor):
- `dispatcher.pid` — `dispatcher_minimal.sh`
- `receipt_processor.pid` — `receipt_processor.sh`
- `heartbeat_ack_monitor.pid` — `heartbeat_ack_monitor.py`
- `dashboard.pid` — `generate_valid_dashboard.sh`
- `intelligence_daemon.pid` — `intelligence_daemon.py`
- `recommendations_engine.pid` — `recommendations_engine_daemon.sh`
- `vnx_supervisor.pid` — self

### Project-Scoped Process Isolation (shipped VNX 1.0.0)

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

**Fingerprints killed** (active process types):
- `dispatcher_minimal.sh`
- `receipt_processor.sh`
- `generate_t0_recommendations.py`
- `generate_valid_dashboard.sh`
- `vnx_supervisor_simple.sh`
- `t0_intelligence_aggregator.py`
- `intelligence_daemon.py`
- `heartbeat_ack_monitor.py`
- `report_watcher.sh`

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
- **Worker Intelligence Injection** (`userpromptsubmit_worker_intelligence_inject.sh`) —
  delivers up to 3 relevant patterns + prevention rules per prompt, budget
  <400 tokens, degrades gracefully with no dispatch or empty intelligence.
- **Quality Digest** (`build_t0_quality_digest.py`) — 3-section append-only
  NDJSON to `state/` (G-L6): Operational Defects, Prompt/Config Tuning,
  Governance Health, each recommendation carrying `evidence_ids` (G-L2).
- **Nightly Intelligence Pipeline** (`nightly_intelligence_pipeline.sh`, 02:00) —
  ordered run: `conversation_analyzer.py` → `tag_intelligence.py` →
  `build_t0_quality_digest.py` → `generate_t0_recommendations.py`. Each phase
  has its own health check; one phase failing does not suppress the rest.
- **T0 Intelligence Aggregator** — progressive context read in 5 levels (1K to
  20K+ tokens, last 10 to last 200 events), rolling window capped at 1000
  events. Receipt correlation, warning detection, terminal insights, tag-based
  report lookup.
- **State Manager Integration** — `state/unified_state.ndjson`, 5-second
  consolidation cycle over dispatches + receipts + terminal status, feeding the
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
claim that drifted (measured 2026-08-30: 0 of these 9 processes were running
while all 15 old checklist items still said "Active"). Check current
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
mirrors reality without a human re-stating it. The 1.0.1 future-state
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
   deferred to 1.x (#867). See ADR-005.

### The derived_status rule (precise)

`track_reconciler._compute_derived_status` evaluates the conditions in order and
returns the first that applies. A track is **`done` only when all of these hold**:

- it has **zero unresolved blocking open-items** — no `track_open_items` row with
  `link_type = 'blocks'` and `resolved_at IS NULL`; and
- **every dependency track is `done`** (each `track_dependencies` edge points at a
  track whose phase is `done`); and
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
(#873, 1.x) sits on top of: deterministic closes auto-apply, judgment cases
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
ADR-007 pattern in 1.0.1 (PR-A1 #859): a schema-preserving, in-place,
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
gates the subscription variant). Full lane map: `docs/core/PROVIDER_LANES.md`;
dispatch decision rules: `docs/core/DISPATCH_RULES.md`.

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

The VNX operator dashboard is a read-only projection over `.vnx-data/state/`. No React, no build toolchain.

**Stack**:
- Frontend: `dashboard/index.html` — vanilla HTML/JS with Alpine.js/htmx (no build step)
- Backend: `dashboard/serve_dashboard.py` — Python stdlib HTTP server (port 4173)
- State source: `.vnx-data/state/` files
- Polling: 5-second auto-refresh

### Design Constraints (Hard Rules)
- Dashboard is **read-only** — it never creates, promotes, or modifies dispatches (G-D1, G-D2)
- `vnx jump` is the only write action (tmux focus switch, fully reversible) (G-D3)
- No AI assistant that executes VNX commands (G-D4)
- No new build toolchain (A-4)
- `serve_dashboard.py` is the only HTTP server (A-5)

### UI Components

| Component | Description |
|-----------|-------------|
| **Attention bar** | Top banner — highlights terminals with `needs_human=true` with priority and reason |
| **Terminal cards** | Status, `context_usage_pct` progress bar, staleness indicator, Jump button |
| **Dispatch Kanban** | Read-only view: staging / queue / active / completed columns |
| **Event timeline** | Chronological list from receipts + dispatches with filter controls |
| **Health indicator** | System-wide health: process count, queue depth, supervisor status |
| **Confirmation gates** | Dialogs on dangerous actions (restart process, unlock terminal) |

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve `dashboard/index.html` |
| GET | `/api/events` | Event timeline from receipts + dispatch activity |
| GET | `/api/dispatches` | Dispatch Kanban state (staging/queue/active/completed) |
| GET | `/api/token-stats` | Token usage summary per terminal |
| GET | `/api/token-stats/sessions` | Per-session token breakdown |
| POST | `/api/jump/{terminal}` | Switch tmux focus to terminal (only write action) |
| POST | `/api/restart-process` | Restart a supervised process (confirmation required) |
| POST | `/api/unlock-terminal` | Clear terminal lock (confirmation required) |

### Startup

```bash
# Start dashboard server (port 4173)
python dashboard/serve_dashboard.py &
# Open http://localhost:4173
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
**Last Major Update**: 2026-09-27 (docs-refresh sweep to current `main`: single-entry door as the primary dispatch path, ephemeral headless workers instead of a fixed T0-T3 terminal grid, receipt pull instead of tmux push, removed hardcoded version/token-reduction claims). Prior: 2026-06-22 (dispatch lanes + single-entry door section; June-15 billing-default correction); 2026-03-28 (Attention model, jump command, worker intelligence injection, adoption tracking, nightly pipeline, 3-section quality digest).
**Dashboard**: Vanilla HTML/JS + Python HTTP server (port 4173, read-only, attention model)
**Governance Model**: Deliverable-based (T0 sole authority, evidence tracking, no auto-completion, G-L1–G-L8 enforced)
**Maintainer**: T-MANAGER (VNX Orchestration Expert)
