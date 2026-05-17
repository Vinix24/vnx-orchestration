#!/bin/bash
# check_route.sh — guardrail wrapper around constraint_enforcer.enforce().
#
# Usage: check_route.sh --provider <provider> [--sub-provider <sub>] [--model <model>]
#                        [--terminal-id <tid>] [--role <role>] [--via <via>]
#
# Exits 0 when the route is allowed. Exits 1 with a clear message when a
# blocking constraint is violated.
#
# PR-SR-2 ships this guardrail; provider_dispatch.py also calls enforce() inline.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"

PROVIDER=""
SUB_PROVIDER=""
MODEL=""
TERMINAL_ID=""
ROLE=""
VIA=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --provider)     PROVIDER="$2";     shift 2 ;;
    --sub-provider) SUB_PROVIDER="$2"; shift 2 ;;
    --model)        MODEL="$2";        shift 2 ;;
    --terminal-id)  TERMINAL_ID="$2";  shift 2 ;;
    --role)         ROLE="$2";         shift 2 ;;
    --via)          VIA="$2";          shift 2 ;;
    *)
      echo "check_route: unknown argument: $1" >&2
      echo "usage: $(basename "$0") --provider <provider> [--sub-provider <sub>] [--model <model>] [--terminal-id <tid>] [--role <role>] [--via <via>]" >&2
      exit 2
      ;;
  esac
done

if [ -z "$PROVIDER" ]; then
  echo "check_route: --provider is required" >&2
  exit 2
fi

PYTHONPATH="$REPO_ROOT/scripts/lib${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON_BIN" -c '
import sys, json
try:
    from constraint_enforcer import enforce, HardConstraintViolation
except ImportError as exc:
    sys.stderr.write(f"check_route: cannot import constraint_enforcer ({exc}).\n")
    sys.exit(1)

args = json.loads(sys.stdin.read())
try:
    enforce(
        provider=args.get("provider") or None,
        sub_provider=args.get("sub_provider") or None,
        model=args.get("model") or None,
        terminal_id=args.get("terminal_id") or None,
        role=args.get("role") or None,
        via=args.get("via") or None,
    )
except HardConstraintViolation as exc:
    sys.stderr.write(f"check_route: BLOCKED — {exc}\n")
    sys.exit(1)
print("check_route: route allowed")
' <<EOF
$(printf '{"provider":"%s","sub_provider":"%s","model":"%s","terminal_id":"%s","role":"%s","via":"%s"}' \
  "$(printf '%s' "$PROVIDER" | sed 's/["\]/\\&/g')" \
  "$(printf '%s' "$SUB_PROVIDER" | sed 's/["\]/\\&/g')" \
  "$(printf '%s' "$MODEL" | sed 's/["\]/\\&/g')" \
  "$(printf '%s' "$TERMINAL_ID" | sed 's/["\]/\\&/g')" \
  "$(printf '%s' "$ROLE" | sed 's/["\]/\\&/g')" \
  "$(printf '%s' "$VIA" | sed 's/["\]/\\&/g')")
EOF
