#!/usr/bin/env bash
# generate_t0_brief.sh - Generate a <2KB T0 orchestration snapshot (JSON)
# Spec: $VNX_HOME/docs/operations/T0_BRIEF_SCHEMA.md

set -euo pipefail

BRIEF_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$BRIEF_SCRIPT_DIR/lib/vnx_paths.sh"
source "$BRIEF_SCRIPT_DIR/lib/t0_brief_dispatch_helpers.sh"
VNX_DIR="$VNX_HOME"
STATE_DIR="$VNX_STATE_DIR"
DISPATCH_DIR="$VNX_DISPATCH_DIR"

RECEIPTS_PATH="$STATE_DIR/t0_receipts.ndjson"
PROGRESS_STATE_PATH="$STATE_DIR/progress_state.yaml"
OPEN_ITEMS_DIGEST_PATH="$STATE_DIR/open_items_digest.json"

OUT_JSON="$STATE_DIR/t0_brief.json"
OUT_MD="$STATE_DIR/t0_brief.md"
HISTORY_DIR="$STATE_DIR/t0_brief_history"
ERROR_LOG="$STATE_DIR/t0_brief_errors.log"
BRIEF_COMPONENT="generate_t0_brief.sh"

mkdir -p "$STATE_DIR" "$HISTORY_DIR"

json_escape() {
  local value="${1:-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "$value"
}

emit_structured_event() {
  local event_type="$1"
  local classification="$2"
  local code="$3"
  local message="$4"
  local details="${5:-}"
  local ts payload

  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  payload="{\"timestamp\":\"$ts\",\"event\":\"$event_type\",\"component\":\"$BRIEF_COMPONENT\",\"classification\":\"$classification\",\"code\":\"$(json_escape "$code")\",\"message\":\"$(json_escape "$message")\""
  if [ -n "$details" ]; then
    payload+=",\"details\":\"$(json_escape "$details")\""
  fi
  payload+="}"

  printf '%s\n' "$payload" >> "$ERROR_LOG"
  if [ "$event_type" = "failure" ]; then
    printf '%s\n' "$payload" >&2
  fi
}

log_critical_failure() {
  local code="$1"
  local message="$2"
  local details="${3:-}"
  emit_structured_event "failure" "critical" "$code" "$message" "$details"
}

log_non_critical_warning() {
  local code="$1"
  local message="$2"
  local details="${3:-}"
  emit_structured_event "warning" "non_critical" "$code" "$message" "$details"
}

log_error() {
  log_critical_failure "brief_generation_error" "$*"
}

capture_non_critical_output() {
  local __dest="$1"
  local fallback="$2"
  local code="$3"
  local message="$4"
  shift 4

  local output
  if output="$("$@" 2>/dev/null)"; then
    printf -v "$__dest" '%s' "$output"
    return 0
  fi

  log_non_critical_warning "$code" "$message" "fallback=$fallback"
  printf -v "$__dest" '%s' "$fallback"
  return 0
}

cleanup_brief_lock() {
  if [ -d "$BRIEF_LOCK_DIR" ] && ! rm -rf "$BRIEF_LOCK_DIR" 2>/dev/null; then
    log_non_critical_warning "lock_cleanup_failed" "Failed to release brief lock directory" "lock_dir=$BRIEF_LOCK_DIR"
  fi
}

# Prevent concurrent writers racing t0_brief.json updates.
LOCKS_DIR="$STATE_DIR/.locks"
BRIEF_LOCK_DIR="$LOCKS_DIR/generate_t0_brief.lock"
if ! mkdir -p "$LOCKS_DIR" 2>/dev/null; then
  log_critical_failure "lock_dir_create_failed" "Failed to ensure lock directory" "lock_dir=$LOCKS_DIR"
  exit 1
fi
if mkdir "$BRIEF_LOCK_DIR" 2>/dev/null; then
  if ! echo $$ > "$BRIEF_LOCK_DIR/pid" 2>/dev/null; then
    log_non_critical_warning "lock_pid_write_failed" "Failed to persist lock pid metadata" "lock_dir=$BRIEF_LOCK_DIR"
  fi
  trap cleanup_brief_lock EXIT INT TERM
