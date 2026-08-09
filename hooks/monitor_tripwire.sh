#!/bin/bash
# monitor_tripwire.sh — the deliberately DUMB tripwire for the producer-freshness monitor.
#
# A monitor that detects silent failure can itself fail silently. The monitor's
# own heartbeat (scripts/producer_freshness_monitor.py writes it on EVERY run,
# including zero-finding runs) is checked here by testing ONLY the file's age
# with `find -mmin` — nothing more.
#
# Independence contract (the whole point): this script shares NO code, NO DB
# connection and NO Python interpreter with the monitor it watches. A break
# like OI-852 (background python3 broken by a Homebrew relink) may kill the
# monitor, but it cannot kill this alarm at the same time. Do not "improve"
# this script by importing shared helpers — dumbness is the feature.
#
# Runs at SessionStart (registered in .claude/settings.json), a path that
# already executes every session. Always exits 0; never blocks a session.
set -u

# Drain the hook payload from stdin.
cat >/dev/null 2>&1 || true

# Max heartbeat age in minutes before the tripwire fires (default: 8h — the
# sweep runs every 6h, so 8h allows schedule jitter + a single missed cycle).
MAX_AGE_MIN="${VNX_TRIPWIRE_MAX_AGE_MIN:-480}"

# Heartbeat locations to check. Test override: VNX_TRIPWIRE_HEARTBEAT_GLOB.
if [ -n "${VNX_TRIPWIRE_HEARTBEAT_GLOB:-}" ]; then
  CANDIDATES="$VNX_TRIPWIRE_HEARTBEAT_GLOB"
else
  CANDIDATES="$HOME/.vnx-data/*/health/producer_freshness_monitor.json"
  if [ -n "${VNX_DATA_DIR:-}" ]; then
    CANDIDATES="$VNX_DATA_DIR/health/producer_freshness_monitor.json $CANDIDATES"
  fi
fi

FOUND=0
STALE=""
# shellcheck disable=SC2086  # intentional glob expansion of CANDIDATES
for hb in $CANDIDATES; do
  [ -f "$hb" ] || continue
  FOUND=1
  if [ -n "$(find "$hb" -mmin +"$MAX_AGE_MIN" 2>/dev/null)" ]; then
    STALE="$STALE $hb"
  fi
done

warn() {
  # Emit a SessionStart additionalContext warning. jq if present, manual JSON
  # otherwise — either way we exit 0 afterwards.
  msg="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -nc --arg msg "$msg" \
      '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$msg}}'
  else
    escaped=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$escaped"
  fi
}

if [ "$FOUND" -eq 0 ]; then
  warn "producer-freshness monitor tripwire: NO heartbeat file found under ~/.vnx-data/*/health/ — the monitor may never have run. Silent-failure detection is BLIND until scripts/producer_freshness_monitor.py runs once."
  exit 0
fi

if [ -n "$STALE" ]; then
  warn "producer-freshness monitor tripwire: heartbeat older than ${MAX_AGE_MIN}m:$STALE — the freshness sweep itself is silently down. Silent-failure detection is BLIND."
  exit 0
fi

exit 0
