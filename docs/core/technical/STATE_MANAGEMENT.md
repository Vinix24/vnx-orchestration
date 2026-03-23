# VNX State Management System - Technical Reference
**Last Updated**: 2026-02-05
**Owner**: T-MANAGER
**Purpose**: Documentation for VNX State Management System - Technical Reference.

**Version**: 2.0
**Date**: 2026-01-26
**Author**: T-MANAGER
**Status**: Active

---

## Overview

The VNX State Management System provides atomic, auditable state tracking for terminal orchestration through a **progress state ledger** (`progress_state.yaml`) and **receipt pipeline** that automatically maintains gate progression and status tracking.

### Purpose

- **Machine Orchestration State**: Predictable, atomic updates for automated decision-making
- **Receipt-Driven Updates**: Automatic state progression based on terminal task completion
- **Gate Progression Tracking**: Systematic advancement through development lifecycle gates
- **Audit Trail**: Complete history of state transitions with timestamps and sources

### Design Principles

1. **Separation of Concerns**: Machine state (progress_state.yaml) vs. human narrative (progress.yaml)
2. **Atomic Updates**: Thread-safe, atomic file writes prevent corruption
3. **Receipt-Driven**: State changes triggered by terminal task completion receipts
4. **Conservative Defaults**: Never overwrite data unless explicitly directed
5. **Audit Trail**: Last 10 transitions tracked per track with timestamps

---

## System Architecture

### Component Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                       Terminal Executes Task                         │
│                   Writes Report to unified_reports/                  │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    receipt_processor_v4.sh                           │
│              Monitors Reports → Generates Receipts                   │
│              Appends to t0_receipts.ndjson                          │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                   update_progress_state.py                           │
│                 Atomic State Mutations                               │
│          Reads Progress State → Applies Changes → Writes             │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     progress_state.yaml                              │
│         Track A/B/C: gate | status | dispatch_id | receipts         │
│                      Last 10 Transitions                            │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    generate_t0_brief.sh                              │
│              Reads State → Computes Next Gates                       │
│                   Writes t0_brief.json                              │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       T0 Orchestrator                                │
│              Reads Brief → Makes Dispatch Decisions                  │
└──────────────────────────────────────────────────────────────────────┘
```

### State Flow Lifecycle

```
1. Dispatcher assigns task
   ├─ Calls update_progress_state.py --track A --status working --dispatch-id XXX
   ├─ Sets gate from dispatch metadata
   └─ Adds transition to history

2. Terminal processes task
   └─ Writes markdown report to unified_reports/

3. Receipt processor parses report
   ├─ Extracts structured receipt with event_type/status
   ├─ Appends to t0_receipts.ndjson
   └─ Calls update_progress_state.py

4. State update based on receipt
   ├─ task_complete + success:
   │   ├─ Advance gate (implementation → review)
   │   ├─ Clear active_dispatch_id
   │   └─ Set status = idle
   │
   ├─ task_started:
   │   ├─ Record receipt metadata
   │   └─ Keep status = working
   │
   └─ task_complete + failure:
       ├─ Set status = blocked
       ├─ Preserve dispatch_id
       └─ Record receipt for troubleshooting

5. T0 reads brief
   └─ Sees track is idle at 'review' gate → dispatches review task
```

---

## Progress State Schema

### YAML Structure

**File**: `.claude/vnx-system/state/progress_state.yaml`

```yaml
version: "1.0"
updated_at: "2026-01-26T12:00:00+00:00"
updated_by: "dispatcher|receipt_processor|sync_from_receipts"

tracks:
  A:  # Track A (T1 - Crawler)
    current_gate: "implementation"
    status: "working"
    active_dispatch_id: "20260126-120000-abc123-A"

    last_receipt:
      event_type: "task_started"
      status: "confirmed"
      timestamp: "2026-01-26T12:00:00+00:00"
      dispatch_id: "20260126-120000-abc123-A"

    history:
      - timestamp: "2026-01-26T12:00:00+00:00"
        gate: "implementation"
        status: "working"
        dispatch_id: "20260126-120000-abc123-A"
        updated_by: "dispatcher"

      - timestamp: "2026-01-26T11:50:00+00:00"
        gate: "planning"
        status: "idle"
        dispatch_id: null
        updated_by: "receipt_processor"

      # ... Last 10 transitions ...

  B:  # Track B (T2 - Storage) - Same structure
    current_gate: "review"
    status: "idle"
    active_dispatch_id: null
    last_receipt: { ... }
    history: [ ... ]

  C:  # Track C (T3 - Infrastructure) - Same structure
    current_gate: "testing"
    status: "blocked"
    active_dispatch_id: "20260126-110000-xyz789-C"
    last_receipt: { ... }
    history: [ ... ]
