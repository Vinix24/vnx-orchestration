"""Component health beacon — atomic JSON heartbeat per VNX component.

Each component writes a heartbeat file at:

    <state_dir>/health/<component>.json

with payload:

    {
        "component": "learning_loop",
        "last_run_ts": 1714400000,
        "last_run_iso": "2026-04-30T...Z",
        "status": "ok|stale|fail",
        "details": {...freeform...},
        "expected_interval_seconds": 86400
    }

CI / dashboard reads all beacons via ``all_beacons()`` and flags any whose
age exceeds ``expected_interval_seconds * 1.5`` as ``stale``. Components
with ``status="fail"`` are flagged as ``fail`` regardless of age.

Per the dispatch, ``state_dir`` is the VNX data root (typically
``.vnx-data/``), and the module owns the ``health/`` subdirectory below
it. The constructor name preserves the dispatch contract.

Atomic write: payload is serialized to a sibling tmp file under an
``fcntl`` lock, then ``os.replace``'d into place. The lock prevents two
concurrent writers from interleaving JSON bytes; the rename gives
readers an all-or-nothing view.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional


class HealthBeacon:
    """Writer for a single component's heartbeat file."""

    def __init__(
        self,
        state_dir: Path,
        component: str,
        expected_interval_seconds: Optional[int] = 86400,
    ) -> None:
        self.path = Path(state_dir) / "health" / f"{component}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.component = component
        self.expected_interval = expected_interval_seconds

    def heartbeat(
        self,
        status: str = "ok",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Atomically write the current heartbeat.

        Best-effort: I/O failures are swallowed so a beacon write never
        breaks the calling component. Callers that need confirmation
        should call :meth:`heartbeat_strict`.
        """
        try:
            self.heartbeat_strict(status=status, details=details)
        except OSError:
            pass

    def heartbeat_strict(
        self,
        status: str = "ok",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Atomically write the heartbeat, raising on I/O failure."""
        now = time.time()
        payload: Dict[str, Any] = {
            "component": self.component,
            "last_run_ts": int(now),
            "last_run_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "status": status,
            "details": details or {},
            "expected_interval_seconds": self.expected_interval,
        }

        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")

        # fcntl-locked write: serialise concurrent writers on the same
        # component, so the tmp+rename pair is never interleaved.
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)


def all_beacons(state_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Read all beacons under ``state_dir/health`` and classify each.

    Classification rules (in order):
      * ``status == "fail"``                         -> ``health = "fail"``
      * unreadable JSON                              -> ``health = "corrupt"``
      * ``expected_interval_seconds`` is None        -> ``health = "ok"``
        (event-driven components — no staleness check)
      * ``age > expected_interval * 1.5``            -> ``health = "stale"``
      * otherwise                                    -> ``health = "ok"``

    Returns a mapping ``component_name -> beacon_dict`` (the raw payload
    plus the derived ``health`` and ``age_seconds`` keys).
    """
    state_dir = Path(state_dir)
    health_dir = state_dir / "health"
    if not health_dir.exists():
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    now = time.time()

    for path in sorted(health_dir.glob("*.json")):
        # Skip tmp / lock siblings.
        if path.suffix != ".json":
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("beacon payload not a JSON object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            out[path.stem] = {
                "component": path.stem,
                "health": "corrupt",
                "error": str(exc),
            }
            continue

        component = data.get("component", path.stem)
        last_ts = data.get("last_run_ts")
        try:
            last_ts_f = float(last_ts) if last_ts is not None else None
        except (TypeError, ValueError):
            last_ts_f = None

        if last_ts_f is None:
            out[component] = {**data, "health": "corrupt", "error": "missing last_run_ts"}
            continue

        age = now - last_ts_f
        data["age_seconds"] = round(age, 1)

        status = data.get("status")
        interval = data.get("expected_interval_seconds")

        if status == "fail":
            data["health"] = "fail"
        elif interval is None:
            # Event-driven component: track last-time only, never stale.
            data["health"] = "ok"
        else:
            try:
                interval_f = float(interval)
            except (TypeError, ValueError):
                interval_f = 86400.0
            if interval_f <= 0:
                data["health"] = "ok"
            else:
                staleness_factor = age / interval_f
                if staleness_factor > 1.5:
                    data["health"] = "stale"
                else:
                    data["health"] = "ok"

        out[component] = data

    return out


def beacon_summary(state_dir: Path) -> Dict[str, Any]:
    """Return a compact summary suitable for dashboard / CI consumption."""
    beacons = all_beacons(state_dir)
    counts: Dict[str, int] = {"ok": 0, "stale": 0, "fail": 0, "corrupt": 0}
    for b in beacons.values():
        h = b.get("health", "corrupt")
        counts[h] = counts.get(h, 0) + 1
    overall = "ok"
    if counts.get("fail", 0) or counts.get("corrupt", 0):
        overall = "fail"
    elif counts.get("stale", 0):
        overall = "stale"
    return {
        "overall": overall,
        "counts": counts,
        "beacons": beacons,
    }


__all__ = ["HealthBeacon", "all_beacons", "beacon_summary"]
