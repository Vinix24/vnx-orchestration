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
# vnx_paths.sh restores the caller's strict mode exactly, so the `set -e` above
# survives the source (regression-tested in tests/test_vnx_paths_shellopts.py);
# no local re-assert is needed. The source already exports the VNX_* environment;
# there is no separate ensure_env in bash (that name only exists in vnx_paths.py).

# ── Interpreter resolution (measured 2026-07-30) ──────────────────────────
# This script runs under launchd (com.vnx.conversation-analyzer), a pure
# background context with no interactive shell alias to mask a broken
# /opt/homebrew/bin/python3 (relinked to a dependency-less 3.14 at 09:32
# today). Resolve one interpreter here and use it at every call site below.
# Precedence: repo venv (uv-managed, has deps) > pinned homebrew 3.12
# (no deps, but a stable interpreter) > bare `python3` (last resort).
if [ -x "$SCRIPT_DIR/../.venv/bin/python" ]; then
    VNX_PYTHON="$SCRIPT_DIR/../.venv/bin/python"
elif [ -x "/opt/homebrew/opt/python@3.12/bin/python3.12" ]; then
    VNX_PYTHON="/opt/homebrew/opt/python@3.12/bin/python3.12"
else
    VNX_PYTHON="python3"
fi

# launchd inherits no shell profile, so the PATH from the plist has no
# ~/.local/bin, where the native installer puts `claude` (OI-1258). Put it first
# so every child process finds the CLI, not only the analyzer's own resolver.
if [ -d "$HOME/.local/bin" ]; then
    export PATH="$HOME/.local/bin:$PATH"
fi

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

# Phase output goes to $LOG_FILE only. The markers below also go to stdout, which
# launchd writes to StandardOutPath (/tmp/vnx-conversation-analyzer.log): no code
# reads that file, but reload_conversation_analyzer.sh points the operator to it.
log_msg() {
    local line
    line="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$line" >> "$LOG_FILE"
    echo "$line"
}

# ── Time limits (OI-2021) ──────────────────────────────────────────────────
# Measured 2026-10-08: Phase 1 sat 9 h 44 min in one blocked read, the log ended
# on its header and the beacon kept the previous night's `ok`. Every phase now
# runs through run_bounded below. Phase 1 took 2 s to 5 min 29 s on the seven
# nights before, hence 1800 s; every other phase gets 900 s.
source "$SCRIPT_DIR/lib/vnx_run_bounded.sh"
GROUP_LAUNCHER="$SCRIPT_DIR/lib/vnx_exec_new_group.py"
KILL_GRACE_SECS=5
REAP_WAIT_SECS=10
BEACON_TIMEOUT_SECS=120

# Sets the variable named $1 to the value of env var $2 when that is a whole
# number of seconds above 0, else to the default $3 (a bad value is logged).
set_limit() {
    local var="$1" env_name="$2" default="$3" value="${!2:-}"
    case "$value" in
        '') value="$default" ;;
        *[!0-9]*)
            log_msg "WARNING: $env_name='$value' is not a whole number of seconds; using $default"
            value="$default"
            ;;
        *)
            if [ "$value" -eq 0 ]; then
                log_msg "WARNING: $env_name=0 would end every phase at once; using $default"
                value="$default"
            fi
            ;;
    esac
    printf -v "$var" '%s' "$value"
}
set_limit PHASE1_TIMEOUT VNX_ANALYZER_PHASE1_TIMEOUT 1800
set_limit PHASE_TIMEOUT VNX_ANALYZER_PHASE_TIMEOUT 900

# run_bounded <label> <limit_secs> <script.py> [args...]
# Runs one Python script of this directory unbuffered and with the fault
# handler on, its output appended to $LOG_FILE by redirection (never through a
# pipe a lingering grandchild could hold open), in its own process group and
# under the limit. On overrun vnx_run_bounded_group sends SIGABRT (a traceback
# lands in the log when the call is interruptible), then SIGKILL, waits a
# bounded time and returns either way. Returns the script's exit status, or
# 124 on overrun; BOUNDED_TIMED_OUT tells the two apart.
run_bounded() {
    local label="$1" limit="$2" script="$3"
    shift 3
    BOUNDED_TIMED_OUT=0
    "$VNX_PYTHON" "$GROUP_LAUNCHER" "$VNX_PYTHON" -u -X faulthandler \
        "$SCRIPT_DIR/$script" "$@" >> "$LOG_FILE" 2>&1 < /dev/null &
    local group_id=$!
    local rc=0
    vnx_run_bounded_group "$group_id" "$limit" "$KILL_GRACE_SECS" "$REAP_WAIT_SECS" || rc=$?
    case "$VNX_BOUNDED_GROUP_OUTCOME" in
        deadline)
            BOUNDED_TIMED_OUT=1
            log_msg "$label TIMEOUT: still running after ${limit}s; sent SIGABRT, then SIGKILL to process group $group_id (exit status $VNX_BOUNDED_GROUP_STATUS)"
            ;;
        deadline_unreaped)
            BOUNDED_TIMED_OUT=1
            log_msg "$label TIMEOUT: still running after ${limit}s; sent SIGABRT, then SIGKILL to process group $group_id; still present ${REAP_WAIT_SECS}s later, continuing without it"
            ;;
        refused)
            log_msg "$label ERROR: no time limit could be applied to process group '$group_id'; continuing without waiting for it"
            ;;
    esac
    return "$rc"
}