```

### Field Definitions

#### Top-Level Fields

| Field | Type | Description |
|-------|------|-------------|
| `version` | string | Schema version (currently "1.0") |
| `updated_at` | ISO timestamp | Last modification time (UTC with timezone) |
| `updated_by` | enum | Source of last update: `dispatcher`, `receipt_processor`, `sync_from_receipts` |

#### Track Fields

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `current_gate` | enum | See Gate Progression | Current development lifecycle gate |
| `status` | enum | `idle`, `working`, `blocked` | Track operational status |
| `active_dispatch_id` | string or null | Format: `YYYYMMDD-HHMMSS-hash-track` | Currently executing dispatch (null if idle) |
| `last_receipt` | object | Receipt metadata | Most recent receipt received from terminal |
| `history` | array | Last 10 transitions | State transition audit trail |

#### Last Receipt Object

| Field | Type | Description |
|-------|------|-------------|
| `event_type` | string | `task_complete`, `task_started`, `task_blocked` |
| `status` | string | `success`, `blocked`, `failed`, `confirmed` |
| `timestamp` | ISO timestamp | Receipt creation time |
| `dispatch_id` | string | Associated dispatch ID |

#### History Entry Object

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | ISO timestamp | When transition occurred |
| `gate` | string | Gate at time of transition |
| `status` | string | Status at time of transition |
| `dispatch_id` | string or null | Active dispatch at time of transition |
| `updated_by` | string | Component that made the update |

---

## State Transitions

### Gate Progression Model

VNX uses a **semantic gate ladder** for orchestration:

```
investigation → planning → implementation → review → testing →
integration → quality_gate → planning (next cycle)
```

**Gate Definitions**:

| Gate | Purpose | Typical Activities | Exit Criteria |
|------|---------|-------------------|---------------|
| `investigation` | Problem analysis | Root cause analysis, feasibility study | Investigation report with findings |
| `planning` | Solution design | Architecture design, task breakdown | Implementation plan approved |
| `implementation` | Code development | Feature development, bug fixes | Code complete, unit tests passing |
| `review` | Code review | Peer review, design validation | Review approval, no blockers |
| `testing` | Quality validation | Integration tests, E2E tests | All tests passing, quality gates met |
| `integration` | System integration | Merge to main, deployment prep | Integration successful, no conflicts |
| `quality_gate` | Final validation | Production readiness check | All quality criteria met |

**Special Gates**:
- `validation`: Alternate completion gate (progresses to `planning`)
- `escalation`: Requires T0 decision (no automatic progression)

### Gate Progression Logic

**Implementation**: `update_progress_state.py` lines 40-50

```python
GATE_PROGRESSION = {
    'investigation': 'planning',
    'planning': 'implementation',
    'implementation': 'review',
    'review': 'testing',
    'testing': 'integration',
    'integration': 'quality_gate',
    'quality_gate': 'planning',  # Next cycle
    'validation': 'planning',     # Alternate completion
    'escalation': None,           # Requires T0 decision
}
```

**Advancement Rules**:
1. Gate only advances on `task_complete` + `status=success` receipt
2. Advancement uses `GATE_PROGRESSION` dictionary lookup
3. If gate not in dictionary, no advancement (stays at current gate)
4. `escalation` gate requires manual T0 intervention

### Status State Machine

```
┌─────────┐
│  idle   │ ◄─────────────────────────────┐
└────┬────┘                                │
     │ dispatcher assigns task             │
     │ (sets dispatch_id)                  │
     ▼                                     │
┌─────────┐                                │
│ working │                                │
└────┬────┘                                │
     │                                     │
     ├─► task_complete + success ──────────┤
     │   (advance gate, clear dispatch_id) │
     │                                     │
     ├─► task_complete + failure ──────────┤
     │   (goto blocked)                    │
     │                                     │
     └─► task_started                      │
         (stay working, record receipt)    │
                                           │
┌─────────┐                                │
│ blocked │ ───────────────────────────────┘
└─────────┘   manual intervention or
              retry dispatch
```

**Status Definitions**:

| Status | Meaning | Triggers | Next Steps |
|--------|---------|----------|------------|
| `idle` | Available for work | Receipt processor on task_complete success | Dispatcher assigns new task |
| `working` | Task in progress | Dispatcher on assignment | Wait for receipt |
| `blocked` | Waiting on intervention | Receipt processor on failure/blocked | Manual investigation or retry |

### Receipt-Driven State Updates

**Implementation**: `receipt_processor_v4.sh` lines 286-364

#### Event: task_complete + status=success

**Triggers**:
- Terminal successfully completes task
- Receipt has `event_type: task_complete` AND `status: success`

**State Changes**:
```bash
update_progress_state.py \
  --track A \
  --advance-gate \
  --status idle \
  --dispatch-id "" \
  --receipt-event task_complete \
  --receipt-status success \
  --receipt-timestamp "2026-01-26T12:00:00Z" \
  --receipt-dispatch-id "20260126-120000-abc123-A" \
  --updated-by receipt_processor
```

**Result**:
- Gate advances (e.g., `implementation` → `review`)
- Status set to `idle`
- `active_dispatch_id` cleared (set to null)
- `last_receipt` updated with completion metadata
- History entry added

#### Event: task_started

**Triggers**:
- Terminal acknowledges task and begins work
- Receipt has `event_type: task_started`

**State Changes**:
```bash
update_progress_state.py \
  --track A \
  --receipt-event task_started \
  --receipt-status confirmed \
  --receipt-timestamp "2026-01-26T12:00:00Z" \
  --receipt-dispatch-id "20260126-120000-abc123-A" \
  --updated-by receipt_processor
```

**Result**:
- Status remains `working` (dispatcher already set this)
- `last_receipt` updated with start confirmation
- NO gate advancement
- NO dispatch_id change
- History entry added

#### Event: task_complete + status=failure|blocked

**Triggers**:
- Terminal encounters blocker or failure
- Receipt has `event_type: task_complete` AND `status: blocked|failed|error`

**State Changes**:
```bash
update_progress_state.py \
  --track A \
  --status blocked \
  --dispatch-id "20260126-120000-abc123-A" \
  --receipt-event task_complete \
  --receipt-status blocked \
  --receipt-timestamp "2026-01-26T12:00:00Z" \
  --receipt-dispatch-id "20260126-120000-abc123-A" \
  --updated-by receipt_processor
