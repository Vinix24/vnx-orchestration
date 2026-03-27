#!/usr/bin/env bash
# VNX Command: jump
# Switch tmux focus to a specific terminal or to the highest-attention terminal.
#
# Usage:
#   vnx jump <T0|T1|T2|T3>    — focus the named terminal
#   vnx jump --attention       — focus the highest-priority attention terminal

cmd_jump() {
  local target="${1:-}"

  if [ -z "$target" ]; then
    err "Usage: vnx jump <T0|T1|T2|T3> | vnx jump --attention"
    return 1
  fi

  local session_name="vnx-$(basename "$PROJECT_ROOT")"
  local panes_file="$VNX_STATE_DIR/panes.json"

  # ── --attention: find highest-priority terminal needing human ─────────
  if [ "$target" = "--attention" ]; then
    target=$(python3 "$VNX_HOME/scripts/lib/canonical_state_views.py" \
      --state-dir "$VNX_STATE_DIR" \
      attention-summary 2>/dev/null \
      | python3 -c "
import json, sys
PRIORITY = {'blocked': 4, 'review-needed': 3, 'stale': 2, 'context-pressure': 1}
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
terminals = data.get('terminals') or {}
best = None
best_p = 0
for tid, info in terminals.items():
    if not info.get('needs_human'):
        continue
    att = info.get('attention') or {}
    p = PRIORITY.get(att.get('type', ''), 0)
    if p > best_p:
        best_p = p
        best = tid
if best:
    print(best)
" 2>/dev/null || true)

    if [ -z "$target" ]; then
      log "No terminal currently needs human attention."
      return 0
    fi
    log "Highest-attention terminal: $target"
  fi

  # ── Validate terminal ID ──────────────────────────────────────────────
  case "$target" in
    T0|T1|T2|T3) ;;
    *)
      err "Unknown terminal: '$target' (valid: T0, T1, T2, T3)"
      return 1
      ;;
  esac

  # ── Verify VNX session exists ─────────────────────────────────────────
  if ! tmux has-session -t "$session_name" 2>/dev/null; then
    err "VNX session '$session_name' not found — is VNX running? (try: vnx start)"
    return 1
  fi

  # ── Resolve pane target ───────────────────────────────────────────────
  # Prefer pane_id from panes.json (stable across layout changes).
  # Fall back to positional index (T0=0, T1=1, T2=2, T3=3) if not found.
  local pane_id=""
  if [ -f "$panes_file" ]; then
    pane_id=$(python3 -c "
import json, sys
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text())
    entry = data.get(sys.argv[2]) or {}
    pid = str(entry.get('pane_id') or '').strip()
    if pid:
        print(pid)
except Exception:
    pass
" "$panes_file" "$target" 2>/dev/null || true)
  fi

  local pane_index
  case "$target" in
    T0) pane_index=0 ;;
    T1) pane_index=1 ;;
    T2) pane_index=2 ;;
    T3) pane_index=3 ;;
  esac

  # ── Execute tmux navigation ───────────────────────────────────────────
  if ! tmux select-window -t "${session_name}:0" 2>/dev/null; then
    err "Failed to select window in session '$session_name'"
    return 1
  fi

  if [ -n "$pane_id" ]; then
    if tmux select-pane -t "$pane_id" 2>/dev/null; then
      log "Jumped to $target (pane $pane_id)"
      return 0
    fi
    # pane_id stale — fall through to index
  fi

  if tmux select-pane -t "${session_name}:0.${pane_index}" 2>/dev/null; then
    log "Jumped to $target (pane index ${pane_index})"
    return 0
  fi

  err "Could not select pane for $target in session '$session_name'"
  return 1
}
