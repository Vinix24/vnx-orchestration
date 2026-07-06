#!/usr/bin/env bash
# VNX fleet audit: does each project's live (or registered) T0 session
# actually load its role file (.claude/terminals/T0/CLAUDE.md)?
#
# Claude Code discovers CLAUDE.md by walking UP from cwd, merging every
# CLAUDE.md found in ancestor directories. .claude/terminals/T0/CLAUDE.md is a
# DESCENDANT of the project root, not an ancestor of it — so a T0 claude
# process only picks it up when its cwd is exactly <project_root>/.claude/
# terminals/T0. Any launch path that starts claude at the project root
# instead (a stale tmux-resurrect/continuum respawn, a manual re-attach, a
# launcher bug) silently drops the orchestrator role file — no error, no
# warning, T0 just runs without its playbook.
#
# This makes that drift observable: it reports, per project/terminal,
# whether the running T0 would load its role file right now.
#
# Usage: bash scripts/commands/t0_role_audit.sh
#
# Standalone by design — this must also audit OTHER registered VNX projects,
# not just the one it happens to be invoked from, so it does not depend on
# bin/vnx's command loader (PROJECT_ROOT/VNX_HOME/log/err are not assumed).

set -uo pipefail

# Detect whether a tmux pane has an active CLI (claude/codex/gemini/node) in
# its process tree. Checked via `ps`, not tmux's `#{pane_current_command}`:
# a running claude process can report its own version string (e.g. "2.1.201")
# there instead of the literal binary name.
_t0_audit_active_cli() {
  local pane_pid="$1" pid comm
  [ -z "$pane_pid" ] && return 1
  for pid in "$pane_pid" $(pgrep -P "$pane_pid" 2>/dev/null); do
    comm="$(ps -o comm= -p "$pid" 2>/dev/null | tr -d ' ')"
    comm="${comm##*/}"
    case "$comm" in
      claude|codex|gemini|node) return 0 ;;
    esac
  done
  return 1
}

_t0_audit_row() {
  printf '%-22s %-6s %-9s %-9s %-55s\n' "$1" "$2" "$3" "$4" "$5"
}

main() {
  if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux not found — cannot audit live sessions." >&2
    exit 1
  fi

  _t0_audit_row "PROJECT" "TERM" "LOADED?" "CLI" "CWD"
  _t0_audit_row "-------" "----" "-------" "---" "---"

  local found_any=0
  local seen_roots=""

  while IFS=' ' read -r pane_id pane_path pane_pid; do
    [ -z "$pane_id" ] && continue

    # Skip per-dispatch worker worktrees (tmux_interactive_dispatch.py checks
    # out one worktree per dispatch under .vnx-data/worktrees/). Those panes
    # run a worker (T1/T2/T3-style implementation task), never a T0
    # orchestrator, even though the checkout also happens to contain a
    # .claude/terminals/T0/CLAUDE.md file inherited from the repo tree.
    case "$pane_path" in
      */.vnx-data/worktrees/*) continue ;;
    esac

    local project_root="" loaded=""
    if [ "${pane_path%/.claude/terminals/T0}" != "$pane_path" ]; then
      # cwd is exactly <root>/.claude/terminals/T0 — role file loads.
      project_root="${pane_path%/.claude/terminals/T0}"
      loaded="yes"
    elif [ -f "$pane_path/.claude/terminals/T0/CLAUDE.md" ]; then
      # cwd is a project root that HAS a T0 role file, but this pane is not
      # inside it — the generic project CLAUDE.md loads instead.
      project_root="$pane_path"
      loaded="no"
    else
      continue  # Not a VNX T0-governed pane — skip (e.g. a plain client session).
    fi

    found_any=1
    case " $seen_roots " in
      *" $project_root "*) ;;
      *) seen_roots="$seen_roots $project_root" ;;
    esac

    local cli="(idle)"
    _t0_audit_active_cli "$pane_pid" && cli="running"

    _t0_audit_row "$(basename "$project_root")" "T0" "$loaded" "$cli" "$pane_path"
  done < <(tmux list-panes -a -F '#{pane_id} #{pane_current_path} #{pane_pid}' 2>/dev/null)

  if [ "$found_any" -eq 0 ]; then
    echo "(no live T0 sessions found)"
  fi

  # Cross-reference the project registry (~/.vnx/projects.json) so projects
  # with a configured T0 role file but no live tmux session are still listed,
  # instead of silently disappearing from the report.
  local registry="$HOME/.vnx/projects.json"
  if [ -f "$registry" ] && command -v python3 >/dev/null 2>&1; then
    python3 - "$registry" "$seen_roots" <<'PYEOF'
import json, os, sys

registry_file, seen_raw = sys.argv[1], sys.argv[2]
seen = set(seen_raw.split())

try:
    with open(registry_file) as f:
        data = json.load(f)
except Exception:
    sys.exit(0)

for p in data.get("projects", []):
    root = p.get("path", "")
    if not root or root in seen:
        continue
    role_file = os.path.join(root, ".claude", "terminals", "T0", "CLAUDE.md")
    if os.path.isfile(role_file):
        print("{:<22} {:<6} {:<9} {:<9} {:<55}".format(
            os.path.basename(root), "T0", "n/a", "(not running)", root))
PYEOF
  fi
}

main "$@"
