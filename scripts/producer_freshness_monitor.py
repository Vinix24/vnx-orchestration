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
    0  EXIT_OK          sweep ran, no findings
    11 EXIT_HEALTH      sweep ran, stale/missing producers found
    20 EXIT_IO          state dir / report not writable
    30 EXIT_DEPENDENCY  registry missing/malformed, PyYAML unavailable

``--no-write`` performs a read-only sweep (acceptance runs against the live
store): nothing is appended, no heartbeat is written, no harvest cache is
touched; the report goes to stdout only.
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
EXIT_HEALTH = 11
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
    parser.add_argument("--human", action="store_true", help="human-readable output")
    args = parser.parse_args(argv)

    try:
        import producer_freshness as pf  # noqa: PLC0415
    except ImportError as exc:
        emit_json({"ok": False, "error": {"code": "dependency", "message": str(exc)}})
        return EXIT_DEPENDENCY

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

    job_exits: Dict[str, object] = {"skipped": True}
    if not args.skip_job_exits and not args.no_write:
        try:
            import job_exit_capture  # noqa: PLC0415

            job_exits = job_exit_capture.harvest_launchd(state_dir)
        except (ImportError, OSError) as exc:
            # The harvest is a bonus signal in the same run; its failure must
            # not suppress the freshness findings.
            job_exits = {"skipped": False, "error": str(exc)}
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
    return EXIT_HEALTH if report["findings_count"] else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
