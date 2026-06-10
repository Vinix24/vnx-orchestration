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
# Only load non-path vars: stale VNX_STATE_DIR / VNX_DATA_DIR from shell profiles
# can override the correctly resolved values computed by ensure_env above and
# cause writers (memory_consolidator, pattern_extractor) to use the pre-ADR-007
# path (~/.vnx-data/state) instead of the canonical project_id-scoped path.
for _rc in "$HOME/.zprofile" "$HOME/.zshrc"; do
    if [ -f "$_rc" ]; then
        eval "$(grep '^export VNX_' "$_rc" 2>/dev/null \
            | grep -v -E 'VNX_(STATE_DIR|DATA_DIR|DATA_HOME|DISPATCH_DIR|LOGS_DIR|PIDS_DIR|LOCKS_DIR|SOCKETS_DIR|REPORTS_DIR|HEADLESS_REPORTS_DIR|DB_DIR|HOME|CANONICAL_ROOT|INTELLIGENCE_DIR)=' \
            || true)"
    fi
done
# Re-anchor canonical path vars after profile load (ADR-007: VNX_DATA_DIR is authoritative).
# VNX_STATE_DIR must equal VNX_DATA_DIR/state so all writers and readers use the same db.
export VNX_STATE_DIR="$VNX_DATA_DIR/state"

LOG_FILE="$VNX_DATA_DIR/state/nightly_pipeline.log"
LOCK_FILE="$VNX_DATA_DIR/state/nightly_pipeline.lock"
PHASES_LOG="$VNX_DATA_DIR/state/nightly_pipeline_phases.ndjson"
HEALTH_FILE="$VNX_DATA_DIR/state/nightly_pipeline_health.json"
# DB_PATH derived from VNX_DATA_DIR/state (authoritative, ADR-007: project_id-scoped).
# NOT VNX_STATE_DIR which can be polluted by stale shell profile exports.
DB_PATH="$VNX_DATA_DIR/state/quality_intelligence.db"

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

# ── Python version guard ──────────────────────────────────────────────────────
# Warns when python3 resolves to < 3.10 so crontab PATH issues surface clearly.
# Does NOT abort: from __future__ import annotations in quality_db_init.py
# and other pipeline scripts covers the X|Y union-syntax crash on Python 3.9.
_py3_path="$(command -v python3 2>/dev/null || echo "(not found)")"
_py3_version="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo "unknown")"
_py3_major="$(python3 -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo "0")"
_py3_minor="$(python3 -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo "0")"
if [ "$_py3_major" -lt 3 ] || { [ "$_py3_major" -eq 3 ] && [ "$_py3_minor" -lt 10 ]; }; then
    log_msg "WARN: python3 at '$_py3_path' is version $_py3_version (< 3.10)."
    log_msg "WARN: This is typically /usr/bin/python3 (system Python) because crontab"
    log_msg "WARN: does not inherit the Homebrew PATH. Scripts use 'from __future__ import"
    log_msg "WARN: annotations' to avoid X|Y union-syntax crashes on 3.9, but full 3.10+"
    log_msg "WARN: features are unavailable. Fix: add the following line above your nightly"
    log_msg "WARN: crontab entry:  PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
fi
unset _py3_path _py3_version _py3_major _py3_minor

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

# ── Phase 2b: Behavioral analysis ────────────────────────────────────────────
# Runs after session-dispatch linkage, before learning cycle.
run_phase "2b-event-analyze" python3 "$SCRIPT_DIR/lib/event_analyzer.py" \
    --all --output "$VNX_DATA_DIR/state/dispatch_behaviors.json"
run_phase "2c-pattern-extract" python3 "$SCRIPT_DIR/lib/pattern_extractor.py" \
    --input "$VNX_DATA_DIR/state/dispatch_behaviors.json"

# ── Phase 4: Learning cycle ───────────────────────────────────────────────────
run_phase "4-learning-cycle" python3 "$SCRIPT_DIR/learning_loop.py" run

# ── Phase 4a: Memory consolidation (extract patterns from dispatch history) ───
run_phase "4a-memory-consolidation" python3 "$SCRIPT_DIR/memory_consolidator.py" --days 7

# ── Phase 4b: Dream consolidation (ADR-019: auto-dream memory consolidation) ──
# Produces pending-review proposals for T0 operator review. Never auto-applies.
# Human gate preserved per ADR-019 review_gate. ADR-007: project_id-scoped.
# Skip when VNX_DREAM_ENABLED=0; active by default when project_id is resolvable.
if [ "${VNX_DREAM_ENABLED:-1}" != "0" ]; then
    _dream_pid="${VNX_PROJECT_ID:-}"
    if [ -z "$_dream_pid" ]; then
        _dream_pid="$(python3 -c "
import sys, os
sys.path.insert(0, '$SCRIPT_DIR/lib')
try:
    from vnx_paths import resolve_project_id
    print(resolve_project_id() or '')
except Exception:
    print('')
" 2>/dev/null || echo "")"
    fi
    if [ -n "$_dream_pid" ]; then
        run_phase "4b-dream-consolidation" python3 "$SCRIPT_DIR/dream/consolidator.py" \
            --project-id "$_dream_pid" \
            --db-path "$DB_PATH"
    else
        log_msg "Phase 4b (dream): skipped — no project_id resolved (set VNX_PROJECT_ID or ensure .vnx-project-id exists)"
        log_phase_result "4b-dream-consolidation" "skipped" "no_project_id"
    fi
    unset _dream_pid
fi

# ── Phase 4c: Weekly digest ───────────────────────────────────────────────────
run_phase "4c-weekly-digest" python3 "$SCRIPT_DIR/weekly_digest.py"

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