```

**Result**:
- Status set to `blocked`
- Gate remains unchanged
- `active_dispatch_id` preserved (for retry)
- `last_receipt` updated with failure details
- History entry added

---

## Receipt Pipeline Integration

### Receipt Types and State Impact

| Receipt Type | event_type | status | State Impact |
|--------------|-----------|--------|--------------|
| **ACK** | `task_started` | `confirmed` | Record receipt, keep status=working |
| **Success** | `task_complete` | `success` | Advance gate, set idle, clear dispatch |
| **Failure** | `task_complete` | `failed` | Set blocked, preserve dispatch |
| **Blocked** | `task_complete` | `blocked` | Set blocked, preserve dispatch |
| **Error** | `task_complete` | `error` | Set blocked, preserve dispatch |

### Receipt Processing Flow

**Source**: `receipt_processor_v4.sh` lines 286-364

```bash
# 1. Extract receipt metadata
event_type=$(echo "$receipt_json" | jq -r '.event_type // .event // empty')
receipt_status=$(echo "$receipt_json" | jq -r '.status // empty')
receipt_ts=$(echo "$receipt_json" | jq -r '.timestamp // empty')
receipt_dispatch_id=$(echo "$receipt_json" | jq -r '.dispatch_id // empty')

# 2. Map terminal to track
case "$terminal" in
    T1) track="A" ;;
    T2) track="B" ;;
    T3) track="C" ;;
    *) track="" ;;  # T-MANAGER doesn't map to track
esac

# 3. Apply state update based on receipt type
if [ "$event_type" = "task_complete" ] && [ "$receipt_status" = "success" ]; then
    # Success: advance gate, clear dispatch, set idle
    update_progress_state.py \
        --track "$track" \
        --advance-gate \
        --status idle \
        --dispatch-id "" \
        --receipt-event "$event_type" \
        --receipt-status "$receipt_status" \
        --receipt-timestamp "$receipt_ts" \
        --receipt-dispatch-id "$receipt_dispatch_id" \
        --updated-by receipt_processor

elif [ "$event_type" = "task_started" ]; then
    # Task started: record receipt only
    update_progress_state.py \
        --track "$track" \
        --receipt-event "$event_type" \
        --receipt-status "$receipt_status" \
        --receipt-timestamp "$receipt_ts" \
        --receipt-dispatch-id "$receipt_dispatch_id" \
        --updated-by receipt_processor

else
    # Failed/blocked: set blocked status
    update_progress_state.py \
        --track "$track" \
        --status blocked \
        --dispatch-id "$receipt_dispatch_id" \
        --receipt-event "$event_type" \
        --receipt-status "$receipt_status" \
        --receipt-timestamp "$receipt_ts" \
        --receipt-dispatch-id "$receipt_dispatch_id" \
        --updated-by receipt_processor
fi
```

### Receipt Validation

**Receipts must contain**:
- `event_type` or `event` field (task_complete, task_started, task_blocked)
- `status` field (success, blocked, failed, confirmed, error)
- `timestamp` field (ISO 8601 format)
- `dispatch_id` field (if applicable)
- `terminal` field (T1, T2, T3)

**Missing fields**: Receipt still recorded but state update may be skipped

---

## State Management Scripts

### update_progress_state.py

**Purpose**: Atomic, type-safe updates to progress_state.yaml

**Location**: `.claude/vnx-system/scripts/update_progress_state.py`

**Core Operations**:

#### 1. Set Gate

```bash
update_progress_state.py --track A --gate implementation
```

**Result**: Sets `current_gate` to specified gate (no validation, allows any string)

#### 2. Advance Gate

```bash
update_progress_state.py --track A --advance-gate
```

**Result**:
- Looks up current gate in `GATE_PROGRESSION` dictionary
- Sets `current_gate` to next gate
- If current gate not in dictionary, no change
- Logs warning if progression undefined

#### 3. Set Status

```bash
update_progress_state.py --track A --status idle
```

**Result**: Sets `status` to `idle`, `working`, or `blocked`

#### 4. Set Dispatch ID

```bash
update_progress_state.py --track A --dispatch-id "20260126-120000-abc123-A"
```

**Result**:
- Sets `active_dispatch_id`
- Use empty string `""` to clear (sets to null in YAML)

#### 5. Record Receipt

```bash
update_progress_state.py --track A \
  --receipt-event task_complete \
  --receipt-status success \
  --receipt-timestamp "2026-01-26T12:00:00Z" \
  --receipt-dispatch-id "20260126-120000-abc123-A"
```

**Result**: Updates `last_receipt` object with metadata

#### 6. Combined Operations

```bash
# Complete success (typical receipt processor call)
update_progress_state.py --track A \
  --advance-gate \
  --status idle \
  --dispatch-id "" \
  --receipt-event task_complete \
  --receipt-status success \
  --receipt-timestamp "2026-01-26T12:00:00Z" \
  --receipt-dispatch-id "20260126-120000-abc123-A" \
  --updated-by receipt_processor
```

**Atomicity Guarantee**:

```python
# Lines 192-209
def save_state(self):
    """Atomic write using temp file + rename"""
    temp_path = f"{self.state_file}.tmp"

    try:
        # 1. Write to temp file
        with open(temp_path, 'w') as f:
            yaml.dump(self.state, f, default_flow_style=False, sort_keys=False)

        # 2. Atomic rename
        os.rename(temp_path, self.state_file)

    except Exception as e:
        # 3. Cleanup on failure
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
```

**History Tracking**:
- Each update adds entry to `history` array
- Maintains last 10 transitions
- Older entries automatically pruned
- Includes: timestamp, gate, status, dispatch_id, updated_by

### sync_progress_state_from_receipts.py

**Purpose**: Rebuild or repair progress_state.yaml from receipt history

**Location**: `.claude/vnx-system/scripts/sync_progress_state_from_receipts.py`

**Use Cases**:
1. **Disaster Recovery**: Rebuild state file from scratch
2. **State Repair**: Fix corrupted or inconsistent state
3. **Validation**: Verify state matches receipt history
4. **Backfill**: Initialize state for existing system

**Core Logic**:

```python
# Lines 160-198: Read receipt history
def get_latest_receipts_per_track(self):
    """Extract most recent receipt for each track"""

    # Read last 5000 lines from t0_receipts.ndjson
    receipts = self.read_receipts(lines=5000)

    # Map terminal to track
    terminal_to_track = {'T1': 'A', 'T2': 'B', 'T3': 'C'}

    # Find latest receipt per track
    latest_receipts = {}
    for receipt in receipts:
        terminal = receipt.get('terminal')
        track = terminal_to_track.get(terminal)

        if track:
            # Only use receipt if newer than current state
            current_ts = self.state['tracks'][track].get('last_receipt', {}).get('timestamp')
            receipt_ts = receipt.get('timestamp')

            if not current_ts or receipt_ts > current_ts:
                latest_receipts[track] = receipt

    return latest_receipts
