#!/bin/bash
# path_parity_check.sh — SessionStart hook: foreground vs background python3 parity.
#
# OI-852 (Homebrew relink broke the background PATH-resolved python3 while the
# foreground shell kept working) contaminated the diagnosis of every other
# silent failure. The issue is closed; this check stays so the NEXT relink is
# caught at session start instead of after days of confusion.
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

STATE_DIR="${VNX_STATE_DIR:-$ROOT/.vnx-data/state}"
OUT="$STATE_DIR/path_parity.json"

RESULT="$(python3 "$CHECKER" --write "$OUT" --no-fail 2>/dev/null)" || exit 0

# Surface a warning into the session context only when parity is broken.
case "$RESULT" in
  *'"parity": false'*)
    python3 - "$RESULT" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    result = json.loads(sys.argv[1])
except ValueError:
    sys.exit(0)
kinds = ", ".join(m.get("kind", "?") for m in result.get("mismatches", []))
msg = ("PATH/interpreter parity BROKEN (%s). Foreground and background "
       "(launchd/cron) python3 diverge — background jobs may be failing while "
       "the foreground looks healthy (OI-852 class). Details: path_parity.json" % kinds)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": msg}}))
PYEOF
    ;;
esac

exit 0
