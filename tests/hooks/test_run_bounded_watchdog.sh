#!/usr/bin/env bash
# Regression test for PR #1247 finding 2: TIMEOUT_CMD was only ever set when
# timeout(1)/gtimeout(1) exist on PATH, and was left empty otherwise — so on
# a machine with neither (measured 2026-07-30: this fleet's macOS boxes),
# the reconcile call ran completely unbounded. The fix
# (scripts/lib/vnx_run_bounded.sh) adds a manual background-watchdog fallback
# for exactly that case, used here with the timeout binary forced off ("")
# regardless of what's actually on this test machine's PATH.
#
# Also regression-tests the codex gate's fix-forward findings on that same
# fallback:
#   - finding 1: a deadline kill must return a status distinct from a
#     command that happens to exit with the same raw signal status on its
#     own (tests 1 and 2 below, run side by side).
#   - finding 2: the prior "canary" here started an unrelated sleep AFTER
#     vnx_run_bounded had already returned and asserted THAT survived —
#     which never exercised the dangerous case (a stray watchdog killing an
#     innocent process requires the OS to reuse the ORIGINAL worker's pid,
#     which no shell test can reliably force on macOS). Test 4 instead
#     asserts the actual property that makes pid reuse harmless regardless:
#     the watchdog cannot still be alive by the time vnx_run_bounded returns.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$ROOT/scripts/lib/vnx_run_bounded.sh"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

overall_pass=1

# ── Test 1: a hang IS killed at the deadline, with no timeout/gtimeout, and ─
# the return status is the distinct deadline sentinel — not a raw signal
# status that could be confused with a command failing on its own.
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
elif [ "$status" -ne "$VNX_RUN_BOUNDED_DEADLINE" ]; then
    echo "FAIL: test1 — expected deadline sentinel status=$VNX_RUN_BOUNDED_DEADLINE, got status=$status (a real caller could mistake this for the command's own exit code)"
    overall_pass=0
else
    echo "PASS: test1 — hung 999s sleep was killed within ${elapsed}s (deadline=2s), pid=$hung_pid confirmed dead, status=$status is the deadline sentinel"
fi

# ── Test 2: a command that exits 143 ENTIRELY ON ITS OWN, well inside the ───
# deadline, must return 143 unchanged — proving test1's sentinel isn't a
# blanket rewrite of "the command died to a TERM-shaped status" but is
# specific to the watchdog itself having intervened. Run side by side with
# test1 to make the two cases directly comparable, per the fix-forward's
# definition of done.
start=$(date +%s)
vnx_run_bounded "" 30 "$$" bash -c 'exit 143'
status=$?
end=$(date +%s)
elapsed=$((end - start))

echo "test2 (command exits 143 on its own, no hang): elapsed=${elapsed}s status=$status"

if [ "$elapsed" -ge 5 ]; then
    echo "FAIL: test2 took ${elapsed}s — a command that exits immediately should not have waited near the 30s deadline"
    overall_pass=0
elif [ "$status" -ne 143 ]; then
    echo "FAIL: test2 — expected the command's own exit status 143 to pass through unchanged, got status=$status"
    overall_pass=0
else
    echo "PASS: test2 — command's own exit status 143 passed through unchanged (status=$status), distinct from test1's deadline sentinel ($VNX_RUN_BOUNDED_DEADLINE)"
fi

# ── Test 3: work finishing before the deadline returns promptly, and the ───
# watchdog does not linger to strike anything afterward.
start=$(date +%s)
vnx_run_bounded "" 10 "$$" sleep 1
status=$?
end=$(date +%s)
elapsed=$((end - start))

echo "test3 (finishes naturally, well under deadline): elapsed=${elapsed}s status=$status"

if [ "$elapsed" -ge 5 ]; then
    echo "FAIL: test3 took ${elapsed}s — run_bounded blocked near the 10s deadline instead of returning at ~1s"
    overall_pass=0
else
    echo "PASS: test3 — returned in ${elapsed}s, well before the 10s deadline"
fi

# ── Test 4: the watchdog cannot outlive the call, by construction ───────────
# What actually makes a stray kill against a recycled pid impossible isn't
# "nothing else happens to be using that pid within some observation
# window" (the removed canary's claim, never actually exercised) — it's
# that vnx_run_bounded always kills+reaps its OWN watchdog
# (`kill "$watchdog_pid"; wait "$watchdog_pid"`) before it returns to the
# caller. So by the time ANY caller-visible code runs, no watchdog process
# can still be alive to fire against any pid, recycled or not. This test
# asserts that property directly: no child process of this test's own pid
# survives past vnx_run_bounded's return, using the deadline-kill scenario
# (the case where the watchdog actually fires, not just exits early because
# the worker was already done) so it exercises the watchdog at its most
# active.
children_before="$(pgrep -P $$ 2>/dev/null | sort -u)"
vnx_run_bounded "" 2 "$$" bash -c 'sleep 999' >/dev/null 2>&1
# vnx_run_bounded's own `wait "$watchdog_pid"` already blocked until the
# watchdog process fully exited before returning — this sleep is only
# giving pgrep's process-table snapshot a moment to catch up, not waiting
# for anything vnx_run_bounded itself hasn't already guaranteed.
sleep 0.2
children_after="$(pgrep -P $$ 2>/dev/null | sort -u)"
stray="$(comm -13 <(printf '%s\n' "$children_before") <(printf '%s\n' "$children_after") 2>/dev/null | sed '/^$/d')"

echo "test4 (no watchdog survives return): children_before=[$(echo "$children_before" | tr '\n' ' ')] children_after=[$(echo "$children_after" | tr '\n' ' ')] stray=[$(echo "$stray" | tr '\n' ' ')]"

if [ -n "$stray" ]; then
    echo "FAIL: test4 — child process(es) of this test outlived vnx_run_bounded's return: $stray"
    overall_pass=0
else
    echo "PASS: test4 — no watchdog (or any other child) process outlived vnx_run_bounded's return"
fi

# ── Test 5: existing timeout/gtimeout fast path still delegates correctly ──
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
    echo "PASS: test5 — fast path delegates to the provided timeout binary and propagates its exit status"
else
    echo "FAIL: test5 — expected exit status 7 via fake timeout fast path, got $status"
    overall_pass=0
fi

if [ "$overall_pass" -eq 1 ]; then
    echo "PASS: all vnx_run_bounded watchdog tests passed"
    exit 0
else
    echo "FAIL: one or more vnx_run_bounded watchdog tests failed"
    exit 1
fi
