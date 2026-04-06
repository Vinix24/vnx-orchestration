# Headless Subprocess Burn-In Test Plan

**Date**: 2026-04-06
**Branch**: `feature/headless-burn-in-plan`
**Components under test**: SubprocessAdapter, EventStore, SSE endpoint, Dashboard agent-stream page

---

## 1. Prerequisites

### Environment Configuration

```bash
# Enable subprocess adapter for T1 (primary burn-in target)
export VNX_ADAPTER_T1=subprocess

# Optionally enable T2 for parallel dispatch testing
export VNX_ADAPTER_T2=subprocess

# Verify the env vars are set
env | grep VNX_ADAPTER
```

### Infrastructure Checks

```bash
# 1. Verify claude CLI is available and responds
claude --version

# 2. Verify EventStore data directory exists
ls -la .vnx-data/events/

# 3. Verify dashboard API server is running (or start it)
curl -s http://localhost:8765/api/agent-stream/status | python3 -m json.tool

# 4. Verify no stale subprocess from prior runs
ps aux | grep "claude -p" | grep -v grep

# 5. Verify no stale leases blocking terminals
python3 -c "
from scripts.lib.runtime_coordination import RuntimeCoordinator
rc = RuntimeCoordinator()
for t in ['T1','T2','T3']:
    lease = rc.get_lease(t)
    print(f'{t}: {lease}')
"

# 6. Clear prior event files to start clean
python3 -c "
from scripts.lib.event_store import EventStore
es = EventStore()
for t in ['T1','T2']:
    es.clear(t)
    print(f'Cleared {t}')
"
```

### Dashboard Verification

Open `http://localhost:3000/agent-stream` in a browser. Confirm the terminal selector renders and no stale events display.

---

## 2. Test Scenarios

### Scenario 1: Simple Single-Tool Task

**Goal**: Verify basic end-to-end subprocess delivery, event capture, and SSE streaming.

```bash
export VNX_ADAPTER_T1=subprocess

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --instruction "Read the file bin/vnx and report the first 5 lines." \
  --model sonnet \
  --dispatch-id burnin-001-simple
```

**Validation Checklist**:
- [ ] Process exits with code 0
- [ ] `.vnx-data/events/T1.ndjson` exists and is non-empty
- [ ] Event file contains `init` event as first line
- [ ] Event file contains at least one `tool_use` event (Read file)
- [ ] Event file contains at least one `tool_result` event
- [ ] Event file contains a `result` event as final line
- [ ] Every event line has `"dispatch_id": "burnin-001-simple"`
- [ ] Every event line has `"terminal": "T1"`
- [ ] Sequence numbers are contiguous (1, 2, 3, ...)
- [ ] `curl http://localhost:8765/api/agent-stream/status` shows T1 with correct event_count
- [ ] SSE stream delivers all events: `curl -N http://localhost:8765/api/agent-stream/T1`

**Event Count Cross-Check**:
```bash
# Count events in NDJSON
wc -l .vnx-data/events/T1.ndjson

# Count events via API
curl -s http://localhost:8765/api/agent-stream/status | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('T1 events:', d.get('terminals',{}).get('T1',{}).get('event_count', 0))
"
```

---

### Scenario 2: Multi-Tool Complex Task

**Goal**: Verify event completeness when the agent uses multiple tools across thinking/tool_use/tool_result cycles.

```bash
export VNX_ADAPTER_T1=subprocess

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --instruction "List all Python files in scripts/lib/, then read the first 10 lines of event_store.py and subprocess_adapter.py, and summarize what each file does in one sentence." \
  --model sonnet \
  --dispatch-id burnin-002-multi-tool
```

**Validation Checklist**:
- [ ] Process exits with code 0
- [ ] Event file contains multiple `tool_use` events (Glob + Read x2 minimum)
- [ ] Each `tool_use` is followed by a corresponding `tool_result`
- [ ] `thinking` events appear (model reasoning visible)
- [ ] `text` events contain the final summary
- [ ] `result` event present
- [ ] All events have `"dispatch_id": "burnin-002-multi-tool"`
- [ ] Event type distribution check:
  ```bash
  python3 -c "
  import json
  from collections import Counter
  types = Counter()
  with open('.vnx-data/events/T1.ndjson') as f:
      for line in f:
          e = json.loads(line)
          types[e['type']] += 1
  for t, c in types.most_common():
      print(f'  {t}: {c}')
  "
  ```