else
  existing_pid=""
  if [ -f "$BRIEF_LOCK_DIR/pid" ] && ! existing_pid="$(cat "$BRIEF_LOCK_DIR/pid" 2>/dev/null)"; then
    log_non_critical_warning "lock_pid_read_failed" "Unable to read existing lock pid" "lock_dir=$BRIEF_LOCK_DIR"
    existing_pid=""
  fi
  if [ -n "$existing_pid" ] && ps -p "$existing_pid" >/dev/null 2>&1; then
    exit 0
  fi
  if ! rm -rf "$BRIEF_LOCK_DIR" 2>/dev/null; then
    log_critical_failure "stale_lock_cleanup_failed" "Failed to remove stale brief lock directory" "lock_dir=$BRIEF_LOCK_DIR"
    exit 1
  fi
  if mkdir "$BRIEF_LOCK_DIR" 2>/dev/null; then
    if ! echo $$ > "$BRIEF_LOCK_DIR/pid" 2>/dev/null; then
      log_non_critical_warning "lock_pid_write_failed" "Failed to persist lock pid metadata after stale lock cleanup" "lock_dir=$BRIEF_LOCK_DIR"
    fi
    trap cleanup_brief_lock EXIT INT TERM
  else
    log_critical_failure "lock_acquire_failed" "Failed to acquire brief lock after stale lock cleanup" "lock_dir=$BRIEF_LOCK_DIR"
    exit 1
  fi
fi

count_md() {
  local dir="$1"
  if [ -d "$dir" ]; then
    find "$dir" -type f -name "*.md" 2>/dev/null | wc -l | tr -d ' '
  else
    echo 0
  fi
}

extract_track_from_dispatch() {
  vnx_brief_extract_track_from_dispatch "$1"
}

extract_gate_from_dispatch() {
  vnx_brief_extract_gate_from_dispatch "$1"
}

derive_next_gate() {
  vnx_brief_derive_next_gate "$1"
}

read_progress_gate() {
  local track="$1"
  local output
  if [ ! -f "$PROGRESS_STATE_PATH" ]; then
    echo ""
    return
  fi

  if ! output="$(python3 - <<PY 2>/dev/null
import sys, yaml
track = "$track"
try:
    with open("$PROGRESS_STATE_PATH","r") as f:
        data = yaml.safe_load(f) or {}
except Exception:
    sys.exit(0)
tracks = data.get("tracks", {}) or {}
t = tracks.get(track, {}) or {}
gate = t.get("current_gate")
if gate:
    sys.stdout.write(str(gate))
PY
)"; then
    log_non_critical_warning "progress_gate_read_failed" "Failed to read current gate from progress state" "track=$track file=$PROGRESS_STATE_PATH"
    echo ""
    return
  fi

  echo "$output"
}

# Phase 4: Read full track data from progress_state for health indicators
read_progress_track_full() {
  local track="$1"
  local output
  if [ ! -f "$PROGRESS_STATE_PATH" ]; then
    echo "{}"
    return
  fi

  if ! output="$(python3 - <<PY 2>/dev/null
import sys, yaml, json
track = "$track"
try:
    with open("$PROGRESS_STATE_PATH","r") as f:
        data = yaml.safe_load(f) or {}
except Exception:
    print("{}")
    sys.exit(0)

tracks = data.get("tracks", {}) or {}
t = tracks.get(track, {}) or {}

# Extract track data
output = {
    "current_gate": t.get("current_gate"),
    "status": t.get("status"),  # working/idle/blocked
    "active_dispatch_id": t.get("active_dispatch_id"),
    "last_receipt": t.get("last_receipt", {}),
    "history_count": len(t.get("history", []))
}

# Calculate health status
status = t.get("status", "")
if status == "blocked":
    output["health"] = "blocked"
elif status == "working" and t.get("active_dispatch_id"):
    output["health"] = "healthy"
elif status == "idle":
    output["health"] = "healthy"
else:
    output["health"] = "unknown"

print(json.dumps(output, separators=(",",":")))
PY
)"; then
    log_non_critical_warning "progress_track_read_failed" "Failed to read full track progress data" "track=$track file=$PROGRESS_STATE_PATH"
    echo "{}"
    return
  fi

  echo "$output"
}

get_recent_receipts_json() {
  local output
  if ! output="$(python3 - <<'PY' 2>/dev/null
import json, os, sys
path = os.environ.get("RECEIPTS_PATH", "")
if not path or not os.path.exists(path):
    print("[]")
    sys.exit(0)

def iter_last_json_lines(fp, max_lines=2500):
    # Read last N lines safely without loading entire file
    with open(fp, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        block = 4096
        data = b""
        while end > 0 and data.count(b"\n") < max_lines:
            start = max(0, end - block)
            f.seek(start)
            chunk = f.read(end - start)
            data = chunk + data
            end = start
            if start == 0:
                break
        for line in data.splitlines()[-max_lines:]:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line.decode("utf-8"))
            except Exception:
                continue

events = list(iter_last_json_lines(path, max_lines=500))
events = events[-3:]
out = []
for e in events:
    out.append({
        "terminal": e.get("terminal"),
        "status": e.get("status"),
        "event_type": e.get("event_type") or e.get("event"),
        "timestamp": e.get("timestamp"),
        "gate": e.get("gate"),
        "task_id": e.get("task_id"),
        "dispatch_id": e.get("dispatch_id"),
    })
print(json.dumps(out, separators=(",",":")))
PY
)"; then
    log_non_critical_warning "recent_receipts_read_failed" "Failed to read recent receipt events" "file=$RECEIPTS_PATH"
    echo "[]"
    return
  fi

  echo "$output"
}

