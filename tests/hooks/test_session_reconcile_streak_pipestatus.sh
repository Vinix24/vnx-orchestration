#!/usr/bin/env bash
# Regression test for PR #1247 fix-forward finding (codex gate): in
# scripts/hooks/session_reconcile_autoclose.sh, the reconcile-streak
# PIPESTATUS[0] used to be read AFTER the if/then/else branch had already
# run an assignment (STREAK_MET="yes"/"no") — bash resets PIPESTATUS after
# EVERY command, including plain assignments, so PIPESTATUS[0] at read time
# always described that assignment (always 0), never the pipeline. Codex
# reproduced this against the real helper: streak_met=no
# captured_PIPESTATUS0=0 expected_deadline=124. Consequence: a
# reconcile-streak call killed by the deadline watchdog logged identically
# to a normal streak_met=no, with no way to tell a real negative
# measurement apart from a discarded one.
#
# This does NOT reimplement the fixed logic — it extracts the actual fixed
# statement block (from the literal `STREAK_MET="?"` line through the `fi`
# that closes the KILLED-log guard) straight out of the live hook file with
# sed and `eval`s it verbatim, with the surrounding variables the real hook
# would have set (TIMEOUT_BIN, DEADLINE_SECS, SELF_PID, PY, CLI, PID_ARGS,
# STAMP, LOG, and vnx_run_bounded/$VNX_RUN_BOUNDED_DEADLINE from the real,
# unmodified lib). Only the reconcile-streak CLI backend is faked — the
# same kind of external-dependency fake test5 in test_run_bounded_watchdog.sh
# uses for the timeout binary — so a change to the real block's logic is
# guaranteed to be exercised here, not silently skipped by a stale copy.
#
# Not `set -u`: the real hook does not run under nounset either, and
# `PID_ARGS=(); "${PID_ARGS[@]}"` raises "unbound variable" on this
# machine's bash 3.2.57 under `set -u` even though it is exactly how the
# hook itself expands PID_ARGS — matching the hook's own execution context
# here, not the stricter mode the test files use for their own harness code.
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK="$ROOT/scripts/hooks/session_reconcile_autoclose.sh"
BOUND_LIB="$ROOT/scripts/lib/vnx_run_bounded.sh"

if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
elif [ -x "/opt/homebrew/opt/python@3.12/bin/python3.12" ]; then
    PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
else
    PY="python3"
fi

source "$BOUND_LIB"

# ── Extract the real fixed block from the live hook file — never retyped ────
start_line="$(grep -n '^    STREAK_MET="?"$' "$HOOK" | head -1 | cut -d: -f1)"
killed_echo_line="$(grep -n 'not a real observation' "$HOOK" | head -1 | cut -d: -f1)"
end_line=$((killed_echo_line + 1))

if [ -z "$start_line" ] || [ -z "$killed_echo_line" ]; then
    echo "FAIL: could not locate the streak block markers in $HOOK — test harness is stale"
    exit 1
fi
end_line_text="$(sed -n "${end_line}p" "$HOOK")"
if [ "$end_line_text" != "    fi" ]; then
    echo "FAIL: expected line $end_line of $HOOK to be the closing 'fi' of the KILLED-log guard, got: $end_line_text"
    exit 1
fi

STREAK_BLOCK="$(sed -n "${start_line},${end_line}p" "$HOOK")"
echo "extracted streak block: lines ${start_line}-${end_line} of $HOOK"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# ── Fake reconcile-streak CLI backend ────────────────────────────────────
# Invoked exactly as the real hook invokes it: "$PY" "$CLI" objective
# reconcile-streak ... --json. Sleeps FAKE_CLI_HANG seconds (if set) before
# emitting {"flip_criterion_met": <FAKE_CLI_FLIP>} so a test can force the
# call past a short DEADLINE_SECS and force the watchdog to fire.
FAKE_CLI="$WORK_DIR/fake_reconcile_cli.py"
cat >"$FAKE_CLI" <<'PYEOF'
import json
import os
import sys
import time

hang = float(os.environ.get("FAKE_CLI_HANG", "0"))
if hang > 0:
    time.sleep(hang)
flip = os.environ.get("FAKE_CLI_FLIP", "0") == "1"
print(json.dumps({"flip_criterion_met": flip}))
sys.exit(0)
PYEOF

overall_pass=1

