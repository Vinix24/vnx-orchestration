# shellcheck shell=bash
# rp_pattern.sh - Pattern usage tracking and progress_state updates
# Sourced by scripts/receipt_processor.sh
# Requires: log() from rp_logging.sh, $SCRIPTS_DIR, $STATE_DIR, $VNX_STATE_DIR (env),
#           rp_end_line.sh (rp_end_outcome),
#           and _rf_* fields populated by extract_receipt_fields()

# ADR-045 §2: a dispatch_finished is an end line too. Sourced here so this file
# is correct on its own, not only under receipt_processor.sh.
# shellcheck source=receipt_processor/rp_end_line.sh
source "$(cd -P "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)/rp_end_line.sh"

# DRY helper: invoke update_progress_state.py with common receipt fields.
# Usage: _call_progress_update <track> [extra_flags...]
_call_progress_update() {
    local track="$1"; shift
    python3 "$SCRIPTS_DIR/update_progress_state.py" \
        --track "$track" \
        "$@" \
        --receipt-event "$_rf_event_type" \
        --receipt-status "$_rf_status" \
        --receipt-timestamp "$_rf_timestamp" \
        --receipt-dispatch-id "$_rf_dispatch_id" \
        --updated-by receipt_processor 2>&1
}

# Sub-helper: Update pattern usage counts in quality_intelligence.db (non-fatal).
_track_pattern_usage() {
    local receipt_json="$1"
    local used_hashes
    used_hashes=$(echo "$receipt_json" | jq -r '.used_pattern_hashes // empty | join(",")' 2>/dev/null)
    [ -z "$used_hashes" ] && return 0
    python3 - "$used_hashes" <<'PY'
import os, sys, sqlite3
from datetime import datetime
hashes = [h.strip().lower() for h in sys.argv[1].split(",") if h.strip()]
if not hashes:
    sys.exit(0)
state_dir = os.environ.get("VNX_STATE_DIR")
if not state_dir:
    raise RuntimeError("VNX_STATE_DIR not set")
db_path = os.path.join(state_dir, "quality_intelligence.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# O(1) lookup via indexed pattern_hash column in snippet_metadata
placeholders = ",".join("?" for _ in hashes)
rows = cur.execute(
    f"SELECT sm.snippet_rowid, sm.pattern_hash, cs.title, cs.usage_count "
    f"FROM snippet_metadata sm "
    f"JOIN code_snippets cs ON cs.rowid = sm.snippet_rowid "
    f"WHERE sm.pattern_hash IN ({placeholders})",
    hashes
).fetchall()

updated = 0
now = datetime.utcnow().isoformat()
for row in rows:
    new_count = int(row["usage_count"] or 0) + 1
    cur.execute("UPDATE code_snippets SET usage_count = ?, last_updated = ? WHERE rowid = ?",
                (new_count, now, row["snippet_rowid"]))
    cur.execute("""
        INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, last_used, confidence)
        VALUES (?, ?, ?, 1, ?, 1.0)
        ON CONFLICT(pattern_id) DO UPDATE SET
            used_count = used_count + 1,
            last_used = excluded.last_used,
            updated_at = CURRENT_TIMESTAMP
    """, (row["pattern_hash"], row["title"], row["pattern_hash"], now))
    updated += 1
if updated:
    conn.commit()
conn.close()
PY
}

# Sub-helper: Fallback success credit for recently offered patterns (non-fatal).
# When a receipt has status=success but NO used_pattern_hashes, give partial
# credit (success_count += 1) to patterns offered within the last 2 hours.
_track_pattern_success_fallback() {
    local receipt_json="$1"
    local status
    status=$(echo "$receipt_json" | jq -r '.status // ""' 2>/dev/null)
    local event_type
    event_type=$(echo "$receipt_json" | jq -r '.event_type // .event // ""' 2>/dev/null)
    local used_hashes
    used_hashes=$(echo "$receipt_json" | jq -r '.used_pattern_hashes // empty | join(",")' 2>/dev/null)

    # Only trigger on a successful completion with no explicit
    # used_pattern_hashes: the legacy task_complete (read literally, unchanged)
    # or an ADR-045 dispatch_finished whose end_state classifies as success.
    # A dispatch_finished with a missing/unknown end_state classifies as none,
    # so it gives no credit (fail-closed) — and its raw status field is never
    # consulted.
    case "$event_type" in
        task_complete) ;;
        dispatch_finished)
            # A dead end-line helper is a failure, not a verdict: the failure is
            # recorded (end_line_helper_failed) and no credit is given.
            if ! status=$(rp_end_outcome "$receipt_json"); then
                status=""
            fi
            ;;
        *) return 0 ;;
    esac
    [ "$status" != "success" ] && return 0
    [ -n "$used_hashes" ] && return 0

    python3 - <<'PY'
import os, sys, sqlite3
from datetime import datetime, timedelta
state_dir = os.environ.get("VNX_STATE_DIR")
if not state_dir:
    sys.exit(0)
db_path = os.path.join(state_dir, "quality_intelligence.db")
if not os.path.exists(db_path):
    sys.exit(0)
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cutoff = (datetime.utcnow() - timedelta(hours=2)).isoformat()
rows = conn.execute('''
    SELECT pattern_id FROM pattern_usage
    WHERE last_offered >= ? AND last_offered IS NOT NULL
''', (cutoff,)).fetchall()
if not rows:
    conn.close()
    sys.exit(0)
