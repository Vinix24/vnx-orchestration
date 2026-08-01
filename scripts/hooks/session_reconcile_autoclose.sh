#!/usr/bin/env bash
# VNX SessionStart auto-close tick
#
# Fires on Claude Code's SessionStart hook for the interactive T0/operator
# session. Runs `objective reconcile` so tracks whose PRs merged since the last
# session close automatically, keeping the horizon in sync with git reality
# without a long-running supervisor.
#
# WHY a session hook and not a cron/launchd agent: `gh` auth is keyring-backed
# on this fleet (no GH_TOKEN), so a headless launchd context cannot authenticate
# the `gh pr view` calls reconcile depends on — every PR would come back
# `unverified` and nothing would close. The interactive session HAS keychain
# access, so this is the reliable context. Session-start is also the moment a
# synced horizon matters most (kickoff).
#
# SAFETY — auto-close is ON BY DEFAULT (operator directive 2026-07-10). reconcile
# only ever closes tracks whose linked PRs are verified MERGED on GitHub (provenance
# chain), so applying by default keeps the horizon in sync with git reality without
# waiting for the trust streak. Opt out with VNX_AUTO_CLOSE=0 → advisory CHECK (zero
# writes). The reconcile-streak is still computed + logged for observability, but no
# longer gates the flip. reconcile's own two-stage close (CONFIRMED → stale_candidate
# → closed) remains the conservative safeguard against a single mis-verify.
#
# Scoped to the interactive session: fires ONLY when VNX_DISPATCH_ID is UNSET.
# A tmux-spawn worker (VNX_DISPATCH_ID set) drains stdin and exits 0 — no-op.
#
# Detached (nohup + background) so it never blocks session start. Always exit 0.
#
# SINGLETON GUARD (OI-851, measured 2026-07-30): five concurrent instances of the
# detached worker below were found running at once, the longest alive 2h12,
# holding the project state DB and corrupting concurrent measurement — every new
# SessionStart forked another worker with nothing to stop overlap. The lock MUST
# be acquired INSIDE the detached subshell below, not in this parent shell: the
# parent exits immediately (see `exit 0` at the bottom), so a parent-held lock —
# or a parent `trap ... EXIT` — would release the instant the parent exits, while
# the worker is still running. That is the exact bug being fixed here.
#
# The lock is a kernel flock(2) mutex (scripts/lib/vnx_flock_lock.sh), not a
# PID file: an earlier mkdir-based design (mkdir the lock, THEN separately
# echo the pid into it) had a real, measured race in a concurrent invocation
# reading the pid file in the gap between "mkdir succeeded" and "pid
# written," seeing it empty, wrongly concluding the lock was stale, and
# stealing it out from under its rightful holder — reproduced locally as 2-4
# winners out of 8-10 concurrent runs. A follow-up PID-file + noclobber-write
# design (existence and content bound in one syscall) fixed that specific
# window, but its STALE-lock RECLAIM path reintroduced the identical race
# class in a different shape: reclaiming required an `rm` followed by a
# SEPARATE noclobber write, and two reclaimers could each pass the
# `kill -0`-dead-holder check in the gap between another reclaimer's `rm` and
# its own write, each then seeing their own write to the now-briefly-absent
# path succeed (PR #1247 finding 1). flock(2) removes the category outright:
# there is no "is the recorded holder dead" check to race at all — the
# kernel atomically releases the lock the instant a dead holder's last fd
# closes, so the very next non-blocking flock attempt simply succeeds.

# Capture the hook payload (best-effort) so this session's id is known — the
# detached reconcile worker below is bound to the session's lifetime, and the
# SessionEnd hook (session_reconcile_cleanup.sh) uses that id to kill it when
# the session ends (OI-873/OI-877 second defense line: a reconcile must never
# outlive its session holding the coordination DB write lock).  `cat` returns
# at EOF, so capturing never blocks the caller.  Parsing happens inside the
# subshell with the resolved interpreter (jq is not guaranteed on this fleet).
STDIN_JSON="$(cat 2>/dev/null)" || STDIN_JSON=""

