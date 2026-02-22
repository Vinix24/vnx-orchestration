# Getting Started (VNX)

**Status**: Active
**Last Updated**: 2026-02-18
**Owner**: T-MANAGER
**Purpose**: Quick orientation and links to the current VNX "source of truth" docs.

---

## Current System Snapshot (2026-02-18)

- Architecture: `00_VNX_ARCHITECTURE.md` (V10.0)
- Monitoring/ops: `../operations/MONITORING_GUIDE.md`
- Receipt pipeline: `../operations/RECEIPT_PIPELINE.md` (V8.1)
- Dispatcher: V8.2 Minimal — see `technical/DISPATCHER_SYSTEM.md`

For full navigation, start at `../DOCS_INDEX.md`.

---

## VNX CLI Quick Reference

```bash
# Initialize VNX in a new project
.claude/vnx-system/bin/vnx init

# Health check
.claude/vnx-system/bin/vnx doctor

# Launch orchestration (tmux session with T0-T3)
.claude/vnx-system/bin/vnx start

# Stop all processes
.claude/vnx-system/bin/vnx stop

# Update VNX from GitHub
.claude/vnx-system/bin/vnx update

# Token cost report
.claude/vnx-system/bin/vnx cost-report
```

### Key Bindings (in tmux)
- `Ctrl+G` — Open dispatch queue popup
- `Ctrl+B D` — Detach (keeps running)
- Mouse — Click to switch panes

### Demo Setup
```bash
# Create a demo project with 6 PRs, quality traps, and full VNX setup
bash /path/to/.claude/vnx-system/demo/setup_demo.sh
```

---

## Historical Notes (V7.1.2)

### System Overview

VNX is a multi-terminal orchestration system enabling parallel development across three tracks (A/B/C) with centralized control through T0. Built on tmux and Claude Code with template-based prompting and file-based message passing.

**Version 7.1.2 Enhanced State Monitoring**:
- **Terminal State Monitoring**: Real-time tracking of T0/T1/T2/T3 status and activity
- **Lock Prevention System**: Automatic stale lock cleanup and timeout mechanisms
- **Enhanced Dashboard**: Lock status visibility and terminal monitoring integration
- **Receipt Bulletproofing**: Comprehensive prevention and recovery for stuck receipts
- **Process Detection Fix**: Corrected Smart Tap V7 detection in dashboard monitoring

**Version 7.1.1 Critical Fixes**:
- **Singleton Enforcement**: Fixed race condition that allowed multiple Smart Tap processes
- **System Stability**: Eliminated quadruple process duplication and infinite loops
- **Popup Control**: Fixed scrollable popup support (90% height vs full screen)
- **Hash Deduplication**: Restored proper block processing to prevent duplicates

**Smart Tap V7 Features**:
- **JSON Dispatch Format**: Machine-readable dispatches with automatic Markdown translation
- **Enhanced Capture Window**: 200-line capture prevents [[DONE]] marker truncation
- **Non-Intrusive Notifications**: Audio/visual alerts without blocking terminals
- **Dual Format Support**: Both JSON and Markdown dispatches work seamlessly
- **25ms Translation**: High-performance JSON-to-Markdown conversion
- **JSON Archive**: Original JSON preserved in `.json/` directory

## Quick Start

```bash
# Launch complete system (V8.2)
cd $PROJECT_ROOT
.claude/vnx-system/bin/vnx start

# Key bindings in tmux
Ctrl+G      - Open dispatch queue popup
Ctrl+B D    - Detach (keeps running)
Mouse       - Click to switch panes

# Monitor system
tail -f .vnx-data/state/t0_receipts.ndjson       # Receipt stream
tail -f .vnx-data/logs/*.log                      # Process logs
cat .vnx-data/state/t0_brief.json | jq .          # T0 decision snapshot
cat .vnx-data/state/dashboard_status.json | jq .  # System metrics

# Stop system
.claude/vnx-system/bin/vnx stop
```

## System Architecture Diagrams

