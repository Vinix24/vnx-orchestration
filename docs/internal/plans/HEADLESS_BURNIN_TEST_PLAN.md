# Headless Subprocess Burn-In Test Plan

**Date**: 2026-04-06
**Branch**: `feature/headless-burn-in-plan`
**Status**: Draft — awaiting operator approval before execution

---

## 1. Prerequisites

### Environment Configuration

```bash
# Set subprocess adapter for T1 (primary burn-in terminal)
export VNX_ADAPTER_T1=subprocess

# For parallel test (S4), also enable T2
export VNX_ADAPTER_T2=subprocess

# Verify VNX_DATA_DIR is set (events write here)
echo "${VNX_DATA_DIR:-.vnx-data}"
```

### Required Services

| Service | Command | Verify |
|---------|---------|--------|
| Dashboard API | `cd dashboard && python3 api_server.py` | `curl -s http://localhost:8787/api/agent-stream/status` returns JSON |
| Dashboard UI | `cd dashboard/token-dashboard && npm run dev` | Browser at `http://localhost:3000/agent-stream` loads |
| `claude` CLI | `claude --version` | Prints version (must support `--output-format stream-json`) |

### Pre-Flight Checks

```bash
# 1. Verify event store directory exists
mkdir -p "${VNX_DATA_DIR:-.vnx-data}/events"

# 2. Clear stale events from prior runs
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T2.ndjson"

# 3. Verify subprocess_dispatch.py is reachable
python3 scripts/lib/subprocess_dispatch.py --help
```

---

## 2. Critical Gap: Event Consumption Pipeline

### The Problem

`subprocess_dispatch.py` calls `adapter.deliver()` which spawns the `claude -p --output-format stream-json` subprocess, but **never calls `adapter.read_events()`**. The subprocess writes stream-json to stdout, but nobody reads it. Events are never persisted to EventStore.

**Current flow (broken)**:
```
dispatch_deliver.sh → subprocess_dispatch.py → adapter.deliver()
                                                  ↓
                                          Popen(claude -p ...)
                                                  ↓
                                          stdout: stream-json lines (UNREAD)
                                                  ↓
                                          EventStore: EMPTY
                                                  ↓
                                          SSE endpoint: NO EVENTS
```

**Required flow (working)**:
```
dispatch_deliver.sh → subprocess_dispatch.py → adapter.deliver()
                                                  ↓
                                          Popen(claude -p ...)
                                                  ↓
                                          adapter.read_events() consumes stdout
                                                  ↓
                                          EventStore.append() per event
                                                  ↓
                                          SSE endpoint polls EventStore
                                                  ↓
                                          Dashboard renders events
```

### Validation Approach

Before running any scenario, verify the read loop works:

```bash
# Manual integration test: spawn claude directly, pipe to event store
cd scripts/lib
python3 -c "
from subprocess_adapter import SubprocessAdapter
adapter = SubprocessAdapter()
adapter.deliver('T1', 'test-manual-001', instruction='List the files in the current directory. Only use the ls command.', model='haiku')
count = 0
for event in adapter.read_events('T1'):
    count += 1
    print(f'[{event.type}] seq={count}')
print(f'Total events: {count}')
"
```

**Expected**: Multiple events printed (init, thinking, tool_use, tool_result, text, result). If count=0, the read loop or subprocess spawn is broken — stop burn-in and fix first.

Then verify events reached EventStore:

```bash
wc -l "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"
# Expected: same count as printed above

# Inspect first and last event
head -1 "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -m json.tool
tail -1 "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -m json.tool
```

---

## 3. Test Scenarios

### S1: Simple Task via Subprocess

**Purpose**: Validate the minimum viable path — one task, one terminal, events persisted.

```bash
# Clear prior events
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"

# Dispatch
python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --dispatch-id "burnin-S1-simple-$(date +%s)" \
  --model haiku \
  --instruction "List the files in the scripts/ directory. Report the count."
```

