"""digest/renderer.py — Pure markdown renderer for the decisions digest.

Phase 1 (D2) scope: progress section only.
Full renderer (decisions, dream, health, queue) ships in D4.

ADR-021: pure functions, no I/O, no exceptions possible.
"""

from __future__ import annotations

from datetime import datetime, timezone

_MANUAL_PLACEHOLDER = (
    "<!-- Manual curation required — D3 auto-selector ships in 1.0.1 -->\n\n"
    "_No decisions queued. Add items manually or wait for D3._"
)


def render_progress_section(progress: dict) -> str:
    """Render yesterday's progress as a markdown table."""
    rate = progress.get("dispatch_success_rate", "n/a")
    rows = [
        ("PRs merged", str(progress.get("pr_merged", 0))),
        ("Dispatches", str(progress.get("dispatches", 0))),
        ("Success rate", str(rate)),
        ("OIs filed", str(progress.get("ois_filed", 0))),
        ("OIs closed", str(progress.get("ois_closed", 0))),
        ("Dream cycles", str(progress.get("auto_dream_cycles", 0))),
        ("Failed CI", str(progress.get("failed_ci", 0))),
    ]
    lines = [
        "## Yesterday's Progress\n",
        "| Metric | Value |",
        "|---|---|",
    ]
    for label, value in rows:
        lines.append(f"| {label} | {value} |")
    return "\n".join(lines)


def render_minimal_digest(
    progress: dict,
    manual_decisions: list[dict] | None,
) -> str:
    """Render header + manual-decisions placeholder + progress section.

    manual_decisions is reserved for D3 wiring. When None, placeholder shown.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts: list[str] = [
        f"# VNX Decisions Digest — {date_str}",
        "",
        "## Need YOUR decision (top 3)",
        "",
        _MANUAL_PLACEHOLDER,
        "",
        render_progress_section(progress),
    ]
    return "\n".join(parts) + "\n"
