#!/usr/bin/env bash
# VNX Conversation Analyzer — Nightly Runner
# Designed for launchd scheduling (macOS) or manual invocation.
#
# launchd plist (install at ~/Library/LaunchAgents/com.vnx.conversation-analyzer.plist):
#   <?xml version="1.0" encoding="UTF-8"?>
#   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
#   <plist version="1.0">
#   <dict>
#     <key>Label</key><string>com.vnx.conversation-analyzer</string>
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
#     <key>StandardOutPath</key><string>/tmp/vnx-conversation-analyzer.log</string>
#     <key>StandardErrorPath</key><string>/tmp/vnx-conversation-analyzer.err</string>
#   </dict>
#   </plist>
#
# Activate: launchctl load ~/Library/LaunchAgents/com.vnx.conversation-analyzer.plist
# Deactivate: launchctl unload ~/Library/LaunchAgents/com.vnx.conversation-analyzer.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"
ensure_env

# Load user environment for email digest (launchd doesn't source ~/.zshrc)
# Reads VNX_DIGEST_EMAIL and VNX_SMTP_PASS from ~/.zshrc or ~/.zprofile
for _rc in "$HOME/.zprofile" "$HOME/.zshrc"; do
    if [ -f "$_rc" ]; then
        # Source only export lines to avoid interactive shell issues
        eval "$(grep '^export VNX_' "$_rc" 2>/dev/null || true)"
    fi
done

LOG_FILE="$VNX_STATE_DIR/conversation_analyzer.log"
LOCK_FILE="$VNX_STATE_DIR/conversation_analyzer.lock"

log_msg() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# Singleton enforcement
if [ -f "$LOCK_FILE" ]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log_msg "Already running (PID $pid), skipping"
        exit 0
    fi
    log_msg "Stale lock file found, removing"
    rm -f "$LOCK_FILE"
fi

echo $$ > "$LOCK_FILE"
cleanup() {
    rm -f "$LOCK_FILE"
}
trap cleanup EXIT

log_msg "=== Nightly conversation analysis starting ==="

# Phase 0: Ensure DB schema is up to date (runs migrations if needed)
log_msg "Phase 0: Running DB schema migrations..."
python3 "$SCRIPT_DIR/quality_db_init.py" 2>&1 | tee -a "$LOG_FILE"
log_msg "Phase 0 complete"

# Optionally start Ollama if not running and available
OLLAMA_STARTED=false
if ! pgrep -x "ollama" >/dev/null 2>&1; then
    if command -v ollama >/dev/null 2>&1; then
        log_msg "Starting Ollama for local inference..."
        ollama serve >> "$LOG_FILE" 2>&1 &
        OLLAMA_PID=$!
        OLLAMA_STARTED=true
        sleep 5
        cleanup() {
            if [ "$OLLAMA_STARTED" = true ] && [ -n "${OLLAMA_PID:-}" ]; then
                kill "$OLLAMA_PID" 2>/dev/null || true
                log_msg "Ollama stopped"
            fi
            rm -f "$LOCK_FILE"
        }
        trap cleanup EXIT
        log_msg "Ollama started (PID $OLLAMA_PID)"
    fi
fi

# Phase 1: Run the analyzer (session parsing + heuristics + deep analysis)
log_msg "Phase 1: Running conversation analyzer..."
python3 "$SCRIPT_DIR/conversation_analyzer.py" \
    --max-sessions 50 \
    --deep-budget 20 \
    2>&1 | tee -a "$LOG_FILE"

ANALYZER_EXIT=${PIPESTATUS[0]}
log_msg "Phase 1 complete (exit=$ANALYZER_EXIT)"

# Phase 1.5: Cross-reference sessions, dispatches, and receipts
log_msg "Phase 1.5: Running session-dispatch linkage..."
if python3 "$SCRIPT_DIR/link_sessions_dispatches.py" 2>&1 | tee -a "$LOG_FILE"; then
    log_msg "Phase 1.5 complete: session-dispatch linkage updated"
else
    log_msg "Phase 1.5 WARNING: session-dispatch linkage failed (non-fatal)"
fi

# Phase 2: Generate T0 session brief (model-based, auto — read-only state file)
log_msg "Phase 2: Generating T0 session brief..."
if python3 "$SCRIPT_DIR/generate_t0_session_brief.py" 2>&1 | tee -a "$LOG_FILE"; then
    log_msg "Phase 2 complete: t0_session_brief.json updated"
else
    log_msg "Phase 2 WARNING: session brief generation failed (non-fatal)"
fi

# Phase 2.5: Governance metrics aggregation + SPC
log_msg "Phase 2.5: Computing governance metrics..."
if python3 "$SCRIPT_DIR/governance_aggregator.py" --backfill 2>&1 | tee -a "$LOG_FILE"; then
    log_msg "Phase 2.5 complete: governance metrics updated"
else
    log_msg "Phase 2.5 WARNING: governance aggregation failed (non-fatal)"
fi

# Phase 3: Generate suggested edits (human-in-the-loop, pending review)
log_msg "Phase 3: Generating suggested edits..."
if python3 "$SCRIPT_DIR/generate_suggested_edits.py" 2>&1 | tee -a "$LOG_FILE"; then
    log_msg "Phase 3 complete: pending_edits.json updated"
else
    log_msg "Phase 3 WARNING: suggested edits generation failed (non-fatal)"
fi

# Phase 4: Send digest email (requires VNX_DIGEST_EMAIL + VNX_SMTP_PASS)
if [ -n "${VNX_DIGEST_EMAIL:-}" ] && [ -n "${VNX_SMTP_PASS:-}" ]; then
    log_msg "Phase 4: Sending digest email to $VNX_DIGEST_EMAIL..."
    if python3 "$SCRIPT_DIR/send_digest_email.py" 2>&1 | tee -a "$LOG_FILE"; then
        log_msg "Phase 4 complete: digest email sent"
    else
        log_msg "Phase 4 WARNING: digest email failed (non-fatal)"
    fi
else
    log_msg "Phase 4: Skipped — VNX_DIGEST_EMAIL or VNX_SMTP_PASS not set"
fi

log_msg "=== Nightly analysis pipeline complete (analyzer_exit=$ANALYZER_EXIT) ==="
exit "$ANALYZER_EXIT"