```

**State Application Rules**:

```python
# Lines 206-245: Apply receipt to state
def apply_receipt_to_track(self, track, receipt):
    """Conservative state update from receipt"""

    event_type = receipt.get('event_type')
    status = receipt.get('status')
    dispatch_id = receipt.get('dispatch_id')

    # task_started → working
    if event_type == 'task_started':
        self.state['tracks'][track]['status'] = 'working'
        if not self.state['tracks'][track].get('active_dispatch_id'):
            self.state['tracks'][track]['active_dispatch_id'] = dispatch_id

    # task_complete + success → idle, advance gate
    elif event_type == 'task_complete' and status == 'success':
        self.state['tracks'][track]['status'] = 'idle'
        self.state['tracks'][track]['active_dispatch_id'] = None

        # Advance gate if progression defined
        current_gate = self.state['tracks'][track].get('current_gate')
        next_gate = GATE_PROGRESSION.get(current_gate)
        if next_gate:
            self.state['tracks'][track]['current_gate'] = next_gate

    # task_complete + failure → blocked
    elif event_type == 'task_complete' and status in ['failed', 'blocked', 'error']:
        self.state['tracks'][track]['status'] = 'blocked'
        # Preserve dispatch_id for retry

    # Update last_receipt
    self.state['tracks'][track]['last_receipt'] = {
        'event_type': event_type,
        'status': status,
        'timestamp': receipt.get('timestamp'),
        'dispatch_id': dispatch_id
    }
```

**CLI Usage**:

```bash
# Dry-run (show what would change)
python3 sync_progress_state_from_receipts.py

# Apply changes
python3 sync_progress_state_from_receipts.py --apply

# Force overwrite dispatch IDs (dangerous)
python3 sync_progress_state_from_receipts.py --apply --force-dispatch-id

# Sync only one track
python3 sync_progress_state_from_receipts.py --apply --only-track A

# Use last N receipts (default 5000)
python3 sync_progress_state_from_receipts.py --apply --receipt-lines 10000
```

**Safety Features**:
- **Conservative by default**: Only updates if receipt is newer than current state
- **Dispatch ID protection**: Never overwrites existing `active_dispatch_id` unless `--force-dispatch-id`
- **Dry-run mode**: Shows changes without applying (default)
- **Per-track sync**: Can limit updates to single track
- **Atomic writes**: Uses temp file + rename pattern

---

## Track Management

### Track Assignment

**Terminal to Track Mapping**:
```
T1 (Crawler)        → Track A
T2 (Storage)        → Track B
T3 (Infrastructure) → Track C
T-MANAGER           → No track (doesn't update progress_state.yaml)
```

**Mapping Logic** (from receipt_processor_v4.sh lines 293-299):
```bash
case "$terminal" in
    T1) track="A" ;;
    T2) track="B" ;;
    T3) track="C" ;;
    *) track="" ;;  # Skip state update for T-MANAGER
esac
```

### Track Lifecycle

#### 1. Idle State (Available for Work)

**Characteristics**:
- `status: idle`
- `active_dispatch_id: null`
- Waiting for dispatcher assignment

**How to Reach**:
- System initialization (default state)
- Task completes successfully (receipt_processor advances gate + sets idle)

#### 2. Working State (Task in Progress)

**Characteristics**:
- `status: working`
- `active_dispatch_id: "YYYYMMDD-HHMMSS-hash-track"`
- Terminal executing assigned task

**How to Reach**:
- Dispatcher assigns task (sets working + dispatch_id + gate)

**Duration**:
- From assignment until receipt received
- Typical: 5-30 minutes for implementation tasks
- Can be hours for complex investigations

#### 3. Blocked State (Waiting on Intervention)

**Characteristics**:
- `status: blocked`
- `active_dispatch_id` preserved
- Requires manual investigation or retry

**How to Reach**:
- Receipt processor receives failure/blocked receipt
- Manual state update for known blockers

**Recovery**:
- Manual investigation and fix
- Retry dispatch (dispatcher can reassign same task)
- Escalation to T0 for decision

### Dispatch ID Lifecycle

**Format**: `YYYYMMDD-HHMMSS-{hash}-{track}`

**Example**: `20260126-120000-abc123def456-A`

**Lifecycle**:
```
1. Dispatcher generates ID
   ├─ Timestamp: YYYYMMDD-HHMMSS
   ├─ Hash: random 8-char hex
   └─ Track: A|B|C

2. Dispatcher sets in progress_state.yaml
   └─ active_dispatch_id = "20260126-120000-abc123def456-A"

3. Terminal receives dispatch
   └─ Includes dispatch_id in report metadata

4. Receipt processor reads report
   └─ Extracts dispatch_id from receipt

5. State update based on outcome
   ├─ Success: Clear dispatch_id (set to null)
   └─ Failure: Preserve dispatch_id (for retry)
