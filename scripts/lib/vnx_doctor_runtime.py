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
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    DB_FILENAME,
    DISPATCH_STATES,
    LEASE_STATES,
    get_connection,
    init_schema,
)

# ---------------------------------------------------------------------------
# Check result model
# ---------------------------------------------------------------------------

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass
class CheckResult:
    """Result of a single doctor check."""
    name: str
    status: str          # "pass" | "warn" | "fail"
    message: str
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.details:
            d["details"] = self.details
        return d


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


def _parse_dt(ts: Optional[str]) -> Optional[datetime]:
    if ts is None:
        return None
    ts = ts.rstrip("Z")
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Check: Runtime schema status
# ---------------------------------------------------------------------------

def check_schema_status(state_dir: Path) -> CheckResult:
    """Validate runtime coordination database exists and has expected schema."""
    db_path = state_dir / DB_FILENAME
    if not db_path.exists():
        return CheckResult(
            name="schema_status",
            status=FAIL,
            message="Runtime coordination database not found",
            details=[f"Expected: {db_path}"],
        )

    try:
        with get_connection(state_dir) as conn:
            required_tables = [
                "runtime_schema_version",
                "dispatches",
                "dispatch_attempts",
                "terminal_leases",
                "coordination_events",
            ]
            missing = [t for t in required_tables if not _table_exists(conn, t)]
            if missing:
                return CheckResult(
                    name="schema_status",
                    status=FAIL,
                    message=f"Missing core tables: {', '.join(missing)}",
                    details=[f"Expected tables: {', '.join(required_tables)}"],
                )

            # Check v2 tables (incident substrate)
            v2_tables = ["incident_log", "retry_budgets"]
            missing_v2 = [t for t in v2_tables if not _table_exists(conn, t)]

            # Check schema version
            versions = conn.execute(
                "SELECT version FROM runtime_schema_version ORDER BY version DESC"
            ).fetchall()
            version_list = [r["version"] for r in versions]
            latest_version = max(version_list) if version_list else 0

            details = [f"Schema version: {latest_version}"]
            if missing_v2:
                return CheckResult(
                    name="schema_status",
                    status=WARN,
                    message=f"v2 migration incomplete — missing: {', '.join(missing_v2)}",
                    details=details + [f"Missing v2 tables: {', '.join(missing_v2)}"],
                )

            details.append(f"Applied versions: {version_list}")
            return CheckResult(
                name="schema_status",
                status=PASS,
                message=f"Schema v{latest_version} — all tables present",
                details=details,
            )
    except Exception as exc:
        return CheckResult(
            name="schema_status",
            status=FAIL,
            message=f"Database access error: {exc}",
        )


# ---------------------------------------------------------------------------
# Check: Lease health
# ---------------------------------------------------------------------------

def check_lease_health(state_dir: Path) -> CheckResult:
    """Validate terminal lease states from canonical runtime state."""
    try:
        with get_connection(state_dir) as conn:
            if not _table_exists(conn, "terminal_leases"):
                return CheckResult(
                    name="lease_health",
                    status=FAIL,
                    message="terminal_leases table not found",
                )

            rows = conn.execute(
                "SELECT * FROM terminal_leases ORDER BY terminal_id"
            ).fetchall()
            leases = [dict(r) for r in rows]

            if not leases:
                return CheckResult(
                    name="lease_health",
                    status=FAIL,
                    message="No terminal lease rows found (expected T1, T2, T3)",
                )

            now = datetime.now(timezone.utc)
            details = []
            expired_terminals = []
            invalid_states = []

            for lease in leases:
                tid = lease["terminal_id"]
                state = lease["state"]
                expires_at = _parse_dt(lease.get("expires_at"))

                if state not in LEASE_STATES:
                    invalid_states.append(f"{tid}: invalid state '{state}'")
                    continue

                if state == "leased" and expires_at and now > expires_at:
                    expired_terminals.append(tid)
                    details.append(f"{tid}: leased but expired (expires_at={lease['expires_at']})")
                elif state == "expired":
                    expired_terminals.append(tid)
                    details.append(f"{tid}: in expired state — needs recovery")
                elif state == "recovering":
                    details.append(f"{tid}: recovery in progress")
                else:
                    details.append(f"{tid}: {state}")

            if invalid_states:
                return CheckResult(
                    name="lease_health",
                    status=FAIL,
                    message=f"Invalid lease states detected",
                    details=invalid_states + details,
                )

            if expired_terminals:
                return CheckResult(
                    name="lease_health",
                    status=WARN,
                    message=f"Expired leases: {', '.join(expired_terminals)}",
                    details=details,
                )

            return CheckResult(
                name="lease_health",
                status=PASS,
                message=f"All {len(leases)} terminal leases healthy",
                details=details,
            )
    except Exception as exc:
        return CheckResult(
            name="lease_health",
            status=FAIL,
            message=f"Lease check error: {exc}",
        )


