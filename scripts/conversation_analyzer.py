#!/usr/bin/env python3
"""
VNX Conversation Analyzer — CLI entrypoint.
Version: 1.1.0

Thin CLI shim. All logic lives in the conversation_analyzer/ package.
Run: python3 scripts/conversation_analyzer.py [--max-sessions N] [--dry-run] ...
"""

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from conversation_analyzer import (  # noqa: E402 (path insert above)
    ConversationAnalyzer, Colors, log,
    DB_PATH, PATHS, VNX_BASE, ANALYZER_VERSION,
    fail_closed_exit_code,
)


def _write_heartbeat(args, stats, run_status, run_error):
    try:
        from health_beacon import HealthBeacon
        details = {"max_sessions": args.max_sessions, "dry_run": args.dry_run}
        if stats is not None:
            details["deep_attempts"] = stats.deep_attempts
            details["deep_failures"] = stats.deep_failures
            details["sessions_by_origin"] = dict(stats.sessions_by_origin)
            details["deep_restricted_claude"] = stats.deep_restricted_claude
            details["deep_restricted_deferred"] = stats.deep_restricted_deferred
        if run_error:
            details["error"] = run_error
        HealthBeacon(
            Path(PATHS["VNX_DATA_DIR"]),
            "conversation_analyzer",
            expected_interval_seconds=86400,
        ).heartbeat(status=run_status, details=details)
    except (ImportError, OSError, RuntimeError) as exc:
        log("WARNING", f"health_beacon failed: {exc}")


def _beacon_path():
    return Path(PATHS["VNX_DATA_DIR"]) / "health" / "conversation_analyzer.json"


def _read_beacon(path):
    """The beacon at ``path`` as a dict, or None when it is absent, cannot be
    read or is not a JSON object."""
    try:
        beacon = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return beacon if isinstance(beacon, dict) else None


def _file_identity(path):
    """(device, inode, mtime) of ``path``, or None when it cannot be stat'ed.
    A beacon write replaces the file with a new one, so this always changes."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns)


def _beacon_written_since(since_ts):
    """The status of the analyzer beacon when it was written at or after
    ``since_ts`` (epoch seconds), else None: no beacon, an older one, or one
    that cannot be read all count as "no beacon of this run"."""
    beacon = _read_beacon(_beacon_path())
    if beacon is None:
        return None
    written = beacon.get("last_run_ts")
    if isinstance(written, int) and written >= since_ts:
        return beacon.get("status")
    return None


def _fail_beacon_not_landed(path, reason, started, before):
    """Why the beacon now at ``path`` is not the ``fail`` beacon with
    ``reason`` that this call wrote, or None when it is. ``started`` is the
    epoch second the write began; ``before`` the file identity before it."""
    beacon = _read_beacon(path)
    if beacon is None:
        return f"no readable beacon at {path}"
    if _file_identity(path) == before:
        return f"the beacon at {path} is unchanged (status={beacon.get('status')})"
    details = beacon.get("details")
    error = details.get("error") if isinstance(details, dict) else None
    written = beacon.get("last_run_ts")
    if (beacon.get("status") != "fail" or error != reason
            or not isinstance(written, int) or written < started):
        return (f"the beacon at {path} holds status={beacon.get('status')}, "
                f"error={error!r}, last_run_ts={written}")
    return None


def _write_fail_beacon(args):
    """Write a ``fail`` beacon for a run the nightly runner saw fail (OI-2021).

    The runner calls this when the analyzer itself cannot report: a phase
    overran its time limit, Phase 1 exited non-zero without a beacon, Phase 0
    failed, or another run still holds the lock. With ``--unless-beacon-since``
    a beacon the analyzer wrote during this run is kept, not overwritten.

    ``_write_heartbeat`` is best-effort, so the beacon is read back: exit 1
    unless it is on disk with status ``fail`` and this reason, written now.
    """
    reason = args.write_fail_beacon
    if args.unless_beacon_since is not None:
        status = _beacon_written_since(args.unless_beacon_since)
        if status is not None:
            print(f"fail beacon not written: this run's beacon is present (status={status})")
            return 0
    path = _beacon_path()
    before = _file_identity(path)
    started = int(time.time())
    _write_heartbeat(args, None, "fail", reason)
    problem = _fail_beacon_not_landed(path, reason, started, before)
    if problem is not None:
        print(f"fail beacon NOT written: {problem}; reason it carried: {reason}",
              file=sys.stderr)
        return 1
    print(f"fail beacon written: {reason}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="VNX Conversation Analyzer — Nightly Session Mining Pipeline")
    parser.add_argument("--max-sessions", type=int, default=50,
                        help="Max sessions to analyze per run")
    parser.add_argument("--deep-budget", type=int, default=20,
                        help="Max LLM deep analysis calls per run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse sessions without storing or LLM calls")
    parser.add_argument("--project-filter",
                        help="Only analyze sessions from project matching this string")
    parser.add_argument("--terminal-filter",
                        help="Only analyze sessions from this terminal (T-MANAGER, T1, T2, T3)")
    parser.add_argument("--write-fail-beacon", metavar="REASON",
                        help="Only write a 'fail' health beacon with REASON and exit")
    parser.add_argument("--unless-beacon-since", type=int, metavar="EPOCH",
                        help="With --write-fail-beacon: keep a beacon written at or after EPOCH")
    args = parser.parse_args()

    if args.unless_beacon_since is not None and args.write_fail_beacon is None:
        parser.error("--unless-beacon-since requires --write-fail-beacon")
    if args.write_fail_beacon is not None:
        return _write_fail_beacon(args)

    print(f"\n{Colors.BLUE}{'=' * 70}")
    print("VNX Conversation Analyzer")
    print(f"Version: {ANALYZER_VERSION}")
    print(f"{'=' * 70}{Colors.RESET}\n")

    if not DB_PATH.exists():
        log("ERROR", f"Quality database not found: {DB_PATH}")
        log("INFO", "Run quality_db_init.py first")
        return 1

    analyzer = ConversationAnalyzer(DB_PATH)
    analyzer.connect()

    rc = 0
    run_status = "ok"
    run_error = None
    stats = None
    try:
        stats = analyzer.run(
            max_sessions=args.max_sessions,
            deep_budget=args.deep_budget,
            dry_run=args.dry_run,
            project_filter=args.project_filter,
            terminal_filter=args.terminal_filter,
        )
    except Exception as e:
        log("ERROR", f"Analysis failed: {e}")
        run_status = "fail"
        run_error = str(e)
        rc = 1
    else:
        # OI-862: a run in which EVERY session failed must not exit 0. The
        # runner's per-session handler counts errors but never influences the
        # exit code, so a night where the whole pipeline failed still reported
        # launchd status 0 — the silent-failure pattern this chain exists to catch.
        if fail_closed_exit_code(stats):
            if stats.deep_attempts > 0 and stats.deep_failures >= stats.deep_attempts:
                run_error = (f"deep analysis failed on all {stats.deep_attempts} attempts")
            else:
                run_error = f"all sessions failed ({stats.errors} errors, 0 analyzed)"
            log("ERROR", f"{run_error}; returning non-zero exit")
            run_status = "fail"
            rc = 1
    finally:
        analyzer.close()
        _write_heartbeat(args, stats, run_status, run_error)

    return rc


if __name__ == "__main__":
    sys.exit(main())
