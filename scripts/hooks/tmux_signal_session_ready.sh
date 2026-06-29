#!/usr/bin/env bash
# VNX tmux-lane SessionStart sentinel
#
# Fires on Claude Code's SessionStart hook. Writes a "session_ready" sentinel
# into $VNX_TMUX_SIGNAL_DIR so the tmux interactive lane can detect readiness via
# the stable HOOK CONTRACT instead of scraping version-specific TUI banners
# (which break on ~weekly Claude Code bumps).
#
# F1.1: also taps the hook's stdin JSON to extract the session_id Claude actually
# used and writes it to "session_id" alongside the sentinel. The lane compares
# this against the pre-assigned VNX_CLAUDE_SESSION_ID; a mismatch means the CLI
# silently ignored --session-id.
#
# Scoped HARD to tmux-spawn workers: fires ONLY when BOTH VNX_TMUX_SIGNAL_DIR and
# VNX_DISPATCH_ID are set. For any normal T0/interactive session (env unset) it
# drains stdin and exits 0 — completely no-op, no behavior change.
#
# Atomic write (.tmp then mv). Never blocks. Any error -> exit 0.

# Capture stdin JSON (best-effort). Claude passes hook payload on stdin.
STDIN_JSON=""
if command -v jq &>/dev/null; then
    STDIN_JSON=$(cat 2>/dev/null) || STDIN_JSON=""
else
    # jq missing: drain the pipe so the caller never blocks, then continue to
    # write the sentinel. Session-id verification is unavailable, not fatal.
    cat >/dev/null 2>&1 || true
fi

# ── Guard: only fire for tmux-spawn workers ──────────────────────────────────
if [ -z "${VNX_TMUX_SIGNAL_DIR:-}" ] || [ -z "${VNX_DISPATCH_ID:-}" ]; then
    exit 0
fi

# Best-effort atomic writes. The sentinel is PRIMARY; session_id is SECONDARY.
{
    mkdir -p "$VNX_TMUX_SIGNAL_DIR" 2>/dev/null

    # F1.1: extract the session_id the CLI reported and persist it for verification.
    if [ -n "$STDIN_JSON" ]; then
        _sid_tmp="$VNX_TMUX_SIGNAL_DIR/session_id.$$.tmp"
        echo "$STDIN_JSON" | jq -r '.session_id // ""' 2>/dev/null >"$_sid_tmp" \
            && mv -f "$_sid_tmp" "$VNX_TMUX_SIGNAL_DIR/session_id" 2>/dev/null
    fi

    _ready_tmp="$VNX_TMUX_SIGNAL_DIR/session_ready.$$.tmp"
    printf '%s\n' "${VNX_DISPATCH_ID}" >"$_ready_tmp" 2>/dev/null \
        && mv -f "$_ready_tmp" "$VNX_TMUX_SIGNAL_DIR/session_ready" 2>/dev/null
} || true

exit 0
