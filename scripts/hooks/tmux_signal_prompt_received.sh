#!/usr/bin/env bash
# VNX tmux-lane UserPromptSubmit sentinel
#
# Fires on Claude Code's UserPromptSubmit hook. Writes a "prompt_received"
# sentinel into $VNX_TMUX_SIGNAL_DIR so the tmux interactive lane can confirm the
# dispatch instruction was actually submitted via the stable HOOK CONTRACT,
# instead of inferring it from version-specific TUI pane scraping.
#
# OI-1126 worker-side detectability: also checks whether the delivered prompt
# actually belongs to THIS dispatch. Every tmux-lane body carries a
# "dispatch_id": "<id>" field inside the completion-protocol JSON that
# _build_completion_protocol() unconditionally appends (tmux_interactive_dispatch.py)
# — a code-guaranteed marker, independent of how the raw instruction bundle happens
# to be formatted. If that embedded id disagrees with $VNX_DISPATCH_ID (set
# race-free at spawn/launch time, never via the shared tmux paste buffer that OI-1126
# found racy), a "dispatch_id_mismatch" sentinel is written so the lane fails loud
# instead of letting the dispatch run to a deadline against content that isn't its
# own. Best-effort: jq missing or prompt unparseable -> skip the check, never block.
#
# Scoped HARD to tmux-spawn workers: fires ONLY when BOTH VNX_TMUX_SIGNAL_DIR and
# VNX_DISPATCH_ID are set. For any normal T0/interactive session (env unset) it
# drains stdin and exits 0 — completely no-op, no behavior change.
#
# Atomic write (.tmp then mv). Never blocks. Any error -> exit 0.

# Read stdin once (this also serves as draining it so the hook caller never
# blocks on an unread pipe, guard or no guard).
INPUT="$(cat 2>/dev/null || true)"

# ── Guard: only fire for tmux-spawn workers ──────────────────────────────────
if [ -z "${VNX_TMUX_SIGNAL_DIR:-}" ] || [ -z "${VNX_DISPATCH_ID:-}" ]; then
  exit 0
fi

# ── OI-1126: does the delivered prompt actually belong to this dispatch? ─────
if command -v jq >/dev/null 2>&1; then
  PROMPT_TEXT="$(printf '%s' "$INPUT" | jq -r '.prompt // ""' 2>/dev/null || echo "")"
  # The completion-protocol JSON is embedded as shell-escaped bash-command text
  # (_make_receipt_json backslash-escapes its inner quotes so the WORKER's later
  # `bash` execution parses them correctly) — it is not raw JSON at the markdown-text
  # level. Strip backslashes first so one plain-quote pattern matches both the
  # escaped (\"dispatch_id\") and unescaped ("dispatch_id") forms.
  CLEAN_PROMPT="$(printf '%s' "$PROMPT_TEXT" | tr -d '\\')"
  DELIVERED_ID="$(printf '%s' "$CLEAN_PROMPT" \
    | grep -oE '"dispatch_id"[[:space:]]*:[[:space:]]*"[^"]+"' \
    | head -1 | sed -E 's/.*"([^"]+)"$/\1/' || true)"
  if [ -n "$DELIVERED_ID" ] && [ "$DELIVERED_ID" != "$VNX_DISPATCH_ID" ]; then
    {
      mkdir -p "$VNX_TMUX_SIGNAL_DIR" 2>/dev/null
      _mtmp="$VNX_TMUX_SIGNAL_DIR/dispatch_id_mismatch.$$.tmp"
      printf 'expected=%s delivered=%s\n' "$VNX_DISPATCH_ID" "$DELIVERED_ID" >"$_mtmp" 2>/dev/null \
        && mv -f "$_mtmp" "$VNX_TMUX_SIGNAL_DIR/dispatch_id_mismatch" 2>/dev/null
    } || true
  fi
fi

# Best-effort atomic sentinel write.
{
  mkdir -p "$VNX_TMUX_SIGNAL_DIR" 2>/dev/null
  _tmp="$VNX_TMUX_SIGNAL_DIR/prompt_received.$$.tmp"
  printf '%s\n' "${VNX_DISPATCH_ID}" >"$_tmp" 2>/dev/null \
    && mv -f "$_tmp" "$VNX_TMUX_SIGNAL_DIR/prompt_received" 2>/dev/null
} || true

exit 0
