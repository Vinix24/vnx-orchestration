#!/usr/bin/env python3
"""
VNX Headless Inspection — Operator-facing views for headless run state.

PR-3 deliverable: provides structured inspection commands so operators can
diagnose headless runs without manual file spelunking.

Views:
  - list_runs:       Tabular overview of recent/active/failed runs
  - inspect_run:     Detailed single-run view with classification and artifacts
  - operator_summary: Dashboard-style summary of headless health
  - format_run_line: Single-line formatter for list views

Contract references:
  O-1 through O-10: Operator observability operations
  G-R2: Operator must inspect failed/hung runs without guesswork
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from headless_run_registry import (
    HeadlessRunRegistry,
    HeadlessRun,
    TERMINAL_STATES,
    _seconds_since,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_ICONS = {
    "init": ".",
    "running": ">",
    "completing": "~",
    "failing": "!",
    "succeeded": "+",
    "failed": "X",
}

FAILURE_CLASS_LABELS = {
    "SUCCESS": "OK",
    "TIMEOUT": "Timed out",
    "NO_OUTPUT": "No output (hang)",
    "INTERRUPTED": "Interrupted (signal)",
    "INFRA_FAIL": "Infrastructure failure",
    "TOOL_FAIL": "Tool/API failure",
    "PROMPT_ERR": "Prompt error",
    "UNKNOWN": "Unknown failure",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _elapsed(iso_ts: Optional[str]) -> str:
    """Return human-readable elapsed time from an ISO timestamp."""
    if not iso_ts:
        return "-"
    secs = _seconds_since(iso_ts)
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.0f}m"
    return f"{secs / 3600:.1f}h"


def _ago(iso_ts: Optional[str]) -> str:
    """Return 'Xs ago' from an ISO timestamp."""
    if not iso_ts:
        return "never"
    secs = _seconds_since(iso_ts)
    if secs < 60:
        return f"{secs:.0f}s ago"
    if secs < 3600:
        return f"{secs / 60:.0f}m ago"
    return f"{secs / 3600:.1f}h ago"


def _short_id(full_id: Optional[str]) -> str:
    """Shorten a UUID to first 8 chars for display."""
    if not full_id:
        return "-"
    return full_id[:8]


def _ts_display(iso_ts: Optional[str]) -> str:
    """Format an ISO timestamp for display."""
    if not iso_ts:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return iso_ts[:19] if len(iso_ts) >= 19 else iso_ts


# ---------------------------------------------------------------------------
# Single-run formatters
# ---------------------------------------------------------------------------

def format_run_line(run: HeadlessRun) -> str:
    """Format a single run as a one-line summary for list views.

    Format: [icon] run_id(short) | state | dispatch | elapsed | exit_class | hint
    """
    icon = STATE_ICONS.get(run.state, "?")
    rid = _short_id(run.run_id)
    did = _short_id(run.dispatch_id)

    if run.state in TERMINAL_STATES:
        elapsed = f"{run.duration_seconds:.1f}s" if run.duration_seconds else "-"
    else:
        elapsed = _elapsed(run.started_at)

    fc = run.failure_class or "-"
    parts = [
        f"[{icon}]",
        rid,
        f"state={run.state}",
        f"dispatch={did}",
        f"elapsed={elapsed}",
    ]
    if run.failure_class and run.failure_class != "SUCCESS":
        parts.append(f"exit={fc}")

    return " | ".join(parts)


def format_run_detail(run: HeadlessRun) -> str:
    """Format a detailed single-run view for operator inspection."""
    lines = [
        f"Headless Run: {run.run_id}",
        f"{'=' * 60}",
        f"  State:           {run.state}",
        f"  Dispatch:        {run.dispatch_id}",
        f"  Attempt:         {run.attempt_id}",
        f"  Target:          {run.target_id} ({run.target_type})",
        f"  Task Class:      {run.task_class}",
        f"  Terminal:        {run.terminal_id or '-'}",
        "",
        f"  Process:",
        f"    PID:           {run.pid or '-'}",
        f"    PGID:          {run.pgid or '-'}",
        "",
        f"  Timestamps:",
        f"    Started:       {_ts_display(run.started_at)}",
        f"    Subprocess:    {_ts_display(run.subprocess_started_at)}",
        f"    Heartbeat:     {_ts_display(run.heartbeat_at)} ({_ago(run.heartbeat_at)})",
        f"    Last Output:   {_ts_display(run.last_output_at)} ({_ago(run.last_output_at)})",
        f"    Completed:     {_ts_display(run.completed_at)}",
    ]

    if run.duration_seconds is not None:
        lines.append(f"    Duration:      {run.duration_seconds:.1f}s")

    lines.append("")
    lines.append(f"  Exit:")
    lines.append(f"    Code:          {run.exit_code if run.exit_code is not None else '-'}")
    lines.append(f"    Class:         {run.failure_class or '-'}")
    if run.failure_class and run.failure_class in FAILURE_CLASS_LABELS:
        lines.append(f"    Label:         {FAILURE_CLASS_LABELS[run.failure_class]}")

    lines.append("")
    lines.append(f"  Artifacts:")
    lines.append(f"    Log:           {run.log_artifact_path or '-'}")
    lines.append(f"    Output:        {run.output_artifact_path or '-'}")
    lines.append(f"    Receipt:       {run.receipt_id or '-'}")

    # Liveness warnings for running runs
    if run.state == "running":
        warnings = []
        if run.heartbeat_at:
            hb_age = _seconds_since(run.heartbeat_at)
            if hb_age > 60:
                warnings.append(f"STALE HEARTBEAT: last pulse {hb_age:.0f}s ago (threshold: 60s)")
        if run.last_output_at:
            out_age = _seconds_since(run.last_output_at)
            if out_age > 120:
                warnings.append(f"NO OUTPUT: silent for {out_age:.0f}s (threshold: 120s)")
        if warnings:
            lines.append("")
            lines.append("  Warnings:")
            for w in warnings:
                lines.append(f"    [!] {w}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# List views
# ---------------------------------------------------------------------------

def list_runs(
    registry: HeadlessRunRegistry,
    *,
    filter_state: Optional[str] = None,
    show_failed: bool = False,
    show_active: bool = False,
    show_problems: bool = False,
    limit: int = 20,
) -> List[str]:
    """Return formatted run lines based on filter criteria.

    Args:
        registry:      HeadlessRunRegistry instance.
        filter_state:  Show only runs in this state.
        show_failed:   Show only failed runs.
        show_active:   Show only active (running) runs.
        show_problems: Show stale + hung runs.
        limit:         Max runs to return.

    Returns:
        List of formatted run lines.
    """
    if show_problems:
        stale = registry.list_stale()
        hung = registry.list_hung()
        seen = set()
        runs = []
        for r in stale + hung:
            if r.run_id not in seen:
                seen.add(r.run_id)
                runs.append(r)
        return [format_run_line(r) for r in runs[:limit]]

    if show_active:
        runs = registry.list_active()
        return [format_run_line(r) for r in runs[:limit]]

    if show_failed:
        runs = registry.list_by_state("failed")
        return [format_run_line(r) for r in runs[:limit]]

    if filter_state:
        runs = registry.list_by_state(filter_state)
        return [format_run_line(r) for r in runs[:limit]]

    runs = registry.list_recent(limit=limit)
    return [format_run_line(r) for r in runs]


# ---------------------------------------------------------------------------
# Operator summary
# ---------------------------------------------------------------------------

@dataclass
class HealthSummary:
    """Aggregated health view for operator dashboard."""
    total_runs: int = 0
    active_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    stale_count: int = 0
    hung_count: int = 0
    failure_class_counts: Dict[str, int] = field(default_factory=dict)
    recent_failures: List[HeadlessRun] = field(default_factory=list)
    problem_runs: List[HeadlessRun] = field(default_factory=list)

    @property
    def has_problems(self) -> bool:
        return self.stale_count > 0 or self.hung_count > 0

    @property
    def status_label(self) -> str:
        if self.has_problems:
            return "ATTENTION"
        if self.failed_count > 0 and self.active_count == 0:
            return "DEGRADED"
        if self.active_count > 0:
            return "ACTIVE"
        return "IDLE"


def build_health_summary(registry: HeadlessRunRegistry) -> HealthSummary:
    """Build an aggregated health summary from current registry state."""
    summary = HealthSummary()

    recent = registry.list_recent(limit=100)
    summary.total_runs = len(recent)

    active = registry.list_active()
    summary.active_count = len(active)

    succeeded = registry.list_by_state("succeeded")
    summary.succeeded_count = len(succeeded)

    failed = registry.list_by_state("failed")
    summary.failed_count = len(failed)
    summary.recent_failures = failed[:5]

    stale = registry.list_stale()
    summary.stale_count = len(stale)

    hung = registry.list_hung()
    summary.hung_count = len(hung)

    # Deduplicate problem runs
    seen = set()
    for r in stale + hung:
        if r.run_id not in seen:
            seen.add(r.run_id)
            summary.problem_runs.append(r)

    # Count failure classes from recent failures
    for r in failed:
        fc = r.failure_class or "UNKNOWN"
        summary.failure_class_counts[fc] = summary.failure_class_counts.get(fc, 0) + 1

    return summary


def format_health_summary(summary: HealthSummary) -> str:
    """Format the health summary for operator display."""
    lines = [
        "VNX Headless Health Summary",
        "=" * 40,
        f"  Status:     {summary.status_label}",
        f"  Total runs: {summary.total_runs}",
        f"  Active:     {summary.active_count}",
        f"  Succeeded:  {summary.succeeded_count}",
        f"  Failed:     {summary.failed_count}",
        "",
    ]

    if summary.has_problems:
        lines.append("Problems:")
        if summary.stale_count > 0:
            lines.append(f"  [!] {summary.stale_count} run(s) with stale heartbeat")
        if summary.hung_count > 0:
            lines.append(f"  [!] {summary.hung_count} run(s) with no recent output")
        lines.append("")
        for r in summary.problem_runs:
            lines.append(f"  {format_run_line(r)}")
        lines.append("")

    if summary.failure_class_counts:
        lines.append("Failure breakdown:")
        for fc, count in sorted(summary.failure_class_counts.items()):
            label = FAILURE_CLASS_LABELS.get(fc, fc)
            lines.append(f"  {fc}: {count} ({label})")
        lines.append("")

    if summary.recent_failures:
        lines.append("Recent failures:")
        for r in summary.recent_failures:
            lines.append(f"  {format_run_line(r)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI entry point called from headless.sh."""
    import argparse

    parser = argparse.ArgumentParser(
        description="VNX Headless Run Inspection",
        prog="vnx headless",
    )
    sub = parser.add_subparsers(dest="command")

    # vnx headless list
    p_list = sub.add_parser("list", help="List headless runs")
    p_list.add_argument("--state", help="Filter by state")
    p_list.add_argument("--active", action="store_true", help="Show active runs only")
    p_list.add_argument("--failed", action="store_true", help="Show failed runs only")
    p_list.add_argument("--problems", action="store_true", help="Show stale/hung runs")
    p_list.add_argument("--limit", type=int, default=20, help="Max runs to show")

    # vnx headless inspect <run_id>
    p_inspect = sub.add_parser("inspect", help="Inspect a single headless run")
    p_inspect.add_argument("run_id", help="Run ID (full or prefix)")

    # vnx headless summary
    sub.add_parser("summary", help="Show headless health summary")

    parser.add_argument(
        "--state-dir",
        default=os.environ.get("VNX_STATE_DIR", ""),
        help="Path to runtime state directory",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    state_dir = Path(args.state_dir) if args.state_dir else None
    if not state_dir or not state_dir.exists():
        print("[headless] State directory not found. Set VNX_STATE_DIR.", file=sys.stderr)
        return 1

    registry = HeadlessRunRegistry(state_dir)

    if args.command == "list":
        lines = list_runs(
            registry,
            filter_state=args.state,
            show_active=args.active,
            show_failed=args.failed,
            show_problems=args.problems,
            limit=args.limit,
        )
        if not lines:
            print("No headless runs found.")
        else:
            for line in lines:
                print(line)
        return 0

    if args.command == "inspect":
        run = _resolve_run(registry, args.run_id)
        if run is None:
            print(f"[headless] Run not found: {args.run_id}", file=sys.stderr)
            return 1
        if args.json_output:
            print(json.dumps(_run_to_dict(run), indent=2))
        else:
            print(format_run_detail(run))
        return 0

    if args.command == "summary":
        summary = build_health_summary(registry)
        if args.json_output:
            print(json.dumps({
                "status": summary.status_label,
                "total_runs": summary.total_runs,
                "active": summary.active_count,
                "succeeded": summary.succeeded_count,
                "failed": summary.failed_count,
                "stale": summary.stale_count,
                "hung": summary.hung_count,
                "has_problems": summary.has_problems,
                "failure_classes": summary.failure_class_counts,
            }, indent=2))
        else:
            print(format_health_summary(summary))
        return 0

    parser.print_help()
    return 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_run(
    registry: HeadlessRunRegistry,
    run_id_or_prefix: str,
) -> Optional[HeadlessRun]:
    """Resolve a run by full ID or prefix match."""
    run = registry.get(run_id_or_prefix)
    if run:
        return run

    # Try prefix match against recent runs
    recent = registry.list_recent(limit=100)
    matches = [r for r in recent if r.run_id.startswith(run_id_or_prefix)]
    if len(matches) == 1:
        return matches[0]
    return None


def _run_to_dict(run: HeadlessRun) -> Dict[str, Any]:
    """Convert a HeadlessRun to a JSON-serializable dict."""
    return {
        "run_id": run.run_id,
        "dispatch_id": run.dispatch_id,
        "attempt_id": run.attempt_id,
        "target_id": run.target_id,
        "target_type": run.target_type,
        "task_class": run.task_class,
        "terminal_id": run.terminal_id,
        "pid": run.pid,
        "pgid": run.pgid,
        "state": run.state,
        "failure_class": run.failure_class,
        "exit_code": run.exit_code,
        "started_at": run.started_at,
        "subprocess_started_at": run.subprocess_started_at,
        "heartbeat_at": run.heartbeat_at,
        "last_output_at": run.last_output_at,
        "completed_at": run.completed_at,
        "duration_seconds": run.duration_seconds,
        "log_artifact_path": run.log_artifact_path,
        "output_artifact_path": run.output_artifact_path,
        "receipt_id": run.receipt_id,
    }


if __name__ == "__main__":
    sys.exit(main())
