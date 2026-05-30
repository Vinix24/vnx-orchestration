# NDJSON Receipt Format V2 Specification
**Last Updated**: 2026-02-15
**Owner**: T-MANAGER
**Purpose**: Documentation for NDJSON Receipt Format V2 Specification.

**Updated**: 2025-09-15  
**Status**: Active
**Replaces**: Original format with gate/status/track  
**New**: Added ACK receipt support (V6 Dispatcher)

## Receipt Types

### 1. ACK Receipt (NEW - V6 Dispatcher)
Immediate acknowledgment receipt sent within 5 seconds of receiving a manager block.

```json
{
  "event": "task_ack",
  "track": "A|B|C",
  "status": "working|blocked",
  "task_id": "<optional>",
  "summary": "Task received, starting execution",
  "timestamp": "2025-09-15T07:00:00Z"
}
```

### 2. Task Complete Receipt (STANDARDIZED)
Final receipt sent after task completion.

## Format Definition

```json
{
  "event": "task_complete",
  "run_id": "<yyyyMMdd-hhmmss-phase>",
  "track": "A|B|C",
  "phase": "2.3|3.0|...", 
  "gate": "planning|implementation|review|testing|validation",
  "task_id": "A3-2_webvitals_core", 
  "cmd_id": "<uuid or hash>",
  "status": "ready|working|blocked|success|fail|offline",
  "summary": "<<=160 chars>",
  "report_path": "<optional: .claude/vnx-system/unified_reports/YYYYMMDD-HHMMSS-TN-TYPE-topic.md>",
  "metrics": { "optional_key": "value" }
}
```

## Field Descriptions

### Required Fields

- **event** (string): Receipt type
  - `task_ack` - Immediate acknowledgment receipt (NEW)
  - `task_complete` - Task completion receipt (STANDARDIZED)
- **run_id** (string): Unique identifier for this run, format: `yyyyMMdd-hhmmss-phase`
  - Example: `20250908-142000-A3-2`
- **track** (string): Track identifier
  - `A` - Crawler (T1)
  - `B` - Storage (T2)
  - `C` - Infrastructure/Deep work (T3)
- **phase** (string): Sprint phase number (e.g., "2.3", "3.0")
- **gate** (string): Current quality gate
  - `planning` - Requirements and design
  - `implementation` - Code development
  - `review` - Code review and quality
  - `testing` - Test execution
  - `validation` - Final validation
- **task_id** (string): Unique task identifier
  - Format: `[Track][Sprint]-[SubTask]_[description]`
  - Example: `A3-2_webvitals_core`
- **cmd_id** (string): Command/dispatch identifier (UUID or hash)
- **status** (string): Task execution status
  - `ready` - Task prepared, awaiting execution
  - `working` - Task in progress
  - `blocked` - Cannot proceed due to dependency
  - `success` - Task completed successfully
  - `fail` - Task failed
  - `offline` - Terminal unavailable
- **summary** (string): Brief description (max 160 characters)

### Optional Fields

- **report_path** (string): Path to detailed report file
  - Format: `.claude/reports/track-[x]/[TYPE]-[TASK_ID]-[TIMESTAMP].md`
- **metrics** (object): Task-specific metrics
  - Track A: `tests_passed`, `tests_failed`, `memory_mb`, `coverage`
  - Track B: `query_time_ms`, `throughput_per_sec`, `cache_hit_rate`
  - Track C: `findings_count`, `critical_issues`, `confidence`
- **provenance** (object): Git repository state at receipt creation time (Phase 1A)
  - **git_ref** (string): SHA of HEAD commit (40 hex characters)
  - **branch** (string): Active branch name
  - **is_dirty** (boolean): True if uncommitted changes exist
  - **dirty_files** (integer): Count of modified/new files
  - **diff_summary** (object|null): Line change statistics (null if clean)
    - **files_changed** (integer): Number of changed files
    - **insertions** (integer): Lines added
    - **deletions** (integer): Lines removed
  - **captured_at** (string): ISO 8601 timestamp when captured
  - **captured_by** (string): Component identifier ("receipt_processor")
- **session** (object): Session metadata for usage tracking (Phase 1B)
  - **session_id** (string): Unique session identifier for usage correlation
  - **terminal** (string): Terminal name (T0, T1, T2, T3, etc.)
  - **model** (string): Model name (e.g., "claude-sonnet-4.6", "gemini-pro")
  - **provider** (string): Provider type ("claude_code", "gemini_cli", "codex_cli")
  - **captured_at** (string): ISO 8601 timestamp when captured

## Receipt Workflow (V6 Dispatcher)

### Two-Phase Receipt Pattern
```
1. Manager Block Received → Send ACK Receipt (within 5 seconds)
                        → Status: "working" or "blocked"
                        
2. Task Execution       → Work on task
                        
3. Task Complete        → Send Final Receipt
                        → Status: "success" or "fail"
```

## Status Transitions

```
ready → working → success
              ↘ → fail
              ↘ → blocked
                     ↓
                  ready (unblocked)

offline (terminal disconnected)
```

## Gate Progression Based on Status

- **success** → Progress to next gate
- **blocked** → Stay in current gate, await resolution
- **fail** → Return to completed gate or planning
- **working** → Stay in current gate, monitor progress
- **ready** → Stay in current gate, awaiting execution
- **offline** → Redistribute work to available terminals