- [ ] Types include: init, thinking, tool_use, tool_result, text, result

---

### Scenario 3: Error Cases

**Goal**: Verify graceful failure handling for invalid inputs.

#### 3a: Invalid Model

```bash
export VNX_ADAPTER_T1=subprocess

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --instruction "Say hello" \
  --model nonexistent-model-xyz \
  --dispatch-id burnin-003a-bad-model
```

**Validation**:
- [ ] Process exits with non-zero code OR event stream contains an `error` event
- [ ] No zombie `claude` process left running: `ps aux | grep "claude -p" | grep -v grep`
- [ ] EventStore state is clean (either empty or contains error event)

#### 3b: Invalid Terminal ID

```bash
python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T99 \
  --instruction "Say hello" \
  --model sonnet \
  --dispatch-id burnin-003b-bad-terminal
```

**Validation**:
- [ ] Process exits with non-zero code
- [ ] No event file created at `.vnx-data/events/T99.ndjson`
- [ ] Error message logged to stderr

#### 3c: Empty Instruction

```bash
export VNX_ADAPTER_T1=subprocess

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --instruction "" \
  --model sonnet \
  --dispatch-id burnin-003c-empty
```

**Validation**:
- [ ] Process exits with non-zero code or produces minimal output
- [ ] No hanging subprocess

---

### Scenario 4: Parallel Dispatches to T1 + T2

**Goal**: Verify concurrent subprocess execution with independent event streams.

```bash
export VNX_ADAPTER_T1=subprocess
export VNX_ADAPTER_T2=subprocess

# Launch both in background
python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --instruction "Read bin/vnx and count the number of lines." \
  --model sonnet \
  --dispatch-id burnin-004-parallel-t1 &
PID_T1=$!

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T2 \
  --instruction "Read scripts/lib/event_store.py and list all method names." \
  --model sonnet \
  --dispatch-id burnin-004-parallel-t2 &
PID_T2=$!

# Wait for both
wait $PID_T1; echo "T1 exit: $?"
wait $PID_T2; echo "T2 exit: $?"
```

**Validation Checklist**:
- [ ] Both processes exit with code 0
- [ ] `.vnx-data/events/T1.ndjson` contains events with `dispatch_id: burnin-004-parallel-t1`
- [ ] `.vnx-data/events/T2.ndjson` contains events with `dispatch_id: burnin-004-parallel-t2`
- [ ] No cross-contamination (T1 events in T2 file or vice versa)
- [ ] Event counts are independent:
  ```bash
  echo "T1 events: $(wc -l < .vnx-data/events/T1.ndjson)"
  echo "T2 events: $(wc -l < .vnx-data/events/T2.ndjson)"
  ```
- [ ] SSE status shows both terminals:
  ```bash
  curl -s http://localhost:8765/api/agent-stream/status | python3 -m json.tool
  ```
- [ ] Dashboard shows both T1 and T2 streams when switching terminals

---

### Scenario 5: Long-Running Task with Many Tool Calls

**Goal**: Stress-test event capture volume, sequence integrity, and SSE streaming under sustained output.

```bash
export VNX_ADAPTER_T1=subprocess

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --instruction "For each Python file in scripts/lib/, read its first 20 lines and write a one-paragraph summary. There are approximately 15+ files. Process all of them." \
  --model sonnet \
  --dispatch-id burnin-005-long-running
```

**Validation Checklist**:
- [ ] Process exits with code 0
- [ ] Event count exceeds 50 (many tool_use/tool_result pairs)
- [ ] Sequence numbers are strictly monotonic:
  ```bash
  python3 -c "
  import json
  prev = 0
  with open('.vnx-data/events/T1.ndjson') as f:
      for i, line in enumerate(f, 1):
          e = json.loads(line)
          seq = e.get('sequence', 0)
          if seq != prev + 1:
              print(f'SEQUENCE GAP at line {i}: expected {prev+1}, got {seq}')
          prev = seq
  print(f'Total events: {prev}, all sequential: {prev == i}')
  "
  ```
