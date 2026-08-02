#!/usr/bin/env bash
# tests/test_session_reconcile_lifetime.sh — OI-873/OI-877 session-lifetime binding.
#
# session_reconcile_autoclose.sh (SessionStart) detaches a reconcile worker so
# session start never blocks on `gh`.  The worker writes its pid to a per-session
# marker; the SessionEnd hook (session_reconcile_cleanup.sh) reads that marker
# and kills the worker's process tree, so a reconcile can never outlive the
# session that fired it and hold the coordination DB write lock fleet-wide
# (measured 2026-07-31: a planning_cli.py objective reconcile reparented to
# PPID 1 blocked every track write for 15+ minutes).
#
# The marker lives under VNX_STATE_DIR/session_reconcile (resolved via vnx_paths
# in the real runtime — ADR-026 SSOT, the central store).  This test isolates by
# pinning VNX_STATE_DIR to the throwaway root's own .vnx-data/state, never the
# real central store.
#
# Runs the REAL hooks against a throwaway git root with a fake planning_cli.py
# that stays alive for `reconcile` — proving the marker protocol and the kill
# work end-to-end without touching the real coordination DB or `gh`.
#
# Run: bash tests/test_session_reconcile_lifetime.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS="$REPO_ROOT/scripts/hooks"
LIB_DIR="$REPO_ROOT/scripts/lib"

PASS=0
FAIL=0

fail() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }
pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
pid_alive() { kill -0 "$1" 2>/dev/null; }

