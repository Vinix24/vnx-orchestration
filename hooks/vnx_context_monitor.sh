#!/usr/bin/env bash
set -euo pipefail

_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_DIR/lib/_vnx_hook_common.sh"

vnx_context_rotation_enabled || exit 0

INPUT="$(cat)"

json_bool_field() {
  local json="$1"
  local jq_expr="$2"

  if ! command -v jq >/dev/null 2>&1; then
    return 1
  fi

  jq -r "$jq_expr" <<<"$json" 2>/dev/null || return 1
}

STOP_ACTIVE="$(
  json_bool_field "$INPUT" 'if (.stop_hook_active // .hook_data.stop_hook_active // false) then "true" else "false" end' \
    || echo "false"
)"

# Loop prevention: Claude may re-enter the Stop hook after we return hook output.
[[ "$STOP_ACTIVE" == "true" ]] && exit 0

TERMINAL="$(vnx_detect_terminal)"
case "$TERMINAL" in
  T1|T2|T3) ;;
  *) exit 0 ;;
esac

STATE_FILE="$VNX_STATE_DIR/context_window.json"
[[ -f "$STATE_FILE" ]] || exit 0

if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

REMAINING_RAW="$(jq -r '.remaining_pct // empty' "$STATE_FILE" 2>/dev/null || true)"
[[ -n "$REMAINING_RAW" ]] || exit 0

REMAINING_INT="${REMAINING_RAW%.*}"
[[ "$REMAINING_INT" =~ ^-?[0-9]+$ ]] || exit 0

if (( REMAINING_INT < 0 )); then REMAINING_INT=0; fi
if (( REMAINING_INT > 100 )); then REMAINING_INT=100; fi
USED_PCT=$((100 - REMAINING_INT))

WARNING_THRESHOLD=60
ROTATION_THRESHOLD=80

if (( USED_PCT < WARNING_THRESHOLD )); then
  exit 0
fi

if (( USED_PCT >= ROTATION_THRESHOLD )); then
  vnx_log "Context pressure high: ${USED_PCT}% used on $TERMINAL (block)"
  cat <<EOF
{"decision":"block","hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"VNX CONTEXT ROTATION REQUIRED (${USED_PCT}% used, ${REMAINING_INT}% remaining)\\n\\nWrite a handover file now in \$VNX_DATA_DIR/rotation_handovers/ named with ${TERMINAL}-ROTATION-HANDOVER and then continue after /clear. Include current status, completed work, and next steps. If you already wrote the handover, do not rewrite it."}}
EOF
  exit 0
fi

vnx_log "Context pressure warning: ${USED_PCT}% used on $TERMINAL"
cat <<EOF
{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"VNX CONTEXT WARNING: ${USED_PCT}% used (${REMAINING_INT}% remaining). Start preparing a ROTATION-HANDOVER note if you are nearing completion of the current subtask."}}
EOF
