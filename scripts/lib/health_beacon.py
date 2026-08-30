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
age exceeds ``expected_interval_seconds`` as ``stale`` — a beacon older
than its interval is stale regardless of the status it recorded at write
time. Event-driven components (``expected_interval_seconds=None``) trust
the ``status`` field as-is, but only up to ``_EVENT_DRIVEN_MAX_AGE_SECONDS``
(D3a): past that backstop even a self-reported ``ok`` becomes ``unknown`` —
"I cannot verify this beacon's freshness" is its own outcome, not a
favorable default (a beacon with no interval that died mid-``ok`` used to
stay ``ok`` forever).

A self-reported ``status`` that isn't ``ok`` can never make the health
verdict more favorable than what the component itself said (D3a): a
recognized value (``stale``/``fail``/``corrupt``) is honored directly, and
any unrecognized value falls to the unfavorable side (``fail``), never to
``ok``.

Callers that pass ``expected`` (component names that MUST have a beacon —
see ``beacon_register.expected_component_names()``) get a fifth health
value, ``absent``, for any expected name with no beacon on disk at all —
distinct from ``ok``/``stale``/``fail``/``corrupt``/``unknown``, because a
component that never once wrote a beacon is a different, more suspect
condition than one that wrote and then went stale.

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
from typing import Any, Dict, Optional, Sequence

# D3a: an event-driven beacon (expected_interval_seconds=None) has no
# interval to measure staleness against, but that must not mean "never
# stale" — a component that wrote once and died is otherwise eternally
# green. 7 days matches this repo's other "this has been silent too long"
# backstop (scripts/receipt_query.py's DEFAULT_RECONCILE_MAX_AGE_DAYS = 7.0)
# and comfortably exceeds any real event-driven cadence measured in this
# fleet (the oldest currently observed, cleanup_worker_exit, fires on every
# dispatch exit).
_EVENT_DRIVEN_MAX_AGE_SECONDS = 7 * 86400

# D3a: statuses a component may self-report that this module recognizes and
# honors directly. Anything else (None, "degraded", "warn", ...) falls to
# the unfavorable side ("fail") rather than defaulting to "ok".
_KNOWN_SELF_REPORTED_STATUSES = {"ok", "fail", "stale", "corrupt"}


def _status_to_health(status: Any) -> str:
    """Map a component's self-reported ``status`` to a health verdict.

    A self-reported status that isn't ``"ok"`` can never be judged more
    favorably than what the component itself said (D3a gap 1). Recognized
    values are honored directly; any unrecognized value (including a
    missing/None status) falls to ``"fail"`` — the unfavorable side, never
    ``"ok"``.
    """
    if status in _KNOWN_SELF_REPORTED_STATUSES:
        return status
    return "fail"


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


def all_beacons(
    state_dir: Path,
    expected: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Read all beacons under ``state_dir/health`` and classify each.

    Classification rules (in order):
      * unreadable JSON                              -> ``health = "corrupt"``
      * ``expected_interval_seconds`` is None (event-driven) and
        ``age > _EVENT_DRIVEN_MAX_AGE_SECONDS``       -> ``health = "unknown"``
        (D3a: no interval does not mean "never stale" — past this backstop
        we can no longer trust a self-reported status of any kind)
      * ``age > expected_interval_seconds``          -> ``health = "stale"``
        (a beacon older than its interval is stale regardless of the
        status it recorded at write time)
      * otherwise                                    -> ``_status_to_health(status)``
        (self-reported ``ok``/``fail``/``stale``/``corrupt`` honored
        directly; any unrecognized status falls to ``"fail"``, never ``"ok"``)

    ``expected`` (D3a gap 2, optional): component names that MUST have a
    beacon — see ``beacon_register.expected_component_names()``. Any name in
    ``expected`` with no beacon found on disk at all gets a synthetic entry
    with ``health = "absent"``. Mirrors the existing fabric convention (see
    ``worker_permissions.resolve_dispatch_write_scope``): ``None`` means "no
    expectation declared, don't add anything" (fully backward compatible —
    every pre-D3a caller keeps its exact previous output); an empty sequence
    is a real, deliberate "expected nothing" and also adds nothing; a
    populated sequence adds one ``"absent"`` entry per name not found.

    Returns a mapping ``component_name -> beacon_dict`` (the raw payload
    plus the derived ``health`` and ``age_seconds`` keys; synthetic
    ``"absent"`` entries carry only ``component`` and ``health``).
    """
    state_dir = Path(state_dir)
    health_dir = state_dir / "health"

    out: Dict[str, Dict[str, Any]] = {}
    now = time.time()

    if health_dir.exists():
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

            if interval is None:
                # Event-driven component: no expected cadence to measure
                # staleness against, but that is not an eternal pass — past
                # the backstop we can no longer trust ANY self-reported
                # status (D3a gap 4, same "age trumps status" precedent as
                # the interval branch below).
                if age > _EVENT_DRIVEN_MAX_AGE_SECONDS:
                    data["health"] = "unknown"
                else:
                    data["health"] = _status_to_health(status)
            else:
                try:
                    interval_f = float(interval)
                except (TypeError, ValueError):
                    interval_f = 86400.0
                if interval_f <= 0:
                    data["health"] = _status_to_health(status)
                elif age > interval_f:
                    # Beacon is older than its expected interval — stale,
                    # regardless of the status it recorded at write time.
                    # A reading older than its interval is untrustworthy.
                    data["health"] = "stale"
                else:
                    data["health"] = _status_to_health(status)

            out[component] = data

    if expected:
        for name in expected:
            if name not in out:
                out[name] = {"component": name, "health": "absent"}

    return out


def beacon_summary(
    state_dir: Path,
    expected: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Return a compact summary suitable for dashboard / CI consumption.

    ``expected`` is forwarded to ``all_beacons`` verbatim — see its
    docstring for the None/empty/populated distinction.

    Both new D3a health values feed ``overall`` (the exact trap this
    dispatch closes elsewhere: adding a health value to ``counts`` without
    teaching ``overall`` about it leaves the roll-up silently unchanged).
    ``absent`` — an expected component with no beacon at all — is at least
    as bad as a confirmed ``fail``, so it joins the ``fail`` tier. ``unknown``
    — an event-driven beacon whose freshness can no longer be verified — is
    "can't verify", not "confirmed bad", so it joins the milder ``stale``
    tier instead.
    """
    beacons = all_beacons(state_dir, expected=expected)
    counts: Dict[str, int] = {
        "ok": 0, "stale": 0, "fail": 0, "corrupt": 0, "absent": 0, "unknown": 0,
    }
    for b in beacons.values():
        h = b.get("health", "corrupt")
        counts[h] = counts.get(h, 0) + 1
    overall = "ok"
    if counts.get("fail", 0) or counts.get("corrupt", 0) or counts.get("absent", 0):
        overall = "fail"
    elif counts.get("stale", 0) or counts.get("unknown", 0):
        overall = "stale"
    return {
        "overall": overall,
        "counts": counts,
        "beacons": beacons,
    }


__all__ = ["HealthBeacon", "all_beacons", "beacon_summary"]