# SPRINT 2: Get recent completion attempts for debugging auto-complete
get_recent_completions_json() {
  local queue_event_log="$STATE_DIR/queue_event_log.jsonl"
  local output
  if ! output="$(python3 - <<'PY' 2>/dev/null
import json, os, sys
event_log_path = os.path.join(os.environ.get("STATE_DIR", ""), "queue_event_log.jsonl")
if not os.path.exists(event_log_path):
    print("[]")
    sys.exit(0)

try:
    with open(event_log_path, 'r') as f:
        lines = f.readlines()
    # Last 5 completion attempts
    completions = []
    for line in reversed(lines):
        try:
            event = json.loads(line)
            if event.get("event") == "completion_attempt":
                completions.append({
                    "pr_id": event.get("pr_id", ""),
                    "dispatch_id": event.get("dispatch_id", ""),
                    "auto_completed": event.get("auto_completed", False),
                    "reason": event.get("reason", ""),
                    "extraction_method": event.get("extraction_method", ""),
                })
                if len(completions) >= 5:
                    break
        except Exception:
            pass
    completions.reverse()  # Chronological order
    print(json.dumps(completions, separators=(",",":")))
except Exception:
    print("[]")
PY
)"; then
    log_non_critical_warning "recent_completions_read_failed" "Failed to read recent completion attempts" "file=$queue_event_log"
    echo "[]"
    return
  fi

  echo "$output"
}