# ---------------------------------------------------------------------------
# Check: Dispatch queue health
# ---------------------------------------------------------------------------

def check_queue_health(state_dir: Path) -> CheckResult:
    """Validate dispatch queue state from canonical runtime state."""
    try:
        with get_connection(state_dir) as conn:
            if not _table_exists(conn, "dispatches"):
                return CheckResult(
                    name="queue_health",
                    status=FAIL,
                    message="dispatches table not found",
                )

            # Count by state
            rows = conn.execute(
                "SELECT state, COUNT(*) as cnt FROM dispatches GROUP BY state"
            ).fetchall()
            state_counts = {r["state"]: r["cnt"] for r in rows}

            total = sum(state_counts.values())
            details = [f"Total dispatches: {total}"]
            for state in sorted(state_counts):
                details.append(f"  {state}: {state_counts[state]}")

            # Check for stuck dispatches (claimed/delivering for too long)
            now = datetime.now(timezone.utc)
            stuck_threshold_seconds = 600  # 10 minutes
            stuck_rows = conn.execute(
                """
                SELECT dispatch_id, state, terminal_id, updated_at
                FROM dispatches
                WHERE state IN ('claimed', 'delivering')
                ORDER BY updated_at ASC
                """
            ).fetchall()

            stuck_dispatches = []
            for row in stuck_rows:
                updated = _parse_dt(row["updated_at"])
                if updated and (now - updated).total_seconds() > stuck_threshold_seconds:
                    stuck_dispatches.append(
                        f"{row['dispatch_id']} ({row['state']} on {row['terminal_id']}, "
                        f"since {row['updated_at']})"
                    )

            # Check for dead-lettered dispatches
            dead_letter_count = state_counts.get("dead_letter", 0)

            # Check for failed deliveries
            failed_count = state_counts.get("failed_delivery", 0)
            timed_out_count = state_counts.get("timed_out", 0)

            if stuck_dispatches:
                details.append(f"Stuck dispatches ({len(stuck_dispatches)}):")
                details.extend(f"  {s}" for s in stuck_dispatches)

            status = PASS
            messages = []

            if stuck_dispatches:
                status = WARN
                messages.append(f"{len(stuck_dispatches)} stuck")

            if dead_letter_count > 0:
                status = WARN
                messages.append(f"{dead_letter_count} dead-lettered")

            if failed_count > 0 or timed_out_count > 0:
                if status == PASS:
                    status = WARN
                parts = []
                if failed_count:
                    parts.append(f"{failed_count} failed_delivery")
                if timed_out_count:
                    parts.append(f"{timed_out_count} timed_out")
                messages.append(", ".join(parts))

            if total == 0:
                return CheckResult(
                    name="queue_health",
                    status=PASS,
                    message="No dispatches in queue",
                    details=details,
                )

            if status == PASS:
                return CheckResult(
                    name="queue_health",
                    status=PASS,
                    message=f"{total} dispatches — queue healthy",
                    details=details,
                )

            return CheckResult(
                name="queue_health",
                status=status,
                message=f"Queue issues: {'; '.join(messages)}",
                details=details,
            )
    except Exception as exc:
        return CheckResult(
            name="queue_health",
            status=FAIL,
            message=f"Queue check error: {exc}",
        )


# ---------------------------------------------------------------------------
# Check: Incident pressure
# ---------------------------------------------------------------------------