**Validation Checklist**:
- [ ] Process exits with code 0
- [ ] `T1.ndjson` exists and is non-empty
- [ ] Event count: `wc -l "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"` > 3
- [ ] Event types present (check with): `cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "import sys,json; types=set(); [types.add(json.loads(l)['type']) for l in sys.stdin]; print(sorted(types))"`
  - Expected types include at minimum: `init`, `text` or `result`
- [ ] Every event has `dispatch_id` field: `cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "import sys,json; missing=[i for i,l in enumerate(sys.stdin) if not json.loads(l).get('dispatch_id')]; print(f'Missing dispatch_id on lines: {missing}' if missing else 'All events have dispatch_id')"`
- [ ] Every event has `terminal` field set to `T1`
- [ ] Every event has monotonically increasing `sequence` numbers

### S2: Multi-Tool Task

**Purpose**: Validate stream completeness for tasks that use multiple tool calls.

```bash
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --dispatch-id "burnin-S2-multitool-$(date +%s)" \
  --model haiku \
  --instruction "Find the file scripts/lib/event_store.py, read it, and count the number of methods defined in the EventStore class. Report each method name."
```

**Validation Checklist**:
- [ ] All S1 checks pass
- [ ] Event types include `tool_use` and `tool_result`
- [ ] At least 2 `tool_use` events (find + read): `cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "import sys,json; c=sum(1 for l in sys.stdin if json.loads(l)['type']=='tool_use'); print(f'tool_use count: {c}')"`
- [ ] Each `tool_use` has a corresponding `tool_result` (count should match)
- [ ] Final `result` event contains the method names

### S3: Error Case — Invalid Model

**Purpose**: Validate error handling when claude CLI rejects the model name.

```bash
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --dispatch-id "burnin-S3-error-$(date +%s)" \
  --model "nonexistent-model-xyz" \
  --instruction "Say hello."
```

**Validation Checklist**:
- [ ] Process exits with non-zero code OR events contain an `error` type event
- [ ] `T1.ndjson` contains at least one event (even errors should be captured)
- [ ] Error is identifiable: `cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "import sys,json; [print(json.loads(l)) for l in sys.stdin if json.loads(l)['type'] in ('error','result')]"`
- [ ] No zombie process left: `ps aux | grep "claude.*nonexistent-model" | grep -v grep`

### S4: Parallel Dispatches — T1 and T2 Simultaneously

**Purpose**: Validate concurrent subprocess execution and per-terminal event isolation.

```bash
export VNX_ADAPTER_T1=subprocess
export VNX_ADAPTER_T2=subprocess
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T2.ndjson"

DISPATCH_TS=$(date +%s)

# Launch both in background
python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --dispatch-id "burnin-S4-parallel-T1-${DISPATCH_TS}" \
  --model haiku \
  --instruction "Count the number of Python files in scripts/lib/. Report the count." &
PID_T1=$!

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T2 \
  --dispatch-id "burnin-S4-parallel-T2-${DISPATCH_TS}" \
  --model haiku \
  --instruction "Count the number of shell scripts in scripts/. Report the count." &
PID_T2=$!

# Wait for both
wait $PID_T1; echo "T1 exit: $?"
wait $PID_T2; echo "T2 exit: $?"
```

**Validation Checklist**:
- [ ] Both processes exit with code 0
- [ ] `T1.ndjson` and `T2.ndjson` both exist and are non-empty
- [ ] T1 events only contain `"terminal":"T1"`: `cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "import sys,json; bad=[i for i,l in enumerate(sys.stdin) if json.loads(l).get('terminal')!='T1']; print(f'Cross-contaminated lines: {bad}' if bad else 'T1 isolation OK')"`
- [ ] T2 events only contain `"terminal":"T2"` (same check with T2)
- [ ] T1 dispatch_ids all match the T1 dispatch ID
- [ ] T2 dispatch_ids all match the T2 dispatch ID
- [ ] No shared sequence numbers between T1 and T2 (sequences are per-terminal)

### S5: Long-Running Task with Many Tool Calls

**Purpose**: Validate stream stability for tasks generating 20+ events.