# ── throwaway roots are cleaned up on exit (kill leftover workers, rm -rf) ──
declare -a _TMP_ROOTS=()
_cleanup() {
    local r m p
    for r in ${_TMP_ROOTS[@]+"${_TMP_ROOTS[@]}"}; do
        for m in "$r"/.vnx-data/state/session_reconcile/*.pid; do
            [ -f "$m" ] || continue
            p="$(cat "$m" 2>/dev/null | tr -d '[:space:]' || true)"
            if [[ "$p" =~ ^[0-9]+$ ]] && pid_alive "$p"; then
                pkill -KILL -P "$p" 2>/dev/null || true
                pkill -KILL -f "$r/scripts/planning_cli.py" 2>/dev/null || true
                kill -KILL "$p" 2>/dev/null || true
            fi
        done
        rm -rf "$r" 2>/dev/null || true
    done
}
trap _cleanup EXIT

setup_root() {
    local tmp
    tmp="$(mktemp -d /tmp/vnx-sesslifetime.XXXXXX)"
    git -C "$tmp" init -q
    mkdir -p "$tmp/scripts/lib" \
             "$tmp/.vnx-data/logs" \
             "$tmp/.vnx-data/locks" \
             "$tmp/.vnx-data/state"
    ln -s "$LIB_DIR/vnx_flock_lock.sh" "$tmp/scripts/lib/vnx_flock_lock.sh"
    ln -s "$LIB_DIR/vnx_run_bounded.sh" "$tmp/scripts/lib/vnx_run_bounded.sh"
    ln -s "$LIB_DIR/vnx_paths.sh" "$tmp/scripts/lib/vnx_paths.sh"
    cat > "$tmp/scripts/planning_cli.py" <<'PYEOF'
import sys, time
args = sys.argv[1:]
if "reconcile-streak" in args:
    print('{"flip_criterion_met": false}')
    sys.exit(0)
if "reconcile" in args:
    time.sleep(60)   # stay alive so the SessionEnd kill is observable
sys.exit(0)
PYEOF
    chmod +x "$tmp/scripts/planning_cli.py"
    echo "$tmp"
}

# NOTE: a function called inside `$(...)` runs in a subshell, so it can never
# mutate the main shell's _TMP_ROOTS.  Callers must append the returned path
# themselves.  ${arr[@]+"${arr[@]}"} keeps `for` safe under `set -u` for an
# empty array.

# Marker dir the hooks resolve to in this test (VNX_STATE_DIR pinned per-root).
marker_for() {
    printf '%s/.vnx-data/state/session_reconcile' "$1"
}

wait_for_marker() {
    local marker="$1" tries=0
    while [ ! -f "$marker" ]; do
        sleep 0.1
        tries=$((tries + 1))
        [ "$tries" -lt 50 ] || return 1
    done
    return 0
}

# Poll until the reconcile flock is free (bounded) — the SessionEnd kill frees
# it immediately in the common case; the watchdog's `sleep 1` can straggle for
# up to a second, which the production DB busy_timeout (10s) would absorb
# anyway.  Asserting freedom within a few seconds is the honest contract.
flock_free_within() {
    local lock="$1" tries=0
    while [ "$tries" -lt 40 ]; do
        if ! source "$LIB_DIR/vnx_flock_lock.sh" 2>/dev/null; then
            sleep 0.1; tries=$((tries + 1)); continue
        fi
        if vnx_flock_acquire "$lock" "test" "python3" 2>/dev/null; then
            vnx_flock_release
            return 0
        fi
        sleep 0.1
        tries=$((tries + 1))
    done
    return 1
}

# OI-916: VNX_STATE_DIR / VNX_PROJECT_ID are consumed by the hook CHILD process
# (the detached reconcile worker), so they must be EXPORTED — a bare
# `VAR=value` assignment inside a script is not exported to child processes.
# The hooks fall back to vnx_paths resolution when VNX_STATE_DIR is absent,
# which would resolve the REAL central store and break this test's isolation
# (and, worse, scatter throwaway markers into the production store).
# shellcheck disable=SC2034  # exported for the hook child process
run_session_start() {
    local root="$1" sid="$2"
    ( cd "$root" \
        && export VNX_STATE_DIR="$root/.vnx-data/state" VNX_PROJECT_ID="" \
        && printf '{"session_id":"%s","hook_event_name":"SessionStart"}' "$sid" \
        | bash "$HOOKS/session_reconcile_autoclose.sh" )
}

run_session_end() {
    local root="$1" sid="$2"
    ( cd "$root" \
        && export VNX_STATE_DIR="$root/.vnx-data/state" VNX_PROJECT_ID="" \
        && printf '{"session_id":"%s","hook_event_name":"SessionEnd"}' "$sid" \
        | bash "$HOOKS/session_reconcile_cleanup.sh" )
}

# ── 1. SessionEnd kills the session's detached reconcile worker tree ────────
echo "[1] SessionEnd kills the session's detached reconcile worker tree"
ROOT_A="$(setup_root)"
_TMP_ROOTS+=("$ROOT_A")
SID_A="800e3edd-6432-41fd-a747-372411fe7f47"
run_session_start "$ROOT_A" "$SID_A"
MARKER_A="$(marker_for "$ROOT_A")/$SID_A.pid"
if ! wait_for_marker "$MARKER_A"; then
    fail "SessionStart wrote no per-session marker ($MARKER_A)"
else
    pass "SessionStart wrote per-session marker"
fi

WORKER_PID="$(cat "$MARKER_A" 2>/dev/null | tr -d '[:space:]')"
if ! pid_alive "$WORKER_PID"; then
    fail "detached reconcile worker (pid=$WORKER_PID) not alive after SessionStart"
else
    pass "detached reconcile worker alive after SessionStart"
fi

# The fake reconcile child (planning_cli.py) should be running as a descendant.
RECONCILE_PID="$(pgrep -P "$WORKER_PID" 2>/dev/null | head -1 || true)"
if [ -z "$RECONCILE_PID" ] || ! pid_alive "$RECONCILE_PID"; then
    fail "no running reconcile child under the worker"
else
    pass "reconcile child (pid=$RECONCILE_PID) running under the worker"
fi

# The worker holds the flock — prove it (a second acquire must fail).
if source "$LIB_DIR/vnx_flock_lock.sh"; then
    if vnx_flock_acquire "$ROOT_A/.vnx-data/locks/session_reconcile_autoclose.lock" "test" "python3"; then
        vnx_flock_release
        fail "worker does NOT hold the reconcile flock (acquire succeeded)"
    else
        pass "worker holds the reconcile flock while alive"
    fi
fi

run_session_end "$ROOT_A" "$SID_A"

if pid_alive "$WORKER_PID"; then
    fail "worker still alive after SessionEnd (pid=$WORKER_PID)"
else
    pass "worker killed at SessionEnd"
fi
if pid_alive "$RECONCILE_PID"; then
    fail "reconcile child still alive after SessionEnd"
else
    pass "reconcile child killed at SessionEnd"
fi
if [ -f "$MARKER_A" ]; then
    fail "marker not removed by SessionEnd"
else
    pass "marker removed by SessionEnd"
fi
if flock_free_within "$ROOT_A/.vnx-data/locks/session_reconcile_autoclose.lock"; then
    pass "flock released after SessionEnd kill (DB write lock freed)"
else
    fail "flock still held after SessionEnd kill"
fi

# ── 2. A DIFFERENT session's SessionEnd touches nothing ─────────────────────
echo "[2] a different session's SessionEnd touches nothing"
ROOT_B="$(setup_root)"
_TMP_ROOTS+=("$ROOT_B")
SID_B="aaaa-bbbb-cccc-dddd"
run_session_start "$ROOT_B" "$SID_B"
MARKER_B="$(marker_for "$ROOT_B")/$SID_B.pid"
if ! wait_for_marker "$MARKER_B"; then
    fail "SessionStart B wrote no marker"
else
    pass "SessionStart B wrote marker"
fi
WORKER_B="$(cat "$MARKER_B" 2>/dev/null | tr -d '[:space:]')"

run_session_end "$ROOT_B" "other-session-id-xyz"
if ! pid_alive "$WORKER_B"; then
    fail "worker B killed by a different session's SessionEnd"
else
    pass "worker B survives a different session's SessionEnd"
fi

run_session_end "$ROOT_B" "$SID_B"
if pid_alive "$WORKER_B"; then
    fail "worker B not killed by its own SessionEnd"
else
    pass "worker B killed by its own SessionEnd"
fi

# ── 3. No marker / stale marker → no-op, never touches anything ─────────────
echo "[3] no marker is a clean no-op"
ROOT_C="$(setup_root)"
_TMP_ROOTS+=("$ROOT_C")
run_session_end "$ROOT_C" "no-such-session"
if source "$LIB_DIR/vnx_flock_lock.sh"; then
    if vnx_flock_acquire "$ROOT_C/.vnx-data/locks/session_reconcile_autoclose.lock" "test" "python3"; then
        vnx_flock_release
        pass "SessionEnd with no marker leaves the world untouched"
    else
        fail "unexpected lock state after no-op SessionEnd"
    fi
fi

echo ""
echo "== session reconcile lifetime: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
