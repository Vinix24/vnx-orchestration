#!/usr/bin/env bash
# vnx_flock_lock.sh — kernel-level flock(2) mutex for bash scripts, via
# python's fcntl module (stdlib — no separate binary dependency).
#
# Race-freedom: flock(2) is a single atomic kernel syscall keyed to the OS
# "open file description" behind a file descriptor, not to file content. A
# holder that dies for ANY reason — normal exit, SIGTERM, SIGKILL, power loss
# — releases the lock the instant its last fd on that open file description
# closes. There is no read-then-claim window for a second acquirer to race
# into, unlike a PID-file scheme where "is the recorded holder dead" and
# "claim the file" are necessarily two separate syscalls (rm, then a
# noclobber write) with a gap between them: two reclaimers can each pass the
# staleness check in that gap and each see their own write to the
# now-briefly-absent path succeed. That was the exact bug this replaces
# (session_reconcile_autoclose.sh's RECLAIM path, PR #1247 finding 1) — the
# noclobber write's atomicity only protects the FIRST write, not a rm-then-
# write pair, so it does not make the reclaim itself atomic.
#
# Same mechanism scripts/singleton_enforcer.sh already uses via the flock(1)
# binary (OI-1518, also regression-tested at
# tests/test_singleton_enforcer_flock.py: 10x parallel contenders, exactly
# one wins). This uses python's fcntl.flock instead of shelling out to
# flock(1) because flock(1) here is ITSELF a separate Homebrew-installed
# binary (/opt/homebrew/bin/flock) — the same fragility class (a Homebrew
# relink silently breaking a binary a background hook depends on) that
# produced the interpreter bug this PR series is fixing elsewhere (OI-852).
# fcntl ships with the interpreter we already resolve for the reconcile CLI
# calls — one less binary that can go missing.
#
# Usage:
#   source .../vnx_flock_lock.sh
#   if vnx_flock_acquire "$LOCK_FILE" "$SELF_PID" "$PY"; then
#       ...critical section...
#       vnx_flock_release   # optional — process exit releases it too
#   else
#       # busy: "$LOCK_FILE" holds the current holder's content, informational only
#   fi
#
# vnx_flock_acquire <lock_file> <content> [python_bin]
#   Opens <lock_file> read-write on fixed FD 200 (matching the convention in
#   scripts/singleton_enforcer.sh: high enough to avoid colliding with
#   stdin/stdout/stderr or the 3-9 range ad-hoc tools use), creating it if
#   absent and WITHOUT truncating on open — a losing attempt must never
#   destroy the current holder's content. Attempts a non-blocking exclusive
#   flock via [python_bin] (default: python3), which inherits FD 200
#   automatically (plain fork/exec from this shell, no redirection needed).
#   On success, truncates+writes <content> (observability only, e.g. the
#   caller's pid — plays no role in correctness) and leaves FD 200 open:
#   that open fd IS the lock. Returns 0.
#   On failure (contended, or the open itself failed), closes FD 200 and
#   returns 1.
#
# vnx_flock_release
#   Closes FD 200, releasing the flock. Safe to call even if the lock was
#   never held (no-op).

vnx_flock_acquire() {
    local lock_file="$1"
    local content="$2"
    local py="${3:-python3}"

    exec 200<>"$lock_file" || return 1

    if ! VNX_FLOCK_CONTENT="$content" "$py" - <<'PYEOF'
import fcntl, os, sys
try:
    fcntl.flock(200, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)
os.ftruncate(200, 0)
os.lseek(200, 0, os.SEEK_SET)
os.write(200, (os.environ.get("VNX_FLOCK_CONTENT", "") + "\n").encode())
os.fsync(200)
PYEOF
    then
        exec 200>&- 2>/dev/null
        return 1
    fi
    return 0
}

vnx_flock_release() {
    exec 200>&- 2>/dev/null || true
}
