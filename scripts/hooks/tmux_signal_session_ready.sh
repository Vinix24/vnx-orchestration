#!/usr/bin/env bash
# VNX tmux-lane SessionStart sentinel
#
# Fires on Claude Code's SessionStart hook. Writes a "session_ready" sentinel
# into $VNX_TMUX_SIGNAL_DIR so the tmux interactive lane can detect readiness via
# the stable HOOK CONTRACT instead of scraping version-specific TUI banners
# (which break on ~weekly Claude Code bumps).
#
# Scoped HARD to tmux-spawn workers: fires ONLY when BOTH VNX_TMUX_SIGNAL_DIR and
# VNX_DISPATCH_ID are set. For any normal T0/interactive session (env unset) it
# drains stdin and exits 0 — completely no-op, no behavior change.
#
# Atomic write (.tmp then mv). Never blocks. Any error -> exit 0.

# Drain stdin so the hook caller never blocks on an unread pipe.
cat >/dev/null 2>&1 || true

# ── Guard: only fire for tmux-spawn workers ──────────────────────────────────
if [ -z "${VNX_TMUX_SIGNAL_DIR:-}" ] || [ -z "${VNX_DISPATCH_ID:-}" ]; then
  exit 0
fi

# Best-effort atomic sentinel write.
{
  mkdir -p "$VNX_TMUX_SIGNAL_DIR" 2>/dev/null
  _tmp="$VNX_TMUX_SIGNAL_DIR/session_ready.$$.tmp"
  printf '%s\n' "${VNX_DISPATCH_ID}" >"$_tmp" 2>/dev/null \
    && mv -f "$_tmp" "$VNX_TMUX_SIGNAL_DIR/session_ready" 2>/dev/null
} || true

exit 0
