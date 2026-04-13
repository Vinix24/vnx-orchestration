#!/usr/bin/env bash
# VNX Nightly Intelligence Pipeline — Consolidated (PR-4)
#
# Replaces two overlapping schedules:
#   - intelligence_daemon.py daily hygiene at 18:00
#   - conversation_analyzer_nightly.sh at 02:00
#
# Runs all intelligence phases in dependency order.
# A failure in any phase is logged but does NOT block subsequent phases.
#
# Install launchd plist (runs at 02:00 daily):
#
#   <?xml version="1.0" encoding="UTF-8"?>
#   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
#   <plist version="1.0">
#   <dict>
#     <key>Label</key><string>com.vnx.nightly-intelligence-pipeline</string>
#     <key>ProgramArguments</key>
#     <array>
#       <string>/bin/bash</string>
#       <string>PATH_TO_THIS_SCRIPT</string>
#     </array>
#     <key>StartCalendarInterval</key>
#     <dict>
#       <key>Hour</key><integer>2</integer>
#       <key>Minute</key><integer>0</integer>
#     </dict>
#     <key>StandardOutPath</key><string>/tmp/vnx-nightly-pipeline.log</string>
#     <key>StandardErrorPath</key><string>/tmp/vnx-nightly-pipeline.err</string>
#   </dict>
#   </plist>
#
# Activate:   launchctl load ~/Library/LaunchAgents/com.vnx.nightly-intelligence-pipeline.plist
# Deactivate old schedules:
#   launchctl unload ~/Library/LaunchAgents/com.vnx.conversation-analyzer.plist
#   Set VNX_DAILY_INTEL_REFRESH=0 env var to prevent daemon from duplicating hygiene

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"
ensure_env

# Load user environment (VNX_DIGEST_EMAIL, VNX_SMTP_PASS, etc.)
for _rc in "$HOME/.zprofile" "$HOME/.zshrc"; do
    if [ -f "$_rc" ]; then
        eval "$(grep '^export VNX_' "$_rc" 2>/dev/null || true)"
    fi
done

LOG_FILE="$VNX_STATE_DIR/nightly_pipeline.log"
LOCK_FILE="$VNX_STATE_DIR/nightly_pipeline.lock"
PHASES_LOG="$VNX_STATE_DIR/nightly_pipeline_phases.ndjson"
HEALTH_FILE="$VNX_STATE_DIR/nightly_pipeline_health.json"
DB_PATH="$VNX_STATE_DIR/quality_intelligence.db"

PIPELINE_START="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
PHASES_RUN=0
PHASES_OK=0
declare -a PHASES_FAILED=()

# ── Helpers ───────────────────────────────────────────────────────────────────

log_msg() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log_phase_result() {
    local phase="$1" status="$2" detail="${3:-}"
    printf '{"phase":"%s","status":"%s","detail":"%s","ts":"%s"}\n' \
        "$phase" "$status" "$detail" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        >> "$PHASES_LOG"
}

# Run a pipeline phase: always returns 0 so subsequent phases are not blocked.
# Usage: run_phase <phase-name> <command> [args...]
run_phase() {
    local phase_name="$1"; shift
    PHASES_RUN=$((PHASES_RUN + 1))
    log_msg "Phase $phase_name: starting..."

    local exit_code=0
    "$@" 2>&1 | tee -a "$LOG_FILE" || exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        PHASES_OK=$((PHASES_OK + 1))
        log_msg "Phase $phase_name: OK"
        log_phase_result "$phase_name" "ok"
    else
        PHASES_FAILED+=("$phase_name")
        log_msg "Phase $phase_name: FAILED (exit=$exit_code) — continuing"
        log_phase_result "$phase_name" "failed" "exit=$exit_code"
    fi
    return 0
}

db_accessible() {
    python3 - <<'PY' 2>/dev/null
import sqlite3, os, sys
db = os.environ.get("VNX_STATE_DIR", "") + "/quality_intelligence.db"
try:
    sqlite3.connect(db).execute("SELECT 1")
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
}

# ── Singleton enforcement ─────────────────────────────────────────────────────

if [ -f "$LOCK_FILE" ]; then
    pid="$(cat "$LOCK_FILE" 2>/dev/null || echo "")"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log_msg "Already running (PID $pid), skipping"
        exit 0
    fi
    log_msg "Stale lock file found, removing"
    rm -f "$LOCK_FILE"
fi

echo $$ > "$LOCK_FILE"
cleanup() { rm -f "$LOCK_FILE"; }
trap cleanup EXIT

# ── Start ─────────────────────────────────────────────────────────────────────

