#!/bin/bash
# cleanup_stale_vnx_sessions.sh — detect and optionally kill stale VNX tmux sessions
#
# Usage: bash scripts/cleanup_stale_vnx_sessions.sh
#
# Finds tmux sessions starting with 'vnx-' that have been idle >7 days.
# Prints a table, prompts for confirmation, then kills if confirmed.
# Exit 0 on clean run (including no stale sessions found); 1 on error.

set -euo pipefail

STALE_DAYS=7
SECONDS_PER_DAY=86400
STALE_THRESHOLD=$(( STALE_DAYS * SECONDS_PER_DAY ))

NOW=$(date +%s)

# ── helpers ──────────────────────────────────────────────────────────────────

die() { echo "[error] $*" >&2; exit 1; }

check_tmux() {
    if ! command -v tmux &>/dev/null; then
        echo "[info] tmux not found — nothing to check."
        exit 0
    fi
}

list_vnx_sessions() {
    # tmux list-sessions returns non-zero when no sessions exist
    tmux list-sessions -F '#{session_name} #{session_activity}' 2>/dev/null \
        | grep '^vnx-' \
        || true   # empty output is fine; don't propagate non-zero from grep
}

days_idle() {
    local activity_ts="$1"
    local idle_s=$(( NOW - activity_ts ))
    echo $(( idle_s / SECONDS_PER_DAY ))
}

# ── main ─────────────────────────────────────────────────────────────────────

check_tmux

RAW=$(list_vnx_sessions)

if [[ -z "$RAW" ]]; then
    echo "[ok] No vnx-* tmux sessions found."
    exit 0
fi

# Collect stale sessions into parallel arrays
STALE_NAMES=()
STALE_DAYS_IDLE=()

while IFS= read -r line; do
    session_name=$(echo "$line" | awk '{print $1}')
    activity_ts=$(echo "$line"  | awk '{print $2}')

    # Guard against non-numeric activity timestamp
    if ! [[ "$activity_ts" =~ ^[0-9]+$ ]]; then
        echo "[warn] Could not parse activity timestamp for session '$session_name' — skipping."
        continue
    fi

    idle=$(days_idle "$activity_ts")

    if (( idle >= STALE_DAYS )); then
        STALE_NAMES+=("$session_name")
        STALE_DAYS_IDLE+=("$idle")
    fi
done <<< "$RAW"

if [[ ${#STALE_NAMES[@]} -eq 0 ]]; then
    echo "[ok] No vnx-* sessions idle >${STALE_DAYS} days."
    exit 0
fi

# Print table
printf "\n%-40s %10s  %s\n" "SESSION" "IDLE (DAYS)" "SUGGESTED KILL"
printf "%s\n" "$(printf '─%.0s' {1..75})"
for i in "${!STALE_NAMES[@]}"; do
    printf "%-40s %10s  tmux kill-session -t %s\n" \
        "${STALE_NAMES[$i]}" \
        "${STALE_DAYS_IDLE[$i]}" \
        "${STALE_NAMES[$i]}"
done
printf "\n"

N=${#STALE_NAMES[@]}

# Prompt (default N — safe)
read -r -p "Kill these $N session(s)? [y/N] " ANSWER </dev/tty || ANSWER="N"

case "$ANSWER" in
    [yY]|[yY][eE][sS])
        echo ""
        for name in "${STALE_NAMES[@]}"; do
            if tmux kill-session -t "$name" 2>/dev/null; then
                echo "[ok] Killed: $name"
            else
                echo "[warn] Could not kill '$name' (already gone?)."
            fi
        done
        echo ""
        echo "[done] ${N} session(s) processed."
        ;;
    *)
        echo "[skip] No sessions killed."
        ;;
esac

exit 0