def check_incident_pressure(state_dir: Path) -> CheckResult:
    """Validate incident log pressure from canonical runtime state."""
    try:
        with get_connection(state_dir) as conn:
            if not _table_exists(conn, "incident_log"):
                return CheckResult(
                    name="incident_pressure",
                    status=PASS,
                    message="Incident log table not present (v2 migration pending)",
                    details=["No incident data to check — schema v2 not applied"],
                )

            # Open and escalated incidents
            open_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM incident_log WHERE state = 'open'"
            ).fetchone()["cnt"]

            escalated_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM incident_log WHERE state = 'escalated'"
            ).fetchone()["cnt"]

            critical_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM incident_log "
                "WHERE state IN ('open', 'escalated') AND severity = 'critical'"
            ).fetchone()["cnt"]

            # Exhausted budgets
            exhausted_count = 0
            halted_count = 0
            if _table_exists(conn, "retry_budgets"):
                exhausted_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM retry_budgets "
                    "WHERE attempts_used >= max_retries AND max_retries > 0"
                ).fetchone()["cnt"]

                halted_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM retry_budgets "
                    "WHERE auto_recovery_halted = 1"
                ).fetchone()["cnt"]

            # Incident breakdown by class
            class_rows = conn.execute(
                "SELECT incident_class, COUNT(*) as cnt "
                "FROM incident_log WHERE state IN ('open', 'escalated') "
                "GROUP BY incident_class ORDER BY cnt DESC"
            ).fetchall()

            details = [
                f"Open: {open_count}",
                f"Escalated: {escalated_count}",
                f"Critical: {critical_count}",
                f"Budgets exhausted: {exhausted_count}",
                f"Auto-recovery halted: {halted_count}",
            ]
            if class_rows:
                details.append("Active by class:")
                for row in class_rows:
                    details.append(f"  {row['incident_class']}: {row['cnt']}")

            # Determine status
            if critical_count > 0 or halted_count > 0:
                parts = []
                if critical_count:
                    parts.append(f"{critical_count} critical")
                if halted_count:
                    parts.append(f"{halted_count} halted")
                return CheckResult(
                    name="incident_pressure",
                    status=FAIL,
                    message=f"Incident pressure high: {', '.join(parts)}",
                    details=details,
                )

            if escalated_count > 0 or exhausted_count > 0:
                parts = []
                if escalated_count:
                    parts.append(f"{escalated_count} escalated")
                if exhausted_count:
                    parts.append(f"{exhausted_count} budgets exhausted")
                return CheckResult(
                    name="incident_pressure",
                    status=WARN,
                    message=f"Incident attention needed: {', '.join(parts)}",
                    details=details,
                )

            if open_count > 0:
                return CheckResult(
                    name="incident_pressure",
                    status=PASS,
                    message=f"{open_count} open incidents — all within budget",
                    details=details,
                )

            return CheckResult(
                name="incident_pressure",
                status=PASS,
                message="No active incidents",
                details=details,
            )
    except Exception as exc:
        return CheckResult(
            name="incident_pressure",
            status=FAIL,
            message=f"Incident check error: {exc}",
        )


# ---------------------------------------------------------------------------
# Check: tmux session profile consistency
# ---------------------------------------------------------------------------

def check_tmux_profile(state_dir: Path) -> CheckResult:
    """Validate tmux session profile against canonical terminal identity.

    Checks profile file existence and structural integrity. Live tmux
    verification is skipped when tmux is not running (e.g., in tests or
    non-interactive contexts).
    """
    profile_path = state_dir / "session_profile.json"

    if not profile_path.exists():
        return CheckResult(
            name="tmux_profile",
            status=WARN,
            message="Session profile not found — tmux layout not declared",
            details=[f"Expected: {profile_path}"],
        )

    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            name="tmux_profile",
            status=FAIL,
            message=f"Session profile corrupt: {exc}",
            details=[f"Path: {profile_path}"],
        )

    # Structural validation
    details = []
    issues = []

    session_name = data.get("session_name", "")
    if not session_name:
        issues.append("Missing session_name")

    home_window = data.get("home_window")
    if not home_window:
        issues.append("Missing home_window")
    else:
        panes = home_window.get("panes", [])
        terminal_ids = [p.get("terminal_id") for p in panes]
        expected_terminals = {"T0", "T1", "T2", "T3"}
        found_terminals = set(terminal_ids)

        missing_terminals = expected_terminals - found_terminals
        if missing_terminals:
            issues.append(f"Missing home terminals: {', '.join(sorted(missing_terminals))}")

        # Check each pane has work_dir
        for pane in panes:
            tid = pane.get("terminal_id", "?")
            if not pane.get("work_dir"):
                issues.append(f"{tid}: no work_dir set (identity anchor missing)")

        details.append(f"Session: {session_name}")
        details.append(f"Home panes: {sorted(terminal_ids)}")
        details.append(f"Dynamic windows: {len(data.get('dynamic_windows', []))}")

    # Check panes.json consistency with profile
    panes_path = state_dir / "panes.json"
    if panes_path.exists():
        try:
            panes_data = json.loads(panes_path.read_text(encoding="utf-8"))
            panes_terminals = set(panes_data.get("panes", {}).keys())
            if home_window:
                profile_terminals = {p.get("terminal_id") for p in home_window.get("panes", [])}
                drift = profile_terminals - panes_terminals
                if drift:
                    issues.append(
                        f"Profile/panes.json drift: {', '.join(sorted(drift))} in profile but not in panes.json"
                    )
            details.append(f"panes.json terminals: {sorted(panes_terminals)}")
        except (json.JSONDecodeError, OSError):
            issues.append("panes.json corrupt or unreadable")
    else:
        details.append("panes.json not found (adapter mapping absent)")

    if issues:
        status = FAIL if any("Missing home" in i or "corrupt" in i for i in issues) else WARN
        return CheckResult(
            name="tmux_profile",
            status=status,
            message=f"Profile issues: {'; '.join(issues[:3])}",
            details=details + [f"Issue: {i}" for i in issues],
        )

    return CheckResult(
        name="tmux_profile",
        status=PASS,
        message=f"Session profile valid — {session_name}",
        details=details,
    )