```bash
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --dispatch-id "burnin-S5-long-$(date +%s)" \
  --model haiku \
  --instruction "Read each of these files and report the line count for each: scripts/lib/subprocess_adapter.py, scripts/lib/event_store.py, scripts/lib/subprocess_dispatch.py, dashboard/api_agent_stream.py, scripts/lib/dispatch_deliver.sh. Then summarize the total lines across all files."
```

**Validation Checklist**:
- [ ] All S1 checks pass
- [ ] Event count > 20: `wc -l "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"`
- [ ] At least 5 `tool_use` events (one per file read)
- [ ] Sequence numbers are contiguous (no gaps): `cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "import sys,json; seqs=[json.loads(l)['sequence'] for l in sys.stdin]; expected=list(range(1,len(seqs)+1)); print('Contiguous' if seqs==expected else f'Gaps: expected {expected}, got {seqs}')"`
- [ ] File size stays under 10MB warning threshold: `ls -la "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"`
- [ ] No malformed JSON lines: `cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "import sys,json; bad=[i for i,l in enumerate(sys.stdin) if not l.strip()]; print(f'Empty lines: {bad}' if bad else 'No empty lines')"`

### S6: Tmux vs Subprocess Comparison

**Purpose**: Run the same task via both delivery paths and compare outputs.

```bash
TASK="List the Python files in scripts/lib/ and count them."
DISPATCH_TS=$(date +%s)

# --- Subprocess path ---
export VNX_ADAPTER_T1=subprocess
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --dispatch-id "burnin-S6-subprocess-${DISPATCH_TS}" \
  --model haiku \
  --instruction "$TASK"

# Save subprocess events
cp "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" "/tmp/burnin-S6-subprocess.ndjson"

# Extract final result text
cat "/tmp/burnin-S6-subprocess.ndjson" | python3 -c "
import sys, json
for line in sys.stdin:
    ev = json.loads(line)
    if ev['type'] == 'result':
        print(json.dumps(ev.get('data', {}), indent=2))
" > /tmp/burnin-S6-subprocess-result.json

# --- Tmux path ---
# Unset to use tmux delivery
unset VNX_ADAPTER_T1

# Dispatch same task via normal tmux path (requires interactive T1 terminal)
# Capture tmux output after task completes:
tmux capture-pane -t T1 -p > /tmp/burnin-S6-tmux-output.txt

# --- Session JSONL comparison ---
# Find the most recent claude session log
LATEST_SESSION=$(ls -t ~/.claude/projects/*/sessions/*.jsonl 2>/dev/null | head -1)
echo "Latest session JSONL: $LATEST_SESSION"

# Extract event types from session JSONL
cat "$LATEST_SESSION" | python3 -c "
import sys, json
types = {}
for line in sys.stdin:
    try:
        ev = json.loads(line)
        t = ev.get('type', 'unknown')
        types[t] = types.get(t, 0) + 1
    except: pass
for t, c in sorted(types.items()):
    print(f'  {t}: {c}')
" > /tmp/burnin-S6-session-types.txt
```

**Validation Checklist**:
- [ ] Both paths produce a result containing the file count
- [ ] Subprocess event types are a subset of session JSONL event types
- [ ] Event counts comparison: `echo "Subprocess events: $(wc -l < /tmp/burnin-S6-subprocess.ndjson)" && echo "Session JSONL lines: $(wc -l < "$LATEST_SESSION")"`
- [ ] Final answer content is semantically equivalent (manual check)

### S7: Event Persistence After Dispatch Completes

**Purpose**: Verify events survive after the subprocess exits and are not prematurely cleared.

```bash
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"

python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --dispatch-id "burnin-S7-persist-$(date +%s)" \
  --model haiku \
  --instruction "Say the word 'persisted' and nothing else."

# Record event count immediately after completion
EVENTS_AFTER=$(wc -l < "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson")
echo "Events after completion: $EVENTS_AFTER"

# Wait 10 seconds — events should still be there
sleep 10
EVENTS_LATER=$(wc -l < "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson")
echo "Events after 10s: $EVENTS_LATER"

# Verify SSE endpoint still serves them
curl -s "http://localhost:8787/api/agent-stream/status" | python3 -m json.tool
```

