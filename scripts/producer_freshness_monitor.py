#!/usr/bin/env python3
"""producer_freshness_monitor.py — sweep CLI for the producer-freshness monitor.

Runs the per-key freshness diff (scripts/lib/producer_freshness.py) over the
registry in configs/producer_freshness.yaml, harvests launchd job exit codes
(scripts/lib/job_exit_capture.py) in the same run so one scheduled sweep
covers both silent-failure classes, appends an NDJSON report, and writes a
heartbeat on EVERY run — including runs with zero findings, because a sweep
that finds nothing and writes nothing is indistinguishable from a sweep that
never ran. hooks/monitor_tripwire.sh watches only that heartbeat's age.

Exit codes (house style, cf. check_intelligence_health.py):
    0  EXIT_OK          sweep ran (findings reported via health file + NDJSON)
    20 EXIT_IO          state dir / report not writable
    30 EXIT_DEPENDENCY  registry missing/malformed, PyYAML unavailable

``--no-write`` performs a read-only sweep (acceptance runs against the live
store): nothing is appended, no heartbeat is written, no harvest cache is
touched; the report goes to stdout only.

``--latest-findings`` (OI-1460) is a SEPARATE, cheaper read-only mode: it runs
NO sweep at all (no directory globs, no sqlite queries, no launchd harvest) —
it only reads back whatever the last completed sweep already persisted to
``producer_freshness.ndjson`` via ``producer_freshness.latest_findings()``.
This is the mode ``hooks/sessionstart.sh`` shells out to on every session
(same "reuse the existing CLI via subprocess" convention as its beacon-health
section calling ``scripts/health_check.py``): a full sweep is a scheduled
batch job, not something a session-start hot path should re-run on every
``claude`` launch.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from cli_output import emit_json, emit_human  # noqa: E402

EXIT_OK = 0
EXIT_HEALTH = 11  # Deprecated (OI-1039): findings reported via health file, not exit code
EXIT_IO = 20
EXIT_DEPENDENCY = 30

DEFAULT_CONFIG = SCRIPT_DIR.parent / "configs" / "producer_freshness.yaml"


def _default_state_dir() -> Path:
    from vnx_paths import ensure_env  # noqa: PLC0415

    env = ensure_env()
    return Path(env["VNX_STATE_DIR"])


def _human_lines(report: Dict[str, object]) -> str:
    lines = [
        f"producer-freshness sweep {report['run_id']} — status={report['status']} "
        f"findings={report['findings_count']} "
        f"(producers={report['producers_evaluated']}, keys={report['keys_evaluated']})",
    ]
    for finding in report["findings"]:  # type: ignore[index]
        if finding.get("kind") == "missing":
            lines.append(
                f"  MISSING {finding['producer']}/{finding['key']}: expected key has never written"
            )
        elif finding.get("kind") == "source_unreadable":
            lines.append(f"  ERROR   {finding['producer']}: {finding.get('error')}")
        else:
            demand = finding.get("demand") or {}
            demand_txt = ""
            if demand:
                demand_txt = (
                    f" — demand while silent: {demand.get('events_since_last_seen')} "
                    f"{demand.get('source')}"
                )
            lines.append(
                f"  STALE   {finding['producer']}/{finding['key']}: last_seen={finding['last_seen']} "
                f"({finding['silence_days']}d ago, cadence {finding['cadence_seconds']}s){demand_txt}"
            )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Per-key producer freshness sweep")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="producer registry YAML")
    parser.add_argument("--state-dir", default=None, help="VNX state dir (default: resolved via vnx_paths)")
    parser.add_argument("--no-write", action="store_true", help="read-only sweep; report to stdout only")
    parser.add_argument("--skip-job-exits", action="store_true", help="do not harvest launchd exit codes")
    parser.add_argument(
        "--skip-beacon-checks",
        action="store_true",
        help=(
            "do not run the beacon reader-coverage / duplicate-writer checks "
            "(C2a: scripts/lib/beacon_reader_register.py, "
            "beacon_register.find_duplicate_beacon_writers)"
        ),
    )
    parser.add_argument("--human", action="store_true", help="human-readable output")
    parser.add_argument(
        "--latest-findings",
        action="store_true",
        help=(
            "read back the last persisted sweep's findings (OI-1460) and exit — "
            "runs NO new sweep, writes NOTHING, does not require --config/PyYAML. "
            "The cheap path a SessionStart hook can afford to call every session."
        ),
    )
    args = parser.parse_args(argv)

    try:
        import producer_freshness as pf  # noqa: PLC0415
    except ImportError as exc:
        emit_json({"ok": False, "error": {"code": "dependency", "message": str(exc)}})
        return EXIT_DEPENDENCY

    if args.latest_findings:
        try:
            state_dir = Path(args.state_dir) if args.state_dir else _default_state_dir()
        except (OSError, RuntimeError, KeyError) as exc:
            emit_json({"ok": False, "error": {"code": "io", "message": f"state dir: {exc}"}})
            return EXIT_IO
        result = pf.latest_findings(state_dir)
        emit_json(result)
        return EXIT_OK

    try:
        registry = pf.load_registry(Path(args.config))
    except ImportError as exc:
        emit_json({"ok": False, "error": {"code": "dependency", "message": f"PyYAML: {exc}"}})
        return EXIT_DEPENDENCY
    except (OSError, ValueError) as exc:
        emit_json({"ok": False, "error": {"code": "dependency", "message": f"registry: {exc}"}})
        return EXIT_DEPENDENCY

    try:
        state_dir = Path(args.state_dir) if args.state_dir else _default_state_dir()
    except (OSError, RuntimeError, KeyError) as exc:
        emit_json({"ok": False, "error": {"code": "io", "message": f"state dir: {exc}"}})
        return EXIT_IO

    report = pf.run_sweep(state_dir, registry)

    # C2a (absence-is-loud, the reader half): a producer/beacon with zero
    # readers is exactly as invisible as one with zero writers. Kept OUTSIDE
    # pf.run_sweep() itself (never touches its signature/behavior) so every
    # existing run_sweep()-level test asserting exact findings_count against
    # a hand-built YAML registry stays unaffected — this only runs at the
    # CLI layer, folded into the SAME NDJSON report + heartbeat as the rest.
    beacon_checks: Dict[str, object] = {"skipped": True}
    if not args.skip_beacon_checks:
        try:
            import beacon_reader_register  # noqa: PLC0415
            import beacon_register  # noqa: PLC0415

            coverage = beacon_reader_register.check_coverage()
            data_dir = state_dir.parent if state_dir.name == "state" else state_dir
            duplicates = beacon_register.find_duplicate_beacon_writers(data_dir)
            duplicate_findings = [
                {
                    "producer": "beacon_reader_coverage",
                    "key": name,
                    "kind": "duplicate_writer",
                    "cadence_seconds": None,
                    "paths": [str(p) for p in paths],
                }
                for name, paths in duplicates.items()
            ]
            new_findings = list(coverage["findings"]) + duplicate_findings
            beacon_checks = {
                "skipped": False,
                "no_reader_count": len(coverage["findings"]),
                "duplicate_writer_count": len(duplicate_findings),
            }
            if new_findings:
                report["findings"].extend(new_findings)
                report["findings_count"] = len(report["findings"])
                report["status"] = "stale"
        except Exception as exc:  # vnx-silent-except: a bonus structural signal; an unexpected failure (ValueError/AttributeError/SyntaxError from the AST-driven reader register, not just ImportError/OSError) must not take the already-built sweep findings down with it
            beacon_checks = {"skipped": True, "error": str(exc)}
    report["beacon_reader_coverage"] = beacon_checks

    job_exits: Dict[str, object] = {"skipped": True}
    if not args.skip_job_exits and not args.no_write:
        try:
            import job_exit_capture  # noqa: PLC0415

            job_exits = job_exit_capture.harvest_launchd(state_dir)
        except Exception as exc:  # vnx-silent-except: the launchd harvest is a bonus signal; an unexpected failure (e.g. a ValueError from a corrupt harvest cache, not just ImportError/OSError) must not take the already-built sweep findings down with it
            job_exits = {"skipped": True, "error": str(exc)}
    report["job_exits"] = job_exits

    if not args.no_write:
        try:
            report_path = pf.append_report(state_dir, report)
            heartbeat_path = pf.write_heartbeat(state_dir, report)  # EVERY run, even 0 findings
        except OSError as exc:
            emit_json({"ok": False, "error": {"code": "io", "message": str(exc)}})
            return EXIT_IO
        report["report_path"] = str(report_path)
        report["heartbeat_path"] = str(heartbeat_path)

    if args.human:
        emit_human(_human_lines(report))
    else:
        emit_json(report)
    return EXIT_OK  # OI-1039: findings reported via health file + NDJSON, not exit code


if __name__ == "__main__":
    raise SystemExit(main())
