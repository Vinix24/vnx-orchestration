# VNX Orchestration System - Complete Architecture

**Status**: Active
**Last Updated**: 2026-06-22
**Owner**: T-MANAGER
**Purpose**: Single source of truth for VNX system architecture, components, and data flow.

**Version**: 1.0.0

> **Dispatch flow:** the end-to-end dispatch lifecycle (single-entry door → assembly → delivery → phantom-guard → gates) lives in [DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md](./DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md). This doc covers terminal/process/intelligence components; where it describes dispatch routing, that doc supersedes it.

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
- **Native Skill Architecture**: V8 uses Claude Code native skills (87% token reduction)
- **Multi-Provider Dispatch**: Claude Code + Codex CLI + Gemini CLI with provider-specific skill invocation
- **Project-Scoped Process Isolation**: `VNX_KILL_SCOPE` prevents cross-project process interference
- **Singleton Process Enforcement**: Bulletproof duplicate prevention
- **Progressive Intelligence**: Token-efficient context aggregation
- **Quality Advisory Pipeline**: Automatic file size/complexity warnings on every completion
- **Track-Agnostic Workers**: T1-T3 handle any task type; T0 dispatches to the next available worker
- **Multi-Model Coordination**: Opus (T0, T3) + Sonnet (T1, T2), Codex CLI (T1 alternative)
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

> **Conceptual roles, not fixed panes.** The T0-T3 grid below shows the *roles* in the system, not a fixed four-pane layout. `vnx start` launches **T0 only**; T1-T3 are on-demand worker lanes spawned per dispatch (see *Terminal Architecture* below).

