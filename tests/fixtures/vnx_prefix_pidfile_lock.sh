#!/usr/bin/env bash
# tests/fixtures/vnx_prefix_pidfile_lock.sh — vendored PRE-FIX lock
# implementation from PR #1247 finding 1: session_reconcile_autoclose.sh's
# `_acquire_lock` as it shipped at commit a6916b2b, before the flock(2)
# rewrite in bd6087d3 (scripts/lib/vnx_flock_lock.sh). Exists ONLY as a test
# fixture — do not source this from production code. It has the exact,
# measured race documented below and in vnx_flock_lock.sh's own header.
#
# WHY this file exists: PR #1247 finding 3 (codex gate) — the dispatch
# report claimed the race harness in tests/hooks/test_session_lock_race.sh
# reproduced 9-11 winners of 20 against this old implementation, as the
# strongest evidence that the flock(2) fix was necessary. That comparison
# lived only in prose, not in the committed suite, so nobody could re-run
# it. This fixture + tests/hooks/test_session_lock_race_prefix_regression.sh
# commit it: same harness, same worker script, this file swapped in for
# vnx_flock_lock.sh, asserting the race actually reproduces.
#
# Exposes the SAME two function names as vnx_flock_lock.sh
# (vnx_flock_acquire/vnx_flock_release) so the identical worker script can
# drive either implementation unmodified — only the sourced file differs.
#
# The race: staleness-check ("is the recorded holder dead") and reclaim
# ("rm the file, THEN a separate noclobber write") are two distinct
# syscalls with a gap between them. Two contenders can each pass the
# dead-holder check in that gap and each see their own write to the
# now-briefly-absent path succeed — noclobber's atomicity only protects the
# FIRST write of a given noclobber attempt, it does not make a rm-then-write
# pair atomic as a unit. That gap is exactly what vnx_flock_lock.sh's single
# atomic flock(2) syscall removes.
#
# vnx_flock_acquire <lock_file> <content> [python_bin — accepted for
#   interface-compat with vnx_flock_lock.sh's signature, unused here: this
#   implementation never depended on python/fcntl, that absence is the
#   entire point of the comparison]
_VNX_PREFIX_PIDFILE_LOCKFILE=""

vnx_flock_acquire() {
    local lock_file="$1"
    local content="$2"

    _VNX_PREFIX_PIDFILE_LOCKFILE="$lock_file"

    if ( set -C; echo "$content" >"$lock_file" ) 2>/dev/null; then
        return 0
    fi

    local held=""
    held="$(cat "$lock_file" 2>/dev/null || true)"
    if [ -n "$held" ] && kill -0 "$held" 2>/dev/null; then
        return 1
    fi

    # Stale (or unparseable, e.g. a non-numeric "worker-N" content string
    # from this fixture's own test harness — kill -0 on that just fails the
    # same way a dead pid would, which is the correct behavior here: content
    # that isn't a live, killable pid is content that cannot prove the
    # holder is still alive). Reclaim: rm, THEN a separate noclobber write —
    # the gap between them is the race this fixture exists to reproduce.
    rm -f "$lock_file" 2>/dev/null
    if ( set -C; echo "$content" >"$lock_file" ) 2>/dev/null; then
        return 0
    fi
    return 1
}

vnx_flock_release() {
    [ -n "$_VNX_PREFIX_PIDFILE_LOCKFILE" ] && rm -f "$_VNX_PREFIX_PIDFILE_LOCKFILE" 2>/dev/null
    return 0
}
