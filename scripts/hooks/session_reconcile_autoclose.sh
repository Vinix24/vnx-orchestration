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
# The lock is a single FILE, acquired via `set -C` (noclobber): the `>` redirect
# fails with EEXIST if the file already exists, so existence and content (our
# pid) land in the SAME syscall. That matters here specifically because an
# earlier mkdir-based design (mkdir the lock, THEN separately echo the pid into
# it) had a real, measured race: a concurrent invocation could read the pid file
# in the gap between "mkdir succeeded" and "pid written", see it empty, wrongly
# conclude the lock was stale, and steal it out from under its rightful holder
# — reproduced locally as 2-4 winners out of 8-10 concurrent runs. Binding
# existence to content in one write removes that window entirely.

# Drain the hook's stdin JSON so the caller never blocks.
cat >/dev/null 2>&1 || true

# ── Guard: skip tmux-spawn workers; only the interactive session ticks ───────
if [ -n "${VNX_DISPATCH_ID:-}" ]; then
    exit 0
fi

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
CLI="$ROOT/scripts/planning_cli.py"
LOG_DIR="$ROOT/.vnx-data/logs"
LOG="$LOG_DIR/objective_reconcile.log"
LOCK_FILE="$ROOT/.vnx-data/locks/session_reconcile_autoclose.lock"

# No CLI, nothing to do.
[ -f "$CLI" ] || exit 0
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

    _acquire_lock() {
        if ( set -C; echo "$SELF_PID" >"$LOCK_FILE" ) 2>/dev/null; then
            return 0
        fi
        local _held_pid=""
        _held_pid="$(cat "$LOCK_FILE" 2>/dev/null || true)"
        if [ -n "$_held_pid" ] && kill -0 "$_held_pid" 2>/dev/null; then
            return 1
        fi
        # Stale: the recorded holder is dead (or the file never got written
        # before a crash). Reclaim. If a concurrent reclaimer wins the race
        # between this rm and the mkdir-equivalent noclobber write below, our
        # write simply fails (EEXIST) and we back off cleanly — noclobber's
        # atomicity is what makes this safe, not the rm itself.
        rm -f "$LOCK_FILE" 2>/dev/null
        if ( set -C; echo "$SELF_PID" >"$LOCK_FILE" ) 2>/dev/null; then
            return 0
        fi
        return 1
    }

    STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if ! _acquire_lock; then
        _busy_pid="$(cat "$LOCK_FILE" 2>/dev/null || echo '?')"
        echo "[$STAMP] session-reconcile: lock held by pid=$_busy_pid — skipping (no work done)" >>"$LOG" 2>&1
        exit 0
    fi
    # Released on any exit path of this subshell (normal completion, a bounded
    # timeout below killing the reconcile call, or an error) — never on the
    # parent's exit, since this trap belongs to the subshell's own process.
    trap 'rm -f "$LOCK_FILE" 2>/dev/null' EXIT

    # ── Bound the runtime (OI-851): the 2h12 hang held the lock the whole
    # time because nothing bounded the reconcile call. `timeout`/`gtimeout` are
    # both ABSENT on this machine (verified) — use them only when present, run
    # direct otherwise. 900s comfortably exceeds a normal reconcile and kills a
    # multi-hour hang.
    TIMEOUT_CMD=""
    if command -v timeout >/dev/null 2>&1; then
        TIMEOUT_CMD="timeout 900"
    elif command -v gtimeout >/dev/null 2>&1; then
        TIMEOUT_CMD="gtimeout 900"
    fi

    # ── Interpreter resolution (OI-852, measured 2026-07-30) ─────────────────
    # /opt/homebrew/bin/python3 was relinked to a dependency-less 3.14 at 09:32
    # today. An interactive shell alias masks this; this detached background
    # context has no alias, so bare `python3` here resolves via PATH straight
    # to the broken interpreter. Resolve once, use it at every call site below.
    if [ -x "$ROOT/.venv/bin/python" ]; then
        PY="$ROOT/.venv/bin/python"
    elif [ -x "/opt/homebrew/opt/python@3.12/bin/python3.12" ]; then
        PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
    else
        PY="python3"
    fi

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
    STREAK_MET="?"
    if $TIMEOUT_CMD "$PY" "$CLI" objective reconcile-streak "${PID_ARGS[@]}" --json 2>/dev/null \
        | "$PY" -c 'import sys,json;
d=json.load(sys.stdin);
sys.exit(0 if d.get("flip_criterion_met") else 1)' 2>/dev/null; then
        STREAK_MET="yes"
    else
        STREAK_MET="no"
    fi

    echo "[$STAMP] session-reconcile tick: mode=$MODE streak_met=$STREAK_MET interpreter=$PY pid=$SELF_PID" >>"$LOG" 2>&1

    if [ "$MODE" = "apply" ]; then
        $TIMEOUT_CMD "$PY" "$CLI" objective reconcile "${PID_ARGS[@]}" --apply --repo-root "$ROOT" >>"$LOG" 2>&1
    else
        $TIMEOUT_CMD "$PY" "$CLI" objective reconcile "${PID_ARGS[@]}" --repo-root "$ROOT" >>"$LOG" 2>&1
    fi
) </dev/null >/dev/null 2>&1 &

exit 0
