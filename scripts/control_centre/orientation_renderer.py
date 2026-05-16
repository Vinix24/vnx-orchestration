"""orientation_renderer.py — Control Centre pool-status rendering.

Reads pool_state_unified from the aggregator DB and renders a markdown table
for Control Centre orientation output.

Wave 6 PR-6.8 — ADR-018 Control Centre pool-integration.
"""

from __future__ import annotations

from pathlib import Path

from scripts.control_centre.pool_supervisor import list_all_pools


def render_pool_table(aggregator_db: Path) -> str:
    """Render a markdown table of cross-project pool status.

    Returns a plain-text fallback when no pools are registered.
    """
    pools = list_all_pools(aggregator_db)
    if not pools:
        return "Geen actieve pools.\n"

    header = [
        "## Pool status (cross-project)\n",
        "| Project | Pool | Active | Min | Max | Policy |",
        "|---|---|---|---|---|---|",
    ]
    rows = [
        f"| {p['project_id']} | {p['pool_id']} | {p['active_count']} "
        f"| {p['min_workers']} | {p['max_workers']} | {p['scaling_policy']} |"
        for p in pools
    ]
    return "\n".join(header + rows) + "\n"