- [ ] No malformed JSON lines:
  ```bash
  python3 -c "
  import json
  with open('.vnx-data/events/T1.ndjson') as f:
      for i, line in enumerate(f, 1):
          try:
              json.loads(line)
          except json.JSONDecodeError as e:
              print(f'MALFORMED line {i}: {e}')
  print(f'Checked {i} lines')
  "
  ```
- [ ] File size stays under 10 MB warning threshold:
  ```bash
  ls -lh .vnx-data/events/T1.ndjson
  ```
- [ ] SSE stream delivers events in real-time during execution (manual observation via `curl -N`)
- [ ] All events share `dispatch_id: burnin-005-long-running`

---

### Scenario 6: tmux vs Subprocess Comparison

**Goal**: Execute identical tasks via both adapters and compare outputs for functional parity.

```bash
TASK="Read the file scripts/lib/event_store.py and report the number of methods defined in the EventStore class."

# --- Run via subprocess ---
export VNX_ADAPTER_T1=subprocess
python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --instruction "$TASK" \
  --model sonnet \
  --dispatch-id burnin-006-subprocess

# Save subprocess events
cp .vnx-data/events/T1.ndjson /tmp/burnin-006-subprocess-events.ndjson

# --- Run via tmux (default) ---
unset VNX_ADAPTER_T1
# Dispatch same task through normal tmux path
# (use bin/vnx dispatch or manual tmux send-keys equivalent)
# Capture tmux pane output after completion:
tmux capture-pane -t T1 -p > /tmp/burnin-006-tmux-output.txt
```

**Comparison Methodology**:
```bash
# 1. Extract final result text from subprocess events
python3 -c "
import json
with open('/tmp/burnin-006-subprocess-events.ndjson') as f:
    for line in f:
        e = json.loads(line)
        if e['type'] == 'result':
            print(e['data'].get('result', ''))
" > /tmp/burnin-006-subprocess-result.txt

# 2. Compare final answers (should be functionally equivalent)
diff /tmp/burnin-006-subprocess-result.txt /tmp/burnin-006-tmux-output.txt

# 3. Check event type coverage in subprocess (tmux has no structured events)
python3 -c "
import json
types = set()
with open('/tmp/burnin-006-subprocess-events.ndjson') as f:
    for line in f:
        types.add(json.loads(line)['type'])
print('Event types captured:', sorted(types))
"
```

**Validation Checklist**:
- [ ] Both runs produce functionally equivalent answers
- [ ] Subprocess run captures structured event types (init, thinking, tool_use, tool_result, text, result)
- [ ] tmux run produces only unstructured terminal text (no event stream)
- [ ] Subprocess events include tool call details not visible in tmux output
- [ ] Subprocess provides dispatch_id correlation; tmux does not

---

## 3. Session JSONL Cross-Reference

Claude Code writes session logs to `~/.claude/projects/*/sessions/*/`. After each scenario, cross-reference:

```bash
# Find the most recent session log
LATEST_SESSION=$(ls -t ~/.claude/projects/*/sessions/*/*.jsonl 2>/dev/null | head -1)
echo "Latest session: $LATEST_SESSION"

# Count message types in session JSONL
python3 -c "
import json, sys
from collections import Counter
types = Counter()
with open('$LATEST_SESSION') as f:
    for line in f:
        try:
            msg = json.loads(line)
            types[msg.get('type', 'unknown')] += 1
        except: pass
for t, c in types.most_common():
    print(f'  {t}: {c}')
"

# Compare event counts: EventStore vs Session JSONL
echo "EventStore events: $(wc -l < .vnx-data/events/T1.ndjson)"
echo "Session JSONL lines: $(wc -l < $LATEST_SESSION)"
```

**Expected**: EventStore event count should be a subset of session JSONL lines. The session log contains additional metadata (system prompts, conversation management) not emitted as stream events.

---

## 4. Stream Archive Proposal

### Problem

`EventStore.clear(terminal)` is called at the start of every new dispatch (`subprocess_adapter.py:213`), permanently deleting prior events. This prevents:
- Post-hoc debugging of completed dispatches
- Audit trail across dispatch boundaries
- Historical event analysis

### Proposed Archive Design

