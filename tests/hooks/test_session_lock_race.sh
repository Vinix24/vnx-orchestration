#!/usr/bin/env bash
# Regression test for PR #1247 finding 1: session_reconcile_autoclose.sh's
# lock RECLAIM path could hand the lock to two racing reclaimers at once
# (rm-then-noclobber-write is two syscalls with a gap between them). The fix
# replaced the PID-file + rm-then-write reclaim with a kernel flock(2) mutex
# (scripts/lib/vnx_flock_lock.sh), which has no "is the recorded holder dead"
# check to race — the kernel itself releases a dead holder's lock atomically.
#
# This spawns N concurrent contenders against a lock file pre-seeded with
# leftover content from a simulated dead holder (nothing actually holds the
# flock on it) and asserts exactly one wins, repeated across multiple rounds
# — a single-run pass is not evidence for a race fix.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOCK_LIB="$ROOT/scripts/lib/vnx_flock_lock.sh"

if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
elif [ -x "/opt/homebrew/opt/python@3.12/bin/python3.12" ]; then
    PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
else
    PY="python3"
fi

N=20
ROUNDS=5
HOLD_SECS=0.3

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# Each worker sources the real lib and calls the real vnx_flock_acquire —
# never reimplement the locking logic in the test itself.
WORKER_SCRIPT='
source "$1"
lock_file="$2"
idx="$3"
py="$4"
out_file="$5"
hold_secs="$6"
if vnx_flock_acquire "$lock_file" "worker-$idx" "$py"; then
    sleep "$hold_secs"
    echo "WON worker-$idx" > "$out_file"
    vnx_flock_release
else
    echo "LOST worker-$idx" > "$out_file"
fi
'

overall_pass=1

for round in $(seq 1 "$ROUNDS"); do
    round_dir="$WORK_DIR/round_$round"
    mkdir -p "$round_dir/out"
    lock_file="$round_dir/session_reconcile_autoclose.lock"

    # Simulate a crashed prior holder: leftover content on disk, nothing
    # actually holds the flock on it (no process has fd 200 open on it).
    echo "999999999" > "$lock_file"

    pids=()
    for i in $(seq 1 "$N"); do
        bash -c "$WORKER_SCRIPT" _ "$LOCK_LIB" "$lock_file" "$i" "$PY" "$round_dir/out/$i" "$HOLD_SECS" &
        pids+=("$!")
    done

    for pid in "${pids[@]}"; do
        wait "$pid"
    done

    winners=$(grep -l "^WON " "$round_dir"/out/* 2>/dev/null | wc -l | tr -d ' ')
    losers=$(grep -l "^LOST " "$round_dir"/out/* 2>/dev/null | wc -l | tr -d ' ')
    winner_content="$(cat "$lock_file" 2>/dev/null)"

    echo "round $round: winners=$winners losers=$losers total=$N final_lock_content='$winner_content'"

    if [ "$winners" != "1" ] || [ "$losers" != "$((N - 1))" ]; then
        echo "FAIL: round $round expected exactly 1 winner and $((N - 1)) losers out of $N contenders"
        overall_pass=0
    fi
done

if [ "$overall_pass" -eq 1 ]; then
    echo "PASS: $ROUNDS/$ROUNDS rounds each produced exactly 1 winner out of $N racing reclaimers"
    exit 0
else
    echo "FAIL: one or more rounds did not produce exactly 1 winner"
    exit 1
fi
