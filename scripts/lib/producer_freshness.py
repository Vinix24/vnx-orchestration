"""producer_freshness.py — per-key freshness diff for VNX producers.

The core distinction this module enforces: group activity by each producer's
OWN key, never by table/directory level. A table can look alive while
individual keys are dead (governance_metrics wrote dispatch_count daily while
fpy/rework_rate were dead for six weeks; the dispatches table held 35 dlv-
rows while the dispatch door had written nothing since 2026-07-16; the
review-gate layer produced zero requests/results while gate=codex_gate
dispatches kept flowing — the 2026-07-31 incident this monitor exists to
catch within a day).

Producers are declared in ``configs/producer_freshness.yaml``. Two source
types:

  directory — glob files, extract the key from the filename via a regex
              (``(?P<key>...)`` group), timestamp = file mtime.
  sqlite    — run a read-only query, take key/timestamp columns from each
              row (optionally transform the key, e.g. ``prefix`` =
              text before the first '-').

A producer may declare ``expected_keys``: keys that MUST appear. A producer
that writes nothing at all can never be detected by grouping existing rows —
absence has to be asserted, not observed.

Output is NDJSON (append-only ``producer_freshness.ndjson`` in the state
dir): one ``producer_freshness_sweep`` summary record per run plus one
``producer_freshness_finding`` record per stale/missing key. NDJSON keeps
ADR-007 (composite UNIQUE/PK over project_id for new central-DB tables) out
of scope — this monitor opens no central-DB write path at all.

Self-monitoring: every sweep writes a HealthBeacon heartbeat, INCLUDING runs
with zero findings — a sweep that finds nothing and writes nothing is
indistinguishable from a sweep that never ran. The dumb tripwire
(``hooks/monitor_tripwire.sh``) checks only that heartbeat's age and shares
no code, no DB connection and no interpreter with this module.
"""
from __future__ import annotations

import fcntl
import json
import logging
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ndjson_io import fsync_fileno, iter_ndjson

_LOG = logging.getLogger(__name__)

REPORT_FILENAME = "producer_freshness.ndjson"
HEARTBEAT_COMPONENT = "producer_freshness_monitor"
HEARTBEAT_INTERVAL_SECONDS = 86400

