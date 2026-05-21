#!/bin/bash
# Singleton Enforcer - atomic flock(2)-based lifecycle management
#
# Usage: source this script at the start of any script that needs singleton
# enforcement, then call `enforce_singleton "<name>" [log_file] [script_path]`.
#
# Race-freedom:
#   flock(2) is a kernel-level POSIX advisory mutex. The file descriptor opened
#   on the lock file is held for the lifetime of the calling script; when bash
#   exits, the kernel atomically releases the lock. There is no read-then-claim
#   window for contenders to slip into. A non-blocking exclusive flock either
#   succeeds (we are the singleton) or fails (another live instance owns it).
#
#   This replaces the prior mkdir+rm_rf+retry design (OI-1518) that had a race
#   window during stale-lock cleanup: two contenders could both rm_rf each
#   other's freshly-acquired lock dir under stop_existing semantics, allowing
#   parallel survivors. The 2026-05-20 receipt-flood incident was triggered
#   by this race compounded by test daemons that leaked into operator state.
#
# Stale-lock handling:
#   No explicit stale-lock check is needed. If the previous holder died
#   without cleaning up (kill -9 / power loss), the kernel released the flock
#   on process exit. The lock file may remain on disk but holds no lock; the
#   next caller's flock() succeeds immediately.
#
# Dependency: flock(1) from util-linux. Default on Linux; on macOS install via
# `brew install util-linux`.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
# shellcheck source=lib/process_lifecycle.sh
source "$SCRIPT_DIR/lib/process_lifecycle.sh"

# Fixed FD for the singleton lock. 200 is conventional for userland and avoids
# collision with stdin/stdout/stderr (0-2) and tools that use 3-9 ad-hoc.
# A fixed FD is required because bash 3.2 (default on macOS) does not support
# the `{varname}>` auto-assign redirect syntax.
_VNX_SINGLETON_LOCK_FD=200

enforce_singleton() {
    local script_name="${1:-$(basename "$0")}"
    local log_file="${2:-}"
    local script_path="${3:-$0}"

    if ! command -v flock >/dev/null 2>&1; then
        echo "[SINGLETON] flock(1) not found. Install util-linux (macOS: brew install util-linux)." >&2
        exit 1
    fi

    mkdir -p "$VNX_LOCKS_DIR" "$VNX_PIDS_DIR"
    local lock_file="$VNX_LOCKS_DIR/${script_name}.lock"
    local pid_file="$VNX_PIDS_DIR/${script_name}.pid"

    # Open the lock file on a fixed FD. The FD inherits the bash process'
    # lifetime; closing it (or bash exiting) releases the kernel flock.
    eval "exec ${_VNX_SINGLETON_LOCK_FD}>\"\$lock_file\""

    if ! flock -n -x "$_VNX_SINGLETON_LOCK_FD"; then
        local existing_pid
        existing_pid="$(cat "$pid_file" 2>/dev/null || echo unknown)"
        echo "[SINGLETON] Another instance of $script_name is already running (PID: $existing_pid)"
        eval "exec ${_VNX_SINGLETON_LOCK_FD}>&-"
        exit 0
    fi

    local fingerprint
    fingerprint="$(vnx_proc_realpath "$script_path")"
    if [ -z "$fingerprint" ]; then
        fingerprint="$(vnx_proc_cmdline "$$")"
    fi
    echo "$$" > "$pid_file"
    echo "$fingerprint" > "${pid_file}.fingerprint"

    # PID files are observability/telemetry only — the lock itself is released
    # by the kernel when fd 200 closes. EXIT/INT/TERM trap cleans the PID
    # artifacts. The lock file on disk is harmless if left behind.
    trap "rm -f '$pid_file' '${pid_file}.fingerprint'" EXIT INT TERM

    echo "[SINGLETON] Lock acquired for $script_name (PID: $$)"
}

export -f enforce_singleton