# ── Guard: skip tmux-spawn workers; only the interactive session ticks ───────
if [ -n "${VNX_DISPATCH_ID:-}" ]; then
    exit 0
fi

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
CLI="$ROOT/scripts/planning_cli.py"
LOG_DIR="$ROOT/.vnx-data/logs"
LOG="$LOG_DIR/objective_reconcile.log"
LOCK_FILE="$ROOT/.vnx-data/locks/session_reconcile_autoclose.lock"
LOCK_LIB="$ROOT/scripts/lib/vnx_flock_lock.sh"
BOUND_LIB="$ROOT/scripts/lib/vnx_run_bounded.sh"

# No CLI, or the shared libs this hook depends on are missing — nothing to
# do (never risk running unlocked/unbounded because a lib failed to install).
[ -f "$CLI" ] || exit 0
[ -f "$LOCK_LIB" ] || exit 0
[ -f "$BOUND_LIB" ] || exit 0
mkdir -p "$LOG_DIR" 2>/dev/null || true
mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true

# Run the whole tick detached so session start never waits on gh network calls.
(
    # ── This subshell's real OS pid ──────────────────────────────────────────
    # `$$` would NOT work here: in a `( ... ) &` background subshell, bash keeps
    # `$$` pointing at the PARENT shell (already gone — it hit `exit 0` at the
    # bottom of this file), so a staleness check against `$$` would test a pid
    # that no longer exists even while this worker is still alive. `$BASHPID`
    # (bash 4+) would give the right answer, but this machine's `env bash`
    # resolves to macOS's stock bash 3.2.57, where BASHPID is undefined.
    #
    # `exec sh -c 'echo $PPID'` is the portable equivalent — but the `exec`
    # matters: without it, bash may fork an extra intermediate process to set
    # up the command substitution (observed here whenever a redirection like
    # `2>/dev/null` is attached to the substituted command), and `sh`'s PPID
    # then names that transient helper — already dead by the time anyone
    # checks it — not this subshell. `exec` tells bash to replace the
    # substitution's process with `sh` outright (POSIX-guaranteed execve, not
    # an optimization bash may or may not apply), so `sh`'s PPID is always
    # exactly this subshell's own pid. Verified against $! across 20+
    # concurrent runs, nested exactly as below, before relying on it here.
    SELF_PID="$(exec sh -c 'echo $PPID' 2>/dev/null)"

    source "$LOCK_LIB"
    source "$BOUND_LIB"

    # ── Interpreter resolution (OI-852, measured 2026-07-30) ─────────────────
    # /opt/homebrew/bin/python3 was relinked to a dependency-less 3.14 at 09:32
    # today. An interactive shell alias masks this; this detached background
    # context has no alias, so bare `python3` here resolves via PATH straight
    # to the broken interpreter. Resolve once, use it at every call site below
    # (including the flock helper, which needs a working fcntl module).
    if [ -x "$ROOT/.venv/bin/python" ]; then
        PY="$ROOT/.venv/bin/python"
    elif [ -x "/opt/homebrew/opt/python@3.12/bin/python3.12" ]; then
        PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
    else
        PY="python3"
    fi

    # ── Bound the runtime (OI-851/852, measured 2026-07-30): the 2h12 hang
    # held the lock the whole time because nothing bounded the reconcile call.
    # `timeout`/`gtimeout` are both ABSENT on this machine (verified) — use
    # them only when present, fall back to a manual watchdog otherwise (see
    # scripts/lib/vnx_run_bounded.sh). 900s comfortably exceeds a normal
    # reconcile and kills a multi-hour hang.
    DEADLINE_SECS=900
    TIMEOUT_BIN=""
    if command -v timeout >/dev/null 2>&1; then
        TIMEOUT_BIN="timeout"
    elif command -v gtimeout >/dev/null 2>&1; then
        TIMEOUT_BIN="gtimeout"
    fi

    # ── Session lifetime binding (OI-873/OI-877, second defense line) ────────
    # Record this worker's own pid in a per-session marker so the SessionEnd
    # hook can kill EXACTLY this worker's process tree when the session that
    # fired it ends.  The kill releases this worker's flock (FD 200) with it,
    # so a reconcile can never outlive its session and block fleet-wide DB
    # writes.  The 900s deadline and the flock below stay — this is a third
    # bound, not a replacement.
    #
    # Marker location: VNX_STATE_DIR via vnx_paths (ADR-026 SSOT — the central
    # store, never a repo-local .vnx-data/state).  The harness exports
    # VNX_STATE_DIR in every real session; fall back to the canonical resolver
    # when it is absent.  No resolver and no env → no marker (fail-safe): the
    # deadline + flock singleton still bound us, and SessionEnd has nothing to
    # clean up.
    SESSION_ID=""
    if [ -n "$STDIN_JSON" ]; then
        SESSION_ID="$(printf '%s' "$STDIN_JSON" | "$PY" -c 'import sys,json
try:
    d = json.load(sys.stdin)
    print(d.get("session_id") or "")
except Exception:
    print("")' 2>/dev/null)"
    fi
    STATE_DIR="${VNX_STATE_DIR:-}"
    if [ -z "$STATE_DIR" ] && [ -f "$ROOT/scripts/lib/vnx_paths.sh" ]; then
        # shellcheck source=/dev/null
        source "$ROOT/scripts/lib/vnx_paths.sh"
        STATE_DIR="${VNX_STATE_DIR:-}"
    fi
    MARKER=""
    if [ -n "$SESSION_ID" ] && [ -n "$STATE_DIR" ]; then
        SAFE_SID="$(printf '%s' "$SESSION_ID" | tr -c 'A-Za-z0-9_-' '-')"
        SESSION_RECONCILE_DIR="$STATE_DIR/session_reconcile"
        mkdir -p "$SESSION_RECONCILE_DIR" 2>/dev/null || true
        MARKER="$SESSION_RECONCILE_DIR/$SAFE_SID.pid"
        printf '%s\n' "$SELF_PID" >"$MARKER" 2>/dev/null || true
        # Remove the marker on ANY exit path of this worker — normal
        # completion, the 900s deadline, or a lock-busy early exit — so the
        # SessionEnd hook finds nothing to kill for a finished run.
        trap 'rm -f "$MARKER"' EXIT
    fi

    STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if ! vnx_flock_acquire "$LOCK_FILE" "$SELF_PID" "$PY"; then
        _busy_pid="$(cat "$LOCK_FILE" 2>/dev/null || echo '?')"
        echo "[$STAMP] session-reconcile: lock held by pid=$_busy_pid — skipping (no work done)" >>"$LOG" 2>&1
        exit 0
    fi
    # FD 200 (opened inside vnx_flock_acquire) IS the lock: the kernel
    # releases it the instant this subshell's last reference to that fd
    # closes, on ANY exit path (normal completion, the watchdog above killing
    # a hung reconcile, or a crash) — no trap required for release. Closed
    # explicitly via vnx_flock_release at the bottom once all work is done.

    # Resolve project_id the same way the CLI does (git remote / .vnx-project-id),
    # falling back to the reconcile default. Empty is fine — the CLI resolves it.
    PID="${VNX_PROJECT_ID:-}"
    PID_ARGS=()
    [ -n "$PID" ] && PID_ARGS=(--project-id "$PID")

    # Auto-close ON BY DEFAULT (operator directive 2026-07-10): apply unless the operator
    # opts out with VNX_AUTO_CLOSE=0. reconcile only closes tracks whose PRs are verified
    # MERGED, so this keeps the horizon in sync with git without waiting for the trust streak.
    if [ "${VNX_AUTO_CLOSE:-1}" = "0" ]; then
        MODE="check"
    else
        MODE="apply"
    fi

    # Streak is still computed + logged for observability (no longer gates the flip).
    # PR #1247 fix-forward finding (codex gate, reproduced against this exact
    # helper: streak_met=no captured_PIPESTATUS0=0 expected_deadline=124): bash
    # resets PIPESTATUS after EVERY command, including plain assignments. The
    # previous shape ran the pipeline as the `if` condition itself, then read
    # PIPESTATUS[0] only after the then/else branch had already run an
    # assignment — by that point PIPESTATUS described the assignment (always
    # 0), never the pipeline. A killed streak call was silently indistinguishable
    # from streak_met=no.
    #
    # Fix: run the pipeline as its own statement, then capture the WHOLE
    # PIPESTATUS array on the very next line — before any other command can
    # touch it, including the array assignment's own right-hand-side
    # expansion, which reads PIPESTATUS as it stood immediately after the
    # pipeline. Element 0 is vnx_run_bounded's own status (deadline sentinel
    # or real exit code); element 1 is the trailing python filter's exit code
    # (flip_criterion_met), which is what the old `if pipeline; then` branch
    # actually keyed streak_met on.
    STREAK_MET="?"
    vnx_run_bounded "$TIMEOUT_BIN" "$DEADLINE_SECS" "$SELF_PID" \
        "$PY" "$CLI" objective reconcile-streak "${PID_ARGS[@]}" --json 2>/dev/null \
        | "$PY" -c 'import sys,json;
d=json.load(sys.stdin);
sys.exit(0 if d.get("flip_criterion_met") else 1)' 2>/dev/null
    STREAK_PIPE_STATUS=("${PIPESTATUS[@]}")
    STREAK_RC="${STREAK_PIPE_STATUS[0]}"
    STREAK_FILTER_RC="${STREAK_PIPE_STATUS[1]}"
    if [ "$STREAK_FILTER_RC" -eq 0 ]; then
        STREAK_MET="yes"
    else
        STREAK_MET="no"
    fi
    if [ "$STREAK_RC" -eq "$VNX_RUN_BOUNDED_DEADLINE" ]; then
        echo "[$STAMP] session-reconcile: reconcile-streak KILLED at ${DEADLINE_SECS}s deadline — streak measurement incomplete (logged as streak_met=$STREAK_MET, not a real observation)" >>"$LOG" 2>&1
    fi

    echo "[$STAMP] session-reconcile tick: mode=$MODE streak_met=$STREAK_MET interpreter=$PY pid=$SELF_PID" >>"$LOG" 2>&1

    if [ "$MODE" = "apply" ]; then
        vnx_run_bounded "$TIMEOUT_BIN" "$DEADLINE_SECS" "$SELF_PID" \
            "$PY" "$CLI" objective reconcile "${PID_ARGS[@]}" --apply --repo-root "$ROOT" >>"$LOG" 2>&1
    else
        vnx_run_bounded "$TIMEOUT_BIN" "$DEADLINE_SECS" "$SELF_PID" \
            "$PY" "$CLI" objective reconcile "${PID_ARGS[@]}" --repo-root "$ROOT" >>"$LOG" 2>&1
    fi
    RECONCILE_RC=$?

    # The fact this hook exists (OI-851/852) is that "killed because it hung"
    # and "ran and failed" are different governance facts and must not land
    # the same way in the log — see vnx_run_bounded.sh's header for why 124
    # unambiguously means "the deadline fired," never "reconcile chose 124."
    if [ "$RECONCILE_RC" -eq "$VNX_RUN_BOUNDED_DEADLINE" ]; then
        echo "[$STAMP] session-reconcile: reconcile KILLED at ${DEADLINE_SECS}s deadline (mode=$MODE) — was still running, not a normal failure" >>"$LOG" 2>&1
    elif [ "$RECONCILE_RC" -ne 0 ]; then
        echo "[$STAMP] session-reconcile: reconcile FAILED (mode=$MODE) exit=$RECONCILE_RC" >>"$LOG" 2>&1
    fi

    vnx_flock_release
) </dev/null >/dev/null 2>&1 &

exit 0