```

**Null vs Empty String**:
- `null` (YAML) = No active dispatch
- Empty string `""` (CLI) = Converted to `null` by update script

---

## Gate Progression

### Gate Sequence

**Standard Flow**:
```
investigation → planning → implementation → review → testing →
integration → quality_gate → planning (next cycle)
```

**Alternate Paths**:
```
validation → planning (quick validation path)
escalation → (requires T0 manual decision)
```

### Quality Gates

Each gate has **exit criteria** before progression:

| Gate | Entry Criteria | Exit Criteria | Typical Artifacts |
|------|---------------|---------------|-------------------|
| **investigation** | Problem identified | Root cause found, solution proposed | Investigation report, RCA |
| **planning** | Solution approved | Implementation plan complete | Architecture doc, task breakdown |
| **implementation** | Plan approved | Code complete, unit tests pass | Working code, unit tests |
| **review** | Implementation done | Code reviewed, no blockers | Review comments, approval |
| **testing** | Review approved | Integration tests pass | Test reports, coverage |
| **integration** | Tests pass | Merged to main, no conflicts | Merge commit, CI logs |
| **quality_gate** | Integration done | All quality criteria met | QA signoff, metrics |

### Gate Advancement Rules

**Automatic Advancement**:
- Triggered by `task_complete` + `status=success` receipt
- Uses `GATE_PROGRESSION` dictionary
- Logged by receipt_processor with ✅ indicator

**Manual Advancement**:
- Use `update_progress_state.py --track A --gate <gate>`
- Required for `escalation` gate (no automatic progression)
- Emergency use for stuck states

**No Advancement**:
- `task_started` receipts (just record metadata)
- `task_complete` + failure/blocked (set blocked status)
- Gates not in progression dictionary

### Gate Validation

**Progress State Spec** (PROGRESS_STATE_SPEC.md lines 74-85):

**Valid Gates**:
- investigation, planning, implementation, review, testing, integration, quality_gate, validation, escalation

**Invalid Gates**:
- Script allows any string (no validation)
- T0 brief may warn about unknown gates

**Recommendation**: Stick to standard gate names for consistency

---

## Conflict Resolution

### Common Conflicts

#### 1. State vs Receipt Mismatch

**Symptom**: progress_state.yaml shows "working" but no recent receipts

**Cause**: Receipt lost or processor crashed before state update

**Detection**:
```bash
# Check state
cat .claude/vnx-system/state/progress_state.yaml | grep -A 5 "track: A"

# Check recent receipts
tail -20 .claude/vnx-system/state/t0_receipts.ndjson | jq 'select(.terminal=="T1")'
```

**Resolution**:
```bash
# Option 1: Reconcile from receipts
python3 .claude/vnx-system/scripts/sync_progress_state_from_receipts.py --apply --only-track A

# Option 2: Manual reset to idle
python3 .claude/vnx-system/scripts/update_progress_state.py --track A --status idle --dispatch-id ""
```

#### 2. Multiple Active Dispatch IDs

**Symptom**: progress_state.yaml has dispatch_id but new dispatch sent

**Cause**: Previous task never completed or receipt lost

**Detection**:
```bash
# Check active dispatch
cat .claude/vnx-system/state/progress_state.yaml | grep active_dispatch_id

# Check if dispatch file exists
ls -la .claude/vnx-system/dispatches/active/
```

**Resolution**:
```bash
# Option 1: Clear old dispatch (if confirmed complete)
python3 .claude/vnx-system/scripts/update_progress_state.py --track A --dispatch-id "" --status idle

# Option 2: Reconcile from receipts
python3 .claude/vnx-system/scripts/sync_progress_state_from_receipts.py --apply --force-dispatch-id
```

#### 3. Corrupted progress_state.yaml

**Symptom**: YAML parse errors, missing fields, invalid structure

**Cause**: Manual edit error, disk corruption, interrupted write

**Detection**:
```bash
# Try to parse YAML
python3 -c "import yaml; yaml.safe_load(open('.claude/vnx-system/state/progress_state.yaml'))"
```

**Resolution**:
```bash
# Option 1: Rebuild from receipts
rm .claude/vnx-system/state/progress_state.yaml
python3 .claude/vnx-system/scripts/sync_progress_state_from_receipts.py --apply

# Option 2: Restore from backup (if available)
cp .claude/vnx-system/state/progress_state.yaml.backup .claude/vnx-system/state/progress_state.yaml

# Option 3: Manual reset to clean state
cat > .claude/vnx-system/state/progress_state.yaml <<EOF
version: "1.0"
updated_at: "$(date -u +%Y-%m-%dT%H:%M:%S%z)"
updated_by: "manual_reset"
tracks:
  A: {current_gate: "planning", status: "idle", active_dispatch_id: null, history: []}
  B: {current_gate: "planning", status: "idle", active_dispatch_id: null, history: []}
  C: {current_gate: "planning", status: "idle", active_dispatch_id: null, history: []}
EOF
```

#### 4. Gate Stuck in Loop

**Symptom**: Gate progresses back to earlier gate unexpectedly

**Cause**: Normal for `quality_gate → planning` (next cycle)

**Detection**:
```bash
# Check history for progression pattern
cat .claude/vnx-system/state/progress_state.yaml | grep -A 20 "history:"
```

**Resolution**:
- If `quality_gate → planning`: This is expected (next development cycle)
- If other gates loop: Check `GATE_PROGRESSION` dictionary for bugs

### Reconciliation Strategies

#### Strategy 1: Receipt-Based Reconciliation (Safest)

```bash
# Dry-run to see changes
python3 .claude/vnx-system/scripts/sync_progress_state_from_receipts.py

# Apply changes
python3 .claude/vnx-system/scripts/sync_progress_state_from_receipts.py --apply
```

**Pros**:
- Ground truth from receipts
- Conservative (only updates if newer)
- Preserves history

**Cons**:
- Requires valid receipts
- May not recover from receipt processor crash

#### Strategy 2: Manual State Update (Fast)

```bash
# Set specific state
python3 .claude/vnx-system/scripts/update_progress_state.py \
  --track A \
  --gate planning \
  --status idle \
  --dispatch-id ""