now = datetime.utcnow().isoformat()
updated = 0
for row in rows:
    conn.execute('''
        UPDATE pattern_usage
        SET success_count = success_count + 1, updated_at = ?
        WHERE pattern_id = ?
    ''', (now, row['pattern_id']))
    updated += 1
if updated:
    conn.commit()
conn.close()
PY
}

# Sub-helper: Read active_dispatch_id from progress_state.yaml for a track.
_get_active_dispatch() {
    local track="$1"
    [ ! -f "$STATE_DIR/progress_state.yaml" ] && return 0
    python3 -c "
import yaml
try:
    with open('$STATE_DIR/progress_state.yaml', 'r') as f:
        data = yaml.safe_load(f)
        print(data.get('tracks', {}).get('$track', {}).get('active_dispatch_id', ''))
except (OSError, yaml.YAMLError, AttributeError, TypeError):
    print('')
" 2>/dev/null
}

_track_from_terminal() {
    local terminal="$1"
    case "$terminal" in
        T1) echo "A" ;;
        T2) echo "B" ;;
        T3) echo "C" ;;
        *) echo "" ;;
    esac
}

# Ensure completion/start receipts carry a concrete dispatch_id whenever possible.
_hydrate_receipt_identity() {
    local receipt_json="$1"
    local terminal="$2"

    local current_dispatch_id
    current_dispatch_id=$(echo "$receipt_json" | jq -r '.dispatch_id // ""' 2>/dev/null)
    local current_dispatch_id_lc
    current_dispatch_id_lc=$(printf '%s' "$current_dispatch_id" | tr '[:upper:]' '[:lower:]')
    case "$current_dispatch_id_lc" in
        ""|"unknown"|"none"|"null")
            ;;
        *)
            echo "$receipt_json"
            return 0
            ;;
    esac

    local track
    track=$(_track_from_terminal "$terminal")
    if [ -z "$track" ]; then
        echo "$receipt_json"
        return 0
    fi

    local active_dispatch_id
    active_dispatch_id=$(_get_active_dispatch "$track")
    local active_dispatch_id_lc
    active_dispatch_id_lc=$(printf '%s' "$active_dispatch_id" | tr '[:upper:]' '[:lower:]')
    case "$active_dispatch_id_lc" in
        ""|"unknown"|"none"|"null")
            echo "$receipt_json"
            return 0
            ;;
    esac

    # Also fill task_id when missing to keep completion evidence correlated.
    echo "$receipt_json" | jq --arg dispatch "$active_dispatch_id" '
        .dispatch_id = $dispatch
        | if ((.task_id // "" | ascii_downcase) == "unknown") or ((.task_id // "") == "") then .task_id = $dispatch else . end
    ' 2>/dev/null || echo "$receipt_json"
}

# Section E: Update progress_state.yaml based on receipt events.
# Reads _rf_* variables. Non-fatal.
update_track_progress() {
    local receipt_json="$1"
    local terminal="$2"

    [ ! -f "$SCRIPTS_DIR/update_progress_state.py" ] && return 0

    local track=""
    track=$(_track_from_terminal "$terminal")
    [ -z "$track" ] && return 0

    log "INFO" "PROGRESS_STATE: Processing receipt for Track $track (event=$_rf_event_type, status=$_rf_status)"
    local current_active_dispatch
    current_active_dispatch=$(_get_active_dispatch "$track")

    # ADR-045 §2: a successful dispatch_finished closes the track exactly like a
    # successful task_complete. The legacy branch keeps its literal status test,
    # unchanged; the new one asks the end_line helper for the classification.
    local completion_success="false"
    if [ "$_rf_event_type" = "task_complete" ] && [ "$_rf_status" = "success" ]; then
        completion_success="true"
    elif [ "$_rf_event_type" = "dispatch_finished" ]; then
        # The helper's exit status is checked before its verdict is believed: a
        # failed helper (recorded as end_line_helper_failed) is no close, never
        # an unread empty string compared against "success".
        local dispatch_outcome=""
        if dispatch_outcome=$(rp_end_outcome "$receipt_json") \
           && [ "$dispatch_outcome" = "success" ]; then
            completion_success="true"
        fi
    fi

    if [ "$completion_success" = "true" ]; then
        _call_progress_update "$track" --status idle --dispatch-id ""
        log "INFO" "PROGRESS_STATE: Task completed → Track $track idle"
    elif [ "$_rf_event_type" = "task_started" ]; then
        _call_progress_update "$track"
        log "INFO" "PROGRESS_STATE: Recorded task_started for Track $track"
    elif [ "$_rf_event_type" = "task_timeout" ] && [ "$_rf_status" = "no_confirmation" ] \
         && [ -n "$_rf_dispatch_id" ] && [ "$_rf_dispatch_id" = "$current_active_dispatch" ]; then
        _call_progress_update "$track" --status blocked --dispatch-id "$_rf_dispatch_id"
        log "WARN" "PROGRESS_STATE: Track $track blocked (awaiting confirmation on $_rf_dispatch_id)"
    elif [ -n "$_rf_event_type" ] || [ -n "$_rf_status" ]; then
        _call_progress_update "$track" --status idle --dispatch-id ""
        log "INFO" "PROGRESS_STATE: Track $track idle (ready for new work)"
    fi
}