run_streak_block() {
    # Runs the extracted block in a fresh subshell so each scenario starts
    # with clean STREAK_* state, mirroring the real hook's own subshell.
    (
        TIMEOUT_BIN=""    # force the manual watchdog fallback, matching this fleet (no timeout/gtimeout)
        DEADLINE_SECS="$1"
        SELF_PID="$$"
        PY="$PY"
        CLI="$FAKE_CLI"
        PID_ARGS=()
        STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        LOG="$2"
        eval "$STREAK_BLOCK"
        echo "STREAK_RC=$STREAK_RC STREAK_FILTER_RC=$STREAK_FILTER_RC STREAK_MET=$STREAK_MET" >"$3"
    )
}

# ── Scenario 1: streak call exceeds the deadline — watchdog must fire ───────
log1="$WORK_DIR/killed.log"
vars1="$WORK_DIR/killed.vars"
: >"$log1"
FAKE_CLI_HANG=4 FAKE_CLI_FLIP=0 run_streak_block 1 "$log1" "$vars1"
source "$vars1"

echo "--- scenario 1 (killed): $(cat "$vars1")"
echo "--- scenario 1 log content:"
cat "$log1"

if [ "$STREAK_RC" != "$VNX_RUN_BOUNDED_DEADLINE" ]; then
    echo "FAIL: scenario 1 — expected captured STREAK_RC=$VNX_RUN_BOUNDED_DEADLINE (the real deadline sentinel), got STREAK_RC=$STREAK_RC"
    overall_pass=0
elif ! grep -q "reconcile-streak KILLED at 1s deadline" "$log1"; then
    echo "FAIL: scenario 1 — expected a KILLED-at-deadline log line naming the watchdog, log had: $(cat "$log1")"
    overall_pass=0
else
    echo "PASS: scenario 1 — killed streak call captured STREAK_RC=$STREAK_RC (deadline sentinel) and logged the KILLED line"
fi

# ── Scenario 2: normal completion, flip_criterion_met=true → streak_met=yes ─
log2="$WORK_DIR/yes.log"
vars2="$WORK_DIR/yes.vars"
: >"$log2"
FAKE_CLI_HANG=0 FAKE_CLI_FLIP=1 run_streak_block 30 "$log2" "$vars2"
source "$vars2"

echo "--- scenario 2 (normal, flip=1): $(cat "$vars2")"

if [ "$STREAK_RC" = "$VNX_RUN_BOUNDED_DEADLINE" ]; then
    echo "FAIL: scenario 2 — normal fast completion was misreported as killed (STREAK_RC=$STREAK_RC)"
    overall_pass=0
elif [ "$STREAK_MET" != "yes" ]; then
    echo "FAIL: scenario 2 — expected streak_met=yes (flip_criterion_met=true), got STREAK_MET=$STREAK_MET"
    overall_pass=0
elif grep -q "KILLED" "$log2"; then
    echo "FAIL: scenario 2 — no KILLED line should appear for a normal completion, log had: $(cat "$log2")"
    overall_pass=0
else
    echo "PASS: scenario 2 — normal completion correctly captured STREAK_RC=$STREAK_RC (not the deadline sentinel), streak_met=yes, no KILLED line logged"
fi

# ── Scenario 3: normal completion, flip_criterion_met=false → streak_met=no,
# and — this is the crux of the finding — this real negative measurement
# must NOT be misreported as a watchdog kill.
log3="$WORK_DIR/no.log"
vars3="$WORK_DIR/no.vars"
: >"$log3"
FAKE_CLI_HANG=0 FAKE_CLI_FLIP=0 run_streak_block 30 "$log3" "$vars3"
source "$vars3"

echo "--- scenario 3 (normal, flip=0): $(cat "$vars3")"

if [ "$STREAK_RC" = "$VNX_RUN_BOUNDED_DEADLINE" ]; then
    echo "FAIL: scenario 3 — a real streak_met=no measurement was misreported as killed (STREAK_RC=$STREAK_RC)"
    overall_pass=0
elif [ "$STREAK_MET" != "no" ]; then
    echo "FAIL: scenario 3 — expected streak_met=no (flip_criterion_met=false), got STREAK_MET=$STREAK_MET"
    overall_pass=0
elif grep -q "KILLED" "$log3"; then
    echo "FAIL: scenario 3 — no KILLED line should appear for a real (non-killed) streak_met=no, log had: $(cat "$log3")"
    overall_pass=0
else
    echo "PASS: scenario 3 — real streak_met=no measurement captured STREAK_RC=$STREAK_RC (not the deadline sentinel), correctly distinct from scenario 1's kill"
fi

if [ "$overall_pass" -eq 1 ]; then
    echo "PASS: all session_reconcile_autoclose.sh streak PIPESTATUS tests passed"
    exit 0
else
    echo "FAIL: one or more session_reconcile_autoclose.sh streak PIPESTATUS tests failed"
    exit 1
fi