**Validation Checklist**:
- [ ] `EVENTS_AFTER` equals `EVENTS_LATER` (no events lost)
- [ ] SSE status endpoint shows T1 with event_count matching
- [ ] Events are readable: `cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "import sys,json; [json.loads(l) for l in sys.stdin]; print('All events parseable')"`
- [ ] Dashboard at `/agent-stream` shows events when T1 is selected

---

## 4. Stream Archive Proposal

### Problem

`EventStore.clear()` truncates the terminal's NDJSON file on every new dispatch. This destroys the audit trail for completed dispatches.

### Proposed Solution

**Archive path**: `.vnx-data/events/archive/{dispatch_id}.ndjson`

**When to archive**: In `EventStore.clear()`, before truncation:

```python
# In event_store.py — clear() method modification
def clear(self, terminal: str) -> None:
    path = self._terminal_path(terminal)
    self._sequences.pop(terminal, None)

    if not path.exists():
        return

    # Archive: copy current events before truncation
    archive_dir = self._events_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Read the last dispatch_id from the file to name the archive
    last_dispatch_id = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    ev = json.loads(line)
                    last_dispatch_id = ev.get("dispatch_id")
                except json.JSONDecodeError:
                    pass

    if last_dispatch_id:
        archive_path = archive_dir / f"{last_dispatch_id}.ndjson"
        import shutil
        shutil.copy2(path, archive_path)
        logger.info("event_store: archived %s -> %s", path, archive_path)

    # Then truncate as before
    with open(path, "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.truncate(0)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
```

**Alternative — accept dispatch_id parameter**: Instead of reading the file to find the dispatch_id, pass it explicitly from `SubprocessAdapter.deliver()` which already knows it:

```python
# In subprocess_adapter.py deliver() method, change:
es.clear(terminal_id)
# To:
es.clear(terminal_id, archive_dispatch_id=previous_dispatch_id)
```

This is cleaner but requires tracking the previous dispatch_id per terminal.

**Retention policy**:
- Keep archives for 7 days
- Cleanup via cron or operator script: `find .vnx-data/events/archive/ -name "*.ndjson" -mtime +7 -delete`
- Total archive size cap: warn at 100MB (add to existing size warning logic)

**Files to modify**:
- `scripts/lib/event_store.py` — add archive logic to `clear()`, add `archive_dir` property
- `scripts/lib/subprocess_adapter.py` — pass dispatch_id context to `clear()` if using the explicit parameter approach

---

## 5. Comparison Methodology

### Subprocess NDJSON vs Claude Session JSONL

Claude Code writes session logs to `~/.claude/projects/{project-hash}/sessions/{session-id}.jsonl`. Each line is a conversation turn.

```bash
# After running a subprocess dispatch, find the matching session
SESSION_ID=$(cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "
import sys, json
for line in sys.stdin:
    ev = json.loads(line)
    sid = ev.get('data', {}).get('session_id')
    if sid:
        print(sid)
        break
")

# Find session file
JSONL_FILE=$(find ~/.claude -name "*.jsonl" -path "*${SESSION_ID}*" 2>/dev/null | head -1)

# Compare event type distributions
echo "=== Subprocess EventStore ==="
cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "
import sys, json, collections
types = collections.Counter()
for line in sys.stdin:
    types[json.loads(line)['type']] += 1
for t, c in sorted(types.items()):
    print(f'  {t}: {c}')
"

echo "=== Session JSONL ==="
cat "$JSONL_FILE" | python3 -c "
import sys, json, collections
types = collections.Counter()
for line in sys.stdin:
    try:
        ev = json.loads(line)
        types[ev.get('type', 'unknown')] += 1
    except: pass
for t, c in sorted(types.items()):
    print(f'  {t}: {c}')
"
```

### Subprocess vs Tmux Output

Tmux capture-pane only captures rendered terminal text — it cannot provide structured event data. Comparison is limited to:

1. **Final answer equivalence**: Both paths should produce the same factual answer
2. **Completion**: Both paths should complete without hanging
3. **No data loss**: Subprocess events should contain all tool calls visible in tmux output

