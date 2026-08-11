#!/bin/bash
# hookpin_check.sh — SessionStart hook: verify every hook path this project's
# own .claude/settings.json points at still resolves to a real file.
#
# OI-1123: a hook pinned to a fabric version/path that stopped existing fails
# SILENTLY — Claude Code prints "Stop hook error: ... No such file or
# directory" to the pane, nobody reads that, and the governed report/guard
# that hook was supposed to provide simply never runs. A doctor-time-only
# check would have caught the install-time state but not this: the pin was
# valid when it was written and only orphaned later, when the fabric version
# it pointed at was retired. Re-checking on every SessionStart is what turns
# that drift into something visible on the very next session instead of
# whenever a human happens to notice a stray pane error.
#
# Fail-soft by contract: this hook NEVER fails the session (always exit 0)
# and does not block the SessionStart budget on a healthy machine.
set -u

cat >/dev/null 2>&1 || true

# The checker itself is engine-anchored (resolved relative to THIS script's
# own location) — correct for every layout: central install, embedded, and
# this repo's own dev checkout. The project being audited is git-toplevel —
# always the consumer's own root, never the engine's.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKER="$SCRIPT_DIR/../lib/hookpin_check.py"
[ -f "$CHECKER" ] || exit 0

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
[ -f "$ROOT/.claude/settings.json" ] || exit 0

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

RESULT="$("$PY" "$CHECKER" --project-root "$ROOT" --json 2>/dev/null)" || exit 0

# Surface a warning into the session context only when a pin is actually dead.
case "$RESULT" in
  *'"status": "missing"'*)
    "$PY" - "$RESULT" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    findings = json.loads(sys.argv[1])
except (ValueError, IndexError):
    sys.exit(0)
missing = [f for f in findings if f.get("status") == "missing"]
if not missing:
    sys.exit(0)
parts = ["%s (%s): %s" % (f.get("event", "?"), f.get("matcher") or "*", f.get("raw_path", "?"))
         for f in missing]
msg = ("%d configured hook path(s) do NOT resolve to a real file — that hook "
       "is silently NOT running: %s. Run `vnx doctor` for detail, then "
       "`vnx regen-settings --merge` (or fix .claude/settings.json directly)."
       % (len(missing), "; ".join(parts)))
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": msg}}))
PYEOF
    ;;
esac

exit 0
