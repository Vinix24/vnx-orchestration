# VNX Orchestration System - Complete Architecture

**Status**: Active
**Last Updated**: 2026-03-06
**Owner**: T-MANAGER
**Purpose**: Single source of truth for VNX system architecture, components, and data flow.

**Version**: 11.0.0

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
9. [Staging Workflow](#staging-workflow)
10. [Multi-Provider Dispatch](#multi-provider-dispatch)
11. [Unified Dashboard](#unified-dashboard)
12. [Demo & Distribution](#demo--distribution)

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

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    VNX ORCHESTRATION SYSTEM V8.2                │
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
│  └──────────────────────────────────────┘                       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Terminal Architecture

### Terminal Specifications

| Terminal | Role | Provider | Model | Permissions | Purpose |
|----------|------|----------|-------|-------------|---------|
| **T0** | Orchestrator | Claude Code | Opus | Read-Only | Manager blocks, coordination, intelligence |
| **T1** | Worker | Claude/Codex | Sonnet | Full R/W | Any task dispatched by T0 (provider configurable) |
| **T2** | Worker | Claude Code | Sonnet | Full R/W | Any task dispatched by T0 |
| **T3** | Worker | Claude Code | Opus | Full R/W | Any task dispatched by T0 (Opus for complex work) |

**Multi-Provider Support**: T1 can run Codex CLI instead of Claude Code (configured via `config.env` or `vnx start --t1-provider codex`). Gemini CLI is also supported. Skills are synced to `~/.claude/skills/`, `~/.codex/skills/`, and `.gemini/skills/` during `vnx init`.

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

---

## Core Components

### 1. Smart Tap V7 (`smart_tap_v7_json_translator.sh`)

**Purpose**: Capture manager blocks and auto-translate JSON ↔ Markdown

**Functionality**:
- Monitors T0 output for `MANAGER:` blocks
- Auto-detects JSON vs Markdown format
- Translates JSON → Markdown for human review
- Creates dual storage: `.md` + `.json` files
- Average translation time: 25ms

**Files Created**:
- `dispatches/queue/{timestamp}-{track}.md` - Human-readable
- `dispatches/queue/.json/{timestamp}-{track}.json` - Machine-readable

### 2. Dispatcher V8 (`dispatcher_v8_minimal.sh`)

**Purpose**: Native skill activation and instruction routing (V8.2 - Current Production)

**Functionality**:
- Maps dispatch roles to native Claude Code skills
- Hybrid dispatch: skill via `send-keys` (triggers slash-command detection) + instruction via `paste-buffer` (~200 tokens)
- No template compilation needed (skills load via `/skill-name args` invocation)
- Track-based routing (A, B, C)
- Mode control (normal, thinking, planning)
- Multi-provider skill invocation: `/skill-name` (Claude), `$skill-name` (Codex), `@skill-name` (Gemini)
- PR-ID included in dispatch prompt for receipt correlation
- Rich footer with "Expected Outputs" guidelines and report metadata template

**Key Features**:
- 87% token reduction vs V7 (200 vs 1500 tokens)
- Guaranteed skill activation via send-keys (same mechanism as `/clear` and `/model`)
- Model switching support (opus/sonnet/haiku)
- Context clearing control
- Intelligence integration maintained
- Provider-aware dispatch (detects Claude/Codex/Gemini per terminal)

**Receipt Footer** (V8.2):
- Task Completion Guidelines section
- Report Metadata block (parsed by receipt processor)
- Expected Outputs section (implementation summary, files modified, testing evidence, open items)
- Report write path: `.vnx-data/unified_reports/`

**Legacy V7** (`dispatcher_v7_compilation.sh` - Reference only):
- Template compilation from agent library
- Full prompt generation (1500+ tokens)
- See `core/technical/DISPATCHER_SYSTEM.md` for V7.3 reference

### 3. Heartbeat ACK Monitor (`heartbeat_ack_monitor.py`) — Current

**Purpose**: Acknowledgment receipt processing and timeout management

**Functionality**:
- Monitors for `task_ack` receipts
- Tracks acknowledgment timestamps
- Manages timeout detection
- Updates dispatch status
- Replaces legacy `ack_dispatcher_v2.sh` + `dispatch_ack_watcher.sh`

### 4. Receipt Processor V4 (`receipt_processor_v4.sh`) - Primary

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

**Note**: `report_watcher.sh` exists but production receipt ingestion is handled by `receipt_processor_v4.sh`.

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

### 9. Unified State Manager (`unified_state_manager_v2.py`)

**Purpose**: Real-time state consolidation

**Functionality**:
- Consolidates dispatches, receipts, terminal status
- Updates every 5 seconds
- Writes to `state/unified_state.ndjson`
- Powers intelligence aggregator

### 10. VNX Supervisor (`vnx_supervisor_simple.sh`)

**Purpose**: Process health monitoring and auto-restart

**Functionality**:
- Monitors all core processes
- Auto-restart on failure
- PID tracking in `state/pids/`
- Health checks every 10 seconds

### 11. Dashboard Generator (`generate_valid_dashboard.sh`)

### 12. T0 Brief Generator (`generate_t0_brief.sh`)

**Purpose**: Build a <2KB “single glance” JSON snapshot for T0 decision-making.

**Output**:
- `state/t0_brief.json` (authoritative)
- `state/t0_brief.md` (human view)

**Purpose**: Real-time system metrics visualization

**Functionality**:
- Updates every 2 seconds
- Terminal status aggregation
- Process health tracking
- Queue depth monitoring
- Performance metrics

**Output**: `state/dashboard_status.json`

### 13. Queue Popup Watcher (`queue_popup_watcher.sh`)

**Purpose**: Automatic popup for new dispatches

**Functionality**:
- Monitors queue directory
- Auto-opens Neovim with dispatch
- Human review and approval workflow
- Moves approved dispatches to active/

---

## Data Flow

### Complete Orchestration Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                  ORCHESTRATION LOOP V8.2                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. T0 Creates Dispatch (JSON/Markdown)                         │
│     └─► MANAGER: {...} block or Markdown template               │
│                                                                  │
│  2. Smart Tap Captures & Translates                             │
│     ├─► Detects format (JSON vs MD)                            │
│     ├─► Translates JSON → Markdown (25ms)                       │
│     └─► Writes: queue/{id}-{track}.md + .json/{id}.json        │
│                                                                  │
│  3. Queue Popup Watcher Opens Editor                            │
│     ├─► Human reviews Markdown dispatch                         │
│     ├─► Approves by saving and closing                          │
│     └─► Moves to active/ directory                              │
│                                                                  │
│  4. Dispatcher V8 Routes to Terminal                            │
│     ├─► Maps role to native skill (@skill_name)               │
│     ├─► Gathers intelligence patterns (maintained)             │
│     ├─► Extracts instruction content                           │
│     ├─► Sends: skill activation + instruction + receipt        │
│     ├─► ~200 tokens total (87% reduction from V7)              │
│     └─► Routes to track-specific terminal                       │
│                                                                  │
│  5. Worker Terminal Receives Task                               │
│     ├─► Loads compiled prompt                                   │
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
│  8. Receipt Processor V4 Handles Report                         │
│     ├─► Detects new report in unified_reports/                 │
│     ├─► Parses structured data via report_parser.py            │
│     ├─► Attaches evidence to open items (does NOT close)       │
│     ├─► Appends to t0_receipts.ndjson                          │
│     └─► Delivers receipt to T0 via tmux paste                  │
│                                                                  │
│  10. Intelligence Aggregator Updates Context                    │
│      ├─► Consolidates all system state                         │
│      ├─► Generates progressive context layers                  │
│      ├─► Updates t0_intelligence.ndjson                        │
│      └─► Enables 80-95% token savings                          │
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
4. **Comprehensive (10K tokens)**: Last 100 events + patterns + warnings
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
- `dispatcher.pid` — `dispatcher_v8_minimal.sh`
- `smart_tap.pid` — `smart_tap_v7_json_translator.sh`
- `receipt_processor.pid` — `receipt_processor_v4.sh`
- `heartbeat_ack_monitor.pid` — `heartbeat_ack_monitor.py`
- `queue_watcher.pid` — `queue_popup_watcher.sh`
- `dashboard.pid` — `generate_valid_dashboard.sh`
- `state_manager.pid` — `unified_state_manager_v2.py`
- `intelligence_daemon.pid` — `intelligence_daemon.py`
- `recommendations_engine.pid` — `recommendations_engine_daemon.sh`
- `vnx_supervisor.pid` — self

### Project-Scoped Process Isolation (V8.2)

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

**Purpose**: Comprehensive process cleanup on `vnx stop` or `vnx start` (restart).

**Fingerprints killed** (18 process types):
- `smart_tap_v7_json_translator.sh`
- `dispatcher_v8_minimal.sh`
- `receipt_processor_v4.sh`
- `receipt_notifier.sh`
- `queue_popup_watcher.sh`
- `generate_t0_brief.sh`
- `generate_t0_recommendations.py`
- `generate_valid_dashboard.sh`
- `vnx_supervisor_simple.sh`
- `t0_intelligence_aggregator.py`
- `intelligence_daemon.py`
- `unified_state_manager_v2.py`
- `heartbeat_ack_monitor.py`
- `dispatch_ack_watcher.sh`
- `ack_dispatcher_v2.sh`
- `report_watcher.sh`
- `report_watcher_shadow.sh`
- `update_pane_mapping.sh`

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

### Pattern Matching Engine (PR #2 & PR #8)

**Integration Status**: ✅ FULLY OPERATIONAL

**Pattern Database**:
- 1,143 patterns from quality_intelligence.db
- FTS5 full-text search for rapid querying
- Pattern delivery to every dispatch (PR #8)
- Usage tracking and confidence adjustment

**Intelligence Flow**:
1. Dispatcher calls `gather_intelligence.py` for every dispatch
2. Extracts task description and technical keywords
3. Queries top 5 relevant patterns from database
4. Embeds patterns in quality_context
5. Propagates through receipt processor to T0

**Quality Context Structure**:
```json
{
  "intelligence_version": "1.4.0",
  "agent_validated": true,
  "patterns_available": true,
  "pattern_count": 5,
  "offered_pattern_hashes": ["a1b2c3...", "d4e5f6...", "g7h8i9...", "j0k1l2...", "m3n4o5..."],
  "tags_analyzed": true,
  "reports_mined": false
}
```

### T0 Intelligence Aggregator

**Progressive Context Architecture**:
```
Level 1 (Quick): 1K tokens
  └─► Last 10 events, basic status

Level 2 (Standard): 3K tokens
  └─► Last 25 events, recent patterns

Level 3 (Detailed): 5K tokens
  └─► Last 50 events, terminal insights

Level 4 (Comprehensive): 10K tokens
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

---

## File System Layout

```
project-root/
├── .claude/vnx-system/              # VNX system code (git-tracked)
│   ├── bin/vnx                      # CLI entry point
│   ├── scripts/                     # Active orchestration scripts
│   │   ├── smart_tap_v7_json_translator.sh
│   │   ├── dispatcher_v8_minimal.sh    # V8 native skills dispatcher
│   │   ├── receipt_processor_v4.sh
│   │   ├── receipt_notifier.sh         # Legacy/fallback
│   │   ├── report_parser.py
│   │   ├── append_receipt.py           # Receipt + quality sidecar writer
│   │   ├── quality_advisory.py         # File size/complexity analysis
│   │   ├── generate_t0_brief.sh
│   │   ├── generate_t0_recommendations.py
│   │   ├── queue_popup_watcher.sh
│   │   ├── vnx_supervisor_simple.sh
│   │   ├── pr_queue_manager.py         # PR queue + staging workflow
│   │   └── lib/                        # Shared libraries
│   │       ├── vnx_paths.sh            # Path resolver (cross-project guard)
│   │       ├── process_lifecycle.sh    # PID-safe process control
│   │       └── canonical_state_views.py
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
│   │   ├── terminal_state.json      # Terminal status (reconciled)
│   │   ├── pr_queue_state.yaml      # PR queue tracking
│   │   ├── quality_intelligence.db  # Quality patterns DB
│   │   ├── open_items.json          # Open items registry
│   │   └── dashboard_status.json    # Real-time metrics
│   │
│   ├── dispatches/                  # Task dispatches
│   │   ├── staging/                 # Batch proposals (no popup)
│   │   ├── queue/                   # Approved (popup trigger)
│   │   ├── active/                  # In progress
│   │   └── completed/              # Finished
│   │
│   ├── unified_reports/             # Markdown reports
│   ├── logs/                        # System logs
│   ├── pids/                        # Process PID files
│   └── locks/                       # Singleton locks
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

## Current System Status (V8.2)

### Active Components ✅
- Smart Tap V7 (JSON/Markdown auto-translation)
- Dispatcher V8 Minimal (Native skills, multi-provider, 87% token reduction)
- Receipt Processor V4 (Report → receipt → T0 delivery)
- Heartbeat ACK Monitor (ACK processing + timeout tracking)
- Queue Popup Watcher (dispatch review UI)
- Dashboard Generator (Real-time metrics → `dashboard_status.json`)
- Unified State Manager V2 (state consolidation, 5s cycle)
- Intelligence Daemon (real-time intelligence updates)
- Recommendations Engine (T0 dispatch suggestions, 30s cycle)
- VNX Supervisor (Health monitoring, 10s checks, auto-restart)
- Quality Advisory Pipeline (file size warnings on every completion)
- PR Queue Manager (parallel PRs, staging → promote workflow)
- Unified Dashboard (Next.js control plane, 8 pages)

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

## Staging Workflow

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
- **Queue**: Promoted dispatches (popup trigger via `queue_popup_watcher.sh`)
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

**Reference**: See [README_PR_QUEUE.md](../orchestration/README_PR_QUEUE.md) for complete workflow guide

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

### 4. Quality Intelligence (Phase 2+)
- **Database**: `state/quality_intelligence.db`
- **Metrics**: Task success rates, error patterns, performance trends
- **Learning**: Pattern extraction from receipts and reports

---

---

## Multi-Provider Dispatch

### Provider Capability Matrix

| Provider | Skill Format | Model Control | Context Clear | Status |
|----------|-------------|---------------|---------------|--------|
| **Claude Code** | `/skill-name` | `/model opus` | `/clear` | Primary |
| **Codex CLI** | `$skill-name` | N/A | `/new` | T1 alternative |
| **Gemini CLI** | `@skill-name` | N/A | `/clear` | Experimental |
| **Kimi** | N/A | N/A | N/A | Future |

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

---

## Unified Dashboard

### Architecture

The VNX dashboard is a unified Next.js 15 control plane combining system monitoring and token analytics.

**Stack**:
- Frontend: Next.js 15 App Router (`dashboard/token-dashboard/`, port 3100)
- Backend: Python FastAPI (`dashboard/serve_dashboard.py`, port 4173)
- Data: State files in `.vnx-data/state/` (proxied via Next.js rewrites)
- Polling: 7-second auto-refresh via `usePolling` hook

### Pages

| Page | Route | Data Source | Features |
|------|-------|-------------|----------|
| Overview | `/` | `dashboard_status.json` + token stats | Health banner, 4 KPI cards, recommendations, charts |
| System | `/system` | `dashboard_status.json` | Process list with restart buttons, queue stats, gates |
| Terminals | `/terminals` | `terminal_state.json` + `dashboard_status.json` | Status cards, unlock buttons, token comparison |
| Open Items | `/open-items` | `open_items_digest.json` | Severity filters, text search, recent closures |
| PR Queue | `/pr-queue` | `pr_queue_state.json` | Progress bar, PR table with status pills |
| Token Analysis | `/tokens` | Token stats API | Context/call trends, cache efficiency |
| Models | `/models` | Token stats API | Model distribution, performance comparison |
| Usage & Costs | `/usage` | Token stats API | Cost estimation per model and terminal |

### Key Features
- **Process Management**: Restart any supervised process from the System page (POST `/api/restart-process`)
- **Terminal Unlock**: Unlock stuck terminals from Terminals page (POST `/api/unlock-terminal`)
- **T0 Status**: Shows "In control" (T0 is always the orchestrator, never idle/blocked)
- **Terminal Status Normalization**: Maps raw statuses (working/active/busy/claimed → working, blocked/error/failed/timeout → blocked)
- **Glassmorphism Design**: Dark theme with VNX brand colors (orange, blue, gold)

### Startup

```bash
# Backend (port 4173)
python dashboard/serve_dashboard.py &

# Frontend (port 3100)
cd dashboard/token-dashboard && npm run dev
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
```

### Project Configuration

**`config.env`** (`.vnx-data/config.env`): Auto-sourced by `vnx start`:
```bash
VNX_PROVIDER=claude           # Primary provider (claude/codex/gemini)
VNX_MODEL=opus                # Default model
VNX_T1_PROVIDER=codex         # T1 can use different provider
```

**`config.yml`** (`.vnx/config.yml`): Project metadata:
```yaml
project_name: my-project
vnx_version: 8.2.0
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

**Document Status**: Production Active (V11.0 Unified Dashboard + Process Alignment)
**Last Major Update**: 2026-03-06 (Unified Dashboard, deprecated process cleanup, supervisor alignment)
**Dispatcher Version**: V8.2 Minimal (Native Skills + Multi-Provider + Expected Outputs)
**Token Reduction**: 87% (200 vs 1500 tokens per dispatch)
**Intelligence Version**: v1.2.0 (Open Items + Staging)
**Dashboard**: Next.js 15 Unified Control Plane (8 pages, 7s polling)
**Governance Model**: Deliverable-based (T0 sole authority, evidence tracking, no auto-completion)
**Maintainer**: T-MANAGER (VNX Orchestration Expert)
