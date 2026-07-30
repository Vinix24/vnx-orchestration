#!/usr/bin/env bash
# vnx_run_bounded.sh — bound a command's runtime when timeout/gtimeout aren't
# on PATH. Measured 2026-07-30: neither timeout(1) nor gtimeout(1) exists on
# this fleet's macOS boxes, so a bare `$TIMEOUT_CMD` that's only ever set
# when one of those binaries is found (and left empty otherwise) never
# actually bounds anything there — the reconcile call runs unbounded, which
# is exactly the class of bug this fixes (PR #1247 finding 2: a single
# process alive 2h12 holding the project state DB, nothing bounding it).
#
# vnx_run_bounded <timeout_bin_or_empty> <deadline_secs> <caller_pid> <cmd...>
#   <timeout_bin_or_empty>: "timeout", "gtimeout", or "" to force the manual
#       watchdog fallback below regardless of what's actually on PATH — lets
#       tests exercise the fallback deterministically on any machine,
#       including ones that DO have timeout/gtimeout.
#   <deadline_secs>: seconds before the command is killed if still running.
#   <caller_pid>: the REAL pid of the calling shell/subshell. Must be passed
#       explicitly, not read via $$ inside this function: when this is
#       sourced into a `( ... ) &` background subshell (as in
#       session_reconcile_autoclose.sh), $$ resolves to the ALREADY-EXITED
#       parent shell's pid, not this subshell's own pid — see that script's
#       own SELF_PID comment for the measured reason. The caller must
#       resolve its own pid correctly (e.g. via `exec sh -c 'echo $PPID'`)
#       and pass that in here.
#
# Fast path: when a real timeout binary is available, use it directly —
# unchanged behavior for any machine that has one.
#
# Fallback (neither binary present): runs <cmd...> in the background and
# starts a second background watchdog that POLLS in short increments rather
# than sleeping once for the whole deadline, so it can notice either of two
# conditions and back off WITHOUT ever sending a signal:
#   - the worker already exited — nothing left to kill.
#   - its own caller (<caller_pid>) is gone — an orphaned watchdog whose
#     caller no longer exists to observe the result must not act.
# Backing off on both is what keeps the watchdog from firing a kill against
# a pid the OS has since recycled for an unrelated process. The main flow
# also always kills+reaps the watchdog itself immediately after the worker
# finishes (whichever finishes first), so the watchdog cannot outlive the
# worker either.
vnx_run_bounded() {
    local timeout_bin="$1"; shift
    local deadline_secs="$1"; shift
    local caller_pid="$1"; shift

    if [ -n "$timeout_bin" ]; then
        "$timeout_bin" "$deadline_secs" "$@"
        return $?
    fi

    "$@" &
    local work_pid=$!

    (
        local elapsed=0
        while [ "$elapsed" -lt "$deadline_secs" ]; do
            kill -0 "$work_pid" 2>/dev/null || exit 0
            kill -0 "$caller_pid" 2>/dev/null || exit 0
            sleep 1
            elapsed=$((elapsed + 1))
        done
        kill -0 "$work_pid" 2>/dev/null || exit 0
        kill -TERM "$work_pid" 2>/dev/null
        sleep 1
        kill -0 "$work_pid" 2>/dev/null && kill -KILL "$work_pid" 2>/dev/null
        exit 0
    ) >/dev/null 2>&1 &
    local watchdog_pid=$!

    local status=0
    wait "$work_pid"
    status=$?

    # The worker is done (naturally, or via the watchdog's TERM/KILL above) —
    # the watchdog has no further purpose. Kill and reap it now so it can
    # never fire later against a pid the OS has since recycled.
    kill "$watchdog_pid" 2>/dev/null
    wait "$watchdog_pid" 2>/dev/null

    return $status
}
