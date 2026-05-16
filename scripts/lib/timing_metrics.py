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


def compute_effective_time(
    ndjson_path: Path,
    threshold_seconds: float = DEFAULT_ACTIVE_THRESHOLD_SECONDS,
) -> Tuple[float, float, int, str, str]:
    """Parse NDJSON event-stream, return (walltime, effective_time, event_count, started_iso, ended_iso).

    Effective = sum of min(t_next - t_curr, threshold) for consecutive timestamps.
    Walltime = last event ts - first event ts.
    Raises OSError on unreadable file; json.JSONDecodeError never silently swallowed.
    """
    timestamps: List[Tuple[float, str]] = []
    with open(ndjson_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.debug("timing_metrics: skipping malformed JSON line in %s", ndjson_path)
                continue
            ts = obj.get("timestamp")
            if not ts:
                continue
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                timestamps.append((t, ts))
            except ValueError:
                log.debug("timing_metrics: unparseable timestamp %r in %s", ts, ndjson_path)

    if len(timestamps) < 2:
        return 0.0, 0.0, len(timestamps), "", ""

    timestamps.sort()
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
    """Find other dispatches whose [start, end] windows overlap with the target.

    Returns (list of overlapping dispatch_ids, total overlap seconds).
    Overlap intervals are summed naively; no deduplication of union periods.
    """
    overlaps: List[str] = []
    total_overlap = 0.0
    for archive in candidate_archives:
        if target_dispatch_id in archive.stem:
            continue
        try:
            _, _, _, started, ended = compute_effective_time(archive)
        except OSError as exc:
            log.debug("timing_metrics: skipping unreadable archive %s: %s", archive, exc)
            continue
        if not started or not ended:
            continue
        try:
            t_start = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
            t_end = datetime.fromisoformat(ended.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        overlap_start = max(target_started, t_start)
        overlap_end = min(target_ended, t_end)
        if overlap_end > overlap_start:
            overlaps.append(archive.stem)
            total_overlap += overlap_end - overlap_start
    return overlaps, total_overlap


def analyze_dispatch(
    dispatch_id: str,
    archive_root: Path,
    threshold_seconds: float = DEFAULT_ACTIVE_THRESHOLD_SECONDS,
) -> Optional[TimingMetrics]:
    """Find NDJSON archive for dispatch_id, compute metrics, detect parallel dispatches."""
    candidates = list(archive_root.rglob(f"{dispatch_id}*.ndjson"))
    if not candidates:
        return None
    ndjson_path = candidates[0]

    try:
        wt, eff, n_events, started, ended = compute_effective_time(ndjson_path, threshold_seconds)
    except OSError as exc:
        log.warning("timing_metrics: failed to read archive %s: %s", ndjson_path, exc)
        return None

    if not started:
        return None

    try:
        t_start = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
        t_end = datetime.fromisoformat(ended.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

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


def _build_timing_block(dispatch_id: str, vnx_data_dir: Path) -> Optional[Dict]:
    """Build a timing dict for embedding in a completion receipt (best-effort).

    Returns None when no archive exists or metrics cannot be computed.
    Safe to call on any dispatch; missing archive is the expected case
    for non-subprocess-routed terminals.
    """
    archive_root = vnx_data_dir / "events" / "archive"
    if not archive_root.exists():
        return None
    metrics = analyze_dispatch(dispatch_id, archive_root)
    if metrics is None:
        return None
    return asdict(metrics)
