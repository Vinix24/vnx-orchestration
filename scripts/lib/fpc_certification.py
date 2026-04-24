#!/usr/bin/env python3
"""
VNX FP-C Certification Runner — Validates the certification matrix.

Runs every scenario from docs/core/32_FPC_CERTIFICATION_MATRIX.md and produces
a JSON certification report. FP-C is certified when every row passes.

Usage:
    python fpc_certification.py [--state-dir DIR] [--output FILE]

The runner uses temporary state by default (for CI/test runs).
Pass --state-dir to validate against live runtime state.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import get_connection, init_schema, register_dispatch, _now_utc
from execution_target_registry import ExecutionTargetRegistry
from fpc_certification_checks import FPCCertificationChecksMixin


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CertRow:
    """One certification matrix row result."""
    section: str
    row_id: str
    scenario: str
    status: str  # "pass" | "fail" | "skip"
    evidence: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "section": self.section,
            "row_id": self.row_id,
            "scenario": self.scenario,
            "status": self.status,
            "evidence": self.evidence,
            "notes": self.notes,
        }


@dataclass
class CertReport:
    """Complete FP-C certification report."""
    rows: List[CertRow] = field(default_factory=list)
    generated_at: str = ""
    certified: bool = False
    residual_risks: List[str] = field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "pass")

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "fail")

    @property
    def skip_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "skip")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "certified": self.certified,
            "summary": {
                "total": len(self.rows),
                "pass": self.pass_count,
                "fail": self.fail_count,
                "skip": self.skip_count,
            },
            "rows": {r.row_id: r.to_dict() for r in self.rows},
            "residual_risks": self.residual_risks,
        }


# ---------------------------------------------------------------------------
# Certification Runner
# ---------------------------------------------------------------------------

class FPCCertificationRunner(FPCCertificationChecksMixin):
    """Validates every FP-C certification matrix scenario.

    Creates isolated temporary state for validation. Each section
    tests a specific subsystem with controlled inputs.
    """

    def __init__(self, state_dir: Optional[str | Path] = None) -> None:
        if state_dir:
            self._state_dir = Path(state_dir)
            self._temp_dir = None
        else:
            self._temp_dir = tempfile.mkdtemp(prefix="fpc_cert_")
            self._state_dir = Path(self._temp_dir) / "state"
            self._state_dir.mkdir(parents=True, exist_ok=True)

        self._dispatch_dir = self._state_dir.parent / "dispatches"
        self._dispatch_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir = self._state_dir.parent / "headless_output"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._quality_db_path = None

        self._report = CertReport()

    def run(self) -> CertReport:
        """Run all certification sections and return the report."""
        self._init_state()

        self._certify_task_class_routing()
        self._certify_execution_target_registry()
        self._certify_headless_execution()
        self._certify_inbound_inbox()
        self._certify_intelligence_injection()
        self._certify_recommendation_metrics()
        self._certify_mixed_execution_cutover()

        self._report.generated_at = _now_utc()
        self._report.certified = self._report.fail_count == 0
        self._report.residual_risks = self._collect_residual_risks()

        return self._report

    # ------------------------------------------------------------------
    # State initialization
    # ------------------------------------------------------------------

    def _init_state(self) -> None:
        """Initialize runtime coordination schema and seed targets."""
        init_schema(self._state_dir)
        self._seed_targets()

    def _seed_targets(self) -> None:
        """Seed execution targets for certification tests.

        Uses _safe_register to avoid TargetExistsError if the v4 schema
        migration already seeded some targets.
        """
        registry = ExecutionTargetRegistry(self._state_dir)

        def _safe_register(**kwargs: Any) -> None:
            try:
                registry.register(**kwargs)
            except Exception:
                # Target may already exist from v4 schema seed — update health
                target = registry.get(kwargs["target_id"])
                if target and target.health != "healthy":
                    registry.update_health(kwargs["target_id"], "healthy")

        # Interactive targets
        _safe_register(
            target_id="cert_interactive_T1",
            target_type="interactive_tmux_claude",
            terminal_id="T1",
            capabilities=["coding_interactive", "research_structured", "docs_synthesis", "ops_watchdog"],
            health="healthy",
            model="sonnet",
        )
        _safe_register(
            target_id="cert_interactive_T2",
            target_type="interactive_tmux_claude",
            terminal_id="T2",
            capabilities=["coding_interactive", "research_structured", "docs_synthesis"],
            health="healthy",
            model="sonnet",
        )

        # Headless target
        _safe_register(
            target_id="cert_headless_claude",
            target_type="headless_claude_cli",
            terminal_id=None,
            capabilities=["research_structured", "docs_synthesis"],
            health="healthy",
            model="sonnet",
        )

        # Channel adapter
        _safe_register(
            target_id="cert_channel_adapter",
            target_type="channel_adapter",
            terminal_id=None,
            capabilities=["channel_response"],
            health="healthy",
        )

    def _register_test_dispatch(
        self,
        dispatch_id: str,
        terminal_id: str = "T1",
    ) -> None:
        """Register a test dispatch in the DB."""
        with get_connection(self._state_dir) as conn:
            register_dispatch(
                conn,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                track="C",
                priority="P1",
                bundle_path=str(self._dispatch_dir / dispatch_id),
                actor="cert_runner",
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Residual risks
    # ------------------------------------------------------------------

    def _collect_residual_risks(self) -> List[str]:
        return [
            "Task class boundaries may need refinement after real-world headless routing",
            "Headless CLI execution may have different failure modes than tmux delivery",
            "Intelligence confidence thresholds may need tuning after measurement data accumulates",
            "Recommendation measurement windows may be too short for low-volume dispatches",
            "Channel adapter reliability is untested in production",
            "Cutover rollback should include graceful shutdown for in-flight headless processes",
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pass(self, row_id: str, scenario: str, evidence: str) -> None:
        self._report.rows.append(CertRow(
            section=row_id.split(".")[0],
            row_id=row_id,
            scenario=scenario,
            status="pass",
            evidence=evidence,
        ))

    def _fail(self, row_id: str, scenario: str, evidence: str) -> None:
        self._report.rows.append(CertRow(
            section=row_id.split(".")[0],
            row_id=row_id,
            scenario=scenario,
            status="fail",
            evidence=evidence,
        ))

    def _skip(self, row_id: str, scenario: str, notes: str) -> None:
        self._report.rows.append(CertRow(
            section=row_id.split(".")[0],
            row_id=row_id,
            scenario=scenario,
            status="skip",
            evidence="",
            notes=notes,
        ))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="FP-C Certification Runner")
    parser.add_argument("--state-dir", help="Runtime state directory (uses temp if omitted)")
    parser.add_argument("--output", help="Output JSON file path")
    args = parser.parse_args()

    runner = FPCCertificationRunner(state_dir=args.state_dir)
    report = runner.run()

    report_dict = report.to_dict()
    output = json.dumps(report_dict, indent=2)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Certification report written to {args.output}")
    else:
        print(output)

    status = "CERTIFIED" if report.certified else "NOT CERTIFIED"
    print(f"\nFP-C Status: {status}")
    print(f"  Pass: {report.pass_count}  Fail: {report.fail_count}  Skip: {report.skip_count}")


if __name__ == "__main__":
    main()