_STATUS_OK = "ok"
_STATUS_STALE = "stale"
_STATUS_ERROR = "error"


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> Optional[float]:
    """Parse a timestamp to epoch seconds.

    Accepts epoch numbers and ISO-8601 strings (``2026-07-16T13:40:13.123Z``,
    ``2026-06-16 02:09:06``). Naive datetimes are read as UTC — the fabric's
    sqlite writers store UTC. Returns None when unparseable.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------


def load_registry(config_path: Path) -> List[Dict[str, Any]]:
    """Load the producer registry YAML. Raises ImportError/ValueError on
    missing dependency or malformed config — the CLI maps those to exit
    codes; the library stays honest about bad input."""
    import yaml  # noqa: PLC0415 — lazy: keeps the importable surface stdlib-only

    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("producers"), list):
        raise ValueError(f"producer registry {config_path} has no 'producers' list")
    return data["producers"]


def _expand(value: Any, *, state_dir: Path, data_dir: Path, project_id: str) -> Any:
    if isinstance(value, str):
        return value.format(state_dir=state_dir, data_dir=data_dir, project_id=project_id)
    if isinstance(value, dict):
        return {k: _expand(v, state_dir=state_dir, data_dir=data_dir, project_id=project_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, state_dir=state_dir, data_dir=data_dir, project_id=project_id) for v in value]
    return value


# ---------------------------------------------------------------------------
# Source scanners — each returns {key: last_seen_epoch}
# ---------------------------------------------------------------------------


def scan_directory(spec: Dict[str, Any], *, now: float) -> Dict[str, Optional[float]]:
    """Group files under ``path``/``glob`` by the ``key_regex`` (?P<key>) group.

    Timestamp source is file mtime. Files whose name does not match the regex
    are bucketed under their own filename (still visible, never dropped).
    """
    root = Path(spec["path"])
    out: Dict[str, Optional[float]] = {}
    if not root.is_dir():
        return out
    pattern = spec.get("glob") or "*"
    key_re = re.compile(spec["key_regex"]) if spec.get("key_regex") else None
    for entry in root.glob(pattern):
        if not entry.is_file():
            continue
        key = entry.name
        if key_re is not None:
            match = key_re.match(entry.name)
            if match and "key" in match.groupdict():
                key = match.group("key")
        try:
            ts: Optional[float] = entry.stat().st_mtime
        except OSError as exc:
            _LOG.warning("producer_freshness: stat failed for %s: %s", entry, exc)
            ts = None
        prev = out.get(key)
        if prev is None or (ts is not None and ts > prev):
            out[key] = ts
    return out


def scan_sqlite(spec: Dict[str, Any], *, now: float) -> Dict[str, Optional[float]]:
    """Run the spec's query READ-ONLY and group rows by key column.

    Read-only URI: a missing DB raises ``sqlite3.OperationalError`` (callers
    degrade to an error entry) instead of silently creating an empty file.
    """
    db_path = Path(spec["db"])
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        cursor = conn.execute(spec["query"])
        col_names = [d[0] for d in cursor.description or []]
        rows = cursor.fetchall()
    finally:
        conn.close()
    key_idx = col_names.index(spec["key_column"])
    ts_idx = col_names.index(spec["timestamp_column"])
    transform = spec.get("key_transform")
    out: Dict[str, Optional[float]] = {}
    for row in rows:
        raw_key = row[key_idx]
        if raw_key is None:
            continue
        key = str(raw_key)
        if transform == "prefix":
            key = key.split("-", 1)[0]
        ts = _parse_ts(row[ts_idx])
        prev = out.get(key)
        if key not in out or (ts is not None and (prev is None or ts > prev)):
            out[key] = ts
    return out


def scan_gate_obligations(spec: Dict[str, Any], *, now: float) -> Dict[str, Optional[float]]:
    """Group review-gate obligations by gate name (OI-876/OI-881).

    An obligation is one JSON file per door-accepted dispatch that declared
    ``gate=<name>`` (scripts/lib/gate_obligations.py). The key is the gate
    name — per sleutel, never per directory, so one live gate cannot hide a
    dead sibling.

    Per-key ``last_seen`` semantics — declaration checked against evidence:

      - if any obligation for the key is still ``pending``, last_seen = the
        OLDEST pending declaration. A declared gate that produced no result
        within cadence then reads as stale: exactly the 2026-07-31 incident
        (nine dispatches declared codex_gate, zero ran, nothing noticed).
      - otherwise last_seen = the NEWEST terminal resolution (fulfilled /
        not_executable / failed) — loud non-execution counts as evidence.

    An unreadable obligation raises ValueError so the caller records a
    source_unreadable finding: a corrupted evidence trail must never read as
    "nothing to do".
    """
    root = Path(spec["path"])
    out: Dict[str, Optional[float]] = {}
    if not root.is_dir():
        return out
    pending_oldest: Dict[str, float] = {}
    resolved_newest: Dict[str, float] = {}
    for entry in sorted(root.glob("*.json")):
        if not entry.is_file():
            continue
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"unreadable gate obligation {entry.name}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"gate obligation {entry.name} is not a JSON object")
        key = str(record.get("gate") or entry.stem)
        status = record.get("status", "pending")
        if status == "pending":
            ts = _parse_ts(record.get("declared_at"))
            if ts is None:
                ts = entry.stat().st_mtime
            prev = pending_oldest.get(key)
            if prev is None or ts < prev:
                pending_oldest[key] = ts
        else:
            ts = _parse_ts(record.get("resolved_at"))
            if ts is None:
                ts = entry.stat().st_mtime
            prev = resolved_newest.get(key)
            if prev is None or ts > prev:
                resolved_newest[key] = ts
    for key in set(pending_oldest) | set(resolved_newest):
        out[key] = pending_oldest.get(key, resolved_newest.get(key))
    return out


_SCANNERS: Dict[str, Callable[..., Dict[str, Optional[float]]]] = {
    "directory": scan_directory,
    "sqlite": scan_sqlite,
    "gate_obligations": scan_gate_obligations,
}


# ---------------------------------------------------------------------------
# Demand evidence — "work kept flowing while this key was silent"
# ---------------------------------------------------------------------------


def count_demand_events(spec: Dict[str, Any], *, since_ts: Optional[float], now: float) -> Optional[int]:
    """Count demand-source events newer than ``since_ts`` (or the last cadence
    window when ``since_ts`` is None — a producer that never wrote).

    Returns None when no demand source is configured or it is unreadable —
    demand evidence is corroborating, never load-bearing.
    """
    demand = spec.get("demand")
    if not isinstance(demand, dict):
        return None
    if demand.get("type") != "ndjson_events":
        _LOG.warning("producer_freshness: unsupported demand type %r", demand.get("type"))
        return None
    path = Path(demand["path"])
    ts_field = demand.get("timestamp_field", "timestamp")
    if since_ts is None:
        since_ts = now - float(spec.get("cadence_seconds", 86400))
    count = 0
    try:
        for record in iter_ndjson(path):
            if not isinstance(record, dict):
                continue
            ts = _parse_ts(record.get(ts_field))
            if ts is not None and ts > since_ts:
                count += 1
    except OSError as exc:
        _LOG.warning("producer_freshness: demand source %s unreadable: %s", path, exc)
        return None
    return count


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_producer(spec: Dict[str, Any], *, now: float) -> Dict[str, Any]:
    """Evaluate one producer spec into a report section with findings."""
    name = spec.get("name", "(unnamed)")
    cadence = float(spec.get("cadence_seconds", 86400))
    scanner = _SCANNERS.get(spec.get("type"))
    section: Dict[str, Any] = {
        "producer": name,
        "type": spec.get("type"),
        "cadence_seconds": cadence,
        "keys": [],
        "findings": [],
    }
    if scanner is None:
        section["error"] = f"unknown producer type {spec.get('type')!r}"
        section["status"] = _STATUS_ERROR
        return section

    try:
        last_seen = scanner(spec, now=now)
    except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
        # A producer whose source is unreadable is itself a finding: the
        # absence of data must never read as "nothing to do".
        section["error"] = f"{type(exc).__name__}: {exc}"
        section["status"] = _STATUS_ERROR
        section["findings"].append(
            {
                "producer": name,
                "key": None,
                "kind": "source_unreadable",
                "error": section["error"],
                "cadence_seconds": cadence,
            }
        )
        return section

    expected = [str(k) for k in spec.get("expected_keys") or []]
    for key in expected:
        if key not in last_seen:
            last_seen[key] = None  # asserted absence — evaluated below

    for key in sorted(last_seen):
        ts = last_seen[key]
        silence = None if ts is None else max(0.0, now - ts)
        entry: Dict[str, Any] = {
            "key": key,
            "last_seen_ts": ts,
            "last_seen": _iso(ts),
            "silence_seconds": silence,
        }
        section["keys"].append(entry)

        stale = ts is None or (silence is not None and silence > cadence)
        if not stale:
            continue
        finding: Dict[str, Any] = {
            "producer": name,
            "key": key,
            "kind": "missing" if ts is None else "stale",
            "last_seen": _iso(ts),
            "silence_seconds": silence,
            "silence_days": None if silence is None else round(silence / 86400.0, 2),
            "cadence_seconds": cadence,
        }
        if key in expected and ts is None:
            finding["expected_key_absent"] = True
        demand = count_demand_events(spec, since_ts=ts, now=now)
        if demand is not None:
            finding["demand"] = {
                "source": (spec.get("demand") or {}).get("label") or (spec.get("demand") or {}).get("path"),
                "events_since_last_seen": demand,
            }
        section["findings"].append(finding)

    section["status"] = _STATUS_STALE if section["findings"] else _STATUS_OK
    return section


def run_sweep(
    state_dir: Path,
    registry: List[Dict[str, Any]],
    *,
    now: Optional[float] = None,
    project_id: str = "",
) -> Dict[str, Any]:
    """Evaluate every producer in the registry. Pure: performs NO writes."""
    now = time.time() if now is None else now
    state_dir = Path(state_dir)
    data_dir = state_dir.parent if state_dir.name == "state" else state_dir
    sections: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    for raw_spec in registry:
        spec = _expand(raw_spec, state_dir=state_dir, data_dir=data_dir, project_id=project_id)
        section = evaluate_producer(spec, now=now)
        sections.append(section)
        findings.extend(section["findings"])
    return {
        "run_id": uuid.uuid4().hex[:12],
        "timestamp": _now_iso(),
        "state_dir": str(state_dir),
        "producers_evaluated": len(sections),
        "keys_evaluated": sum(len(s["keys"]) for s in sections),
        "findings_count": len(findings),
        "status": _STATUS_STALE if findings else _STATUS_OK,
        "producers": sections,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Writes — NDJSON report + always-on heartbeat
# ---------------------------------------------------------------------------


def _append_ndjson_locked(path: Path, records: List[Dict[str, Any]]) -> None:
    """Locked append of NDJSON records (fcntl.flock + fsync before release)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, default=str))
                fh.write("\n")
            fh.flush()
            fsync_fileno(fh, context=str(path))
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def append_report(state_dir: Path, report: Dict[str, Any]) -> Path:
    """Append the sweep summary + one record per finding to the NDJSON report."""
    path = Path(state_dir) / REPORT_FILENAME
    records: List[Dict[str, Any]] = [
        {
            "event_type": "producer_freshness_sweep",
            "run_id": report["run_id"],
            "timestamp": report["timestamp"],
            "producers_evaluated": report["producers_evaluated"],
            "keys_evaluated": report["keys_evaluated"],
            "findings_count": report["findings_count"],
            "status": report["status"],
        }
    ]
    for finding in report["findings"]:
        records.append(
            {
                "event_type": "producer_freshness_finding",
                "run_id": report["run_id"],
                "timestamp": report["timestamp"],
                **finding,
            }
        )
    _append_ndjson_locked(path, records)
    return path


