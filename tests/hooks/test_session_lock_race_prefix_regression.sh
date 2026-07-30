#!/usr/bin/env bash
# Regression test for PR #1247 finding 3 (codex gate): the strongest evidence
# for the flock(2) rewrite was a comparison — this exact race harness,
# run against the PRE-FIX pid-file reclaim, produced multiple winners
# (measured 9-11 of 20 in the original dispatch report). That comparison
# lived only in prose, not in the committed suite, so nobody could re-run
# it and the claim was only as good as the report's word.
#
# This commits it: the SAME N-contenders-per-round harness as
# tests/hooks/test_session_lock_race.sh, the SAME real
# vnx_flock_acquire/vnx_flock_release call sites in the worker script
# (never reimplemented), pointed instead at
# tests/fixtures/vnx_prefix_pidfile_lock.sh — the vendored pre-fix
# implementation. Asserts MORE than one winner in at least one round: proof
# that this harness actually detects the race it claims to detect, not just
# proof that the current flock(2) implementation happens to avoid it.
#
# This test is EXPECTED to "fail" in the sense of reproducing the race —
# its PASS condition is "the race reproduced," i.e. old code = red. If this
# ever stopped reproducing a multi-winner round, that would mean the
# harness itself had lost the ability to distinguish broken locking from
# fixed locking, which would silently invalidate what
# test_session_lock_race.sh's "exactly 1 winner" result is supposed to
# prove about the CURRENT implementation.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOCK_LIB="$ROOT/tests/fixtures/vnx_prefix_pidfile_lock.sh"

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

# Identical to test_session_lock_race.sh's WORKER_SCRIPT — sources the real
# vnx_flock_acquire/vnx_flock_release from whichever lib is passed in, never
# reimplements the locking logic in the test itself.
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

rounds_with_race=0

for round in $(seq 1 "$ROUNDS"); do
    round_dir="$WORK_DIR/round_$round"
    mkdir -p "$round_dir/out"
    lock_file="$round_dir/session_reconcile_autoclose.lock"

    # Simulate a crashed prior holder: leftover content on disk, nothing
    # actually holds any lock on it (this fixture has no fd-based lock at
    # all — that absence is exactly what makes it racy).
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

    echo "round $round: winners=$winners losers=$losers total=$N (pre-fix pid-file lock, tests/fixtures/vnx_prefix_pidfile_lock.sh)"

    if [ "$winners" -gt 1 ]; then
        rounds_with_race=$((rounds_with_race + 1))
    fi
done

echo "rounds with more than one winner: $rounds_with_race / $ROUNDS"

if [ "$rounds_with_race" -ge 1 ]; then
    echo "PASS (expected-red target): the pre-fix pid-file lock reproduced the reclaim race — $rounds_with_race/$ROUNDS rounds had >1 winner, confirming this harness can detect a broken lock, not just confirm a working one"
    exit 0
else
    echo "FAIL: the pre-fix pid-file lock did NOT reproduce a multi-winner race in any of $ROUNDS rounds — either the harness can no longer distinguish broken locking from fixed locking, or this run got unlucky with OS scheduling (rerun before trusting a red result here)"
    exit 1
fi
