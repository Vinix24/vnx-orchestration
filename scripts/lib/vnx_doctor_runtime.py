#!/usr/bin/env python3
"""
VNX Doctor Runtime — Canonical runtime health checks for vnx doctor.

PR-4 deliverable: hardens vnx doctor into a real operator integrity command
that validates runtime health, incident pressure, tmux/session coherence,
and recovery readiness from canonical state.

Design:
  - All checks are read-only and idempotent (A-R6, A-R8).
  - Every check reads canonical runtime state, not projections (G-R6).
  - Output distinguishes PASS / WARN / FAIL with concrete reasons.
  - Recovery preflight identifies blockers for vnx recover.

Governance references:
  A-R6:  vnx doctor is a preflight and integrity command, not a best-effort ping
  A-R7:  vnx recover must reconcile before resuming — doctor identifies what
  A-R10: No new operability shortcut may bypass runtime evidence
  G-R6:  vnx doctor and vnx recover must operate on canonical runtime state
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from vnx_doctor_checks import (  # noqa: F401
    PASS,
    WARN,
    FAIL,
    CheckResult,
    check_schema_status,
    check_lease_health,
    check_queue_health,
    check_incident_pressure,
    check_tmux_profile,
    check_lease_dispatch_coherence,
    compute_recovery_preflight,
)


@dataclass
class DoctorReport:
    """Aggregated doctor report across all checks."""
    checks: List[CheckResult] = field(default_factory=list)
    recovery_preflight: List[str] = field(default_factory=list)
    generated_at: str = ""

    @property
    def overall_status(self) -> str:
        if any(c.status == FAIL for c in self.checks):
            return FAIL
        if any(c.status == WARN for c in self.checks):
            return WARN
        return PASS

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.status == PASS)

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.status == WARN)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.status == FAIL)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "pass": self.pass_count,
            "warn": self.warn_count,
            "fail": self.fail_count,
            "checks": [c.to_dict() for c in self.checks],
            "recovery_preflight": self.recovery_preflight,
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---------------------------------------------------------------------------
# Main doctor runtime entry point
# ---------------------------------------------------------------------------

def run_runtime_checks(state_dir: str | Path) -> DoctorReport:
    """Execute all runtime health checks and return a structured report.

    Read-only and idempotent. Safe to run in any runtime condition.
    """
    sd = Path(state_dir)
    report = DoctorReport(generated_at=_now_utc())

    report.checks.append(check_schema_status(sd))

    # Only run deeper checks if schema is available
    if report.checks[0].status != FAIL:
        report.checks.append(check_lease_health(sd))
        report.checks.append(check_queue_health(sd))
        report.checks.append(check_incident_pressure(sd))
        report.checks.append(check_lease_dispatch_coherence(sd))

    report.checks.append(check_tmux_profile(sd))
    report.recovery_preflight = compute_recovery_preflight(sd, report.checks)

    return report


# ---------------------------------------------------------------------------
# CLI interface for doctor.sh integration
# ---------------------------------------------------------------------------

def _format_status_icon(status: str) -> str:
    if status == PASS:
        return "OK"
    if status == WARN:
        return "WARN"
    return "FAIL"


def _print_report(report: DoctorReport, verbose: bool = False) -> None:
    """Print doctor report in operator-readable format."""
    for check in report.checks:
        icon = _format_status_icon(check.status)
        print(f"  [{icon}] {check.name}: {check.message}")
        if verbose and check.details:
            for detail in check.details:
                print(f"        {detail}")

    if report.recovery_preflight:
        print()
        print("  Recovery preflight:")
        for item in report.recovery_preflight:
            print(f"    - {item}")

    print()
    print(
        f"  Runtime: {report.pass_count} pass, "
        f"{report.warn_count} warn, {report.fail_count} fail"
    )


def main() -> int:
    """CLI entry point called from doctor.sh."""
    import argparse

    parser = argparse.ArgumentParser(description="VNX Doctor Runtime Checks")
    parser.add_argument(
        "--state-dir",
        required=True,
        help="Path to .vnx-data/state directory",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show check details",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Only output recovery preflight items",
    )
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    if not state_dir.exists():
        print(f"[doctor] FAIL: State directory not found: {state_dir}", file=sys.stderr)
        return 1

    report = run_runtime_checks(state_dir)

    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.preflight_only:
        if report.recovery_preflight:
            for item in report.recovery_preflight:
                print(item)
            return 1 if any(item.startswith("BLOCKER") for item in report.recovery_preflight) else 0
        return 0
    else:
        _print_report(report, verbose=args.verbose)

    return 0 if report.overall_status != FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
