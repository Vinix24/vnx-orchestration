"""timing_metrics.py — Wave 5 PR-5.x: effective time + parallel detection.

Two use-cases:
- Forward: append_receipt enriches new receipts with timing block
- Retrospective: pr_timing_report CLI analyzes archived event-streams

Effective time = sum of inter-event gaps capped at threshold_seconds.
Long gaps (gates running, waiting on review) don't count as active work.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_ACTIVE_THRESHOLD_SECONDS = float(
    os.environ.get("VNX_TIMING_ACTIVE_THRESHOLD_SECONDS", "10.0")
)


@dataclass
class TimingMetrics:
    dispatch_id: str
    walltime_seconds: float
    effective_seconds: float
    event_count: int
    started_at: str
    ended_at: str
    parallel_dispatch_ids: List[str]
    parallel_seconds: float


def _parse_timestamp(ts: str) -> float:
    """Return POSIX timestamp from ISO 8601 string. Raises ValueError on bad input."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def compute_effective_time(
    ndjson_path: Path,
    threshold_seconds: float = DEFAULT_ACTIVE_THRESHOLD_SECONDS,
) -> Tuple[float, float, int, str, str]:
    """Parse NDJSON event-stream, return (walltime, effective_time, event_count, started_iso, ended_iso).

    Effective = sum of min(t_next - t_curr, threshold) for consecutive event timestamps.
    Walltime = last event ts - first event ts.
    """
    timestamps: List[Tuple[float, str]] = []
    with open(ndjson_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = obj.get("timestamp")
            if not ts:
                continue
            try:
                t = _parse_timestamp(ts)
                timestamps.append((t, ts))
            except ValueError:
                continue

    if len(timestamps) < 2:
        started = timestamps[0][1] if timestamps else ""
        return 0.0, 0.0, len(timestamps), started, started

    timestamps.sort(key=lambda x: x[0])
    walltime = timestamps[-1][0] - timestamps[0][0]
    effective = sum(
        min(timestamps[i + 1][0] - timestamps[i][0], threshold_seconds)
        for i in range(len(timestamps) - 1)
    )
    return walltime, effective, len(timestamps), timestamps[0][1], timestamps[-1][1]


def detect_parallel_dispatches(
    target_dispatch_id: str,
    target_started: float,
    target_ended: float,
    candidate_archives: List[Path],
) -> Tuple[List[str], float]:
    """For target dispatch's [start, end] window, find other dispatches whose windows overlap.

    Returns (list of overlapping dispatch_ids, total seconds during which 1+ other was active).
    """
    overlaps: List[str] = []
    overlap_intervals: List[Tuple[float, float]] = []
    for archive in candidate_archives:
        if target_dispatch_id in archive.stem:
            continue
        try:
            _, _, _, started, ended = compute_effective_time(archive)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not started or not ended:
            continue
        try:
            t_start = _parse_timestamp(started)
            t_end = _parse_timestamp(ended)
        except ValueError:
            continue
        overlap_start = max(target_started, t_start)
        overlap_end = min(target_ended, t_end)
        if overlap_end > overlap_start:
            overlaps.append(archive.stem)
            overlap_intervals.append((overlap_start, overlap_end))
    parallel_seconds = sum(end - start for start, end in overlap_intervals)
    return overlaps, parallel_seconds


def analyze_dispatch(
    dispatch_id: str,
    archive_root: Path,
    threshold_seconds: float = DEFAULT_ACTIVE_THRESHOLD_SECONDS,
) -> Optional[TimingMetrics]:
    """Find the NDJSON archive for dispatch_id, compute metrics, detect parallel."""
    candidates = list(archive_root.rglob(f"{dispatch_id}*.ndjson"))
    if not candidates:
        return None
    ndjson_path = candidates[0]

    wt, eff, n_events, started, ended = compute_effective_time(ndjson_path, threshold_seconds)
    if not started:
        return None

    t_start = _parse_timestamp(started)
    t_end = _parse_timestamp(ended)
    all_archives = list(archive_root.rglob("*.ndjson"))
    parallel_ids, parallel_secs = detect_parallel_dispatches(
        dispatch_id, t_start, t_end, all_archives
    )

    return TimingMetrics(
        dispatch_id=dispatch_id,
        walltime_seconds=wt,
        effective_seconds=eff,
        event_count=n_events,
        started_at=started,
        ended_at=ended,
        parallel_dispatch_ids=parallel_ids,
        parallel_seconds=parallel_secs,
    )


def _build_timing_block(
    dispatch_id: str,
    vnx_data_dir: Path,
    threshold_seconds: float = DEFAULT_ACTIVE_THRESHOLD_SECONDS,
) -> Optional[Dict]:
    """Build a timing dict for receipt enrichment. Returns None if archive unavailable."""
    archive_root = vnx_data_dir / "events" / "archive"
    if not archive_root.exists():
        return None
    metrics = analyze_dispatch(dispatch_id, archive_root, threshold_seconds)
    if metrics is None:
        return None
    return asdict(metrics)
