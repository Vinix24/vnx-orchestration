#!/bin/sh
# vnx_hook_shim.sh — start a VNX hook entry so that a missing file never blocks a session.
#
# Usage: vnx_hook_shim.sh [--scope user|project] <entry>
#
# <entry> is a file name under scripts/hooks/ (for example t0_context_guard.py). Claude Code
# treats exit 2 from a hook as blocking, and both `python3 /missing.py` and dash starting a
# missing script exit 2 (OI-2071). This script never exits 2 itself and maps an entry's 2 to 1,
# which Claude Code reports as a non-blocking error with stderr shown.
#
#   1. The root is the nearest directory, from the physical cwd up, holding a `.git` entry: the
#      folder `git rev-parse --show-toplevel` names, found without starting git, so nothing here
#      can outlast the hook's 10 s timeout. Unless that root has a `.vnx-project-id` with a
#      non-empty first line and a `.claude/terminals/T0/CLAUDE.md`, and the cwd is outside
#      `.vnx-data/worktrees/`, step 1 uses `CLAUDE_PROJECT_DIR` (Claude Code sets it to the
#      folder the session started in) and applies the same rule there. A candidate under
#      `.vnx-data/worktrees/` yields nothing and stops the search, and an empty or unset
#      `CLAUDE_PROJECT_DIR` yields nothing. With no project on either step it exits 0 without
#      output: only a VNX project with a T0 is guarded (rule 1 of the T0 guard; the guard
#      checks it again on the payload, through the same two steps).
#   2. The fabric repo runs its own `<root>/scripts/hooks/<entry>` when present. Everyone else
#      runs the released code: a user-scope call the entry beside this script, in the release
#      its registered command names; a project-scope call
#      `${VNX_HOME:-$HOME/.vnx-system/current}/scripts/hooks/<entry>`. A user-scope call
#      ignores VNX_HOME: `bin/vnx` exports it as the copy that launched the session (a version
#      directory, a dispatch worktree) and a project's settings `env` block can set it, so it
#      would let either choose the code a user-level registration starts. The repo writes its
#      own `.vnx-project-id`, so the id alone never selects repo code: the root's real path must
#      also be the real path of the first `vnx-dev` entry in the user-owned registry
#      `$HOME/.vnx/projects.json`. A missing or unreadable registry means the released code. The
#      registry is JSON, so `python3 -I -S` reads it: the cwd is the repo, and isolated mode
#      keeps a module the repo plants there (json.py, site hooks) from being imported. That
#      read ends itself after 2 s (SIGALRM), so a registry that blocks (a FIFO, a stalled
#      mount) costs at most 2 s and means the released code.
#   3. A missing entry: one stderr line naming the path, exit 1.
#   4. `python3 <entry>` with stdin passed through, not exec'd, so its exit 2 becomes 1. A
#      shim-routed entry decides through JSON on stdout, never through exit 2.
#   5. VNX_HOOK_SCOPE=<scope> (default project) is exported to the entry.
#
# POSIX sh on purpose: dash is /bin/sh on Debian and Ubuntu. Nothing is sourced, because a
# second file that can be missing is the failure this script contains. Every exit below is an
# explicit 0 or 1; no builtin is called in a way that makes dash abort with 2.

usage="usage: vnx_hook_shim.sh [--scope user|project] <entry>"

scope=project
if [ "$#" -ge 1 ] && [ "$1" = "--scope" ]; then
  if [ "$#" -lt 2 ]; then
    echo "vnx: hook shim: --scope needs user or project. $usage" >&2
    exit 1
  fi
  scope=$2
  shift 2
fi
case "$scope" in
  user|project) ;;
  *) echo "vnx: hook shim: unknown scope '$scope'. $usage" >&2; exit 1 ;;
esac
if [ "$#" -ne 1 ]; then
  echo "vnx: hook shim: expected one entry. $usage" >&2
  exit 1