log_msg "=== VNX Nightly Intelligence Pipeline starting (PID $$) ==="
log_msg "State dir: $VNX_STATE_DIR"

# ── Phase 0: DB schema migrations ────────────────────────────────────────────
run_phase "0-schema-init" python3 "$SCRIPT_DIR/quality_db_init.py"

# ── Health gate after schema init ────────────────────────────────────────────
if ! db_accessible; then
    log_msg "WARN: quality_intelligence.db not accessible after schema init — intelligence phases may produce empty output"
fi

# ── Phase 1: Intelligence quality refresh (replaces daemon daily hygiene) ────
run_phase "1a-quality-scan"    python3 "$SCRIPT_DIR/code_quality_scanner.py"
run_phase "1b-snippet-extract" python3 "$SCRIPT_DIR/code_snippet_extractor.py"
run_phase "1c-doc-extract"     python3 "$SCRIPT_DIR/doc_section_extractor.py"

# ── Phase 2: Conversation analysis ───────────────────────────────────────────
run_phase "2-conversation-analyze" python3 "$SCRIPT_DIR/conversation_analyzer.py" \
    --max-sessions 50 \
    --deep-budget 20

# ── Phase 3: Session-dispatch linkage ────────────────────────────────────────
run_phase "3-session-dispatch-link" python3 "$SCRIPT_DIR/link_sessions_dispatches.py"

# ── Health gate: session analytics row count ─────────────────────────────────
SESSION_COUNT="$(python3 - <<'PY' 2>/dev/null || echo "0"
import sqlite3, os
db = os.environ.get("VNX_STATE_DIR","") + "/quality_intelligence.db"
try:
    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM session_analytics").fetchone()[0]
    print(n)
except Exception:
    print(0)
PY
)"
log_msg "Health check: session_analytics rows=${SESSION_COUNT}"

# ── Phase 4: Learning cycle ───────────────────────────────────────────────────
run_phase "4-learning-cycle" python3 "$SCRIPT_DIR/learning_loop.py" run

# ── Phase 4b: Weekly digest ───────────────────────────────────────────────────
run_phase "4b-weekly-digest" python3 "$SCRIPT_DIR/weekly_digest.py"

# ── Phase 5: Mark stale pending edits ────────────────────────────────────────
run_phase "5-stale-edits" python3 "$SCRIPT_DIR/tag_intelligence.py" stale

# ── Phase 6: T0 session brief ────────────────────────────────────────────────
run_phase "6-session-brief" python3 "$SCRIPT_DIR/generate_t0_session_brief.py"

# ── Phase 7: Governance aggregation ──────────────────────────────────────────
run_phase "7-governance" python3 "$SCRIPT_DIR/governance_aggregator.py" --backfill

# ── Phase 8: Suggested edits (human-in-the-loop) ─────────────────────────────
run_phase "8-suggested-edits" python3 "$SCRIPT_DIR/generate_suggested_edits.py"

# ── Phase 9: Quality digest — 3-section NDJSON format ────────────────────────
run_phase "9-quality-digest" python3 "$SCRIPT_DIR/build_t0_quality_digest.py"

# ── Phase 10: Recommendations engine — 24h lookback ─────────────────────────
run_phase "10-recommendations" python3 "$SCRIPT_DIR/generate_t0_recommendations.py" \
    --lookback 1440

# ── Phase 11: Email digest (optional) ────────────────────────────────────────
if [ -n "${VNX_DIGEST_EMAIL:-}" ] && [ -n "${VNX_SMTP_PASS:-}" ]; then
    run_phase "11-email-digest" python3 "$SCRIPT_DIR/send_digest_email.py"
else
    log_msg "Phase 11 (email): skipped — VNX_DIGEST_EMAIL or VNX_SMTP_PASS not set"
fi

# ── Write pipeline health summary ─────────────────────────────────────────────
FAILED_CSV="$(IFS=','; printf '%s' "${PHASES_FAILED[*]:-}")"
OVERALL_STATUS="ok"
[ -n "$FAILED_CSV" ] && OVERALL_STATUS="partial"

python3 - <<PY 2>/dev/null || true
import json
from datetime import datetime, timezone
health = {
    "pipeline_run": "$PIPELINE_START",
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "phases_run": $PHASES_RUN,
    "phases_ok": $PHASES_OK,
    "phases_failed": [p for p in "$FAILED_CSV".split(",") if p],
    "overall_status": "$OVERALL_STATUS",
}
with open("$HEALTH_FILE", "w") as fh:
    json.dump(health, fh, indent=2)
PY

log_msg "=== Pipeline complete: ${PHASES_OK}/${PHASES_RUN} phases OK, failed=[${PHASES_FAILED[*]:-none}] ==="
exit 0
