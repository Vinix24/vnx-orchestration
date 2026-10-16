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
#   1. The root is `git rev-parse --show-toplevel` from the process cwd. Unless that root has a
#      `.vnx-project-id` with a non-empty first line and a `.claude/terminals/T0/CLAUDE.md`, and
#      the cwd is outside `.vnx-data/worktrees/`, it exits 0 without output: only a VNX project
#      with a T0 is guarded (rule 1 of the T0 guard; the guard checks it again on the payload).
#   2. The fabric repo runs its own `<root>/scripts/hooks/<entry>` when present. Everyone else
#      runs `${VNX_HOME:-$HOME/.vnx-system/current}/scripts/hooks/<entry>`. The repo writes its
#      own `.vnx-project-id`, so the id alone never selects repo code: the root's real path must
#      also be the real path of the first `vnx-dev` entry in the user-owned registry
#      `$HOME/.vnx/projects.json`. A missing or unreadable registry means `current`. The
#      registry is JSON, so `python3 -I -S` reads it: the cwd is the repo, and isolated mode
#      keeps a module the repo plants there (json.py, site hooks) from being imported.
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

# 1. Rule 1: a VNX project with a T0, outside a dispatch worktree.
cwd=$(pwd -P 2>/dev/null) || exit 0
case "$cwd/" in */.vnx-data/worktrees/*) exit 0 ;; esac
root=$(git rev-parse --show-toplevel 2>/dev/null </dev/null) || exit 0
[ -n "$root" ] || exit 0
case "$root/" in */.vnx-data/worktrees/*) exit 0 ;; esac
[ -f "$root/.claude/terminals/T0/CLAUDE.md" ] || exit 0
[ -f "$root/.vnx-project-id" ] && [ -r "$root/.vnx-project-id" ] || exit 0
id=
IFS= read -r id < "$root/.vnx-project-id" || [ -n "$id" ] || exit 0
id=${id%"${id##*[![:space:]]}"}
id=${id#"${id%%[![:space:]]*}"}
[ -n "$id" ] || exit 0

# 2. Which code: the registered fabric repo runs its own, everyone else the released one.
#    hook_coverage.registered_fabric_root is the Python twin of this reader.
registered_fabric_root='
import json, os, sys
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
file=${VNX_HOME:-$HOME/.vnx-system/current}/scripts/hooks/$entry
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