```
┌─────────────────────────────────────────────────────────────────┐
│                    VNX ORCHESTRATION SYSTEM V1.0                │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │  T0 (BRAIN)  │  │  T1 (Worker) │  │  T2 (Worker) │         │
│  │ Claude Opus  │  │Claude/Codex  │  │Claude Sonnet │         │
│  │ Read-Only    │  │  Full R/W    │  │  Full R/W    │         │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘         │
│         │                  │                  │                  │
│         │   ┌──────────────┴──────────────────┘                │
│         │   │          ┌──────────────┐                         │
│         │   │          │  T3 (Worker) │                         │
│         │   │          │ Claude Opus  │                         │
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
│  │  • Smart Tap (JSON/MD detection)     │                       │
│  │  • Dispatcher V8 (Native skills)     │                       │
│  │  • Receipt Processor V4 (Delivery)   │                       │
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

---

## Terminal Architecture

### Roles, not fixed panes

`vnx start` launches **T0 only** as a tmux pane (`scripts/commands/start.sh:330,346,562`); T1-T3 are **on-demand worker lanes spawned per dispatch**, not fixed panes. A worker lane is created when a dispatch needs it (subprocess adapter or leaseless tmux lane) and torn down after. Model and provider are chosen **per dispatch via lane selection**, not pinned to a terminal — the model/provider columns below describe the *typical* role assignment, not a hard pin.

### Terminal Role Specifications

| Terminal | Role | Typical Provider | Typical Model | Permissions | Purpose |
|----------|------|----------|-------|-------------|---------|
| **T0** | Orchestrator | Claude Code | Opus | Read-Only | Manager blocks, coordination, intelligence (the one launched pane) |
| **T1** | Worker lane | Claude/Codex | Sonnet | Full R/W | Any task dispatched by T0 (provider per-dispatch) |
| **T2** | Worker lane | Claude Code | Sonnet | Full R/W | Any task dispatched by T0 |
| **T3** | Worker lane | Claude Code | Opus | Full R/W | Any task dispatched by T0 (Opus for complex work) |

**Multi-Provider Support**: a worker lane can run Codex CLI or Gemini CLI instead of Claude Code; the provider is resolved per dispatch by the lane selector (see [DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md](./DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md) §3). Skills are synced to `~/.claude/skills/`, `~/.codex/skills/`, and `.gemini/skills/` during `vnx init`.

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

### 1. Dispatch routing (the single-entry door)

Routing is now the **single-entry door** (`dispatch_cli.run_dispatch`): validate → snapshot → compile_plan → permit → execute, with lane selection (provider → lane is a hard rule), assembly (skill + intelligence injection), delivery, and govern all behind one entry point. It still yields the V8 properties — native-skill invocation per provider (`/skill` Claude, `$skill` Codex, `@skill` Gemini), ~87% token reduction vs template compilation, PR-ID correlation, and the report-metadata footer (`.vnx-data/unified_reports/`). The standalone `dispatcher_minimal.sh` send-keys/paste-buffer path is **legacy** (still on disk, replaced by the door). Full flow: [DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md](./DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md) §2-4.

### 3. Heartbeat ACK Monitor (`heartbeat_ack_monitor.py`)

**Purpose**: Acknowledgment receipt processing and timeout management

**Functionality**:
- Monitors for `task_ack` receipts
- Tracks acknowledgment timestamps
- Manages timeout detection
- Updates dispatch status

### 4. Receipt Processor V4 (`receipt_processor.sh`) - Primary

**Purpose**: Parse new markdown reports into receipts, attach evidence to open items, append to `t0_receipts.ndjson`, and deliver the receipt into the T0 pane reliably.

**Functionality**:
- Monitors `.claude/vnx-system/unified_reports/*.md` (monitor mode with time filtering)
- Uses `report_parser.py` to generate a compact JSON receipt
- Attaches evidence to tracked open items via PR-ID (does NOT close items or complete PRs)
- Appends receipts to `state/t0_receipts.ndjson` (production receipt log)
- Delivers receipts to T0 via tmux (buffer paste + double Enter)
- Includes flood protection + singleton enforcement

**Governance**: Receipt processor is evidence-only. T0 reviews evidence, closes satisfied open items, and completes PRs when all blockers/warnings are resolved.

### 5. Receipt Notifier (`receipt_notifier.sh`) — Deprecated

**Purpose**: Legacy receipt delivery. Replaced by Receipt Processor V4 which handles parsing, appending, and delivery in one process.

**Note**: Kept in codebase as reference. Not started by supervisor.

### 6. Report Parser (`report_parser.py`)

**Purpose**: Extract a structured receipt from a worker markdown report

**Functionality**:
- Parses `.claude/vnx-system/unified_reports/*.md`
- Normalizes metadata, tags, metrics, recommendations
- Produces compact JSON for `t0_receipts.ndjson`

**Note**: `report_watcher.sh` exists but production receipt ingestion is handled by `receipt_processor.sh`.

### 7. Context Rotation Hooks (Stop/PostToolUse/SessionStart) - v2.4

**Purpose**: Optional context-rotation automation for long-running sessions.

**Hooks**:
- **Stop hook** (`vnx_context_monitor.sh`): observes `context_window.json` and emits block/warn guidance.
- **PostToolUse hook** (`vnx_handover_detector.sh`): detects handover docs, acquires lock, appends receipt, triggers rotator.
- **SessionStart hook** (`vnx_rotation_recovery.sh`): injects last handover into new session context.

**Activation**:
- Experimental / opt-in via `VNX_CONTEXT_ROTATION_ENABLED=1`.
- Default no-op (backward-compatible).

**Receipts**:
- `context_rotation` receipts are **informational only**. T0 does not need to act on these receipts unless paired with a human decision or explicit dispatch.

### 8. T0 Intelligence Aggregator (`t0_intelligence_aggregator.py`)

**Purpose**: Progressive context management for T0 orchestration

**Functionality**:
- Aggregates all system state into single NDJSON
- Progressive reading: 5 levels (1K → 20K+ tokens)
- Receipt correlation and warnings
- Terminal insights and patterns
- Tag-based report lookup
- 80-95% token savings

**Output**: `state/t0_intelligence.ndjson` (rolling window, last 1000 events)

### 9. VNX Supervisor (`vnx_supervisor_simple.sh`)

**Purpose**: Process health monitoring and auto-restart

**Functionality**:
- Monitors all core processes
- Auto-restart on failure
- PID tracking in `state/pids/`
- Health checks every 10 seconds

### 11. Dashboard Generator (`generate_valid_dashboard.sh`)

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

The end-to-end dispatch lifecycle is now an **8-stage pipeline** behind the single-entry door (validate → snapshot → compile_plan → permit → execute → deliver → govern/phantom-guard → gates), not the older 11-step ACK/heartbeat loop. The canonical, current diagram and stage-by-stage description live in **[DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md](./DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md) §2 (End-to-end flow)**. The intelligence and process components that the pipeline drives — receipt processor, intelligence aggregator, T0 brief — are described in *Core Components* and *Intelligence Systems* below.

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
    "workflow": "[[@.claude/terminals/library/templates/agents/architect.md]]",
    "gate": "validation",
    "priority": "P0",
    "cognition": "deep"
  },
  "title": "Investigate terminal status detection logic",
  "instructions": "Detailed task instructions...",
  "context_files": [
    "@.claude/vnx-system/scripts/generate_valid_dashboard.sh",
    "@.claude/vnx-system/state/terminal_status.ndjson"
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
- @.claude/vnx-system/scripts/generate_valid_dashboard.sh
- @.claude/vnx-system/state/terminal_status.ndjson

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
export VNX_KILL_SCOPE="$scripts_dir"  # e.g. /path/to/project/.claude/vnx-system/scripts

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

### Pattern Matching Engine

**Integration Status**: ✅ FULLY OPERATIONAL

**Pattern Database**:
- Patterns stored in `quality_intelligence.db` (`pattern_usage` table)
- FTS5 full-text search for rapid querying
- Tag-based pairwise/triple combination matching (`tag_combinations` table)
- Usage tracking with `used_count`, `ignored_count`, confidence scores

**Intelligence Flow** (with adoption tracking):
1. Dispatcher calls `gather_intelligence.py` for every dispatch
2. Extracts task description and technical keywords
3. Queries top relevant patterns from database
4. Calls `record_pattern_offer(pattern_id, terminal, dispatch_id)` → appends to `intelligence_usage.ndjson` (G-L7 audit)
5. Patterns injected into terminal via intelligence hook
6. On receipt processing: `record_adoption_from_receipt()` correlates receipt file changes with offered patterns → increments `pattern_usage.used_count`
7. `ignored_count` increments when dispatch lifecycle closes without adoption

**Adoption Tracking Files**:
- `state/intelligence_usage.ndjson` — append-only audit log of offer/adoption events

**Quality Context Structure**:
```json
{
  "intelligence_version": "2.0.0",
  "agent_validated": true,
  "patterns_available": true,
  "pattern_count": 5,
  "offered_pattern_hashes": ["a1b2c3...", "d4e5f6..."],
  "tags_analyzed": true,
  "reports_mined": false
}
```

### Worker Intelligence Injection (`userpromptsubmit_worker_intelligence_inject.sh`)

**Purpose**: Deliver task-relevant intelligence to T1-T3 workers on every prompt (PR-2).

**Behavior**:
- Reads the terminal's active dispatch to extract tags/scope
- Queries `gather_intelligence.py` for relevant patterns (max 3) and prevention rules
- Injects into prompt context via `UserPromptSubmit` hook
- Strict token budget: <400 tokens per prompt
- Degrades gracefully if no dispatch or empty intelligence (A-5)
- Logs each injection event to `intelligence_usage.ndjson` with timestamp, terminal, dispatch_id, pattern_ids (G-L7)

**Contrast with T0**: T0 injection (`userpromptsubmit_intelligence_inject.sh`) focuses on recommendations and quality hotspots, not terminal status noise.

### Quality Digest V3 (`build_t0_quality_digest.py`)

**Format**: 3 structured sections, append-only NDJSON output to `state/` (G-L6).

| Section | Content |
|---------|---------|
| Operational Defects | Top 5 actionable items with receipt/dispatch evidence |
| Prompt/Config Tuning | Pattern adoption signals, CLAUDE.md patch suggestions |
| Governance Health | Pending recommendation count, G-L1–G-L8 compliance |

Each recommendation includes `evidence_ids` (receipt IDs, dispatch IDs, file paths) per G-L2.

### Nightly Intelligence Pipeline (`nightly_intelligence_pipeline.sh`)

Consolidates the former two overlapping schedules (18:00 hygiene + 02:00 analysis) into a single ordered pipeline:

```
02:00 — conversation_analyzer.py     (session analytics)
     → tag_intelligence.py           (pairwise/triple tag combinations)
     → build_t0_quality_digest.py    (3-section digest → state/ NDJSON)
     → generate_t0_recommendations.py (structured recommendations, cap 5)
```

Each phase has health checks; failure in one phase is recorded without suppressing later phases.

### T0 Intelligence Aggregator

**Progressive Context Architecture**:
```
Level 1 (Quick): 1K tokens
  └─► Last 10 events, basic status

Level 2 (Standard): 3K tokens
  └─► Last 25 events, recent patterns

Level 3 (Detailed): 5K tokens
  └─► Last 50 events, terminal insights

Level 4 (Full context): 10K tokens
  └─► Last 100 events, warnings, correlations

Level 5 (Full): 20K+ tokens
  └─► Last 200 events, complete context, tag queries
```

**Token Savings**: 80-95% reduction vs. raw file reading

**Features**:
- Receipt correlation (ACK → completion matching)
- Warning detection (missing receipts, timeouts)
- Terminal insights (activity patterns, availability)
- Tag-based report lookup
- Rolling window (last 1000 events max)

### State Manager Integration

**Unified State Consolidation**:
- Updates every 5 seconds
- Sources: Dispatches + Receipts + Terminal Status
- Output: `state/unified_state.ndjson`
- Feeds: Intelligence Aggregator

### Governance Measurement System (v8.1.0)

**Integration Status**: OPERATIONAL (2026-03-07)

Replaces self-reported status with objective quality scoring using SPC (Statistical Process Control).

**3-Layer Architecture**:
```
Layer 1: CQS Calculator     -> Per dispatch, real-time, 0-100 composite score
Layer 2: Nightly Aggregation -> FPY, rework rate, SPC control charts
Layer 3: Weekly Report       -> Controlled model/role analysis, actionable items
```

**Key Metrics**:
- **Composite Quality Score (CQS)**: Weighted score from status normalization (30%), completion signals (25%), effort efficiency (20%), error density (15%), rework detection (10%)
- **First-Pass Yield (FPY)**: % of unique tasks succeeded on first attempt (Toyota/Six Sigma standard)
- **SPC Anomaly Detection**: Western Electric rules (out-of-control, trend, shift, run)

**Database**: `governance_metrics`, `spc_control_limits`, `spc_alerts` tables in quality_intelligence.db

**Full Reference**: `docs/intelligence/GOVERNANCE_MEASUREMENT.md`

### Deterministic Gates

VNX implements a three-tier verification pipeline:

1. **Contract blocks** -- dispatches include machine-checkable success criteria
2. **Lightweight verification** (`verify_claims.py`) -- runs after receipt processing; checks file changes, existence, patterns
3. **Pre-merge gate** (`vnx gate-check --pr <PR-ID>`) -- heavy checks: pytest, AST, artifact verification, shell syntax

Gate results are stored in `.vnx-data/state/gate_results/<PR-ID>.json` with per-check GO/HOLD verdicts.

### Phantom-Guard

Phantom-guard runs **inline at govern for every dispatch** (`dispatch_govern.py:268`, `phantom_guard.py`): a delivery-role worker that emits a "GATE GREEN / done" completion claim while producing **no worktree/branch diff** is rejected as a phantom — `token_usage > 0` does **not** exempt it (tokens mean an LLM thought, not that a deliverable was produced). Review roles (`plan-reviewer`, `code-reviewer`, `security-reviewer`, `reviewer`) are exempt — a verdict, not a diff, is the expected output. A legitimate no-op delivery uses the operator escape `VNX_OVERRIDE_PHANTOM_GUARD=1`. The guard abstains on an unresolvable ref (never false-rejects) and is best-effort at govern (a guard failure never loses the report). Full description: [DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md](./DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md) §6.

---

## File System Layout

```
project-root/
├── .claude/vnx-system/              # VNX system code (git-tracked)
│   ├── bin/vnx                      # CLI entry point
│   ├── scripts/                     # Active orchestration scripts
│   │   ├── dispatcher_minimal.sh    # V8 native skills dispatcher
│   │   ├── receipt_processor.sh     # Receipt processing + T0 delivery
│   │   ├── report_parser.py
│   │   ├── append_receipt.py           # Receipt + quality sidecar writer
│   │   ├── generate_t0_recommendations.py
│   │   ├── vnx_supervisor_simple.sh
│   │   ├── pr_queue_manager.py         # PR queue + staging workflow
│   │   ├── gather_intelligence.py      # Intelligence aggregation
│   │   ├── learning_loop.py            # Adoption signals, pending_rules queue
│   │   ├── tag_intelligence.py         # Pairwise/triple tag subsets
│   │   ├── build_t0_quality_digest.py  # 3-section NDJSON digest
│   │   ├── check_intelligence_health.py # Intelligence health check
│   │   ├── gate_runner.py              # Deterministic gate execution
│   │   ├── review_gate_manager.py      # Review-gate policy execution
│   │   ├── commands/                   # Extracted CLI command files
│   │   │   ├── jump.sh                 # vnx jump <terminal> | --attention
│   │   │   ├── start.sh
│   │   │   ├── stop.sh
│   │   │   ├── doctor.sh
│   │   │   ├── new_worktree.sh
│   │   │   ├── merge_preflight.sh
│   │   │   ├── finish_worktree.sh
│   │   │   ├── recover.sh
│   │   │   └── headless.sh
│   │   └── lib/                        # Shared libraries
│   │       ├── vnx_paths.sh            # Path resolver (cross-project guard)
│   │       ├── process_lifecycle.sh    # PID-safe process control
│   │       ├── runtime_core.py         # Runtime state machine core
│   │       ├── dispatch_router.py      # Dispatch routing logic
│   │       ├── subprocess_adapter.py   # Headless subprocess delivery
│   │       └── subprocess_dispatch.py  # Subprocess dispatch orchestration
│   │
│   ├── skills/                      # 18 native skills
│   │   ├── skills.yaml              # Skill registry
│   │   └── {skill-name}/SKILL.md    # Per-skill docs + references
│   │
│   ├── templates/terminals/         # T0-T3 agent templates
│   ├── schemas/                     # Quality intelligence SQL schema
│   ├── demo/                        # Demo setup (setup_demo.sh + FEATURE_PLAN.md)
│   └── docs/                        # This documentation tree
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
│   │   ├── pending/<id>/            # Door lifecycle: per-dispatch bundle, human-gated promote
│   │   ├── staging/                 # Legacy PR-queue: batch proposals (no popup)
│   │   ├── queue/                   # Legacy PR-queue: approved (popup trigger)
│   │   ├── active/                  # Legacy PR-queue: in progress
│   │   └── completed/              # Legacy PR-queue: finished
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

## Key Performance Metrics

- **JSON Translation**: 25ms average (Smart Tap)
- **Template Compilation**: <100ms (Dispatcher)
- **Receipt Delivery**: <500ms (Receipt Notifier)
- **Intelligence Update**: 5-second cycle (State Manager)
- **Dashboard Refresh**: 2-second cycle
- **Token Savings**: 80-95% (Intelligence Aggregator)

---

## Current System Status (VNX 1.0.0)

### Active Components ✅
- Single-entry dispatch door (`dispatch_cli.run_dispatch` — merged on main PR #896; runtime **default-OFF** pending the flip, `_DEFAULT_ENABLED=False`)
- Dispatch assembly (lane selection + skill + intelligence injection behind the door)
- Phantom-guard (inline at govern, rejects evidence-free GATE-GREEN delivery receipts; on main)
- GLM→harness normalization (on main)
- Smart Tap V7 (JSON/Markdown auto-translation)
- Dispatcher V8 Minimal (Native skills, multi-provider, 87% token reduction — superseded by the door for routing; standalone `dispatcher_minimal.sh` now legacy)
- Receipt Processor V4 (Report → receipt → T0 delivery, adoption tracking)
- Heartbeat ACK Monitor (ACK processing + timeout tracking)
- Queue Popup Watcher (dispatch review UI)
- Dashboard Generator (Real-time metrics → `dashboard_status.json`)
- Unified State Manager V2 (state consolidation, 5s cycle)
- Intelligence Daemon (real-time intelligence updates)
- Recommendations Engine (T0 dispatch suggestions, 30s cycle, max 5 pending — G-L8)
- VNX Supervisor (Health monitoring, 10s checks, auto-restart)
- Quality Advisory Pipeline (file size warnings on every completion)
- PR Queue Manager (parallel PRs, staging → promote workflow)
- Operator Dashboard (vanilla HTML/JS + `serve_dashboard.py`, attention model, jump command)
- Worker Intelligence Injection (`userpromptsubmit_worker_intelligence_inject.sh` — T1-T3)
- Nightly Intelligence Pipeline (`nightly_intelligence_pipeline.sh`, 02:00, 4-phase ordered)

### Deprecated Components (not started by supervisor)
- ACK Dispatcher V2 (`ack_dispatcher_v2.sh`) — replaced by `heartbeat_ack_monitor.py`
- Report Watcher (`report_watcher.sh`) — replaced by Receipt Processor V4
- Receipt Notifier (`receipt_notifier.sh`) — replaced by Receipt Processor V4
- Dispatcher V7 — reference only (see `core/technical/DISPATCHER_SYSTEM.md`)

### Terminal Status
- **T0 (Claude Opus)**: Orchestrator brain, read-only
- **T1 (Claude Sonnet / Codex CLI)**: Worker (provider configurable)
- **T2 (Claude Sonnet)**: Worker
- **T3 (Claude Opus)**: Worker (Opus for complex tasks)

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
reconciliation batch (PRD `claudedocs/PRD-future-state-reconciliation-v1.1.md`)
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

> **Legacy PR-queue lifecycle.** The staging → queue → active → completed path below is the **legacy PR-queue** model (`pr_queue_manager.py`). The single-entry door's lifecycle is per-dispatch `dispatches/pending/<id>/` bundles, promoted through a **human-gated promote** — see [DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md](./DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md) §2. This section documents the PR-queue model that still ships for batch-PR feature plans.

### Purpose
Separates proposal review (staging) from approved work (queue), preventing premature popup notifications.

### Architecture
```
dispatches/
├── staging/     # Proposals (no popup) - Batch PR dispatches generated here
├── queue/       # Approved (popup trigger) - Promoted PRs ready for execution
├── active/      # In progress
└── completed/   # Finished
```

### PR Queue Batch Dispatch Workflow (PRIMARY METHOD)

**Batch Generation → Staging Review → Selective Promotion → Popup Approval**

```
FEATURE_PLAN.md  →  init-feature  →  staging/  →  T0 review  →  promote  →  queue/  →  popup
                     (all PRs)         (7 files)   (show/patch)   (1 PR)     (1 file)   (appears)
```

**Key Principles**:
- ✅ **Batch init**: Generate ALL PR dispatches upfront (once per feature)
- ✅ **Staging review**: T0 reviews dispatches before they trigger popup
- ✅ **Dependency-aware**: Promotion blocked if dependencies unmet
- ✅ **Popup trigger**: Only promoted dispatches appear in popup
- ❌ **NO auto-dispatch**: No automatic dispatch generation per PR
- ❌ **NO terminal output**: Manager blocks only created via CLI, not printed to terminal

**CLI Commands**:
```bash
# ONE TIME: Generate all PR dispatches to staging/
python .claude/vnx-system/scripts/pr_queue_manager.py init-feature FEATURE_PLAN.md

# Review staging with dependency status
python .claude/vnx-system/scripts/pr_queue_manager.py staging-list

# Promote individual PR to queue (triggers popup)
python .claude/vnx-system/scripts/pr_queue_manager.py promote <dispatch-id>
```

**State Management**: `.claude/vnx-system/state/pr_queue_state.yaml`
- Tracks completed PRs, in-progress PR, execution order
- Dependency validation during promotion
- Evidence attachment via receipt processor (T0 reviews and completes PRs)

### Notification System
- **Staging**: Batch-generated PR dispatches (no popup)
- **Queue**: Promoted dispatches (operator-driven promotion)
- **Seen Cache**: `state/staging_seen.json` prevents duplicate notifications

### T0 Decision Tree
```
📥 STAGING_READY notification (from init-feature)
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
    │    ├─ YES → `pr_queue_manager.py promote <id>` → Popup appears
    │    └─ NO → `pr_queue_manager.py reject <id> --reason "X"`
    │
    └─ NO → Ignore (stays in staging)
```

**Reference**: See [DISPATCH_GUIDE.md](../DISPATCH_GUIDE.md) for the current workflow guide

---

## Intelligence Features Summary

### 1. Recommendation Engine (v1.2.0)
- **Sources**: Receipts, PR queue, open items, staging
- **Output**: `state/t0_recommendations.json`
- **Types**: Gate progression, dependencies, conflicts, staging, blockers
- **Cycle**: 30-second update interval

### 2. T0 Brief Generator
- **Purpose**: <2KB decision snapshot
- **Includes**: Terminal status, queue counts, open items, PR progress
- **Format**: JSON + Markdown views
- **Token Efficiency**: 95% reduction vs raw state

### 3. Cached Intelligence
- **Progressive Aggregation**: 5 levels of context depth
- **Files**: `state/cached_intelligence_*.ndjson`
- **Token Savings**: 80-95% reduction
- **Update Cycle**: 5 seconds

### 4. Quality Intelligence
- **Database**: `state/quality_intelligence.db`
- **Metrics**: Task success rates, error patterns, performance trends
- **Learning**: Pattern extraction from receipts and reports
- **Tables**: `pattern_usage`, `session_analytics`, `prevention_rules`, `tag_combinations`

### 5. Adoption Tracking
- **Offer log**: `record_pattern_offer()` → `intelligence_usage.ndjson`
- **Adoption detection**: `record_adoption_from_receipt()` correlates receipt file changes with offered patterns
- **Confidence updates**: `used_count`/`ignored_count` drive per-pattern confidence scores
- **Governance**: No auto-activation — all generated rules go to `pending_rules.json` (G-L1)

### 6. Worker Intelligence Injection (T1-T3)
- **Hook**: `userpromptsubmit_worker_intelligence_inject.sh` (registered via `vnx regen-settings --merge`)
- **Content**: max 3 patterns + relevant prevention rules relevant to active dispatch tags
- **Budget**: <400 tokens per prompt
- **Audit**: Every injection logged to `intelligence_usage.ndjson` (G-L7)

### 7. Nightly Intelligence Pipeline
- **Script**: `nightly_intelligence_pipeline.sh`
- **Schedule**: 02:00 daily (replaces two overlapping schedules)
- **Phases**: session analysis → tag intelligence → quality digest → recommendations
- **Output**: Append-only NDJSON to `state/` (G-L6)

---

---

## Multi-Provider Dispatch

### Dispatch lanes and provider routing

VNX drives every provider as a CLI subprocess and never imports a vendor SDK
(the `no-anthropic-sdk` constraint enforces this in CI). The hard guard-rails on
which provider may serve which lane live in one machine-readable SSOT:
`scripts/lib/providers/provider_constraints.yaml`.

Two Claude worker lanes exist:

- **claude-tmux-spawn** (`scripts/lib/tmux_interactive_dispatch.py`) — the
  default. Interactive `claude` (never `claude -p`) in a single-shot tmux
  session, on the subscription. `dispatch.sh` selects it unless a dispatch opts
  into the burst lane.
- **claude-subprocess** (`scripts/lib/subprocess_dispatch.py`) — the headless
  `claude -p` burst lane. After the June 15, 2026 billing change it bills API
  credits, so it is opt-in and blocked by default (`claude-headless` constraint;
  `VNX_OVERRIDE_CLAUDE_HEADLESS=1` to open it).

Non-Claude providers (codex, gemini, kimi, deepseek-harness, litellm sub-providers,
local-gemma) route through `scripts/lib/provider_dispatch.py`. Kimi is CLI-OAuth
only (`kimi-via-cli-only`); GLM-5.1 routes via OpenRouter (`zai-via-openrouter-only`);
DeepSeek runs through the Claude harness with an own key (`deepseek-harness-subscription-blocked`
gates the subscription variant). Full lane map: `docs/core/PROVIDER_LANES.md`;
dispatch decision rules: `docs/core/DISPATCH_RULES.md`.

**Single-entry door (merged on main, runtime default-OFF pending the flip).** Lane
selection runs behind one entry point (`scripts/lib/dispatch_cli.py`,
`run_dispatch`): validate → snapshot → compile_plan → permit → execute. The door,
the GLM→harness normalization, and a phantom-guard that rejects evidence-free
GATE-GREEN receipts are **merged to main (PR #896)**. It is **runtime default-OFF**
pending the default-flip: `_DEFAULT_ENABLED = False` in
`scripts/lib/dispatch_flags.py:31`, so `VNX_SINGLE_ENTRY_DISPATCH` resolves off
unless explicitly enabled. `VNX_DISPATCH_LEGACY=1` is the absolute rollback (wins
over everything). The flip to `True` is a separate, gated step.

### Provider Capability Matrix

| Provider | Skill Format | Model Control | Context Clear | Status |
|----------|-------------|---------------|---------------|--------|
| **Claude Code** | `/skill-name` | `/model opus` | `/clear` | Primary |
| **Codex CLI** | `$skill-name` | N/A | `/new` | T1 alternative |
| **Gemini CLI** | `@skill-name` | N/A | `/clear` | Experimental |
| **Kimi** | CLI OAuth | N/A | N/A | Production (CLI lane, 6/6 skill-injection verified) |

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

**`config.env`** (`.vnx-data/config.env`): Auto-sourced by `vnx start`:
```bash
VNX_PROVIDER=claude           # Primary provider (claude/codex/gemini)
VNX_MODEL=opus                # Default model
VNX_T1_PROVIDER=codex         # T1 can use different provider
```

**`config.yml`** (`.vnx/config.yml`): Project metadata:
```yaml
project_name: my-project
vnx_version: 1.0.0
created_at: 2026-02-18
```

### Demo Setup

**Script**: `.claude/vnx-system/demo/setup_demo.sh`

Creates a complete LeadFlow SaaS project with:
- 6 PRs across 3 parallel tracks (A/B/C)
- PR dependency graph with quality gates
- Quality advisory trap file (555 lines > 500 warning threshold)
- VNX cloned from GitHub and initialized
- T1 provider auto-configured as Codex CLI

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

**Document Status**: Active (VNX 1.0.0 release candidate — Dashboard Attention Model + intelligence injection). The consolidation/self-learning loop ships but is opt-in and currently dormant: existing patterns inject into dispatch context, but the pool does not grow on its own yet.
**Last Major Update**: 2026-06-23 (single-entry door **merged on main, PR #896** — runtime default-OFF pending the flip; phantom-guard inline at govern; T0-only start + on-demand worker lanes; dispatch flow SSOT cross-linked to DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md). Prior: 2026-06-22 (dispatch lanes + single-entry door section; June-15 billing-default correction); 2026-03-28 (Attention model, jump command, worker intelligence injection, adoption tracking, nightly pipeline, 3-section quality digest)
**Dispatcher Version**: V8.2 Minimal (Native Skills + Multi-Provider + Expected Outputs; VNX 1.0.0)
**Token Reduction**: 87% (200 vs 1500 tokens per dispatch)
**Intelligence Version**: v2.0.0 (Adoption tracking, worker injection, pairwise tag matching, 3-section digest)
**Dashboard**: Vanilla HTML/JS + Python HTTP server (port 4173, read-only, attention model)
**Governance Model**: Deliverable-based (T0 sole authority, evidence tracking, no auto-completion, G-L1–G-L8 enforced)
**Maintainer**: T-MANAGER (VNX Orchestration Expert)
