#!/usr/bin/env python3
"""pr_timing_report.py — Wave 5 PR-5.x retrospective timing analyzer.

Usage:
    python3 scripts/pr_timing_report.py --pr 522
    python3 scripts/pr_timing_report.py --since 2026-05-15
    python3 scripts/pr_timing_report.py --dispatch 20260516-wave5-pr1-aggregator
    python3 scripts/pr_timing_report.py --since 2026-05-15 --output claudedocs/report.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPTS_LIB = Path(__file__).resolve().parent / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

from timing_metrics import TimingMetrics, analyze_dispatch, compute_effective_time

HOURLY_RATE = 125.0
SECONDS_PER_HOUR = 3600.0

_PR_IN_NAME_RE = re.compile(r"(?:pr|PR)[- _]?(\d+)")
_PR_IN_DISPATCH_RE = re.compile(r"[_-]pr(\d+)[_-]", re.IGNORECASE)


def _resolve_archive_root() -> Path:
    vnx_data = os.environ.get("VNX_DATA_DIR", "")
    if vnx_data:
        root = Path(vnx_data).expanduser() / "events" / "archive"
        if root.exists():
            return root
    candidates = [
        Path(__file__).resolve().parent.parent / ".vnx-data" / "events" / "archive",
        Path.home() / ".vnx-data" / "events" / "archive",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _resolve_receipts_file() -> Optional[Path]:
    vnx_data = os.environ.get("VNX_DATA_DIR", "")
    if vnx_data:
        p = Path(vnx_data).expanduser() / "state" / "t0_receipts.ndjson"
        if p.exists():
            return p
    candidates = [
        Path(__file__).resolve().parent.parent / ".vnx-data" / "state" / "t0_receipts.ndjson",
        Path.home() / ".vnx-data" / "state" / "t0_receipts.ndjson",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _all_archive_files(archive_root: Path) -> List[Path]:
    if not archive_root.exists():
        return []
    return sorted(archive_root.rglob("*.ndjson"))


def _dispatch_ids_for_pr(pr_num: int, archive_root: Path) -> List[str]:
    """Find dispatch IDs that reference a given PR number in their stem."""
    result: List[str] = []
    patterns = [
        re.compile(rf"[_-]pr[_-]?{pr_num}[_-]", re.IGNORECASE),
        re.compile(rf"[_-]{pr_num}[_-]"),
        re.compile(rf"pr{pr_num}[_-]", re.IGNORECASE),
    ]
    seen: set = set()
    for f in _all_archive_files(archive_root):
        stem = f.stem
        if stem in seen:
            continue
        for pat in patterns:
            if pat.search(stem):
                result.append(stem)
                seen.add(stem)
                break

    if not result:
        result = _dispatch_ids_for_pr_from_receipts(pr_num)

    return result


def _dispatch_ids_for_pr_from_receipts(pr_num: int) -> List[str]:
    receipts_file = _resolve_receipts_file()
    if not receipts_file:
        return []
    result: List[str] = []
    seen: set = set()
    pr_re = re.compile(rf"\bpr[_-]?{pr_num}\b", re.IGNORECASE)
    try:
        with open(receipts_file, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                dispatch_id = str(obj.get("dispatch_id") or "")
                if not dispatch_id or dispatch_id in seen:
                    continue
                searchable = " ".join(
                    str(obj.get(k, ""))
                    for k in ("dispatch_id", "title", "gate", "task_id")
                )
                if pr_re.search(searchable):
                    result.append(dispatch_id)
                    seen.add(dispatch_id)
    except OSError:
        pass
    return result


def _dispatch_ids_since(since_date: datetime, archive_root: Path) -> List[str]:
    """Return dispatch IDs whose archive file was last-modified on or after since_date."""
    result: List[str] = []
    seen: set = set()
    for f in _all_archive_files(archive_root):
        stem = f.stem
        if stem in seen:
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime >= since_date:
            result.append(stem)
            seen.add(stem)
    return result


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m < 60:
        return f"{m}m{s:02d}s" if s else f"{m}m"
    h = m // 60
    rm = m % 60
    return f"{h}h{rm:02d}m" if rm else f"{h}h"


def _effective_rate(effective_seconds: float) -> str:
    if effective_seconds <= 0:
        return "n/a"
    rate = HOURLY_RATE / (effective_seconds / SECONDS_PER_HOUR)
    return f"€{rate:.0f}/u"


def _active_ratio(effective_seconds: float, walltime_seconds: float) -> str:
    if walltime_seconds <= 0:
        return "n/a"
    ratio = effective_seconds / walltime_seconds * 100
    return f"{ratio:.0f}%"


def _render_dispatch_block(metrics: TimingMetrics) -> str:
    lines = [
        f"  Dispatch: {metrics.dispatch_id}",
        f"    Walltime:  {_format_duration(metrics.walltime_seconds)}",
        f"    Effective: {_format_duration(metrics.effective_seconds)}",
        f"    Events:    {metrics.event_count}",
        f"    Active:    {_active_ratio(metrics.effective_seconds, metrics.walltime_seconds)}",
    ]
    if metrics.parallel_dispatch_ids:
        lines.append(f"    Parallel:  {', '.join(metrics.parallel_dispatch_ids)}")
        lines.append(f"    Para-secs: {_format_duration(metrics.parallel_seconds)}")
    return "\n".join(lines)


def _render_pr_section(
    pr_num: Optional[int],
    dispatch_results: List[TimingMetrics],
    pr_title: str = "",
) -> str:
    if not dispatch_results:
        label = f"PR #{pr_num}" if pr_num else "?"
        return f"{label} — no timing data found\n"

    walltime_sum = sum(m.walltime_seconds for m in dispatch_results)
    effective_sum = sum(m.effective_seconds for m in dispatch_results)
    all_parallel = list(
        {pid for m in dispatch_results for pid in m.parallel_dispatch_ids}
    )
    para_secs = sum(m.parallel_seconds for m in dispatch_results)

    header = f"PR #{pr_num}" if pr_num else "Dispatches"
    if pr_title:
        header += f" — {pr_title}"
    lines = [header]
    lines.append(f"  Dispatches:   {len(dispatch_results)}")
    lines.append(f"  Walltime sum: {_format_duration(walltime_sum)}")
    lines.append(f"  Effective sum:{_format_duration(effective_sum)}")
    lines.append(f"  Active ratio: {_active_ratio(effective_sum, walltime_sum)}")
    if all_parallel:
        pr_refs = sorted({_extract_pr_ref(d) for d in all_parallel if _extract_pr_ref(d)})
        label = ", ".join(f"PR #{p}" for p in pr_refs) if pr_refs else ", ".join(all_parallel[:3])
        lines.append(f"  Parallel with:{label}")
        lines.append(f"  Para-seconds: {_format_duration(para_secs)}")
    lines.append(f"  Effective rate: {_effective_rate(effective_sum)}")
    lines.append("")
    for m in dispatch_results:
        lines.append(_render_dispatch_block(m))
        lines.append("")
    return "\n".join(lines)


def _extract_pr_ref(dispatch_id: str) -> Optional[int]:
    m = _PR_IN_DISPATCH_RE.search(dispatch_id)
    if m:
        return int(m.group(1))
    return None


def _render_aggregate(all_results: List[Tuple[Optional[int], List[TimingMetrics]]]) -> str:
    total_walltime = sum(m.walltime_seconds for _, ml in all_results for m in ml)
    total_effective = sum(m.effective_seconds for _, ml in all_results for m in ml)
    total_dispatches = sum(len(ml) for _, ml in all_results)

    lines = [
        "## Aggregate",
        f"  Total dispatches: {total_dispatches}",
        f"  Total walltime:   {_format_duration(total_walltime)}",
        f"  Total effective:  {_format_duration(total_effective)}",
        f"  Overall active:   {_active_ratio(total_effective, total_walltime)}",
        f"  Effective rate:   {_effective_rate(total_effective)}",
    ]
    return "\n".join(lines)


def run_pr(pr_num: int, archive_root: Path) -> Tuple[Optional[int], List[TimingMetrics]]:
    dispatch_ids = _dispatch_ids_for_pr(pr_num, archive_root)
    results = []
    for did in dispatch_ids:
        m = analyze_dispatch(did, archive_root)
        if m:
            results.append(m)
    return pr_num, results


def run_since(since_str: str, archive_root: Path) -> List[Tuple[Optional[int], List[TimingMetrics]]]:
    try:
        since_dt = datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"Invalid --since date: {exc}") from exc
    dispatch_ids = _dispatch_ids_since(since_dt, archive_root)
    by_pr: Dict[Optional[int], List[TimingMetrics]] = {}
    for did in dispatch_ids:
        m = analyze_dispatch(did, archive_root)
        if not m:
            continue
        pr_ref = _extract_pr_ref(did)
        by_pr.setdefault(pr_ref, []).append(m)
    return sorted(by_pr.items(), key=lambda x: (x[0] is None, x[0]))


def run_dispatch(dispatch_id: str, archive_root: Path) -> Tuple[Optional[int], List[TimingMetrics]]:
    m = analyze_dispatch(dispatch_id, archive_root)
    if not m:
        return None, []
    return _extract_pr_ref(dispatch_id), [m]


def build_report(sections: List[Tuple[Optional[int], List[TimingMetrics]]]) -> str:
    parts = ["# VNX Timing Report\n"]
    for pr_num, results in sections:
        parts.append(_render_pr_section(pr_num, results))
    if len(sections) > 1:
        parts.append(_render_aggregate(sections))
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="VNX timing retrospective report")
    parser.add_argument("--pr", type=int, help="PR number to analyze")
    parser.add_argument("--since", help="ISO date (e.g. 2026-05-15) — report all dispatches since")
    parser.add_argument("--dispatch", help="Single dispatch ID to analyze")
    parser.add_argument("--output", help="Write markdown report to this file path")
    args = parser.parse_args()

    if not any([args.pr, args.since, args.dispatch]):
        parser.print_help()
        raise SystemExit(1)

    archive_root = _resolve_archive_root()

    sections: List[Tuple[Optional[int], List[TimingMetrics]]] = []

    if args.dispatch:
        sections.append(run_dispatch(args.dispatch, archive_root))
    elif args.pr:
        sections.append(run_pr(args.pr, archive_root))
    elif args.since:
        sections.extend(run_since(args.since, archive_root))

    report = build_report(sections)
    print(report)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(report, encoding="utf-8")
        os.replace(tmp, out_path)
        print(f"\nReport written to: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