**Archive Path**: `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson`

**Example**:
```
.vnx-data/events/
  T1.ndjson                          # Active dispatch events (current behavior)
  archive/
    T1/
      burnin-001-simple.ndjson       # Archived on next dispatch
      burnin-002-multi-tool.ndjson
    T2/
      burnin-004-parallel-t2.ndjson
```

**When to Archive**: Before `clear()` in `EventStore`, move the current file to the archive path using the `dispatch_id` from the last event's metadata.

**Retention Policy**:
- Keep archives for 7 days by default (configurable via `VNX_EVENT_ARCHIVE_RETENTION_DAYS`)
- Archives older than retention period cleaned up on next `clear()` call
- Total archive size cap: 100 MB per terminal (oldest-first eviction)

### Code Changes Required

**File: `scripts/lib/event_store.py`**

Add `archive(terminal, dispatch_id)` method:

```python
def archive(self, terminal: str, dispatch_id: str) -> Optional[Path]:
    """Move current event file to archive before clearing."""
    event_file = self._event_file(terminal)
    if not event_file.exists() or event_file.stat().st_size == 0:
        return None

    archive_dir = self._events_dir / "archive" / terminal
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{dispatch_id}.ndjson"

    shutil.copy2(str(event_file), str(archive_path))
    return archive_path

def cleanup_archives(self, terminal: str, max_age_days: int = 7, max_size_mb: int = 100):
    """Remove archives older than max_age_days or exceeding max_size_mb."""
    archive_dir = self._events_dir / "archive" / terminal
    if not archive_dir.exists():
        return

    cutoff = time.time() - (max_age_days * 86400)
    files = sorted(archive_dir.glob("*.ndjson"), key=lambda f: f.stat().st_mtime)

    # Age-based cleanup
    for f in files:
        if f.stat().st_mtime < cutoff:
            f.unlink()

    # Size-based cleanup (oldest first)
    total = sum(f.stat().st_size for f in archive_dir.glob("*.ndjson"))
    max_bytes = max_size_mb * 1024 * 1024
    remaining = sorted(archive_dir.glob("*.ndjson"), key=lambda f: f.stat().st_mtime)
    for f in remaining:
        if total <= max_bytes:
            break
        total -= f.stat().st_size
        f.unlink()
```

**File: `scripts/lib/subprocess_adapter.py`**

Modify `deliver()` to archive before clearing:

```python
# In deliver(), before es.clear():
last = es.last_event(terminal_id)
if last and last.get("dispatch_id"):
    es.archive(terminal_id, last["dispatch_id"])
    es.cleanup_archives(terminal_id)
es.clear(terminal_id)
```

**File: `dashboard/api_agent_stream.py`**

Add archive query endpoint:

```python
# GET /api/agent-stream/archive/{terminal}/{dispatch_id}
# Returns archived events for a completed dispatch
```

---

## 5. Comparison Methodology: Subprocess vs tmux vs JSONL

### Data Sources

| Source | Format | Location | Structured | Dispatch-Aware |
|--------|--------|----------|-----------|----------------|
| EventStore | NDJSON | `.vnx-data/events/{terminal}.ndjson` | Yes | Yes (dispatch_id field) |
| Session JSONL | JSONL | `~/.claude/projects/*/sessions/*/*.jsonl` | Yes | No |
| tmux pane | Plain text | `tmux capture-pane -t {terminal} -p` | No | No |

### Comparison Commands

```bash
# --- After running the same task via all three paths ---

# 1. Event type coverage
echo "=== EventStore event types ==="
python3 -c "
import json
from collections import Counter
c = Counter()
with open('.vnx-data/events/T1.ndjson') as f:
    for line in f:
        c[json.loads(line)['type']] += 1
for t, n in sorted(c.items()): print(f'  {t}: {n}')
"

# 2. Session JSONL message types
echo "=== Session JSONL types ==="
LATEST=\$(ls -t ~/.claude/projects/*/sessions/*/*.jsonl 2>/dev/null | head -1)
python3 -c "
import json
from collections import Counter
c = Counter()
with open('\$LATEST') as f:
    for line in f:
        try: c[json.loads(line).get('type','?')] += 1
        except: pass
for t, n in sorted(c.items()): print(f'  {t}: {n}')
"

# 3. tmux output line count
echo "=== tmux output ==="
tmux capture-pane -t T1 -p | wc -l

# 4. Tool call count comparison
echo "=== Tool calls in EventStore ==="
python3 -c "
import json
with open('.vnx-data/events/T1.ndjson') as f:
    tools = [json.loads(l)['data'].get('tool','') for l in f if json.loads(l)['type']=='tool_use']
print(f'Tool calls: {len(tools)}')
for t in tools: print(f'  - {t}')
" 2>/dev/null || echo "(re-read file for tool extraction)"
```

