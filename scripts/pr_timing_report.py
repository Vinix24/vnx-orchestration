#!/usr/bin/env python3
"""pr_timing_report.py — Wave 5 PR-5.x: retrospective timing analysis.

Usage:
  python3 scripts/pr_timing_report.py --pr 522
  python3 scripts/pr_timing_report.py --since 2026-05-15
  python3 scripts/pr_timing_report.py --dispatch 20260516-wave5-pr1-aggregator
  python3 scripts/pr_timing_report.py --since 2026-05-15 --output claudedocs/timing-report.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Resolve project root for imports
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "lib"))

from timing_metrics import TimingMetrics, analyze_dispatch, DEFAULT_ACTIVE_THRESHOLD_SECONDS


COST_BENCHMARK_HOURLY = 125.0  # € per hour (Vincent's internal benchmark)


def _vnx_data_dir() -> Path:
    env_val = os.environ.get("VNX_DATA_DIR", "")
    if env_val:
        candidate = Path(env_val)
        if candidate.exists():
            return candidate
    default = _HERE.parent / ".vnx-data"
    return default


def _archive_root(data_dir: Path) -> Path:
    return data_dir / "events" / "archive"


def _receipts_path(data_dir: Path) -> Path:
    return data_dir / "state" / "t0_receipts.ndjson"


def _load_receipts(receipts_path: Path) -> List[Dict]:
    if not receipts_path.exists():
        return []
    receipts: List[Dict] = []
    with open(receipts_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                receipts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return receipts


def _dispatch_ids_for_pr(pr_number: str, receipts: List[Dict]) -> List[str]:
    """Map a PR number to its dispatch IDs by scanning receipts."""
    norm = str(pr_number).lstrip("PR-pr").strip()
    seen: List[str] = []
    seen_set: set = set()
    targets = {f"PR-{norm}", norm, f"pr-{norm}"}
    for r in receipts:
        pr_id = str(r.get("pr_id") or r.get("pr") or "")
        dispatch_id = str(r.get("dispatch_id") or "").strip()
        if not dispatch_id or dispatch_id in seen_set:
            continue
        if pr_id in targets or pr_id.lstrip("PR-pr") == norm:
            seen.append(dispatch_id)
            seen_set.add(dispatch_id)
    return seen


def _dispatch_ids_since(since_iso: str, receipts: List[Dict]) -> List[str]:
    """Return dispatch IDs whose receipts have timestamps >= since_iso."""
    try:
        cutoff = datetime.fromisoformat(since_iso).replace(tzinfo=timezone.utc)
    except ValueError:
        sys.exit(f"Invalid date format for --since: {since_iso!r}. Use YYYY-MM-DD.")
    seen: List[str] = []
    seen_set: set = set()
    for r in receipts:
        ts_raw = str(r.get("timestamp") or "").strip()
        dispatch_id = str(r.get("dispatch_id") or "").strip()
        if not dispatch_id or dispatch_id in seen_set:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.astimezone(timezone.utc) >= cutoff:
            seen.append(dispatch_id)
            seen_set.add(dispatch_id)
    return seen


def _format_seconds(secs: float) -> str:
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.0f}m"
    return f"{secs / 3600:.1f}h"


def _effective_rate(effective_seconds: float) -> str:
    if effective_seconds <= 0:
        return "n/a"
    hours = effective_seconds / 3600
    rate = COST_BENCHMARK_HOURLY / hours if hours > 0 else 0
    return f"€{rate:.0f}/u"


def _build_dispatch_block(
    dispatch_id: str,
    archive_root: Path,
    threshold: float,
) -> Optional[TimingMetrics]:
    return analyze_dispatch(dispatch_id, archive_root, threshold)


def _render_pr_section(
    pr_label: str,
    dispatch_ids: List[str],
    metrics_list: List[TimingMetrics],
) -> str:
    lines: List[str] = []
    lines.append(f"PR {pr_label}")

    if not metrics_list:
        lines.append("  No archived event streams found for this PR.")
        return "\n".join(lines)

    total_walltime = sum(m.walltime_seconds for m in metrics_list)
    total_effective = sum(m.effective_seconds for m in metrics_list)
    total_events = sum(m.event_count for m in metrics_list)
    all_parallel = list({p for m in metrics_list for p in m.parallel_dispatch_ids})
    total_parallel_secs = sum(m.parallel_seconds for m in metrics_list)

    active_ratio = (total_effective / total_walltime * 100) if total_walltime > 0 else 0
    lines.append(f"  Dispatches: {len(metrics_list)}")
    lines.append(f"  Walltime sum: {_format_seconds(total_walltime)}")
    lines.append(f"  Effective sum: {_format_seconds(total_effective)}")
    lines.append(f"  Active ratio: {active_ratio:.0f}%")
    if all_parallel:
        lines.append(f"  Parallel with: {', '.join(all_parallel[:5])}")
    lines.append(f"  Parallel seconds: {total_parallel_secs:.0f}s")
    lines.append(f"  Effective rate: {_effective_rate(total_effective)} "
                 f"(at €{COST_BENCHMARK_HOURLY:.0f}/u-benchmark, {active_ratio:.0f}% effective)")
    return "\n".join(lines)


def _render_aggregate(all_metrics: List[TimingMetrics]) -> str:
    if not all_metrics:
        return "No metrics available."
    total_wt = sum(m.walltime_seconds for m in all_metrics)
    total_eff = sum(m.effective_seconds for m in all_metrics)
    ratio = (total_eff / total_wt * 100) if total_wt > 0 else 0
    lines = [
        "",
        "## Aggregate",
        f"  Total dispatches analyzed: {len(all_metrics)}",
        f"  Total walltime: {_format_seconds(total_wt)}",
        f"  Total effective: {_format_seconds(total_eff)}",
        f"  Overall active ratio: {ratio:.0f}%",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retrospective timing report for VNX dispatches / PRs."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", metavar="NUMBER", help="Single PR number to analyze")
    group.add_argument("--since", metavar="DATE", help="Analyze all dispatches since YYYY-MM-DD")
    group.add_argument("--dispatch", metavar="DISPATCH_ID", help="Single dispatch ID to analyze")
    parser.add_argument("--output", metavar="PATH", help="Write markdown report to file")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_ACTIVE_THRESHOLD_SECONDS,
        help=f"Active gap cap in seconds (default: {DEFAULT_ACTIVE_THRESHOLD_SECONDS})",
    )
    args = parser.parse_args()

    data_dir = _vnx_data_dir()
    archive_root = _archive_root(data_dir)
    receipts = _load_receipts(_receipts_path(data_dir))

    sections: List[str] = []
    all_metrics: List[TimingMetrics] = []

    if args.dispatch:
        m = _build_dispatch_block(args.dispatch, archive_root, args.threshold)
        metrics_list = [m] if m else []
        all_metrics.extend(metrics_list)
        sections.append(_render_pr_section(f"dispatch/{args.dispatch}", [args.dispatch], metrics_list))

    elif args.pr:
        dispatch_ids = _dispatch_ids_for_pr(args.pr, receipts)
        if not dispatch_ids:
            print(f"No dispatch IDs found for PR {args.pr} in receipts.")
        metrics_list = [
            m for d in dispatch_ids
            for m in [_build_dispatch_block(d, archive_root, args.threshold)]
            if m is not None
        ]
        all_metrics.extend(metrics_list)
        sections.append(_render_pr_section(f"#{args.pr}", dispatch_ids, metrics_list))

    elif args.since:
        dispatch_ids = _dispatch_ids_since(args.since, receipts)
        by_pr: Dict[str, Tuple[List[str], List[TimingMetrics]]] = {}
        for did in dispatch_ids:
            pr_id = "unknown"
            for r in receipts:
                if str(r.get("dispatch_id") or "") == did:
                    pr_id = str(r.get("pr_id") or r.get("pr") or "unknown")
                    break
            m = _build_dispatch_block(did, archive_root, args.threshold)
            ids_list, metrics_list = by_pr.setdefault(pr_id, ([], []))
            ids_list.append(did)
            if m is not None:
                metrics_list.append(m)
                all_metrics.append(m)
        for pr_id, (ids_list, metrics_list) in sorted(by_pr.items()):
            sections.append(_render_pr_section(f"#{pr_id}", ids_list, metrics_list))

    output_lines = "\n".join(sections) + _render_aggregate(all_metrics)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_lines + "\n", encoding="utf-8")
        print(f"Report written to {out_path}")
    else:
        print(output_lines)


if __name__ == "__main__":
    main()
