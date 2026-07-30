#!/usr/bin/env bash
# Regression test for PR #1247 finding 2: TIMEOUT_CMD was only ever set when
# timeout(1)/gtimeout(1) exist on PATH, and was left empty otherwise — so on
# a machine with neither (measured 2026-07-30: this fleet's macOS boxes),
# the reconcile call ran completely unbounded. The fix
# (scripts/lib/vnx_run_bounded.sh) adds a manual background-watchdog fallback
# for exactly that case, used here with the timeout binary forced off ("")
# regardless of what's actually on this test machine's PATH.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$ROOT/scripts/lib/vnx_run_bounded.sh"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

overall_pass=1

# ── Test 1: a hang IS killed at the deadline, with no timeout/gtimeout ──────
pidfile="$WORK_DIR/hung.pid"
start=$(date +%s)
vnx_run_bounded "" 2 "$$" bash -c 'echo $$ > "$1"; sleep 999' _ "$pidfile"
status=$?
end=$(date +%s)
elapsed=$((end - start))

hung_pid="$(cat "$pidfile" 2>/dev/null || echo '')"
still_alive=1
if [ -n "$hung_pid" ] && ! kill -0 "$hung_pid" 2>/dev/null; then
    still_alive=0
fi

echo "test1 (hang killed at deadline): elapsed=${elapsed}s status=$status hung_pid=$hung_pid still_alive=$still_alive"

if [ "$elapsed" -ge 10 ]; then
    echo "FAIL: test1 took ${elapsed}s — the 999s sleep was not bounded (expected ~2s)"
    overall_pass=0
elif [ "$still_alive" -ne 0 ]; then
    echo "FAIL: test1 — hung process pid=$hung_pid is still alive after run_bounded returned"
    overall_pass=0
else
    echo "PASS: test1 — hung 999s sleep was killed within ${elapsed}s (deadline=2s), pid=$hung_pid confirmed dead"
fi

# ── Test 2: work finishing before the deadline returns promptly, and the ───
# watchdog does not linger to strike anything afterward.
start=$(date +%s)
vnx_run_bounded "" 10 "$$" sleep 1
status=$?
end=$(date +%s)
elapsed=$((end - start))

echo "test2 (finishes naturally, well under deadline): elapsed=${elapsed}s status=$status"

if [ "$elapsed" -ge 5 ]; then
    echo "FAIL: test2 took ${elapsed}s — run_bounded blocked near the 10s deadline instead of returning at ~1s"
    overall_pass=0
else
    echo "PASS: test2 — returned in ${elapsed}s, well before the 10s deadline"
fi

# Canary: a fresh, unrelated background process started right after
# run_bounded returns. If the watchdog from test2 (deadline would have
# elapsed at t=10s from test2's start) were still alive and fired a stray
# kill later, it could hit this canary purely by pid coincidence. The canary
# must outlive our own observation window on its own merits (30s), so that
# checking it at t+9.5s tests for a STRAY kill, not natural completion.
sleep 30 &
canary_pid=$!
sleep 9.5
canary_alive=1
kill -0 "$canary_pid" 2>/dev/null || canary_alive=0
kill "$canary_pid" 2>/dev/null
wait "$canary_pid" 2>/dev/null

if [ "$canary_alive" -ne 1 ]; then
    echo "FAIL: canary process (pid=$canary_pid) died — a stray watchdog kill fired after cancellation"
    overall_pass=0
else
    echo "PASS: canary process survived past test2's original deadline — no stray watchdog kill"
fi

# ── Test 3: existing timeout/gtimeout fast path still delegates correctly ──
fake_timeout="$WORK_DIR/fake_timeout"
cat > "$fake_timeout" <<'EOF'
#!/usr/bin/env bash
# Minimal stand-in for timeout(1): ignore the deadline arg, exec the rest.
shift
exec "$@"
EOF
chmod +x "$fake_timeout"

vnx_run_bounded "$fake_timeout" 5 "$$" bash -c 'exit 7'
status=$?
if [ "$status" -eq 7 ]; then
    echo "PASS: test3 — fast path delegates to the provided timeout binary and propagates its exit status"
else
    echo "FAIL: test3 — expected exit status 7 via fake timeout fast path, got $status"
    overall_pass=0
fi

if [ "$overall_pass" -eq 1 ]; then
    echo "PASS: all vnx_run_bounded watchdog tests passed"
    exit 0
else
    echo "FAIL: one or more vnx_run_bounded watchdog tests failed"
    exit 1
fi