### Expected Differences

| Aspect | Subprocess EventStore | Session JSONL | tmux |
|--------|----------------------|---------------|------|
| Event granularity | Per-token/tool-call | Per-message | None |
| dispatch_id | Present | Absent | Absent |
| Tool call details | Full (name, input, output) | Full | Partial (visible output only) |
| Thinking content | Captured | Captured | Not visible |
| Timing precision | Millisecond timestamps | Timestamps | None |
| Audit trail | NDJSON per terminal | Session-scoped | Scrollback buffer |

---

## 6. Success Criteria

### Must-Pass (Burn-In Fails Without These)

1. **Scenarios 1-2 complete successfully**: Simple and multi-tool tasks execute, events captured
2. **Event integrity**: Every event has valid JSON, dispatch_id, terminal, contiguous sequence
3. **Event type coverage**: At least `init`, `tool_use`, `tool_result`, `text`, `result` types present
4. **Parallel isolation** (Scenario 4): T1 and T2 event files contain only their own dispatch_id
5. **SSE streaming works**: `curl -N` against the SSE endpoint delivers events matching the NDJSON file
6. **No zombie processes**: After every scenario, `ps aux | grep "claude -p"` shows no orphans

### Should-Pass (Degraded but Acceptable if Failed)

7. **Error cases** (Scenario 3): Graceful failures, no crashes
8. **Long-running stability** (Scenario 5): 50+ events captured without gaps
9. **Dashboard renders**: Agent-stream page shows events color-coded by type
10. **tmux parity** (Scenario 6): Subprocess produces functionally equivalent results

### Informational (Observed but Not Blocking)

11. Session JSONL cross-reference: EventStore event count as expected fraction of JSONL lines
12. Event file size stays under 10 MB for all scenarios
13. SSE latency: Events appear in dashboard within 1 second of generation

---

## 7. Risk Assessment

### Failure Modes

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `claude` CLI not installed or wrong version | Low | Blocks all scenarios | Check `claude --version` in prerequisites |
| Subprocess hangs (no exit) | Medium | Blocks test completion | Set timeout: `timeout 300 python3 scripts/lib/subprocess_dispatch.py ...` |
| EventStore file locking deadlock | Low | Corrupts event file | Monitor with `lsof .vnx-data/events/T1.ndjson` |
| Dashboard API server not running | Medium | SSE validation fails | Start server before burn-in; test with curl first |
| Model rate limiting | Medium | Intermittent failures | Space scenarios 30s apart; use sonnet (lower rate limit pressure) |
| Stale terminal lease blocks dispatch | High | Dispatch rejected | Clear leases in prerequisites (see Section 1) |
| Event clear() destroys evidence | High | Can't review prior scenarios | Copy NDJSON after each scenario before running next |

### Rollback Plan

The burn-in is read-only from a code perspective; no production code is modified during testing. If issues arise:

1. Kill any hanging subprocesses: `pkill -f "claude -p --output-format stream-json"`
2. Clear event files: `rm .vnx-data/events/*.ndjson`
3. Unset adapter flags: `unset VNX_ADAPTER_T1 VNX_ADAPTER_T2`
4. Release terminal leases via RuntimeCoordinator CLI
5. Normal tmux dispatch resumes immediately (default adapter)

### Critical Note on Evidence Preservation

Since `clear()` wipes events on each new dispatch, **copy the NDJSON file after each scenario**:

```bash
# After each scenario N:
cp .vnx-data/events/T1.ndjson .vnx-data/events/burnin-snapshot-scenario-N.ndjson
```

This preserves evidence until the archive proposal (Section 4) is implemented.