```

**Pros**:
- Immediate fix
- Works without receipts

**Cons**:
- No history preservation
- May create inconsistency

#### Strategy 3: Complete Rebuild (Last Resort)

```bash
# Delete state file
rm .claude/vnx-system/state/progress_state.yaml

# Rebuild from receipts
python3 .claude/vnx-system/scripts/sync_progress_state_from_receipts.py --apply
```

**Pros**:
- Clean slate
- Guaranteed consistency with receipts

**Cons**:
- Loses manual state changes
- Requires valid receipts

---

## Operations

### Monitoring State

#### Health Check Command

```bash
# State overview
cat .claude/vnx-system/state/progress_state.yaml | grep -E "current_gate|status|active_dispatch_id"

# Track-specific check
python3 -c "
import yaml
state = yaml.safe_load(open('.claude/vnx-system/state/progress_state.yaml'))
track = state['tracks']['A']
print(f\"Track A: {track['status']} at {track['current_gate']}\")
print(f\"Dispatch: {track.get('active_dispatch_id', 'None')}\")
print(f\"Last Receipt: {track.get('last_receipt', {}).get('timestamp', 'None')}\")
"
```

#### Watch State Changes

```bash
# Watch progress_state.yaml updates
watch -n 2 'cat .claude/vnx-system/state/progress_state.yaml | grep -A 3 "tracks:"'

# Monitor recent state transitions
watch -n 5 'cat .claude/vnx-system/state/progress_state.yaml | grep -A 30 "history:" | head -20'
```

#### Dashboard Integration

**File**: `.claude/vnx-system/state/dashboard_status.json`

**State Fields**:
```json
{
  "tracks": {
    "A": {
      "current_gate": "implementation",
      "status": "working",
      "active_dispatch_id": "20260126-120000-abc123-A",
      "last_activity": "2026-01-26T12:00:00Z"
    }
  }
}
```

**Query Dashboard**:
```bash
# All tracks status
cat .claude/vnx-system/state/dashboard_status.json | jq '.tracks'

# Single track
cat .claude/vnx-system/state/dashboard_status.json | jq '.tracks.A'
```

### Troubleshooting

#### Issue: State Not Updating

**Symptoms**:
- Receipts appear in t0_receipts.ndjson
- progress_state.yaml not changing

**Diagnostic**:
```bash
# 1. Check receipt processor is running
ps aux | grep receipt_processor_v4.sh

# 2. Check processing log
tail -50 .claude/vnx-system/state/receipt_processing.log | grep PROGRESS_STATE

# 3. Test update_progress_state.py directly
python3 .claude/vnx-system/scripts/update_progress_state.py \
  --track A \
  --status idle \
  --receipt-event test \
  --receipt-status test
```

**Fixes**:
```bash
# Restart receipt processor
pkill -f receipt_processor_v4.sh
# (supervisor will restart automatically)

# Manual state reconciliation
python3 .claude/vnx-system/scripts/sync_progress_state_from_receipts.py --apply
```

#### Issue: Stuck in Working State

**Symptoms**:
- Track shows `status: working` for extended period
- No recent receipts from terminal

**Diagnostic**:
```bash
# Check last receipt timestamp
cat .claude/vnx-system/state/progress_state.yaml | grep -A 5 "last_receipt:"

# Check terminal activity
tail -20 .claude/vnx-system/state/t0_receipts.ndjson | jq 'select(.terminal=="T1")'

# Check active dispatch
ls -la .claude/vnx-system/dispatches/active/ | grep "$(cat .claude/vnx-system/state/progress_state.yaml | grep active_dispatch_id | cut -d\" -f2)"
```

**Fixes**:
```bash
# Option 1: Reset to idle (if task confirmed complete)
python3 .claude/vnx-system/scripts/update_progress_state.py --track A --status idle --dispatch-id ""

# Option 2: Set blocked (if task stuck)
python3 .claude/vnx-system/scripts/update_progress_state.py --track A --status blocked

# Option 3: Reconcile from receipts
python3 .claude/vnx-system/scripts/sync_progress_state_from_receipts.py --apply --only-track A
```

#### Issue: Gate Not Advancing

**Symptoms**:
- Receipt shows `task_complete` + `success`
- Gate remains unchanged

**Diagnostic**:
```bash
# Check gate progression dictionary
grep -A 10 "GATE_PROGRESSION" .claude/vnx-system/scripts/update_progress_state.py

# Check current gate
cat .claude/vnx-system/state/progress_state.yaml | grep current_gate

# Check processing log for advancement
tail -50 .claude/vnx-system/state/receipt_processing.log | grep "advancing gate"
```

**Fixes**:
```bash
# Option 1: Manual advancement
python3 .claude/vnx-system/scripts/update_progress_state.py --track A --advance-gate

# Option 2: Set specific gate
python3 .claude/vnx-system/scripts/update_progress_state.py --track A --gate review

# Option 3: Check if gate is in progression dictionary
# (escalation gate has no automatic progression)
```

### Recovery Procedures

#### Emergency State Reset

```bash
#!/bin/bash
# emergency_state_reset.sh

echo "=== EMERGENCY STATE RESET ==="

# 1. Backup current state
cp .claude/vnx-system/state/progress_state.yaml \
   .claude/vnx-system/state/progress_state.yaml.backup.$(date +%Y%m%d-%H%M%S)

# 2. Stop receipt processor
pkill -f receipt_processor_v4.sh

# 3. Rebuild from receipts
python3 .claude/vnx-system/scripts/sync_progress_state_from_receipts.py --apply

# 4. Wait for supervisor restart
sleep 10

# 5. Verify state
cat .claude/vnx-system/state/progress_state.yaml | grep -E "current_gate|status|active_dispatch_id"

echo "=== RESET COMPLETE ==="
```

#### Partial Track Recovery

```bash
# Reset single track to clean state
python3 .claude/vnx-system/scripts/update_progress_state.py \
  --track A \
  --gate planning \
  --status idle \
  --dispatch-id "" \
  --updated-by manual_recovery
```

#### History Preservation

```bash
# Before making changes, preserve history
cat .claude/vnx-system/state/progress_state.yaml | \
  grep -A 50 "history:" > \
  .claude/vnx-system/state/history_backup.$(date +%Y%m%d-%H%M%S).txt
```

---

## Integration

### Dispatcher Integration

**File**: `.claude/vnx-system/scripts/dispatcher_v7_compilation.sh`

**Integration Point**: After successful tmux dispatch

```bash
# Dispatcher sets track to working when assigning task
python3 .claude/vnx-system/scripts/update_progress_state.py \
  --track "$TRACK" \
  --gate "$GATE" \
  --status working \
  --dispatch-id "$DISPATCH_ID" \
  --updated-by dispatcher
```

**Result**:
- Track marked as `working`
- Gate set from dispatch metadata
- Dispatch ID recorded
- History entry added

### Intelligence Integration

**File**: `.claude/vnx-system/scripts/unified_state_manager_v2.py`

**Integration Point**: State manager reads progress_state.yaml

```python
# Read progress state for brief generation
with open(PROGRESS_STATE_PATH) as f:
    progress_state = yaml.safe_load(f)

# Extract track states
for track, data in progress_state['tracks'].items():
    current_gate = data['current_gate']
    status = data['status']
    active_dispatch = data.get('active_dispatch_id')

    # Include in T0 brief
    brief['tracks'][track] = {
        'current_gate': current_gate,
        'status': status,
        'next_gate': compute_next_gate(current_gate)
    }
```

**T0 Brief Generation**:
- Reads `progress_state.yaml` for current gates
- Computes `next_gates` using progression dictionary
- Includes in `t0_brief.json` for T0 decision-making

### T0 Orchestration

**Read-Only Access**: T0 never writes to progress_state.yaml

**Data Sources**:
1. `t0_brief.json` - Current state + next gates
2. `t0_receipts.ndjson` - Recent task completions
3. `dashboard_status.json` - Real-time metrics

**Decision Flow**:
```
1. T0 reads t0_brief.json
   └─ Sees Track A at "review" gate, status "idle"

2. T0 decides to dispatch review task
   └─ Writes manager block with "Gate: review"

3. Smart tap extracts dispatch
   └─ Creates dispatch markdown

4. Dispatcher sends to terminal
   └─ Calls update_progress_state.py (sets working)

5. Terminal completes review
   └─ Writes report

6. Receipt processor parses report
   └─ Calls update_progress_state.py (advances to testing)

7. T0 reads updated t0_brief.json
   └─ Sees Track A at "testing" gate, status "idle"
   └─ Dispatches testing task
```

---

## Testing

### State Validation

#### Test 1: Basic State Update

```bash
# Create test update
python3 .claude/vnx-system/scripts/update_progress_state.py \
  --track A \
  --gate testing \
  --status working \
  --dispatch-id "test-12345"

# Verify update
cat .claude/vnx-system/state/progress_state.yaml | grep -A 5 "tracks:" | grep -A 5 "A:"

# Expected:
# current_gate: testing
# status: working
# active_dispatch_id: test-12345
```

#### Test 2: Gate Advancement

```bash
# Set to known gate
python3 .claude/vnx-system/scripts/update_progress_state.py --track A --gate implementation

# Advance
python3 .claude/vnx-system/scripts/update_progress_state.py --track A --advance-gate

# Verify
cat .claude/vnx-system/state/progress_state.yaml | grep -A 2 "A:" | grep current_gate

# Expected: current_gate: review
```

#### Test 3: Receipt Integration

```bash
# Simulate task_complete receipt
python3 .claude/vnx-system/scripts/update_progress_state.py \
  --track A \
  --advance-gate \
  --status idle \
  --dispatch-id "" \
  --receipt-event task_complete \
  --receipt-status success \
  --receipt-timestamp "$(date -u +%Y-%m-%dT%H:%M:%S%z)" \
  --receipt-dispatch-id "test-complete-12345" \
  --updated-by test_suite

# Verify state advanced and cleared
cat .claude/vnx-system/state/progress_state.yaml | grep -A 10 "A:"

# Expected:
# status: idle
# active_dispatch_id: null
# last_receipt:
#   event_type: task_complete
#   status: success
```

### Receipt Testing

#### Test 1: Create Test Receipt

```bash
# Write test receipt
echo '{
  "event_type": "task_complete",
  "status": "success",
  "terminal": "T1",
  "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%S%z)'",
  "dispatch_id": "test-receipt-12345"
}' >> .claude/vnx-system/state/t0_receipts.ndjson
```

#### Test 2: Reconcile from Test Receipt

```bash
# Run reconciliation
python3 .claude/vnx-system/scripts/sync_progress_state_from_receipts.py --apply --only-track A

# Verify state matches receipt
cat .claude/vnx-system/state/progress_state.yaml | grep -A 5 "last_receipt:"
```

### End-to-End Test

```bash
#!/bin/bash
# e2e_state_test.sh

echo "=== STATE MANAGEMENT E2E TEST ==="

# 1. Initial state
echo "1. Setting initial state (planning, idle)..."
python3 .claude/vnx-system/scripts/update_progress_state.py \
  --track A --gate planning --status idle --dispatch-id ""

# 2. Dispatcher assigns task
echo "2. Simulating dispatcher assignment..."
python3 .claude/vnx-system/scripts/update_progress_state.py \
  --track A --gate implementation --status working --dispatch-id "e2e-test-001" \
  --updated-by test_dispatcher

# 3. Terminal starts task
echo "3. Simulating task_started receipt..."
python3 .claude/vnx-system/scripts/update_progress_state.py \
  --track A --receipt-event task_started --receipt-status confirmed \
  --receipt-timestamp "$(date -u +%Y-%m-%dT%H:%M:%S%z)" \
  --receipt-dispatch-id "e2e-test-001" \
  --updated-by test_receipt_processor

# 4. Terminal completes task
echo "4. Simulating task_complete success receipt..."
python3 .claude/vnx-system/scripts/update_progress_state.py \
  --track A --advance-gate --status idle --dispatch-id "" \
  --receipt-event task_complete --receipt-status success \
  --receipt-timestamp "$(date -u +%Y-%m-%dT%H:%M:%S%z)" \
  --receipt-dispatch-id "e2e-test-001" \
  --updated-by test_receipt_processor

# 5. Verify final state
echo "5. Verifying final state..."
cat .claude/vnx-system/state/progress_state.yaml | grep -A 15 "A:"

# Expected:
# current_gate: review (advanced from implementation)
# status: idle (cleared after success)
# active_dispatch_id: null (cleared after success)
# last_receipt:
#   event_type: task_complete
#   status: success

echo "=== E2E TEST COMPLETE ==="
```

---

## Appendix

### File Reference

**Core State Files**:
- `.claude/vnx-system/state/progress_state.yaml` - Machine orchestration ledger
- `.claude/vnx-system/state/t0_receipts.ndjson` - Receipt event stream
- `.claude/vnx-system/state/t0_brief.json` - T0 decision snapshot

**Scripts**:
- `.claude/vnx-system/scripts/update_progress_state.py` - Atomic state mutations
- `.claude/vnx-system/scripts/sync_progress_state_from_receipts.py` - State reconciliation
- `.claude/vnx-system/scripts/receipt_processor_v4.sh` - Receipt processing engine
- `.claude/vnx-system/scripts/generate_t0_brief.sh` - Brief generation

**Documentation**:
- `.claude/vnx-system/docs/operations/PROGRESS_STATE_SPEC.md` - State specification
- `.claude/vnx-system/docs/operations/RECEIPT_PIPELINE.md` - Receipt pipeline overview
- `.claude/vnx-system/docs/technical/STATE_MANAGEMENT.md` - This document

### Schema Definitions

#### progress_state.yaml Schema (YAML)

```yaml
version: string  # "1.0"
updated_at: iso8601_timestamp  # "2026-01-26T12:00:00+00:00"
updated_by: enum  # dispatcher | receipt_processor | sync_from_receipts

tracks:
  type: object
  properties:
    A|B|C:
      type: object
      properties:
        current_gate:
          type: string
          enum: [investigation, planning, implementation, review, testing, integration, quality_gate, validation, escalation]

        status:
          type: string
          enum: [idle, working, blocked]

        active_dispatch_id:
          type: [string, null]
          pattern: "YYYYMMDD-HHMMSS-[a-f0-9]{16}-[ABC]"

        last_receipt:
          type: object
          properties:
            event_type: {type: string}
            status: {type: string}
            timestamp: {type: string, format: iso8601}
            dispatch_id: {type: string}

        history:
          type: array
          maxItems: 10
          items:
            type: object
            properties:
              timestamp: {type: string, format: iso8601}
              gate: {type: string}
              status: {type: string}
              dispatch_id: {type: [string, null]}
              updated_by: {type: string}
```

#### Receipt Schema (JSON)

```json
{
  "event_type": "task_complete | task_started | task_blocked",
  "status": "success | blocked | failed | confirmed | error",
  "terminal": "T1 | T2 | T3",
  "timestamp": "ISO 8601 timestamp",
  "dispatch_id": "YYYYMMDD-HHMMSS-hash-track",
  "report_path": "/absolute/path/to/report.md",

  // Optional enrichment
  "gate": "string",
  "task_id": "string",
  "confidence": 0.0-1.0,
  "tags": {},
  "metrics": {},
  "validation": {}
}
```

### New State Directories (VNX V8)

| Directory | Purpose |
|-----------|---------|
| `gate_results/` | Per-PR gate check results (`<PR-ID>.json`) with per-check GO/HOLD verdicts |
| `verification_results/` | Contract verification output from `verify_claims.py` |
| `dispatch_payloads/` | Temp files for large tmux dispatch payloads (>50KB); auto-cleaned by `vnx recover` |

### Lock Lifecycle (V8)

Process locks in `.vnx-data/locks/<name>.lock/` now include:
- `pid` — process ID
- `fingerprint` — canonicalized script path
- `created_at` — epoch timestamp when lock was acquired
- `heartbeat` — epoch timestamp, updated periodically by long-running processes

**Stale-lock expiry**: Locks older than `VNX_LOCK_MAX_AGE` (default 3600s) are auto-cleaned on next acquisition attempt, even if the PID still exists.

**Unclean-shutdown marker**: `.vnx-data/locks/.unclean_shutdown` is written before risky operations and cleared on clean exit. `vnx recover` detects and clears this.

### PID Metadata

PID files in `.vnx-data/pids/` store:
- `<name>.pid` — process ID
- `<name>.pid.fingerprint` — script path fingerprint for ownership validation

### Version History

**Version 2.0 (2026-01-26)**:
- Complete technical reference consolidation
- Added state transition diagrams
- Documented conflict resolution
- Added comprehensive testing procedures
- Integrated receipt pipeline documentation

**Version 1.1 (2026-01-08)**:
- Phase 3 complete: Receipt processor state integration
- Added comprehensive logging with visual indicators
- Enhanced gate advancement logic

**Version 1.0 (2026-01-07)**:
- Initial production deployment
- Atomic state mutations
- Receipt-driven updates
- History tracking

---

**Last Updated**: 2026-01-26 by T-MANAGER
**Next Review**: After production deployment validation
**Status**: Complete technical reference