### Terminal Layout & Roles
```
┌────────────────────┬────────────────────┐
│    T0 - Brain      │    T1 - Track A    │
│    (Opus, R/O)     │    (Sonnet, R/W)   │
│    Pane: %0        │    Pane: %1        │
├────────────────────┼────────────────────┤
│    T2 - Track B    │    T3 - Deep Work  │
│    (Sonnet, R/W)   │    (Opus, R/W)     │
│    Pane: %2        │    Pane: %3        │
└────────────────────┴────────────────────┘
```

| Terminal | Role | Model | Permissions | Track | Pane ID |
|----------|------|-------|-------------|-------|---------|
| T0 | Orchestrator | Opus | Read-Only | - | %0 |
| T1 | Crawler Dev | Sonnet | Full R/W | A | %1 |
| T2 | Storage Dev | Sonnet | Full R/W | B | %2 |
| T3 | Deep Analysis | Opus | Full R/W | C | %3 |

### Complete Message Flow Diagram
```
┌─────────────────────────────────────────────────────────────────┐
│                     T0 (Orchestrator)                           │
│  1. Creates manager block                                       │
│  2. Writes to conversation                                      │
└────────────────────┬───────────────────────────────────────────┘
                     │
                     ▼ smart_tap_v7_json_translator.sh
              [Capture-pane every 2s]
                     │
                     ▼
        ┌────────────────────────┐
        │  dispatches/queue/     │◄── Manager blocks saved here
        │  {timestamp}-{track}.md│
        └────────────┬───────────┘
                     │
                     ▼ queue_popup_watcher.sh
              [Detects new files]
                     │
                     ▼
            ┌────────────────┐
            │  Popup UI      │◄── Human reviews (Ctrl+G)
            │  (curses)      │
            └────────┬───────┘
                     │ [A]ccept
                     ▼ dispatcher_v7_compilation.sh
              [Routes to terminal]
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
    ┌───────┐   ┌───────┐   ┌───────┐
    │  T1   │   │  T2   │   │  T3   │
    │Track A│   │Track B│   │Track C│
    └───┬───┘   └───┬───┘   └───┬───┘
        │           │           │
        ▼           ▼           ▼
    [Execute]   [Execute]   [Execute]
        │           │           │
        ▼           ▼           ▼
    Write .md   Write .md   Write .md
    report      report      report
        │           │           │
        ▼           ▼           ▼
    unified_reports/*.md
        │
        ▼ receipt_processor_v4.sh
   [Parses reports → receipts]
        │
        ▼
    t0_receipts.ndjson
                    │
                    ▼
    ┌──────────────────────────────┐
    │  Format receipt + footer     │
    │  Send to T0 pane (%0)        │
    └──────────────────────────────┘
                    │
                    ▼
              T0 receives
            Creates new block
              (cycle repeats)
```

## Core Script Operations

### 1. smart_tap_v7_json_translator.sh (V7 Enhancement)
**Purpose**: Monitors T0 for both JSON and Markdown dispatch blocks with automatic translation  
**Trigger**: Runs continuously, checks every 2 seconds  
**Method**: `tmux capture-pane -t %0` (last 50 lines)  
**Detection**: JSON (`{"dispatch_format":"json"`) or Markdown (`[[TARGET:` and `[[DONE]]`)  
**Translation**: JSON → Markdown conversion in 25ms average (jq + bash fallback)  
**Dual Storage**: Markdown for popup/terminal + JSON archive in `.json/` directory  
**Singleton**: Enforced via singleton_enforcer.sh  
**Output**: Always delivers Markdown format to queue for optimal processing  
**Features**: Automatic popup display with human-readable format  

### 2. queue_popup_watcher.sh
**Purpose**: Detects new dispatches and opens popup  
**Trigger**: File system watch on `dispatches/queue/`  
**Action**: Opens tmux popup when new files appear  
**Binding**: Creates Ctrl+G keybinding for manual access  

### 3. dispatcher_v7_compilation.sh (Production)
**Purpose**: Routes accepted dispatches to the correct worker and compiles the correct agent template  
**Trigger**: When a dispatch is accepted from the queue UI  
**Delivery**: Sends compiled prompt to the terminal pane via tmux  
**ACK**: ACK receipts are generated by `heartbeat_ack_monitor.py` (multi-signal detection)

