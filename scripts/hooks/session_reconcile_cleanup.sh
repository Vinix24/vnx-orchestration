#!/usr/bin/env bash
# VNX SessionEnd reconcile cleanup — kill the session's detached reconcile worker.
#
# session_reconcile_autoclose.sh (SessionStart) detaches a reconcile worker so
# session start never blocks on `gh` network calls.  That worker is bound to the
# session's lifetime: it writes its own pid to a per-session marker under
# $VNX_STATE_DIR/session_reconcile/<session_id>.pid (resolved via vnx_paths —
# ADR-026 SSOT, the central store, never a repo-local .vnx-data/state).  This
# SessionEnd hook finds that marker and kills the worker's whole process tree.
#
# WHY (OI-873 / OI-877, second defense line — measured 2026-07-31): a
# `planning_cli.py objective reconcile` spawned by a main-checkout session
# survived the session's end (reparented to PPID 1) and held the coordination
# DB write lock for 15+ minutes, blocking every fleet-wide track write.  The
# worktree-scoped lsof scan (#1260 worktree_process_cleanup.py) and the
# spawn-time PGID registry cannot reach it: it is the interactive session's OWN
# process group, which both mechanisms deliberately never signal.  Killing it
# here, bound to the session it came from, closes that gap.
#
# The kill is scoped HARD to the recorded process tree: only the recorded
# worker pid and its descendants are signalled, never the session's own process
# group (the group this hook itself runs in).  The recorded command is verified
# before any signal, so a recycled pid is never touched.  Best-effort, never
# blocks, always exits 0.

# ── Guard: tmux-spawn workers never start a reconcile worker (the SessionStart
# hook is a no-op for them), so there is nothing to clean.  Interactive
# sessions only.
if [ -n "${VNX_DISPATCH_ID:-}" ]; then
    exit 0
fi

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

PY=""
if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
elif [ -x "/opt/homebrew/opt/python@3.12/bin/python3.12" ]; then
    PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
else
    PY="python3"
fi

# ── Session id from the hook payload (best-effort) ───────────────────────────
STDIN_JSON="$(cat 2>/dev/null)" || STDIN_JSON=""
SESSION_ID=""
if [ -n "$STDIN_JSON" ]; then
    SESSION_ID="$(printf '%s' "$STDIN_JSON" | "$PY" -c 'import sys,json
try:
    d = json.load(sys.stdin)
    print(d.get("session_id") or "")
except Exception:
    print("")' 2>/dev/null)"
fi
[ -n "$SESSION_ID" ] || exit 0

SAFE_SID="$(printf '%s' "$SESSION_ID" | tr -c 'A-Za-z0-9_-' '-')"

# ── Marker location: VNX_STATE_DIR via vnx_paths (ADR-026 SSOT) — the SAME
# resolution the SessionStart hook used to write the marker, so both hooks can
# never disagree about where it lives.  Never fall back to a repo-local
# .vnx-data/state — that split-brain is the defect this protocol exists to
# avoid.  No resolver and no env → nothing to clean.
STATE_DIR="${VNX_STATE_DIR:-}"
if [ -z "$STATE_DIR" ] && [ -f "$ROOT/scripts/lib/vnx_paths.sh" ]; then
    # shellcheck source=/dev/null
    source "$ROOT/scripts/lib/vnx_paths.sh"
    STATE_DIR="${VNX_STATE_DIR:-}"
fi
[ -n "$STATE_DIR" ] || exit 0

MARKER="$STATE_DIR/session_reconcile/$SAFE_SID.pid"
LOCK_FILE="$ROOT/.vnx-data/locks/session_reconcile_autoclose.lock"
[ -f "$MARKER" ] || exit 0

RECONCILE_PID="$(cat "$MARKER" 2>/dev/null | tr -d '[:space:]')"
if [ -z "$RECONCILE_PID" ] || ! [[ "$RECONCILE_PID" =~ ^[0-9]+$ ]]; then
    rm -f "$MARKER" 2>/dev/null || true
    exit 0
fi

# ── Identity guard: only signal a live process that is still our worker.  A
# dead pid, or a recycled pid now running something else, is never touched.
if ! kill -0 "$RECONCILE_PID" 2>/dev/null; then
    rm -f "$MARKER" 2>/dev/null || true
    exit 0
fi
WORKER_CMD="$(ps -p "$RECONCILE_PID" -o command= 2>/dev/null || true)"
case "$WORKER_CMD" in
    *session_reconcile_autoclose*)
        ;;
    *)
        # Recycled pid, or the worker already exited on its own — nothing of
        # ours to kill.  Drop the stale marker and move on.
        rm -f "$MARKER" 2>/dev/null || true
        exit 0
        ;;
esac

# ── Kill the worker's process tree (never the session's own group) ───────────
# The worker root, the reconcile, the watchdog AND every descendant of the
# watchdog inherit the flock fd (200) from vnx_flock_acquire, so the flock is
# only released once every last holder dies.  The recorded root's PGID is the
# SESSION's own group — captured up front so the final sweep below can scope
# by it without accidentally touching another session's worker.
SESSION_PGID="$(ps -o pgid= -p "$RECONCILE_PID" 2>/dev/null | tr -d ' ' || true)"

_collect_descendants() {
    local parent="$1"
    local child
    for child in $(pgrep -P "$parent" 2>/dev/null || true); do
        _collect_descendants "$child"
        DESCENDANTS+=("$child")
    done
}

DESCENDANTS=()
_collect_descendants "$RECONCILE_PID"

# Phase 1: SIGTERM descendants (deepest first from the post-order walk), then
# the worker root — mirrors the SIGTERM-then-SIGKILL grace period used by
# worktree_process_cleanup / dispatch_process_registry.
for pid in "${DESCENDANTS[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
done
kill -TERM "$RECONCILE_PID" 2>/dev/null || true
sleep 0.3

# Phase 2: SIGKILL any survivor of the recorded tree.
for pid in "${DESCENDANTS[@]}"; do
    kill -KILL "$pid" 2>/dev/null || true
done
kill -KILL "$RECONCILE_PID" 2>/dev/null || true

# Phase 3 (race closer): the watchdog's `sleep 1` can be spawned between the
# descendant collection and the kill above, escaping the tree scan with an
# inherited flock fd (measured 2026-08-01).  Sweep the lock file and SIGKILL
# any remaining holder — the ONLY way a process holds this exclusive flock is
# as part of the worker tree, and scoping by the session's PGID guarantees a
# concurrent session's worker is never touched.  A leftover `sleep` would
# self-release within a second (and the DB busy_timeout would absorb it), but
# freeing it now is the point of this hook.
if [ -n "$SESSION_PGID" ] && command -v lsof >/dev/null 2>&1; then
    for holder in $(lsof -t "$LOCK_FILE" 2>/dev/null || true); do
        holder_pgid="$(ps -o pgid= -p "$holder" 2>/dev/null | tr -d ' ' || true)"
        if [ -n "$holder_pgid" ] && [ "$holder_pgid" = "$SESSION_PGID" ]; then
            kill -KILL "$holder" 2>/dev/null || true
        fi
    done
fi

rm -f "$MARKER" 2>/dev/null || true
exit 0
