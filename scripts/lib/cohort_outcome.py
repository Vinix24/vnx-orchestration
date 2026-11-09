"""The autonomy verdict of ADR-056 and its write-once outcome marker (zelfst PR-7b).

The three thresholds below are the ADR-056 "Drempel" values. Changing one is an ADR
amendment and never only a code change: ``tests/test_cohort_outcome.py`` reads the ADR
text and fails when the table and these constants differ.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

MIN_COHORT = 50
ZERO_RATE_FLOOR = 0.80
UNKNOWN_RATE_CEILING = 0.20
END_SOURCE = "dispatches.state"
MARKER_DIR = "autonomy_markers"

INSUFFICIENT = "onvoldoende_meting"
AUTONOMOUS = "selfstandig"
NOT_AUTONOMOUS = "niet_selfstandig"


class MarkerExists(Exception):
    """A marker of this name is already on disk; markers are never rewritten."""


def _rate(value: Any) -> Optional[float]:
    """A rate in [0, 1], or ``None`` for anything else (bool, NaN, text, out of range)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0.0 <= value <= 1.0 else None


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def verdict(n: Any, zero_rate: Any, unknown_rate: Any) -> Dict[str, str]:
    """``{"verdict", "reason"}`` in the ADR order; a missing or unreadable value is insufficient."""
    count, zero, unknown = _count(n), _rate(zero_rate), _rate(unknown_rate)
    if count is None:
        return {"verdict": INSUFFICIENT, "reason": f"N is missing or unreadable: {n!r}"}
    if count < MIN_COHORT:
        return {"verdict": INSUFFICIENT, "reason": f"N={count} < {MIN_COHORT}"}
    if unknown is None:
        return {"verdict": INSUFFICIENT, "reason": f"unknown_rate is missing or unreadable: {unknown_rate!r}"}
    if unknown > UNKNOWN_RATE_CEILING:
        return {"verdict": INSUFFICIENT, "reason": f"unknown_rate={unknown} > {UNKNOWN_RATE_CEILING}"}
    if zero is None:
        return {"verdict": INSUFFICIENT, "reason": f"zero_rate is missing or unreadable: {zero_rate!r}"}
    if zero >= ZERO_RATE_FLOOR:
        return {"verdict": AUTONOMOUS, "reason": f"zero_rate={zero} >= {ZERO_RATE_FLOOR}"}
    return {"verdict": NOT_AUTONOMOUS, "reason": f"zero_rate={zero} < {ZERO_RATE_FLOOR}"}


def default_marker_name(since: str, until: str) -> str:
    return f"zelfstandigheid-{since[:10]}--{until[:10]}"


def git_sha(repo: Path) -> str:
    """The SHA of the code that writes the marker; ``unknown`` when git cannot say."""
    try:
        done = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else "unknown"


def build_marker(result: Mapping[str, Any], coverage: Mapping[str, Any], sha: str,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """The marker content: the measurement, the verdict, the thresholds and the five projects."""
    judged = verdict(result.get("N"), result.get("zero_rate"), result.get("unknown_rate"))
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "written_once": True, "written_at": stamp, "project_id": result.get("project_id"),
        "period": dict(result.get("period") or {}), "N": result.get("N"),
        "zero_rate": result.get("zero_rate"), "unknown_rate": result.get("unknown_rate"),
        "thresholds": {"min_cohort": MIN_COHORT, "zero_rate_floor": ZERO_RATE_FLOOR,
                       "unknown_rate_ceiling": UNKNOWN_RATE_CEILING},
        "verdict": judged["verdict"], "reason": judged["reason"], "end_source": END_SOURCE,
        "coverage": dict(coverage), "code_sha": sha,
    }


def _check_name(name: str) -> None:
    if not name or name in (".", "..") or os.sep in name or (os.altsep and os.altsep in name):
        raise ValueError(f"marker name must be a plain file name: {name!r}")


def write_marker(state_dir: Path, name: str, content: Mapping[str, Any]) -> Path:
    """Publish the marker whole, once: a per-writer temp is linked to the name, which fails if it exists."""
    _check_name(name)
    directory = Path(state_dir) / MARKER_DIR
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{name}.json"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(content, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, final)
        except FileExistsError as exc:
            raise MarkerExists(f"marker {final} already exists; markers are never rewritten") from exc
    finally:
        os.unlink(tmp_name)
    return final