### 4. receipt_processor_v4.sh (Primary receipt delivery)
**Purpose**: Converts new markdown reports into receipts and delivers them into T0  
**Trigger**: Detects new `.claude/vnx-system/unified_reports/*.md`  
**Writes**: Appends to `.claude/vnx-system/state/t0_receipts.ndjson`  
**Delivery**: tmux paste-buffer + double Enter for reliable submission (receipts to T0)

### 5. vnx_supervisor_simple.sh
**Purpose**: Bulletproof process management and health monitoring  
**Trigger**: Continuous monitoring every 10 seconds  
**Monitors**: dispatcher, smart_tap, receipt_processor, ack systems, queue_watcher, dashboard, state manager  
**Features**: Singleton supervisor, critical process restart, PID management  
**Locking**: Atomic directory creation (macOS compatible)

### 6. generate_valid_dashboard.sh
**Purpose**: Creates real-time status JSON  
**Trigger**: Runs every 1 second  
**Updates**: Process PIDs, queue depths, track status  
**Output**: `state/dashboard_status.json`

## Communication Protocols

### Dispatch Formats (T0 → Workers)

#### JSON Format V7 (Machine Readable with Auto-Translation)
```json
{
  "dispatch_format": "json",
  "version": "7.0",
  "metadata": {
    "track": "A",
    "dispatch_id": "20250915-094500-abc123",
    "timestamp": "2025-09-15T09:45:00Z",
    "priority": "high",
    "timeout": 300,
    "gate": "implementation",
    "phase": "sprint.3.2",
    "cognition": "focused"
  },
  "content": {
    "title": "Implement WebVitals Plugin",
    "objective": "Create modular plugin system for performance metrics",
    "context": "Part of A3-2 crawler architecture refactoring",
    "instructions": "1. Design plugin interface\n2. Implement WebVitalsPlugin\n3. Add registration system\n4. Update tests",
    "success_criteria": [
      "Plugin interface well-defined",
      "WebVitals fully implemented", 
      "Tests pass with >90% coverage",
      "Memory usage <20MB"
    ],
    "resources": [
      "docs/plugin-architecture.md",
      "src/crawler/plugins/README.md"
    ]
  }
}
```

**JSON Features**:
- **Automatic Translation**: Smart Tap V7 converts to Markdown for display
- **Dual Storage**: JSON archived in `.json/` directory for analytics
- **Rich Metadata**: Structured data for priority, timing, routing
- **25ms Translation**: High-performance conversion to human-readable format

#### Markdown Format (Legacy - Direct Processing)
```markdown
[[TARGET:A|B|C]]
Phase: current.phase.number
Doel: strategic objective with context
Instructions: [[@templates/agents/agent.md]]
Context: [[@docs/relevant.md]] [[@specs/feature.md]]
Gate: planning|implementation|review|testing|validation
Priority: P0|P1|P2; Cognition: normal|deep
[[DONE]]
```

**Markdown Features**:
- **Direct Processing**: No translation needed, immediate popup display
- **Human Readable**: Easy to write and understand
- **Template References**: Supports [[@path]] expansion
- **Backward Compatible**: All existing dispatches work unchanged

### Receipt Formats (Workers → T0)

#### ACK Receipt (Immediate - Within 5 seconds)
```json
{
  "event": "task_ack",
  "track": "A|B|C",
  "task_id": "dispatch-id-from-block",
  "status": "working",
  "timestamp": "2025-09-15T09:45:05Z"
}
```

#### Final Receipt (After Task Completion)
```json
{
  "event": "task_receipt",
  "run_id": "unique-identifier",
  "track": "A|B|C",
  "phase": "3.2",
  "gate": "implementation",
  "task_id": "unique-task-id",
  "status": "success|blocked|fail|working",
  "summary": "What was accomplished",
  "report_path": ".claude/vnx-system/unified_reports/{YYYYMMDD-HHMMSS}-T{N}-{TYPE}-{topic-slug}.md",
  "metrics": {
    "tests_passed": 100,
    "coverage": "95%",
    "custom_metric": "value"
  }
}
```

## Core Components

