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
# Return status (PR #1247 finding 1, codex gate): a command killed for
# overrunning the deadline returns $VNX_RUN_BOUNDED_DEADLINE (124), NEVER the
# raw wait/signal status (SIGTERM would otherwise look identical to a command
# that simply exits 143 on its own). 124 was chosen — not an arbitrary spare
# number — because it is GNU/BSD coreutils timeout(1)'s own documented exit
# status for exactly this case ("124 if COMMAND times out, and
# --preserve-status is not specified"), which is also why it cannot collide
# with a command's OWN intended exit code in practice: coreutils' own manual
# already tells command authors to avoid it for that reason. Any other exit
# status — including 143/137 from a command that catches/ignores/dies to a
# real signal on its own — passes through unchanged and means "the command
# itself decided that status," not "the deadline fired."
#
# Fast path: when a real timeout binary is available, use it directly. Its
# own contract already returns exactly $VNX_RUN_BOUNDED_DEADLINE on a timeout
# kill (see above) and the command's real exit status otherwise — the two
# paths agree by construction, nothing to translate here.
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
#
# The watchdog and the main flow are separate background processes with no
# shared memory, so "did the watchdog actually intervene" is communicated
# through a marker file rather than an exit-status side channel: the
# watchdog creates it at the exact instant it decides the deadline elapsed
# AND the worker is still alive (right before sending TERM) — never merely
# because the deadline duration passed. The main flow checks for the marker
# AFTER reaping both processes and overrides the return status to the
# sentinel only if it exists, then removes it.
#
# Guarded against double-sourcing: bash treats a second `readonly NAME=`
# on an already-readonly name as an error, and this lib gets sourced by
# more than one caller in the same test process (e.g. a test that sources
# it directly AND sources a hook script that sources it again).
if [ -z "${VNX_RUN_BOUNDED_DEADLINE:-}" ]; then
    readonly VNX_RUN_BOUNDED_DEADLINE=124
fi

vnx_run_bounded() {
    local timeout_bin="$1"; shift
    local deadline_secs="$1"; shift
    local caller_pid="$1"; shift

    if [ -n "$timeout_bin" ]; then
        "$timeout_bin" "$deadline_secs" "$@"
        return $?
    fi

    # `mktemp -u`: print a unique name WITHOUT creating the file. Existence
    # of this path is the entire signal ("did the watchdog actually fire") —
    # a bare `mktemp` pre-creates an empty file, which would make that file
    # already exist before the watchdog ever runs, collapsing the signal.
    local killed_marker
    killed_marker="$(mktemp -u 2>/dev/null)" || killed_marker=""

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
        [ -n "$killed_marker" ] && : > "$killed_marker"
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

    if [ -n "$killed_marker" ]; then
        if [ -f "$killed_marker" ]; then
            rm -f "$killed_marker" 2>/dev/null
            return "$VNX_RUN_BOUNDED_DEADLINE"
        fi
        rm -f "$killed_marker" 2>/dev/null
    fi

    return $status
}

# vnx_run_bounded_group <group_id> <deadline_secs> <grace_secs> <reap_secs>
#
# Bounds a child that the CALLING shell started with `&` as the leader of its
# own process group, so that its pid is also its group id (start it through
# scripts/lib/vnx_exec_new_group.py; macOS has no setsid(1)). Unlike
# vnx_run_bounded above, which signals one worker pid, this signals the whole
# group: a phase that hangs in a grandchild is ended as a unit (OI-2021).
#
#   <group_id>:      the child's pid ($!), equal to its process group id.
#   <deadline_secs>: whole seconds the group may run; it is never cut short.
#   <grace_secs>:    seconds between SIGABRT and SIGKILL. SIGABRT comes first
#       so a Python child started with `-X faulthandler` dumps its traceback
#       into its own stderr before it dies.
#   <reap_secs>:     seconds to wait for the child to be gone after SIGKILL.
#
# Never signals when <group_id> is empty, not a number, 0 or 1 (kill(2) reads
# -0 as "my own group" and -1 as "every process I may signal"), or equal to the
# calling shell's own process group, or when that own group cannot be read.
# Those cases return $VNX_RUN_BOUNDED_REFUSED at once, without waiting.
#
# Never waits unbounded: the child is polled with `kill -0` (bash reaps a
# finished background child by itself, after which `kill -0` fails), and
# `wait` is only called once the child is gone, so it returns at once. A child
# still present after the reap window (stuck in an uninterruptible call) is
# left behind and the function returns anyway.
#
# Return status: the child's own exit status when it ended inside the
# deadline; $VNX_RUN_BOUNDED_DEADLINE (124) after an overrun. Detail for the
# caller's log lands in two globals (bash 3.2 has no namerefs):
#   VNX_BOUNDED_GROUP_OUTCOME  exited | deadline | deadline_unreaped | refused
#   VNX_BOUNDED_GROUP_STATUS   the child's raw wait status, empty when unknown
#                              (134 = ended by SIGABRT, 137 = by SIGKILL)
if [ -z "${VNX_RUN_BOUNDED_REFUSED:-}" ]; then
    readonly VNX_RUN_BOUNDED_REFUSED=125
fi

# Polls until <pid> is gone (return 0) or more than <secs> whole seconds have
# passed (return 1). Short naps first, so a quick phase costs no full second.
_vnx_bounded_group_gone_within() {
    local pid="$1" secs="$2" start=$SECONDS
    while kill -0 "$pid" 2>/dev/null; do
        [ $((SECONDS - start)) -le "$secs" ] || return 1
        if [ $((SECONDS - start)) -lt 2 ]; then
            sleep 0.1
        else
            sleep 0.5
        fi
    done
    return 0
}

vnx_run_bounded_group() {
    local group_id="$1" deadline_secs="$2" grace_secs="$3" reap_secs="$4"
    VNX_BOUNDED_GROUP_OUTCOME="refused"
    VNX_BOUNDED_GROUP_STATUS=""

    local value
    for value in "$deadline_secs" "$grace_secs" "$reap_secs"; do
        case "$value" in
            ''|*[!0-9]*) return "$VNX_RUN_BOUNDED_REFUSED" ;;
        esac
    done
    case "$group_id" in
        ''|*[!0-9]*|0|1) return "$VNX_RUN_BOUNDED_REFUSED" ;;
    esac
    local own_group
    own_group="$(ps -o pgid= -p "$$" 2>/dev/null | tr -d ' ')" || own_group=""
    if [ -z "$own_group" ] || [ "$group_id" = "$own_group" ]; then
        return "$VNX_RUN_BOUNDED_REFUSED"
    fi

    local status=0
    if _vnx_bounded_group_gone_within "$group_id" "$deadline_secs"; then
        wait "$group_id" || status=$?
        VNX_BOUNDED_GROUP_OUTCOME="exited"
        VNX_BOUNDED_GROUP_STATUS="$status"
        return "$status"
    fi

    kill -s ABRT -- "-$group_id" 2>/dev/null || true
    _vnx_bounded_group_gone_within "$group_id" "$grace_secs" || true
    # Also when the leader died on SIGABRT: members it started may remain.
    kill -s KILL -- "-$group_id" 2>/dev/null || true
    if _vnx_bounded_group_gone_within "$group_id" "$reap_secs"; then
        wait "$group_id" || status=$?
        VNX_BOUNDED_GROUP_OUTCOME="deadline"
        VNX_BOUNDED_GROUP_STATUS="$status"
    else
        VNX_BOUNDED_GROUP_OUTCOME="deadline_unreaped"
    fi
    return "$VNX_RUN_BOUNDED_DEADLINE"
}