def write_heartbeat(state_dir: Path, report: Dict[str, Any]) -> Path:
    """Write the sweep heartbeat via the canonical HealthBeacon.

    Called on EVERY run, including zero-finding runs — a sweep that finds
    nothing and writes nothing is indistinguishable from a sweep that never
    ran. ``hooks/monitor_tripwire.sh`` watches only this file's age.
    """
    from health_beacon import HealthBeacon  # noqa: PLC0415

    state_dir = Path(state_dir)
    data_dir = state_dir.parent if state_dir.name == "state" else state_dir
    beacon = HealthBeacon(
        data_dir,
        HEARTBEAT_COMPONENT,
        expected_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )
    beacon.heartbeat_strict(
        status=report["status"],
        details={
            "run_id": report["run_id"],
            "findings_count": report["findings_count"],
            "producers_evaluated": report["producers_evaluated"],
            "keys_evaluated": report["keys_evaluated"],
        },
    )
    return beacon.path


__all__ = [
    "REPORT_FILENAME",
    "HEARTBEAT_COMPONENT",
    "load_registry",
    "scan_directory",
    "scan_sqlite",
    "scan_gate_obligations",
    "count_demand_events",
    "evaluate_producer",
    "run_sweep",
    "append_report",
    "write_heartbeat",
]