```bash
# Rough comparison: extract tool names from both
echo "=== Subprocess tool calls ==="
cat "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson" | python3 -c "
import sys, json
for line in sys.stdin:
    ev = json.loads(line)
    if ev['type'] == 'tool_use':
        print(f\"  {ev.get('data', {}).get('name', 'unknown')}\")
"

echo "=== Tmux output tool references ==="
grep -oE '(Read|Write|Edit|Bash|Glob|Grep|Agent)\b' /tmp/burnin-S6-tmux-output.txt | sort | uniq -c
```

---

## 6. Success Criteria

| Criterion | Threshold | How to Measure |
|-----------|-----------|----------------|
| Event pipeline works | S1 produces > 3 events in EventStore | `wc -l T1.ndjson` |
| Stream completeness | Every `tool_use` has a matching `tool_result` | Type count comparison script |
| dispatch_id linkage | 100% of events have non-empty `dispatch_id` | Validation script from S1 |
| Terminal isolation | 0 cross-contaminated events in S4 | Isolation check script |
| Sequence integrity | Contiguous 1..N sequences per dispatch | Sequence check script from S5 |
| Error capture | S3 produces identifiable error event or non-zero exit | Manual check |
| Persistence | Events survive 10s after completion (S7) | Count comparison |
| Dashboard renders | SSE endpoint serves events, dashboard displays them | Manual browser check |
| No zombie processes | `ps aux \| grep claude` shows no orphans after all scenarios | Manual check |

**Overall pass**: All 9 criteria met. Any failure blocks burn-in sign-off.

---

## 7. Risk Assessment

| Risk | Likelihood | Impact | Detection | Mitigation |
|------|-----------|--------|-----------|------------|
| `read_events()` never called (current state) | **Certain** | Events never reach EventStore | S1 produces 0 events | Wire up read loop in `subprocess_dispatch.py` before burn-in |
| Subprocess hangs (no stdout, no exit) | Medium | Terminal blocked indefinitely | Process still running after 5 min with 0 new events | Add timeout to `read_events()` or watchdog in SubprocessAdapter |
| NDJSON corruption from concurrent writes | Low | SSE serves partial JSON | Malformed line check in validation | fcntl.flock already in place; verify via S4 |
| EventStore.clear() races with SSE reader | Low | SSE gets empty/partial read | Events disappear mid-stream during S4 | LOCK_SH in tail() should prevent; verify |
| Large output exceeds 10MB warning | Low | Performance degradation | Size check in S5 | Already logged; add hard cap if needed |
| claude CLI version incompatible | Low | `--output-format stream-json` not recognized | S1 fails to spawn | Pre-flight: `claude --version` check |
| Process group cleanup fails on macOS | Low | Orphan claude processes | `ps aux` check post-test | os.killpg in stop(); verify with S3 |
| Session JSONL path changes between CLI versions | Medium | S6 comparison fails | Session file not found | Fallback: `find ~/.claude -name "*.jsonl" -newer` |

### Rollback

If burn-in reveals blocking issues:

```bash
# 1. Kill any orphan subprocess processes
pkill -f "claude -p --output-format stream-json" || true

# 2. Disable subprocess adapter
unset VNX_ADAPTER_T1
unset VNX_ADAPTER_T2

# 3. Clear corrupted event files
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T1.ndjson"
rm -f "${VNX_DATA_DIR:-.vnx-data}/events/T2.ndjson"

# 4. Verify tmux delivery still works (should be unaffected)
# Normal dispatch cycle continues via tmux path
```

---

## Appendix: Event Envelope Schema

Each line in `{terminal}.ndjson` follows this schema (from `event_store.py`):

```json
{
  "type": "text|thinking|tool_use|tool_result|result|error|init",
  "timestamp": "2026-04-06T12:00:00.000+00:00",
  "dispatch_id": "burnin-S1-simple-1712400000",
  "terminal": "T1",
  "sequence": 1,
  "data": { /* original stream-json payload */ }
}
```

The `data` field contains the raw event from `claude --output-format stream-json`. The envelope fields (`timestamp`, `dispatch_id`, `terminal`, `sequence`) are added by EventStore.