count_completed_last_hour() {
  local output
  if ! output="$(RECEIPTS_PATH="$RECEIPTS_PATH" python3 - <<'PY' 2>/dev/null
import json, os, sys
from datetime import datetime, timedelta, timezone

path = os.environ.get("RECEIPTS_PATH", "")
if not path or not os.path.exists(path):
    print("0")
    sys.exit(0)

cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
count = 0

def parse_ts(ts):
    if not ts:
        return None
    try:
        ts = ts.replace("Z","+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

with open(path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f.readlines()[-500:]:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if (e.get("event_type") or e.get("event")) not in ("task_complete", "quality_gate_verification"):
            continue
        dt = parse_ts(e.get("timestamp"))
        if dt and dt >= cutoff:
            count += 1
print(str(count))
PY
)"; then
    log_non_critical_warning "completed_last_hour_failed" "Failed to count recent completion receipts" "file=$RECEIPTS_PATH"
    echo "0"
    return
  fi

  echo "$output"
}

cleanup_tmp_file() {
  local tmp="$1"
  local failure_context="$2"
  if ! rm -f "$tmp" 2>/dev/null; then
    log_non_critical_warning "brief_tmp_cleanup_failed" "Failed to remove tmp file after $failure_context" "tmp=$tmp"
  fi
}

trim_brief_payload_if_needed() {
  local tmp="$1"
  local size
  size="$(wc -c < "$tmp" | tr -d ' ')"
  if [ "${size:-999999}" -le 2048 ]; then
    return 0
  fi

  if python3 - <<'PY' "$tmp" > "${tmp}.trim" 2>/dev/null
import json, sys
path = sys.argv[1]
with open(path,"r") as f:
    data=json.load(f)
data["recent_receipts"] = (data.get("recent_receipts") or [])[:2]
data["recent_completions"] = (data.get("recent_completions") or [])[:3]
data["active_work"] = (data.get("active_work") or [])[:3]
data["blockers"] = (data.get("blockers") or [])[:3]
print(json.dumps(data, separators=(",",":")))
PY
  then
    if ! mv "${tmp}.trim" "$tmp" 2>/dev/null; then
      log_non_critical_warning "brief_trim_apply_failed" "Failed to apply trimmed brief payload" "tmp=$tmp"
    fi
    return 0
  fi

  log_non_critical_warning "brief_trim_failed" "Failed to trim oversized brief payload; continuing with untrimmed output" "tmp=$tmp size=$size"
  return 0
}

write_history_snapshot() {
  local stamp
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  if ! cp "$OUT_JSON" "$HISTORY_DIR/${stamp}.json" 2>/dev/null; then
    log_non_critical_warning "history_snapshot_failed" "Failed to write brief history snapshot" "history_dir=$HISTORY_DIR"
  fi
  if [ "$(ls -1 "$HISTORY_DIR"/*.json 2>/dev/null | wc -l | tr -d ' ')" -gt 10 ]; then
    if ! ls -t "$HISTORY_DIR"/*.json 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null; then
      log_non_critical_warning "history_prune_failed" "Failed to prune old brief history snapshots" "history_dir=$HISTORY_DIR"
    fi
  fi
}

main() {
  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  local queue_count pending_count active_count conflicts_count
  queue_count="$(count_md "$DISPATCH_DIR/queue")"
  pending_count="$(count_md "$DISPATCH_DIR/pending")"
  active_count="$(count_md "$DISPATCH_DIR/active")"
  conflicts_count="$(count_md "$DISPATCH_DIR/conflicts")"

  local completed_last_hour
  completed_last_hour="$(count_completed_last_hour)"

  local terminal_snapshot_json
  capture_non_critical_output \
    terminal_snapshot_json \
    '{"terminals":{},"degraded":true,"degraded_reasons":["terminal_snapshot_unavailable"]}' \
    "terminal_snapshot_read_failed" \
    "Failed to read terminal snapshot; using degraded fallback" \
    env PYTHONPATH="$BRIEF_SCRIPT_DIR/lib${PYTHONPATH:+:$PYTHONPATH}" \
      python3 "$BRIEF_SCRIPT_DIR/lib/canonical_state_views.py" --state-dir "$STATE_DIR" brief-terminals

  local t1_status t2_status t3_status t1_last t2_last t3_last
  t1_status="$(echo "$terminal_snapshot_json" | jq -r '.terminals.T1.status // "offline"' 2>/dev/null || echo "offline")"
  t2_status="$(echo "$terminal_snapshot_json" | jq -r '.terminals.T2.status // "offline"' 2>/dev/null || echo "offline")"
  t3_status="$(echo "$terminal_snapshot_json" | jq -r '.terminals.T3.status // "offline"' 2>/dev/null || echo "offline")"
  t1_last="$(echo "$terminal_snapshot_json" | jq -r '.terminals.T1.last_update // "never"' 2>/dev/null || echo "never")"
  t2_last="$(echo "$terminal_snapshot_json" | jq -r '.terminals.T2.last_update // "never"' 2>/dev/null || echo "never")"
  t3_last="$(echo "$terminal_snapshot_json" | jq -r '.terminals.T3.last_update // "never"' 2>/dev/null || echo "never")"

  local active_work_json="[]"
  if [ -d "$DISPATCH_DIR/active" ]; then
    if ! active_work_json="$(DISPATCH_ACTIVE_DIR="$DISPATCH_DIR/active" python3 - <<'PY' 2>/dev/null
import json, os
from datetime import datetime, timezone
import glob

dispatch_dir = os.environ.get("DISPATCH_ACTIVE_DIR","")
items = []
for path in sorted(glob.glob(os.path.join(dispatch_dir, "*.md"))):
    try:
        st = os.stat(path)
        started = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00","Z")
        dispatch_id = os.path.basename(path).rsplit(".",1)[0]
        track = ""
        gate = ""
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "[[TARGET:" in line and not track:
                    track = line.split("[[TARGET:",1)[1].split("]]",1)[0].strip()
                if line.strip().lower().startswith("gate:") and not gate:
                    gate = line.split(":",1)[1].strip()
                if track and gate:
                    break
        items.append({
            "dispatch_id": dispatch_id,
            "track": track or None,
            "gate": gate or None,
            "started_at": started
        })
    except Exception:
        continue
items = items[:5]
print(json.dumps(items, separators=(",",":")))
PY
)"; then
      log_non_critical_warning "active_work_scan_failed" "Failed to derive active dispatch work summary" "dispatch_dir=$DISPATCH_DIR/active"
      active_work_json="[]"
    fi
  fi

  local receipts_json
  receipts_json="$(RECEIPTS_PATH="$RECEIPTS_PATH" get_recent_receipts_json)"
  [ -z "$receipts_json" ] && receipts_json="[]"

  # SPRINT 2: Get recent completion attempts for debugging
  local completions_json
  completions_json="$(STATE_DIR="$STATE_DIR" get_recent_completions_json)"
  [ -z "$completions_json" ] && completions_json="[]"

  # Gates removed — replaced by open items system

  # Phase 4: Read full track data for health indicators
  local track_a_full track_b_full track_c_full
  track_a_full="$(read_progress_track_full A)"
  track_b_full="$(read_progress_track_full B)"
  track_c_full="$(read_progress_track_full C)"

  local warnings_json="[]"
  local health_status="healthy"
  local uptime_seconds=0

  # Compute uptime since panes.json (rough proxy for VNX restart) if available
  if [ -f "$STATE_DIR/panes.json" ]; then
    uptime_seconds="$(python3 - <<PY 2>/dev/null || echo 0
import os, time
st=os.stat("$STATE_DIR/panes.json")
print(int(time.time()-st.st_mtime))
PY
)"
  fi

  # Blockers: conflicts
  local blockers_json="[]"
  if ! blockers_json="$(python3 - <<PY 2>/dev/null
import json
blockers=[]
conflicts=int("${conflicts_count:-0}")
if conflicts>0:
    blockers.append({
        "type":"conflict",
        "severity":"medium" if conflicts<3 else "high",
        "track":None,
        "terminal":None,
        "message":f"{conflicts} dispatch(es) blocked in conflicts/",
        "since":None,
        "duration_seconds":None,
        "resolution_path":"Review ${VNX_DISPATCH_DIR}/conflicts/"
    })
print(json.dumps(blockers, separators=(",",":")))
PY
)"; then
    log_non_critical_warning "blockers_build_failed" "Failed to build blockers summary payload" "conflicts_count=${conflicts_count:-0}"
    blockers_json="[]"
  fi
  [ -z "$blockers_json" ] && blockers_json="[]"

  # Health degradation based on terminal snapshot status
  local snapshot_degraded snapshot_reasons
  snapshot_degraded="$(echo "$terminal_snapshot_json" | jq -r '.degraded // false' 2>/dev/null || echo "false")"
  snapshot_reasons="$(echo "$terminal_snapshot_json" | jq -c '.degraded_reasons // []' 2>/dev/null || echo "[]")"
  if [ "$snapshot_degraded" = "true" ]; then
    health_status="degraded"
    warnings_json="$snapshot_reasons"
  fi

  if [ "$t1_status" = "offline" ] || [ "$t2_status" = "offline" ] || [ "$t3_status" = "offline" ]; then
    health_status="degraded"
  fi

  # Assemble brief (compact JSON)
  local tmp
  if ! tmp="$(mktemp "$OUT_JSON.tmp.XXXXXX")"; then
    log_critical_failure "brief_tmp_create_failed" "Failed to create temporary brief output file" "out_json=$OUT_JSON"
    return 1
  fi

  if ! T0_BRIEF_TIMESTAMP="$now" \
  T0_BRIEF_T1_STATUS="$t1_status" T0_BRIEF_T2_STATUS="$t2_status" T0_BRIEF_T3_STATUS="$t3_status" \
  T0_BRIEF_T1_LAST="$t1_last" T0_BRIEF_T2_LAST="$t2_last" T0_BRIEF_T3_LAST="$t3_last" \
  T0_BRIEF_QUEUE_PENDING="$queue_count" T0_BRIEF_QUEUE_ACTIVE="$active_count" T0_BRIEF_QUEUE_CONFLICTS="$conflicts_count" \
  T0_BRIEF_COMPLETED_1H="$completed_last_hour" \
  T0_BRIEF_UPTIME_SECONDS="$uptime_seconds" T0_BRIEF_HEALTH_STATUS="$health_status" \
  T0_BRIEF_WARNINGS_JSON="$warnings_json" \
  T0_BRIEF_ACTIVE_WORK_JSON="$active_work_json" \
  T0_BRIEF_RECENT_RECEIPTS_JSON="$receipts_json" \
  T0_BRIEF_RECENT_COMPLETIONS_JSON="$completions_json" \
  T0_BRIEF_BLOCKERS_JSON="$blockers_json" \
  T0_BRIEF_TRACK_A_JSON="$track_a_full" T0_BRIEF_TRACK_B_JSON="$track_b_full" T0_BRIEF_TRACK_C_JSON="$track_c_full" \
  T0_BRIEF_WORKING_STALE_SECONDS="${T0_BRIEF_WORKING_STALE_SECONDS:-3600}" \
  python3 - <<'PY' > "$tmp" 2>/dev/null
import json
import os
from datetime import datetime, timezone

def to_int(v, default=0):
    try:
        return int(str(v))
    except Exception:
        return default

def to_status(v, default="offline"):
    v = (v or "").strip()
    return v if v else default

def ready_from_status(status):
    return status == "idle"

def json_load(v, default):
    try:
        if not v or not v.strip():
            return default
        return json.loads(v)
    except Exception:
        return default

ts = os.environ.get("T0_BRIEF_TIMESTAMP", "")
t1_status = to_status(os.environ.get("T0_BRIEF_T1_STATUS"))
t2_status = to_status(os.environ.get("T0_BRIEF_T2_STATUS"))
t3_status = to_status(os.environ.get("T0_BRIEF_T3_STATUS"))
t1_last = os.environ.get("T0_BRIEF_T1_LAST", "never")
t2_last = os.environ.get("T0_BRIEF_T2_LAST", "never")
t3_last = os.environ.get("T0_BRIEF_T3_LAST", "never")
stale_seconds = to_int(os.environ.get("T0_BRIEF_WORKING_STALE_SECONDS"), 3600)

# Phase 4: Parse track progress_state data
track_a_data = json_load(os.environ.get("T0_BRIEF_TRACK_A_JSON", ""), {})
track_b_data = json_load(os.environ.get("T0_BRIEF_TRACK_B_JSON", ""), {})
track_c_data = json_load(os.environ.get("T0_BRIEF_TRACK_C_JSON", ""), {})

def parse_ts(val):
    if not val or val in ("never", "unknown"):
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except Exception:
        return None

def status_age_seconds(track_data, fallback_ts):
    receipt = track_data.get("last_receipt") or {}
    receipt_ts = parse_ts(receipt.get("timestamp"))
    if not receipt_ts:
        receipt_ts = parse_ts(fallback_ts)
    if not receipt_ts:
        return None
    now = datetime.now(timezone.utc)
    if receipt_ts.tzinfo is None:
        receipt_ts = receipt_ts.replace(tzinfo=timezone.utc)
    return max(0, int((now - receipt_ts).total_seconds()))

def terminal_age_seconds(last_update_ts):
    parsed = parse_ts(last_update_ts)
    if not parsed:
        return None
    now = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((now - parsed).total_seconds()))

def terminal_status_from(track_data, terminal_state_status, terminal_state_last):
    # Canonical terminal_state indicates terminal health first.
    if terminal_state_status == "offline":
        return "offline", "terminal_state"
    if terminal_state_status == "idle":
        return "idle", "terminal_state_priority"

    track_status = (track_data.get("status") or "").strip()
    active_dispatch_id = track_data.get("active_dispatch_id")
    age_seconds = status_age_seconds(track_data, terminal_state_last)

    # Never downgrade a claimed/working terminal to idle from stale progress hints.
    if terminal_state_status in ("working", "blocked") and active_dispatch_id:
        return terminal_state_status, "terminal_state_priority"

    if track_status in ("idle", "working", "blocked"):
        if track_status == "idle" and active_dispatch_id:
            return "working", "progress_state_claim"
        if track_status == "working":
            # SPRINT 2 FIX: If there's an active dispatch, terminal IS working (ignore receipt age)
            if active_dispatch_id:
                return "working", "progress_state"
            # No active dispatch - allow canonical terminal fallback when progress receipt is stale.
            if age_seconds is not None and age_seconds > stale_seconds and terminal_state_status in ("idle", "offline", "blocked"):
                return terminal_state_status, "terminal_state_fallback"
        return track_status, "progress_state"

    return terminal_state_status, "terminal_state"

t1_status, t1_source = terminal_status_from(track_a_data, t1_status, t1_last)
t2_status, t2_source = terminal_status_from(track_b_data, t2_status, t2_last)
t3_status, t3_source = terminal_status_from(track_c_data, t3_status, t3_last)

t1_age = terminal_age_seconds(t1_last)
t2_age = terminal_age_seconds(t2_last)
t3_age = terminal_age_seconds(t3_last)

def current_task_for(status, track_data):
  if status == "idle":
    return None
  return (track_data or {}).get("active_dispatch_id")

brief = {
    "timestamp": ts,
    "version": "1.0",
    "terminals": {
        "T1": {"status": t1_status, "track": "A", "ready": ready_from_status(t1_status), "last_update": t1_last, "current_task": current_task_for(t1_status, track_a_data), "source": t1_source, "status_age_seconds": t1_age},
        "T2": {"status": t2_status, "track": "B", "ready": ready_from_status(t2_status), "last_update": t2_last, "current_task": current_task_for(t2_status, track_b_data), "source": t2_source, "status_age_seconds": t2_age},
        "T3": {"status": t3_status, "track": "C", "ready": ready_from_status(t3_status), "last_update": t3_last, "current_task": current_task_for(t3_status, track_c_data), "source": t3_source, "status_age_seconds": t3_age},
    },
    "queues": {
        "pending": to_int(os.environ.get("T0_BRIEF_QUEUE_PENDING")),
        "active": to_int(os.environ.get("T0_BRIEF_QUEUE_ACTIVE")),
        "completed_last_hour": to_int(os.environ.get("T0_BRIEF_COMPLETED_1H")),
        "conflicts": to_int(os.environ.get("T0_BRIEF_QUEUE_CONFLICTS")),
    },
    "tracks": {
        "A": track_a_data,
        "B": track_b_data,
        "C": track_c_data,
    },
    "active_work": json_load(os.environ.get("T0_BRIEF_ACTIVE_WORK_JSON", ""), []),
    "recent_receipts": json_load(os.environ.get("T0_BRIEF_RECENT_RECEIPTS_JSON", ""), []),
    "recent_completions": json_load(os.environ.get("T0_BRIEF_RECENT_COMPLETIONS_JSON", ""), []),
    "blockers": json_load(os.environ.get("T0_BRIEF_BLOCKERS_JSON", ""), []),
    # next_gates removed — replaced by open items system
    "system_health": {
        "status": os.environ.get("T0_BRIEF_HEALTH_STATUS", "degraded"),
        "uptime_seconds": to_int(os.environ.get("T0_BRIEF_UPTIME_SECONDS")),
        "warnings": json_load(os.environ.get("T0_BRIEF_WARNINGS_JSON", "[]"), []),
    },
}

# Remove nullish keys to stay under size
def prune(obj):
  if isinstance(obj, dict):
    return {k: prune(v) for k,v in obj.items() if v not in (None,"",[],{})}
  if isinstance(obj, list):
    return [prune(v) for v in obj][:5]
  return obj

brief = prune(brief)
print(json.dumps(brief, separators=(",",":")))
PY
  then
    log_critical_failure "brief_json_generation_failed" "Failed to generate base t0_brief JSON payload" "tmp=$tmp"
    cleanup_tmp_file "$tmp" "generation error"
    return 1
  fi

  trim_brief_payload_if_needed "$tmp"

  # Validate JSON and atomically write
  if command -v jq >/dev/null 2>&1; then
    if ! jq -e . "$tmp" >/dev/null 2>&1; then
      log_error "Invalid JSON generated, keeping last valid brief"
      rm -f "$tmp"
      return 1
    fi
  fi

  if ! mv "$tmp" "$OUT_JSON"; then
    log_critical_failure "brief_atomic_write_failed" "Failed to atomically write t0_brief.json" "tmp=$tmp out_json=$OUT_JSON"
    cleanup_tmp_file "$tmp" "atomic write failure"
    return 1
  fi

  # Write history snapshot (best-effort)
  write_history_snapshot

  # Add open items summary if digest exists
  if [ -f "$OPEN_ITEMS_DIGEST_PATH" ]; then
    # Extract open items summary
    open_items_summary=$(jq -c '{
      open_count: .summary.open_count,
      blocker_count: .summary.blocker_count,
      top_blockers: .top_blockers[0:2]
    }' "$OPEN_ITEMS_DIGEST_PATH" 2>/dev/null || echo '{}')

    # Add to brief JSON
    jq --argjson openitems "$open_items_summary" \
       '.open_items_summary = $openitems' "$OUT_JSON" > "$OUT_JSON.tmp"
    mv "$OUT_JSON.tmp" "$OUT_JSON"
  fi

  # Add PR progress tracking if pr_queue_state.yaml exists
  if [ -f "$STATE_DIR/pr_queue_state.yaml" ]; then
    # Parse PR queue state using yq if available, fallback to python
    if command -v yq >/dev/null 2>&1; then
      total_prs=$(yq '.execution_order | length' "$STATE_DIR/pr_queue_state.yaml" 2>/dev/null || echo 0)
      completed=$(yq '.completed_prs | length' "$STATE_DIR/pr_queue_state.yaml" 2>/dev/null || echo 0)
      in_progress=$(yq '.in_progress // "none"' "$STATE_DIR/pr_queue_state.yaml" 2>/dev/null || echo "none")
    else
      # Fallback to python parsing
      read total_prs completed in_progress <<< $(STATE_FILE="$STATE_DIR/pr_queue_state.yaml" python3 - <<'PY' 2>/dev/null || echo "0 0 none"
import yaml, os
try:
    with open(os.environ["STATE_FILE"], "r") as f:
        data = yaml.safe_load(f) or {}
    total = len(data.get("execution_order", []))
    completed = len(data.get("completed_prs", []))
    in_prog = data.get("in_progress") or "none"
    print(f"{total} {completed} {in_prog}")
except Exception:
    print("0 0 none")
PY
)
    fi

    # Calculate percentage
    if [ "$total_prs" -gt 0 ]; then
      percent=$((completed * 100 / total_prs))
    else
      percent=0
    fi

    # Add to brief JSON using jq
    jq --arg total "$total_prs" \
       --arg completed "$completed" \
       --arg in_progress "$in_progress" \
       --arg percent "$percent" \
       '.pr_progress = {
         total: ($total | tonumber),
         completed: ($completed | tonumber),
         in_progress: $in_progress,
         completion_percentage: ($percent | tonumber)
       }' "$OUT_JSON" > "$OUT_JSON.tmp"
    mv "$OUT_JSON.tmp" "$OUT_JSON"
  fi

  # Generate STATE.md (GSD pattern for instant resume)
  local STATE_MD="$STATE_DIR/STATE.md"
  local timestamp=$(date -Iseconds)
  local health=$(jq -r '.system_health.status' "$OUT_JSON")
  local active_work=$(jq -r '.terminals | to_entries[] | "- Track \(.key): \(.value.status) - \(.value.current_task // "idle")"' "$OUT_JSON")
  local recent_activity=$(jq -r '.recent_receipts[0:3][] | "- [\(.event_type)] \(.dispatch_id // .task_id) - \(.status)"' "$OUT_JSON")

  # Start STATE.md
  cat > "$STATE_MD" << EOF
# VNX System State

**Updated**: $timestamp
**Health**: $health

## Active Work
$active_work

## PR Progress
EOF

  # Add PR progress section
  if [ -f "$STATE_DIR/pr_queue_state.yaml" ]; then
    local feature_name progress_pct progress_completed progress_total completed_list in_prog next_pr

    progress_pct=$(jq -r '.pr_progress.completion_percentage // 0' "$OUT_JSON")
    progress_completed=$(jq -r '.pr_progress.completed // 0' "$OUT_JSON")
    progress_total=$(jq -r '.pr_progress.total // 0' "$OUT_JSON")

    if command -v yq >/dev/null 2>&1; then
      feature_name=$(yq '.active_feature.name' "$STATE_DIR/pr_queue_state.yaml" 2>/dev/null || echo "None")
      completed_list=$(yq '.completed_prs | join(", ")' "$STATE_DIR/pr_queue_state.yaml" 2>/dev/null || echo "none")
      in_prog=$(yq '.in_progress // "none"' "$STATE_DIR/pr_queue_state.yaml" 2>/dev/null || echo "none")
      next_pr=$(yq '.next_available[0] // "none"' "$STATE_DIR/pr_queue_state.yaml" 2>/dev/null || echo "none")
    else
      # Fallback to Python (use tab separator to handle spaces in names)
      local py_output
      py_output=$(STATE_FILE="$STATE_DIR/pr_queue_state.yaml" python3 - <<'PY' 2>/dev/null || echo "None	none	none	none"
import yaml, os
try:
    with open(os.environ["STATE_FILE"], "r") as f:
        data = yaml.safe_load(f) or {}
    feature = data.get("active_feature", {}).get("name", "None")
    completed = data.get("completed_prs", [])
    completed_str = ", ".join(completed) if completed else "none"
    in_prog = data.get("in_progress") or "none"
    next_available = data.get("next_available", [])
    next_pr = next_available[0] if next_available else "none"
    print(f"{feature}\t{completed_str}\t{in_prog}\t{next_pr}")
except Exception:
    print("None\tnone\tnone\tnone")
PY
)
      IFS=$'\t' read -r feature_name completed_list in_prog next_pr <<< "$py_output"
    fi

    cat >> "$STATE_MD" << EOF
**Feature**: $feature_name
**Progress**: $progress_pct% ($progress_completed/$progress_total)

EOF
    if [ "$completed_list" != "[]" ] && [ "$completed_list" != "none" ]; then
      echo "✅ Completed: $completed_list" >> "$STATE_MD"
    else
      echo "✅ Completed: none" >> "$STATE_MD"
    fi
    echo "🔄 In Progress: $in_prog" >> "$STATE_MD"
    echo "⏳ Next: $next_pr" >> "$STATE_MD"
  else
    echo "No active feature" >> "$STATE_MD"
  fi

  # Add recent activity section
  cat >> "$STATE_MD" << EOF

## Recent Activity
$recent_activity
EOF

  # Update markdown brief with PR progress and open items
  if command -v jq >/dev/null 2>&1; then
    if ! jq -r '
      "# T0 Brief\n"
      + "- Time: \(.timestamp)\n"
      + "- Health: \(.system_health.status)\n"
      + "- Queues: pending=\(.queues.pending) active=\(.queues.active) conflicts=\(.queues.conflicts // 0) completed_1h=\(.queues.completed_last_hour)\n"
      + "- Terminals: T1=\(.terminals.T1.status) T2=\(.terminals.T2.status) T3=\(.terminals.T3.status)\n"
      + (if .open_items_summary then "- Open Items: \(.open_items_summary.open_count // 0) total (\(.open_items_summary.blocker_count // 0) blockers)\n" else "" end)
      + (if .pr_progress then "- PR Progress: \(.pr_progress.completion_percentage)% (\(.pr_progress.completed)/\(.pr_progress.total))\n" else "" end)
    ' "$OUT_JSON" > "$OUT_MD" 2>/dev/null; then
      log_non_critical_warning "markdown_brief_render_failed" "Failed to render markdown brief output" "out_md=$OUT_MD"
    fi
  fi
}

if ! main; then
  exit 1
fi