# ---------------------------------------------------------------------------
# Check: Lease/dispatch coherence
# ---------------------------------------------------------------------------

def check_lease_dispatch_coherence(state_dir: Path) -> CheckResult:
    """Cross-validate leases and dispatches for orphan/stale references."""
    try:
        with get_connection(state_dir) as conn:
            if not _table_exists(conn, "terminal_leases") or not _table_exists(conn, "dispatches"):
                return CheckResult(
                    name="lease_dispatch_coherence",
                    status=PASS,
                    message="Tables not present — skipping coherence check",
                )

            # Find leases referencing dispatches that are in terminal states
            rows = conn.execute(
                """
                SELECT tl.terminal_id, tl.state AS lease_state, tl.dispatch_id,
                       d.state AS dispatch_state
                FROM terminal_leases tl
                LEFT JOIN dispatches d ON tl.dispatch_id = d.dispatch_id
                WHERE tl.state = 'leased' AND tl.dispatch_id IS NOT NULL
                """
            ).fetchall()

            orphans = []
            terminal_dispatch_conflicts = []

            for row in rows:
                tid = row["terminal_id"]
                dispatch_state = row["dispatch_state"]
                dispatch_id = row["dispatch_id"]

                if dispatch_state is None:
                    orphans.append(f"{tid}: leased to unknown dispatch {dispatch_id}")
                elif dispatch_state in ("completed", "expired", "dead_letter"):
                    terminal_dispatch_conflicts.append(
                        f"{tid}: leased to {dispatch_id} which is '{dispatch_state}'"
                    )

            details = []
            issues = orphans + terminal_dispatch_conflicts
            if issues:
                details.extend(issues)
                return CheckResult(
                    name="lease_dispatch_coherence",
                    status=WARN,
                    message=f"{len(issues)} lease/dispatch coherence issue(s)",
                    details=details,
                )

            return CheckResult(
                name="lease_dispatch_coherence",
                status=PASS,
                message="Lease/dispatch references consistent",
            )
    except Exception as exc:
        return CheckResult(
            name="lease_dispatch_coherence",
            status=FAIL,
            message=f"Coherence check error: {exc}",
        )


# ---------------------------------------------------------------------------
# Recovery preflight
# ---------------------------------------------------------------------------

def compute_recovery_preflight(
    state_dir: Path,
    checks: List[CheckResult],
) -> List[str]:
    """Identify blockers and warnings for vnx recover based on check results.

    Returns a list of human-readable preflight items. Empty list means
    recovery can proceed safely.
    """
    blockers: List[str] = []

    for check in checks:
        if check.name == "schema_status" and check.status == FAIL:
            blockers.append(f"BLOCKER: {check.message} — cannot recover without database")

        if check.name == "lease_health" and check.status == FAIL:
            blockers.append(f"BLOCKER: {check.message} — lease reconciliation impossible")

        if check.name == "incident_pressure" and check.status == FAIL:
            blockers.append(
                f"BLOCKER: {check.message} — resolve critical incidents before recovery"
            )

        if check.name == "lease_dispatch_coherence" and check.status == WARN:
            blockers.append(
                f"WARNING: {check.message} — vnx recover will attempt reconciliation"
            )

        if check.name == "tmux_profile" and check.status == FAIL:
            blockers.append(
                f"WARNING: {check.message} — tmux remap may not work correctly"
            )

        if check.name == "queue_health" and check.status == WARN:
            blockers.append(
                f"INFO: {check.message} — vnx recover will process stuck dispatches"
            )

    return blockers


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