### Active Processes (Singleton Architecture)
1. **VNX Supervisor** - Process health monitoring (10s checks)
2. **Smart Tap V7** - Captures T0 dispatch blocks (JSON/MD translation)
3. **Dispatcher V8** - Routes to terminals via native skills (87% token reduction)
4. **Receipt Processor V4** - Report → receipt → T0 delivery + quality sidecar
5. **Queue Popup Watcher** - Auto-popup for new dispatches
6. **T0 Brief Generator** - <2KB decision snapshot for T0
7. **Dashboard Generator** - Real-time JSON status

### Bootstrap System
- **SessionStart Hooks** - Auto-load terminal context
- **Bootstrap Files** - Role-specific guidance per terminal
- **Sprint Docs** - Flexible folder at `T0/sprints/`

## Directory Structure (Consolidated State)

### Unified State Directory
```
.claude/vnx-system/state/         # Runtime state files
├── t0_receipts.ndjson           # Production receipts (ACK + completion)
├── progress.yaml                # Machine-owned progress/gates ledger
├── t0_brief.json                # Token-minimal T0 decision snapshot
├── drafts/                      # Worker report artifacts
│   └── IMPL-A3-2-*.md          # Implementation reports
├── panes.json                   # Terminal routing config
├── processed_receipts.txt       # Deduplication tracking
├── dashboard_status.json        # Live system metrics
├── heartbeat.timestamp          # Process monitoring
└── receipts_track_*.ndjson      # Legacy (not required)

.claude/state -> vnx-system/state # Symlink for backward compatibility
```

### Terminal Workspace Configuration
Each terminal starts in its own directory (T0-T3) for hook functionality:
```
.claude/terminals/T0/  # T0 starts here, accesses project via symlinks
.claude/terminals/T1/  # T1 starts here, full project access via **
.claude/terminals/T2/  # T2 starts here, full project access via **
.claude/terminals/T3/  # T3 starts here, full project access via **
```

### Orchestration System Directory
```
.claude/vnx-system/
├── scripts/                     # Core orchestration scripts
├── dispatches/                  # Task queue system
│   ├── queue/                  # Pending manager blocks
│   ├── accepted/               # Processed dispatches
│   └── rejected/               # Failed dispatches
├── state/                      # Consolidated state (see above)
└── reports/C/                  # Track C deep analysis
    └── latest.md               # Current investigation

.claude/terminals/
├── T0/
│   ├── CLAUDE.md               # Instructions
│   ├── bootstrap.md            # Quick reference
│   └── sprints/                # Dynamic sprint docs
└── T1-T3/
    ├── CLAUDE.md
    └── bootstrap.md
```

## Terminal Configuration

### Settings.json Files

#### T0 Settings (Read-Only Orchestrator)
```json
{
  "permissions": {
    "allow": ["Read", "Grep", "Glob", "mcp__sequential-thinking"],
    "ask": ["Write", "Edit", "MultiEdit", "Bash", "NotebookEdit"]
  },
  "sessionStartCommand": "bash .claude/hooks/sessionstart_bootpack.sh",
  "environment": {
    "CLAUDE_ROLE": "orchestrator",
    "CLAUDE_MODEL": "opus"
  }
}
```

#### T1/T2/T3 Settings (Worker Terminals)
```json
{
  "permissions": {
    "auto_allow": [
      "Read**", "Grep**", "Glob**",
      "mcp__sequential-thinking**"
    ],
    "ask": [
      "Write", "Edit", "MultiEdit", "Bash", 
      "NotebookEdit", "WebFetch", "WebSearch"
    ]
  },
  "sessionStartCommand": "bash .claude/hooks/sessionstart_worker.sh",
  "environment": {
    "CLAUDE_ROLE": "worker",
    "CLAUDE_TRACK": "A|B|C",
    "CLAUDE_MODEL": "sonnet|opus"
  }
}
```

### SessionStart Bootstrap System

#### How It Works
The project-level `.claude/settings.json` contains smart SessionStart and UserPromptSubmit hooks that:
1. Detect which terminal you're in based on `$PWD`
2. Run the appropriate hook (bootpack for T0, worker for T1-T3)
3. Work on both fresh session start AND a context reset command (`/clear` in Claude Code, `/new` in Codex CLI)

#### Important: Path Configuration
- **Hardcoded paths required**: Environment variables like `$CLAUDE_PROJECT_DIR` don't work in hooks
- **Project settings override**: The main `.claude/settings.json` takes precedence
- **Terminal detection**: Based on PWD ending with /T0, /T1, /T2, or /T3