## Example Receipts

### ACK Receipt Examples (NEW)

#### Immediate Acknowledgment - Track A
```json
{
  "event": "task_ack",
  "track": "A",
  "status": "working",
  "summary": "Task received, starting A3-2 WebVitals implementation",
  "timestamp": "2025-09-15T07:00:00Z"
}
```

#### Blocked ACK - Track B
```json
{
  "event": "task_ack",
  "track": "B",
  "status": "blocked",
  "summary": "Cannot accept: Currently processing critical migration",
  "timestamp": "2025-09-15T07:00:05Z"
}
```

### Final Receipt Examples

### Track A (Crawler) - Success
```json
{
  "event": "task_complete",
  "run_id": "20250908-142000-A3-2",
  "track": "A",
  "phase": "3.2",
  "gate": "implementation",
  "task_id": "A3-2_webvitals_core",
  "cmd_id": "a8e41fe0-1211-490a",
  "status": "success",
  "summary": "WebVitals implementation complete, all 405 tests passing, memory 72MB",
  "report_path": ".claude/vnx-system/unified_reports/20250908-142000-T1-IMPL-feature-implementation.md",
  "metrics": {
    "tests_passed": 103,
    "tests_failed": 0,
    "memory_mb": 72,
    "coverage": 92
  }
}
```

### Track B (Storage) - Working
```json
{
  "event": "task_complete",
  "run_id": "20250908-143000-B2-1",
  "track": "B",
  "phase": "2.1",
  "gate": "implementation",
  "task_id": "B2-1_rag_pipeline_optimization",
  "cmd_id": "b5df8da2-b6b1-4714",
  "status": "working",
  "summary": "RAG pipeline optimization in progress, 60% complete",
  "metrics": {
    "progress_percent": 60,
    "current_step": "index_optimization"
  }
}
```

### Track C (Infrastructure) - Blocked
```json
{
  "event": "task_complete",
  "run_id": "20250908-102355-C1-1",
  "track": "C",
  "phase": "1.1",
  "gate": "review",
  "task_id": "C1-1_rag_architecture_review",
  "cmd_id": "c45d3cdc-1e65-4cb7",
  "status": "blocked",
  "summary": "Architecture review blocked awaiting Track B schema finalization",
  "report_path": ".claude/vnx-system/unified_reports/20250922-143000-T3-REVIEW-system-analysis.md"
}
```

### Enhanced Receipt with Provenance and Session (Phase 1A+1B)

```json
{
  "event": "task_complete",
  "run_id": "20260215-143000-A3-2",
  "track": "A",
  "phase": "5.0",
  "gate": "implementation",
  "task_id": "A3-2_receipt_upgrade",
  "cmd_id": "f8d91ac2-4e3f-429b",
  "status": "success",
  "summary": "Implemented git provenance and session metadata capture for receipts",
  "report_path": ".claude/vnx-system/unified_reports/20260215-143000-T1-IMPL-receipt-upgrade.md",
  "metrics": {
    "tests_passed": 12,
    "performance_impact_ms": 25
  },
  "provenance": {
    "git_ref": "e3a4b26a6e91ccc1122293ca41bd8f2f71d6553b",
    "branch": "feature/pr3-registry-merge",
    "is_dirty": true,
    "dirty_files": 68,
    "diff_summary": {
      "files_changed": 26,
      "insertions": 1677,
      "deletions": 618
    },
    "captured_at": "2026-02-15T14:30:00Z",
    "captured_by": "receipt_processor"
  },
  "session": {
    "session_id": "abc-def-123-456",
    "terminal": "T1",
    "model": "claude-sonnet-4.6",
    "provider": "claude_code",
    "captured_at": "2026-02-15T14:30:00Z"
  }
}
```

## File Locations

**Canonical production receipt log** (what T0 reads):
- `.claude/vnx-system/state/t0_receipts.ndjson`

**Legacy/compatibility files** (may exist but are not required for production):
- `.claude/vnx-system/state/receipts_track_*.ndjson`
- `.claude/vnx-system/state/unified_receipts.ndjson`

**Event naming**:
- Target canonical field: `event_type` (parsers may also accept legacy `event` during migration)

## Changes from V1

| Field | V1 Format | V2 Format |
|-------|-----------|-----------|
| event | (not present) | "task_complete" |
| run_id | Simple timestamp | Phase-aware ID |
| phase | (not present) | Sprint phase number |
| task_id | (not present) | Structured task ID |
| cmd_id | (not present) | Dispatch tracking |
| status | ok/blocked/warning | 6 distinct states |
| artifact_path | Used | Renamed to report_path |
| ts | ISO timestamp | (removed - in run_id) |

## Integration Points

### Gates Controller
- Reads status field to determine gate transitions
- Uses phase to track sprint progress
- Creates new dispatches based on gate progression

### Receipt Notifier
- Parses all fields for T0 notification
- Highlights task_id and phase for context
- Maps status to action recommendations

### T0 Orchestrator
- Uses status to determine next manager block
- Tracks run_id for session continuity
- Reads report_path for detailed analysis

---

*This format tracks task status, sprint phase, and provides a structured audit trail.*
