#!/usr/bin/env bash
# VNX digest command -- decisions-first nightly digest viewer and decision logger.
#
# Usage:
#   vnx digest                       Show today's decisions digest
#   vnx digest --email               Send digest email
#   vnx digest decide DEC-N <action> [reason]  Log a decision
#   vnx digest history               Show last 7 days of decisions

cmd_digest() {
  local subcmd="${1:-show}"
  shift || true

  case "$subcmd" in
    --email)
      python3 "$VNX_HOME/scripts/send_digest_email.py" --decisions "$@"
      ;;

    decide)
      local dec_id="${1:-}"
      local action="${2:-}"
      local reason="${3:-}"
      if [ -z "$dec_id" ] || [ -z "$action" ]; then
        printf 'Usage: vnx digest decide <DEC-N> <accept|alt|defer> [reason]\n' >&2
        return 1
      fi
      case "$action" in
        accept|alt|defer) ;;
        *)
          printf 'ERROR: action must be one of: accept, alt, defer\n' >&2
          return 1
          ;;
      esac
      python3 "$VNX_HOME/scripts/decisions_log.py" "$dec_id" "$action" "$reason" "operator"
      ;;

    history)
      local log_path="${VNX_STATE_DIR:-$VNX_DATA_DIR/state}/decisions_log.ndjson"
      if [ ! -f "$log_path" ]; then
        printf 'No decisions logged yet.\n'
        return 0
      fi
      python3 - "$log_path" <<'PY'
import json, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

log_path = Path(sys.argv[1])
cutoff = datetime.now(timezone.utc) - timedelta(days=7)
records = []
for line in log_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        rec = json.loads(line)
        ts_raw = rec.get("timestamp", "")
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw else None
        if ts and ts >= cutoff:
            records.append(rec)
    except Exception:
        pass
records.reverse()
if not records:
    print("No decisions in last 7 days.")
else:
    print(f"Last 7 days ({len(records)} decisions):\n")
    for r in records:
        ts_str = r.get("timestamp", "")[:10]
        dec_id = r.get("dec_id", "")
        action = r.get("action", "")
        reason = r.get("reason", "")
        print(f"  {ts_str}  {dec_id:<10}  {action:<8}  {reason}")
PY
      ;;

    show|"")
      python3 "$VNX_HOME/scripts/build_decisions_digest.py" "$@"
      ;;

    *)
      # Passthrough unknown args to show
      python3 "$VNX_HOME/scripts/build_decisions_digest.py" "$subcmd" "$@"
      ;;
  esac
}