fi
entry=$1
case "$entry" in
  ""|.*|-*|*/*|*[!A-Za-z0-9._-]*)
    echo "vnx: hook shim: '$entry' is not a file name under scripts/hooks/" >&2
    exit 1 ;;
esac

# 1. Rule 1: a VNX project with a T0, outside a dispatch worktree. One order for both sides:
#    hook_coverage.session_project_root is the Python twin of this walk. The physical cwd comes
#    first; when it yields no rule-1 root, CLAUDE_PROJECT_DIR does. A candidate under
#    .vnx-data/worktrees/ yields nothing and stops the resolution; an empty or unset
#    CLAUDE_PROJECT_DIR yields nothing.
#    rule1_root <candidate>: sets root and id, returns 0 when <candidate> lies in a rule-1 project.
rule1_root() {
  case "$1/" in */.vnx-data/worktrees/*) return 1 ;; esac
  root=$1
  while [ ! -e "$root/.git" ]; do
    [ -n "$root" ] && [ "$root" != / ] || return 1
    root=${root%/*}
    [ -n "$root" ] || root=/
  done
  case "$root/" in */.vnx-data/worktrees/*) return 1 ;; esac
  [ -f "$root/.claude/terminals/T0/CLAUDE.md" ] || return 1
  [ -f "$root/.vnx-project-id" ] && [ -r "$root/.vnx-project-id" ] || return 1
  id=
  IFS= read -r id < "$root/.vnx-project-id" || [ -n "$id" ] || return 1
  id=${id%"${id##*[![:space:]]}"}
  id=${id#"${id%%[![:space:]]*}"}
  [ -n "$id" ] || return 1
  return 0
}
cwd=$(pwd -P 2>/dev/null) || exit 0
if ! rule1_root "$cwd"; then
  case "$cwd/" in */.vnx-data/worktrees/*) exit 0 ;; esac
  project_dir=${CLAUDE_PROJECT_DIR:-}
  [ -n "$project_dir" ] || exit 0
  rule1_root "$project_dir" || exit 0
fi

# 2. Which code: the registered fabric repo runs its own, everyone else the released one.
#    hook_coverage.shim_entry_file and registered_fabric_root are the Python twins.
registered_fabric_root='
import json, os, signal, sys
signal.alarm(2)
root, registry = sys.argv[1], sys.argv[2]
try:
    with open(registry, encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, UnicodeDecodeError, ValueError):
    sys.exit(1)
projects = data.get("projects") if isinstance(data, dict) else None
for project in projects if isinstance(projects, list) else ():
    if isinstance(project, dict) and project.get("project_id") == "vnx-dev":
        path = project.get("path")
        ok = isinstance(path, str) and os.path.isabs(path)
        sys.exit(0 if ok and os.path.realpath(path) == os.path.realpath(root) else 1)
sys.exit(1)
'
if [ "$scope" = user ]; then
  case "$0" in */*) hooks=${0%/*} ;; *) hooks=. ;; esac
else
  hooks=${VNX_HOME:-$HOME/.vnx-system/current}/scripts/hooks
fi
file=$hooks/$entry
if [ "$id" = "vnx-dev" ] && [ -f "$root/scripts/hooks/$entry" ] && [ -n "${HOME:-}" ] &&
  python3 -I -S -c "$registered_fabric_root" "$root" "$HOME/.vnx/projects.json" \
    </dev/null >/dev/null 2>&1; then
  file=$root/scripts/hooks/$entry
fi

# 3. A missing entry is reported, never blocking.
if [ ! -f "$file" ]; then
  echo "vnx: hook entry missing: $file" >&2
  exit 1
fi

# 4 and 5.
VNX_HOOK_SCOPE=$scope
export VNX_HOOK_SCOPE
python3 "$file"
rc=$?
if [ "$rc" -eq 2 ]; then
  rc=1
fi
exit "$rc"