# Failures the analyzer cannot report itself, joined with "; ". Each new one
# rewrites the `fail` beacon with all of them, so a later failure never hides an
# earlier one.
FAIL_REASONS=""

# write_fail_beacon [keep_since_epoch]
# With an epoch, a beacon the analyzer itself wrote at or after it is kept.
write_fail_beacon() {
    local rc=0
    if [ -n "${1:-}" ]; then
        run_bounded "Fail beacon" "$BEACON_TIMEOUT_SECS" conversation_analyzer.py \
            --write-fail-beacon "$FAIL_REASONS" --unless-beacon-since "$1" || rc=$?
    else
        run_bounded "Fail beacon" "$BEACON_TIMEOUT_SECS" conversation_analyzer.py \
            --write-fail-beacon "$FAIL_REASONS" || rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
        log_msg "WARNING: fail beacon write ended with exit $rc"
    fi
}

# record_failure <reason> [keep_since_epoch]
record_failure() {
    FAIL_REASONS="${FAIL_REASONS:+$FAIL_REASONS; }$1"
    write_fail_beacon "${2:-}"
}

# run_phase <label> <limit_secs> <script.py> [args...]
# run_bounded plus: an overrun is recorded as a failure in the beacon.
# PHASE_TIMED_OUT keeps the overrun flag of the phase itself, because the
# beacon write that follows runs through run_bounded too.
run_phase() {
    local label="$1" limit="$2" rc=0
    run_bounded "$@" || rc=$?
    PHASE_TIMED_OUT="$BOUNDED_TIMED_OUT"
    if [ "$PHASE_TIMED_OUT" -eq 1 ]; then
        record_failure "$label timed out after ${limit}s"
    fi
    return "$rc"
}

# Age of the lock file as "<h>h<mm>m (<s>s)", or "unknown". GNU stat first: on
# macOS `stat -c` fails without output, while GNU `stat -f %m` would print.
lock_age() {
    local mtime now age
    mtime="$(stat -c %Y "$LOCK_FILE" 2>/dev/null || stat -f %m "$LOCK_FILE" 2>/dev/null)" || mtime=""
    case "$mtime" in
        ''|*[!0-9]*) echo "unknown"; return 0 ;;
    esac
    now="$(date +%s)"
    age=$((now - mtime))
    printf '%dh%02dm (%ds)\n' $((age / 3600)) $((age % 3600 / 60)) "$age"
}

# The deepseek-harness deep analysis needs the operator's own DeepSeek key. The rc
# loader above keeps only `export VNX_` lines, so this one variable is read from the
# provider env file by a parser that never sources it and never prints the value.
# Missing key = loud line here; the analyzer then records the failure in the digest.
source "$SCRIPT_DIR/lib/load_deepseek_key.sh"
if [ "${VNX_ANALYZER_LLM:-}" = "deepseek-harness" ]; then
    if ! vnx_load_deepseek_key; then
        log_msg "ERROR: VNX_ANALYZER_LLM=deepseek-harness but DEEPSEEK_API_KEY was not found in ${VNX_PROVIDER_ENV_FILE:-$HOME/.config/vnx/provider-usage.env}; deep analysis will be recorded as failed"
    fi
fi

# Singleton enforcement. A live holder means the previous run never finished:
# that is a failure to report, not a quiet skip (OI-2021). The holder is left
# alone and its lock stays; the trap below is not installed yet.
if [ -f "$LOCK_FILE" ]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        holder="another run still holds the lock (PID $pid, lock age $(lock_age))"
        log_msg "Already running: $holder; this run is skipped and reported as failed"
        record_failure "$holder"
        exit 1
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
PHASE0_EXIT=0
run_phase "Phase 0" "$PHASE_TIMEOUT" quality_db_init.py || PHASE0_EXIT=$?
if [ "$PHASE0_EXIT" -ne 0 ]; then
    log_msg "Phase 0 FAILED: DB schema migrations failed (exit=$PHASE0_EXIT), aborting"
    if [ "$PHASE_TIMED_OUT" -eq 0 ]; then
        record_failure "Phase 0 exited $PHASE0_EXIT"
    fi
    exit 1
fi
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

