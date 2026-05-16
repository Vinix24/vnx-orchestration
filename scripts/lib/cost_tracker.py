"""cost_tracker — Read recent dispatch costs from t0_receipts.ndjson.

Used by cost_aware scaling policy to estimate hourly burn rate.
Reads only the last N receipts (default 200) for performance.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List


def recent_cost_per_hour(
    workers: int,
    provider_mix: List[str],
    window_minutes: int = 60,
) -> float:
    """Estimate hourly cost based on recent receipts.

    Reads .vnx-data/state/t0_receipts.ndjson (last ~200 lines), filters by
    timestamp window and provider, then computes avg cost_usd per dispatch
    multiplied by estimated dispatches/hour per worker.

    Returns 0.0 if receipt file is absent or empty.
    """
    receipts_path = _resolve_receipts_path()
    if not receipts_path.exists():
        return 0.0

    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=window_minutes)

    costs_by_provider: Dict[str, List[float]] = {}
    for line in _read_last_lines(receipts_path, n=200):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue

        cost = r.get("cost_usd")
        if cost is None:
            continue

        raw_ts = r.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(raw_ts.rstrip("Z")).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue

        if ts < cutoff:
            continue

        provider = (r.get("provider") or "claude")
        costs_by_provider.setdefault(provider, []).append(float(cost))

    if not costs_by_provider:
        return 0.0

    avg_costs = {p: sum(cs) / len(cs) for p, cs in costs_by_provider.items()}

    DISPATCHES_PER_HOUR = 6.0
    distribution = _distribute_workers(workers, provider_mix or ["claude"] * workers)

    hourly = 0.0
    fallback_avg = avg_costs.get("claude", 0.0)
    for provider, count in distribution.items():
        avg_cost = avg_costs.get(provider, fallback_avg)
        hourly += avg_cost * DISPATCHES_PER_HOUR * count

    return hourly


def _resolve_receipts_path() -> Path:
    """Resolve path via VNX_STATE_DIR or default."""
    state_dir = os.environ.get("VNX_STATE_DIR", ".vnx-data/state")
    return Path(state_dir) / "t0_receipts.ndjson"


def _read_last_lines(path: Path, n: int) -> List[str]:
    """Read last N non-empty lines efficiently."""
    with path.open("rb") as f:
        f.seek(0, 2)
        end_pos = f.tell()
        if end_pos == 0:
            return []

        # Estimate: NDJSON receipt lines average ~400 bytes; read enough to hold n lines.
        read_bytes = min(end_pos, n * 400)
        f.seek(end_pos - read_bytes)
        raw = f.read(read_bytes).decode("utf-8", errors="replace")
        lines = raw.splitlines()

        # Drop potentially partial first line when we didn't start from file beginning.
        if end_pos > read_bytes:
            lines = lines[1:]

        return [ln for ln in lines[-n:] if ln.strip()]


def _distribute_workers(total: int, mix: List[str]) -> Dict[str, int]:
    """Distribute workers over provider mix round-robin."""
    distribution: Dict[str, int] = {}
    for i in range(total):
        provider = mix[i % len(mix)]
        distribution[provider] = distribution.get(provider, 0) + 1
    return distribution
