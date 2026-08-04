#!/bin/bash
# path_parity_check.sh — SessionStart hook: real launchd/cron consumers vs
# this project's requires-python range.
#
# OI-852 (Homebrew relink broke the background PATH-resolved python3 while the
# foreground shell kept working) contaminated the diagnosis of every other
# silent failure. The issue is closed; this check stays so the NEXT relink is
# caught at session start instead of after days of confusion.
#
# The check itself (scripts/lib/path_parity.py) scans real launchd agents and
# crontab entries and only alarms when one of them actually resolves an
# out-of-range interpreter — a bare foreground/background PATH probe is kept
# as diagnostic info only, never the alarm (see that module's docstring for
# why: a minimal PATH on a CLT-less Mac always resolves Xcode's old python3,
# which is a property of macOS, not a defect anyone can fix).
#
# Fail-soft by contract: this hook NEVER fails the session (always exit 0) and
# takes well under the 5s SessionStart budget on a healthy machine.
set -u

# Drain the hook payload from stdin.
cat >/dev/null 2>&1 || true

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
CHECKER="$ROOT/scripts/lib/path_parity.py"
if [ ! -f "$CHECKER" ]; then
  exit 0
fi

# ── Resolve the CENTRAL state dir (ADR-026), not a repo-local fallback ──────
# git-toplevel-relative (the old `${VNX_STATE_DIR:-$ROOT/.vnx-data/state}`
# fallback) splits the store: VNX_STATE_DIR is unset in a normal session, and
# inside a dispatch worktree BOTH bash resolvers in this repo
# (vnx_resolve_root.sh's git-toplevel walk, and vnx_paths.sh's explicit
# worktree override) deliberately re-derive to that worktree's own
# `.vnx-data` — correct for dispatch isolation, wrong for a SessionStart
# artifact that must land in the one central per-project store every reader
# expects. build_t0_state_hook.sh hit the identical trap (OI-859) and fixed
# it the same way: shell out to the Python resolver (vnx_paths.resolve_paths),
# which keeps the store central regardless of worktree, and only fall back to
# a repo-local path if that resolution itself fails (no working interpreter).
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
[ -x "$PY" ] || PY="python3"

STATE_DIR="$("$PY" -c "
import sys
sys.path.insert(0, '$ROOT/scripts/lib')
from vnx_paths import resolve_paths
print(resolve_paths()['VNX_STATE_DIR'])
" 2>/dev/null)"
[ -n "$STATE_DIR" ] || STATE_DIR="${VNX_STATE_DIR:-$ROOT/.vnx-data/state}"
OUT="$STATE_DIR/path_parity.json"

RESULT="$("$PY" "$CHECKER" --write "$OUT" --no-fail --repo-root "$ROOT" 2>/dev/null)" || exit 0

# Surface a warning into the session context only when a real consumer is broken.
case "$RESULT" in
  *'"parity": false'*)
    "$PY" - "$RESULT" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    result = json.loads(sys.argv[1])
except ValueError:
    sys.exit(0)
parts = []
for m in result.get("mismatches", []):
    parts.append("%s -> %s (%s, needs %s)" % (
        m.get("consumer", "?"), m.get("interpreter", "?"),
        m.get("version", "?"), m.get("requires_python", "?"),
    ))
if not parts:
    sys.exit(0)
msg = ("PATH/interpreter parity BROKEN: %s. This launchd/cron consumer "
       "resolves a python3 outside this project's requires-python range — "
       "it may be failing silently in the background while the foreground "
       "looks healthy (OI-852 class). Details: path_parity.json" % "; ".join(parts))
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": msg}}))
PYEOF
    ;;
esac

exit 0