# Phase 1: Run the analyzer (session parsing + heuristics + deep analysis).
# The analyzer writes its own beacon when it ends; the runner writes a `fail`
# beacon when it overran, or exited non-zero without a beacon of this run.
# The later phases run after any Phase 1 outcome: they read the rows the
# analyzer saved per session, so without a fresh Phase 1 they work on the rows
# of earlier nights.
log_msg "Phase 1: Running conversation analyzer..."
PHASE1_STARTED_AT="$(date +%s)"
ANALYZER_EXIT=0
run_phase "Phase 1" "$PHASE1_TIMEOUT" conversation_analyzer.py \
    --max-sessions 50 \
    --deep-budget 20 || ANALYZER_EXIT=$?

if [ "$ANALYZER_EXIT" -eq 0 ]; then
    log_msg "Phase 1 complete (exit=$ANALYZER_EXIT)"
else
    log_msg "Phase 1 FAILED (exit=$ANALYZER_EXIT)"
    if [ "$PHASE_TIMED_OUT" -eq 0 ]; then
        record_failure "Phase 1 exited $ANALYZER_EXIT" "$PHASE1_STARTED_AT"
    fi
fi

# Phase 1.5: Cross-reference sessions, dispatches, and receipts
log_msg "Phase 1.5: Running session-dispatch linkage..."
if run_phase "Phase 1.5" "$PHASE_TIMEOUT" link_sessions_dispatches.py; then
    log_msg "Phase 1.5 complete: session-dispatch linkage updated"
else
    log_msg "Phase 1.5 WARNING: session-dispatch linkage failed (non-fatal)"
fi

# Phase 2: Generate T0 session brief (model-based, auto — read-only state file)
log_msg "Phase 2: Generating T0 session brief..."
if run_phase "Phase 2" "$PHASE_TIMEOUT" generate_t0_session_brief.py; then
    log_msg "Phase 2 complete: t0_session_brief.json updated"
else
    log_msg "Phase 2 WARNING: session brief generation failed (non-fatal)"
fi

# Phase 2.5: Governance metrics aggregation + SPC
log_msg "Phase 2.5: Computing governance metrics..."
if run_phase "Phase 2.5" "$PHASE_TIMEOUT" governance_aggregator.py --backfill; then
    log_msg "Phase 2.5 complete: governance metrics updated"
else
    log_msg "Phase 2.5 WARNING: governance aggregation failed (non-fatal)"
fi

# Phase 2.6: Learning loop. Shadow mode unless VNX_LEARNING_LOOP_PERSIST=1 is
# exported for this job: it then computes patterns and proposals, writes one
# report and persists nothing. Every run writes a beacon
# (learning_loop_nightly_beacon.json) that vnx_doctor reads; a failed run
# writes status=failed.
log_msg "Phase 2.6: Running learning loop (persist=${VNX_LEARNING_LOOP_PERSIST:+SET})..."
if run_phase "Phase 2.6" "$PHASE_TIMEOUT" learning_loop_nightly.py; then
    log_msg "Phase 2.6 complete: learning loop beacon written"
else
    log_msg "Phase 2.6 WARNING: learning loop FAILED, beacon status=failed (non-fatal)"
fi

# Phase 3: Generate suggested edits (human-in-the-loop, pending review)
log_msg "Phase 3: Generating suggested edits..."
if run_phase "Phase 3" "$PHASE_TIMEOUT" generate_suggested_edits.py; then
    log_msg "Phase 3 complete: pending_edits.json updated"
else
    log_msg "Phase 3 WARNING: suggested edits generation failed (non-fatal)"
fi

# Phase 4: Send digest email (requires VNX_DIGEST_EMAIL; the SMTP password
# comes from VNX_SMTP_PASS or, when unset, the macOS keychain — the shell
# doesn't know which source will resolve, so send_digest_email.py judges
# availability itself and reports failure via its own exit code + log line).
if [ -n "${VNX_DIGEST_EMAIL:-}" ]; then
    log_msg "Phase 4: Sending digest email to $VNX_DIGEST_EMAIL..."
    if run_phase "Phase 4" "$PHASE_TIMEOUT" send_digest_email.py; then
        log_msg "Phase 4 complete: digest email sent"
    else
        log_msg "Phase 4 WARNING: digest email failed (non-fatal)"
    fi
else
    log_msg "Phase 4: Skipped — VNX_DIGEST_EMAIL not set"
fi

# A failing later phase stays non-fatal, an overrun does not: it is the hang
# this runner now reports, and the beacon already says `fail`.
if [ "$ANALYZER_EXIT" -ne 0 ] || [ -n "$FAIL_REASONS" ]; then
    log_msg "=== Nightly analysis pipeline FAILED (analyzer_exit=$ANALYZER_EXIT${FAIL_REASONS:+; $FAIL_REASONS}) ==="
    if [ "$ANALYZER_EXIT" -ne 0 ]; then
        exit "$ANALYZER_EXIT"
    fi
    exit "$VNX_RUN_BOUNDED_DEADLINE"
fi
log_msg "=== Nightly analysis pipeline complete (analyzer_exit=$ANALYZER_EXIT) ==="
