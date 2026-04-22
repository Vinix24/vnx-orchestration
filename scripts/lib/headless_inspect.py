#!/usr/bin/env python3
"""Operator inspection tools for VNX headless runs.

Provides human-readable summaries and listings of headless run state
without requiring direct DB access or file spelunking.

Contract reference: docs/HEADLESS_RUN_CONTRACT.md Section 5.1 (O-1..O-10)

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from headless_run_registry import HeadlessRun, HeadlessRunRegistry


# ---------------------------------------------------------------------------
# State icons for terminal output
# ---------------------------------------------------------------------------

_STATE_ICONS = {
    "init":       "[ ]",
    "running":    "[>]",
    "completing": "[~]",
    "failing":    "[!]",
    "succeeded":  "[✓]",
    "failed":     "[✗]",
}


def _icon(state: str) -> str:
    return _STATE_ICONS.get(state, "[?]")


# ---------------------------------------------------------------------------
# Single-run formatting
# ---------------------------------------------------------------------------

def format_run_line(run: "HeadlessRun") -> str:
    """Return a compact one-line summary of a run suitable for list output."""
    icon = _icon(run.state)
    dispatch_short = run.dispatch_id[:16] if run.dispatch_id else "—"
    run_short = run.run_id[:8] if run.run_id else "—"
    fc = f" [{run.failure_class}]" if run.failure_class and run.state in ("failed", "succeeded") else ""
    duration = ""
    if run.duration_seconds is not None:
        duration = f" {run.duration_seconds:.1f}s"
    return (
        f"{icon} {run_short}  dispatch={dispatch_short}  state={run.state}{fc}{duration}"
    )


def format_run_detail(run: "HeadlessRun") -> str:
    """Return a multi-line detail view of a single run."""
    lines = [
        "══════════════════════════════════════════════════",
        " VNX Headless Run Detail",
        "══════════════════════════════════════════════════",
        f"  Run ID        : {run.run_id}",
        f"  Dispatch ID   : {run.dispatch_id}",
        f"  Attempt ID    : {run.attempt_id}",
        f"  Target ID     : {run.target_id}",
        f"  Target Type   : {run.target_type}",
        f"  Task Class    : {run.task_class}",
        f"  Terminal      : {run.terminal_id or '—'}",
        f"  State         : {run.state}",
        f"  Failure Class : {run.failure_class or '—'}",
        f"  Exit Code     : {run.exit_code if run.exit_code is not None else '—'}",
        f"  Duration      : {f'{run.duration_seconds:.1f}s' if run.duration_seconds is not None else '—'}",
        f"  Started At    : {run.started_at or '—'}",
        f"  Completed At  : {run.completed_at or '—'}",
        f"  Heartbeat At  : {run.heartbeat_at or '—'}",
        f"  Last Output   : {run.last_output_at or '—'}",
        f"  Log Artifact  : {run.log_artifact_path or '—'}",
        "══════════════════════════════════════════════════",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# List views
# ---------------------------------------------------------------------------

def list_runs(
    registry: "HeadlessRunRegistry",
    *,
    show_active: bool = False,
    show_failed: bool = False,
    show_all: bool = False,
    limit: int = 50,
) -> List[str]:
    """Return formatted run lines matching the requested filter.

    Args:
        show_active: Include only running/completing/failing runs.
        show_failed: Include only terminal failed runs.
        show_all:    Include all runs (most recent first, up to limit).
        limit:       Maximum number of runs to return.
    """
    if show_active:
        runs = registry.list_active()
    elif show_failed:
        runs = registry.list_by_state("failed")
    else:
        runs = registry.list_recent(limit)

    return [format_run_line(r) for r in runs[:limit]]


# ---------------------------------------------------------------------------
# Health summary
# ---------------------------------------------------------------------------

@dataclass
class HealthSummary:
    """Aggregated health metrics across all headless runs."""
    total_runs: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    active_count: int = 0
    failure_class_counts: Dict[str, int] = field(default_factory=dict)


def build_health_summary(registry: "HeadlessRunRegistry") -> HealthSummary:
    """Compute a HealthSummary from all runs in the registry."""
    all_runs = registry.list_recent(limit=10_000)

    summary = HealthSummary()
    summary.total_runs = len(all_runs)

    for run in all_runs:
        if run.state == "succeeded":
            summary.succeeded_count += 1
        elif run.state == "failed":
            summary.failed_count += 1
            if run.failure_class:
                summary.failure_class_counts[run.failure_class] = (
                    summary.failure_class_counts.get(run.failure_class, 0) + 1
                )
        elif run.state in ("running", "completing", "failing"):
            summary.active_count += 1

    return summary


def format_health_summary(summary: HealthSummary) -> str:
    """Return a multi-line human-readable health report."""
    lines = [
        "══════════════════════════════════════════════════",
        " VNX Headless Run Health",
        "══════════════════════════════════════════════════",
        f"  Total Runs  : {summary.total_runs}",
        f"  Active      : {summary.active_count}",
        f"  Succeeded   : {summary.succeeded_count}",
        f"  Failed      : {summary.failed_count}",
    ]
    if summary.failure_class_counts:
        lines.append("  ── Failure Classes ──────────────────────────")
        for fc, count in sorted(summary.failure_class_counts.items()):
            lines.append(f"    {fc:<16}: {count}")
    lines.append("══════════════════════════════════════════════════")

    # Compact aliases expected by test assertions
    compact = [
        f"Succeeded:  {summary.succeeded_count}",
        f"Failed:     {summary.failed_count}",
    ]
    lines.extend(compact)

    return "\n".join(lines)