#### T0 Bootstrap Hook (sessionstart_bootpack.sh)
```bash
#!/bin/bash
# Loads orchestrator context on startup and context reset
# Returns JSON with:
- Core instructions from CLAUDE.md
- Manager block template
- Sprint documentation from T0/sprints/
- Progress.yaml reference
- Read-only reminder
```

#### Worker Bootstrap Hook (sessionstart_worker.sh)
```bash
#!/bin/bash
# Detects terminal from PWD or environment
# Returns JSON with:
- Terminal-specific CLAUDE.md
- Bootstrap quick reference
- Current sprint documentation
- Critical reminders (output order, memory limits)
- Track-specific requirements
```

#### Hook Configuration
```json
// Project-level .claude/settings.json
"SessionStart": [{
  "hooks": [{
    "command": "if [[ \"$PWD\" == */T0 ]]; then /path/to/sessionstart_bootpack.sh; 
                elif [[ \"$PWD\" == */T[1-3] ]]; then /path/to/sessionstart_worker.sh; fi"
  }]
}]
```

### Bootstrap Content Per Terminal

#### T0 Bootstrap
- Manager block format enforcement
- Receipt → assess → dispatch workflow
- Sprint documentation access
- Read-only permissions emphasis
- Gate progression rules

#### T1 Bootstrap (Track A - Crawler)
- Output order: .md FIRST, receipt LAST
- Memory limit: <85MB
- Dutch market formats
- 405 tests requirement
- Plugin architecture reminders

#### T2 Bootstrap (Track B - Storage)
- RAG pipeline: multilingual-e5-small
- Query performance: <50ms p95
- GPT-4o-mini for keywords
- Supabase hybrid search
- Storage architecture patterns

#### T3 Bootstrap (Track C - Deep Work)
- Investigation workflow phases
- Report structure template
- Track C receipt requirements
- Cross-track analysis framework
- Evidence-based findings

## Critical Operating Rules

### Receipt Workflow (ALL Terminals)
1. **Receive dispatch** from popup/terminal
2. **Send ACK receipt** within 5 seconds (optional but recommended)
3. **Execute task** and write .md artifact
4. **Verify** file exists
5. **Send final receipt** with valid report_path

### Output Order for Final Receipt
1. Write .md artifact FIRST
2. Verify file exists
3. Write final receipt LAST with valid report_path

### Track C Targeting (T0)
Use [[TARGET:C]] for:
- Deep investigations requiring Opus
- Cross-track analysis
- Architecture decisions
- Complex problems beyond A/B

### Dutch Market Requirements
- KvK: 8 digits, modulus 11 check
- BTW: 21% (high), 9% (low), 0% (exempt)
- Format: €1.234,56
- Postcode: 1234 AB

## Troubleshooting

| Issue | Check | Solution |
|-------|-------|----------|
| Receipt not reaching T0 | `pgrep -f receipt_notifier` | Check panes.json mapping |
| Wrong TARGET | T0 decision tree | Review Track C criteria |
| Output order violation | Terminal bootstrap | Enforce .md before receipt |
| Process down | Heartbeat logs | Restart VNX_HYBRID_FINAL |

## Multi-line Input in Claude Code Terminals

When running Claude Code CLI inside tmux in iTerm2, the CLI captures keyboard input before tmux can process it, so shift+enter doesn't work. Use these solutions:

### Option 1: Ctrl+V, Ctrl+J (Recommended)
1. Type your first line
2. Press `Ctrl+V` then `Ctrl+J` to insert a literal newline
3. Continue typing your next line
4. Repeat for more lines

### Option 2: Backslash Continuation
```
This is line one \
and this is line two \
and this is line three
```

### Option 3: Triple Quotes
```
"""
This is line one
This is line two
This is line three
"""
```

## Success Metrics
- Receipt delivery: <2s
- Output compliance: 100%
- Track routing: 100% accurate
- Process uptime: 99.9%
- Bootstrap load: <100ms

---

*For architecture details see 00_VNX_ARCHITECTURE.md (V10.0) | For operations see ../operations/MONITORING_GUIDE.md*
