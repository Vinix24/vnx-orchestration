"""``report``: the window read, the done check and the scanner (ADR-063 besluit 2, 3, 6, 8).

``--metric ID[:KEY] --last N --json`` pools the final days of the window the way the
baseline does; no measured value is ``pooled_value: null`` with a reason, never 0.
``--check`` exits 0 only when every condition holds, else 1 with each one named in
``unmet``; a loaded job whose newest settled day is not fresh is ``status: "stale"``.
``--scan-zero-for-unknown`` exits 1 when any day file or revision holds a value next to an
``unknown_reason``, a file is unreadable or the store is empty; ``--check`` runs it too.
Missing, malformed or unknown evidence is never a pass. No ``DAILY_STEP``.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from daily_metrics import baseline, days, proposal_ledger
from daily_metrics.commands.propose import _resolve
from daily_metrics.metric import KEYED, REQUIRED
from daily_metrics.store import DayStore

EXIT_OK, EXIT_UNMET, EXIT_USAGE = 0, 1, 2
FRESH_RUN_DAYS = 7
MAX_GAP_HOURS = 48
MAX_LAST_DAYS = 366
LABEL_PREFIX = "com.vnx.daily-metrics."
LAUNCHCTL_ENV = "VNX_DAILY_METRICS_LAUNCHCTL"
UNKNOWN_WINDOW = "no_measured_value_in_window"
DAY_CONDITIONS = ("missing_ids", "keyed_without_entries", "malformed_records",
                  "unknown_without_reason", "zero_for_unknown")
_UNREADABLE = "unreadable"
_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(\.r\d+)?\.json$")

Runner = Callable[[list[str]], tuple[int, str]]


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("report", help="read a metric window or check that the measurement is done")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--scan-zero-for-unknown", dest="scan_zero_for_unknown", action="store_true")
    mode.add_argument("--metric", default=None, metavar="ID[:KEY]")
    parser.add_argument("--last", type=int, default=FRESH_RUN_DAYS, metavar="N")
    parser.add_argument("--json", action="store_true", help="accepted; the output is always JSON")
    for option in ("--project-id", "--now", "--store"):
        parser.add_argument(option, dest=option[2:].replace("-", "_"), default=None)


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.utcoffset() is not None else None


def _load(store: DayStore, day: date) -> Any:
    """The newest revision, ``None`` when absent, ``"unreadable"`` when it does not parse."""
    try:
        return store.load(day.isoformat())
    except (OSError, ValueError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return _UNREADABLE


def _window(now: datetime, last: int) -> list[date]:
    newest = days.latest_settled_day(now)
    return [newest - timedelta(days=offset) for offset in range(last - 1, -1, -1)]


def _scalar_findings(name: str, record: Any, out: dict[str, list]) -> None:
    if not isinstance(record, Mapping):
        out["malformed_records"].append(name)
        return
    value, reason = record.get("value"), record.get("unknown_reason")
    if value is not None and reason not in (None, ""):
        out["zero_for_unknown"].append({"metric": name, "value": value, "unknown_reason": reason})
    elif value is not None and not baseline._is_number(value):
        out["malformed_records"].append(name)
    if value is None and not (isinstance(reason, str) and reason.strip()):
        out["unknown_without_reason"].append(name)


def record_findings(doc: Mapping[str, Any]) -> dict[str, list]:
    """Per-record contract breaches in one day document; empty lists mean none."""
    out: dict[str, list] = {name: [] for name in DAY_CONDITIONS}
    metrics = doc.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}  # no metrics: all ten ids missing
    out["missing_ids"] = sorted(REQUIRED - set(metrics))
    for metric_id in sorted(metrics):
        record = metrics[metric_id]
        if metric_id not in KEYED:
            _scalar_findings(metric_id, record, out)
            continue
        entries = record.get("entries") if isinstance(record, Mapping) else None
        if not isinstance(entries, Mapping) or not entries:
            out["keyed_without_entries"].append(metric_id)
            continue
        for key in sorted(entries):
            _scalar_findings(f"{metric_id}:{key}", entries[key], out)
    return out


def scan(root: Path) -> dict[str, Any]:
    """Every day file and revision under ``root``; an empty store is not clean."""
    files = sorted(path for path in Path(root).glob("*.json") if _FILE_RE.match(path.name))
    violations: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            doc = None
        if not isinstance(doc, Mapping) or not isinstance(doc.get("metrics"), Mapping):
            unreadable.append(path.name)
            continue
        violations += [{"file": path.name, **item} for item in record_findings(doc)["zero_for_unknown"]]
    return {"files_scanned": len(files), "violations": len(violations), "records": violations,
            "unreadable_files": unreadable, "store_empty": not files}


def _day_state(doc: Any, day: date, project_id: str, now: datetime) -> str:
    """``fresh`` only for this project's ``final``, not backfilled, in-window collection."""
    if not isinstance(doc, Mapping):
        return "missing" if doc is None else _UNREADABLE
    if doc.get("project_id") != project_id or doc.get("day") != day.isoformat():
        return "foreign"
    if doc.get("status") != days.FINAL_STATUS:
        return "not_final"
    if doc.get("backfilled") is not False:
        return "backfilled" if doc.get("backfilled") is True else "not_fresh"
    collected = _parse_ts(doc.get("collected_at"))
    if collected is None or not days.settled_at(day) <= collected <= min(days.fresh_until(day), now):
        return "not_fresh"
    return "fresh"


def _max_gap_hours(docs: Mapping[date, Any], now: datetime) -> float | None:
    """The largest gap between consecutive collections, ``now`` as the last point."""
    points = sorted(stamp for doc in docs.values() if isinstance(doc, Mapping)
                    and (stamp := _parse_ts(doc.get("collected_at"))) is not None and stamp <= now)
    if not points:
        return None  # no readable collection: nothing proves the job ran
    points.append(now)
    return max((later - earlier).total_seconds() / 3600 for earlier, later in zip(points, points[1:]))


def _run_launchctl(argv: list[str]) -> tuple[int, str]:
    proc = subprocess.run([os.environ.get(LAUNCHCTL_ENV) or "launchctl", *argv],
                          capture_output=True, text=True, timeout=10, check=False)
    return proc.returncode, proc.stdout


def job_state(project_id: str, runner: Runner | None = None) -> dict[str, Any]:
    """``loaded`` is True only on exit 0, ``None`` when launchctl cannot be asked."""
    label = LABEL_PREFIX + project_id
    try:
        code, _ = (runner or _run_launchctl)(["print", f"gui/{os.getuid()}/{label}"])
    except (OSError, subprocess.SubprocessError):
        return {"label": label, "loaded": None}
    return {"label": label, "loaded": code == 0}


def _approvals_without_tty(root: Path) -> list[Any] | None:
    """Approved events whose ``evidence.tty`` is not exactly ``True``; ``None`` when unreadable."""
    try:
        events = proposal_ledger.read_events(proposal_ledger.ledger_path(root))
    except (proposal_ledger.LedgerUnreadable, OSError, ValueError):
        return None
    return [event.get("proposal_id") for event in events
            if event.get("event") == proposal_ledger.APPROVED
            and not (isinstance(event.get("evidence"), Mapping) and event["evidence"].get("tty") is True)]


def check(store: DayStore, project_id: str, now: datetime, runner: Runner | None = None) -> dict:
    """The done check; ``status`` is ``ok`` only when ``unmet`` is empty."""
    window = _window(now, FRESH_RUN_DAYS)
    docs = {day: _load(store, day) for day in window}
    states = {day.isoformat(): _day_state(docs[day], day, project_id, now) for day in window}
    fresh_run = 0
    while fresh_run < len(window) and states[window[-1 - fresh_run].isoformat()] == "fresh":
        fresh_run += 1
    findings: dict[str, dict[str, list]] = {name: {} for name in DAY_CONDITIONS}
    for day, doc in docs.items():
        for name, items in (record_findings(doc) if isinstance(doc, Mapping) else {}).items():
            if name in findings and items:
                findings[name][day.isoformat()] = items
    approvals, max_gap = _approvals_without_tty(store.root), _max_gap_hours(docs, now)
    unmet = [name for name, failed in (
        ("store_empty", not store.list_days()), ("fresh_days", fresh_run < FRESH_RUN_DAYS),
        ("max_gap_hours", max_gap is None or max_gap > MAX_GAP_HOURS),
        *((name, bool(findings[name])) for name in DAY_CONDITIONS),
        ("ledger_unreadable", approvals is None), ("approval_without_tty", bool(approvals)),
    ) if failed]
    scanned = scan(store.root)
    unmet += [name for name, failed in (("zero_for_unknown", scanned["violations"] > 0),
                                        ("unreadable_files", bool(scanned["unreadable_files"])))
              if failed and name not in unmet]
    job = job_state(project_id, runner)
    stale = job["loaded"] is True and states[window[-1].isoformat()] != "fresh"
    return {"status": "stale" if stale else ("ok" if not unmet else "unmet"), "unmet": unmet,
            "project_id": project_id, "now": now.isoformat(), "job": job,
            "window": {"from": window[0].isoformat(), "to": window[-1].isoformat()},
            "fresh_days": fresh_run, "days": states, "max_gap_hours": max_gap, **findings,
            "approvals_without_tty": approvals, "scan": scanned}


def _day_value(doc: Any, metric_id: str, key: str | None) -> tuple[str | None, Any]:
    """``(exclusion reason, record)``; the reason is ``None`` for a usable day."""
    if not isinstance(doc, Mapping):
        return ("missing_day" if doc is None else _UNREADABLE), None
    if doc.get("status") != days.FINAL_STATUS:
        return "not_final", None
    record = baseline._lookup(metric_id, doc, key)
    if record is None:
        return "no_record", None
    if not baseline._is_number(record.get("value")):
        return "value_unknown", None
    if baseline.KIND[metric_id] == "quantile" and not baseline._samples(record)[0]:
        return "no_samples", None
    return None, record


def window_report(store: DayStore, metric_ref: str, last: int, now: datetime) -> dict:
    """The pooled value of ``ID[:KEY]`` over the last ``last`` settled days."""
    metric_id, _, key_text = metric_ref.partition(":")
    key = key_text or None
    if metric_id not in baseline.KIND or (metric_id in KEYED) != (key is not None):
        raise ValueError(f"unknown metric or key: {metric_ref!r} (a keyed id needs ID:KEY)")
    if not 1 <= last <= MAX_LAST_DAYS:
        raise ValueError(f"--last must be within 1..{MAX_LAST_DAYS}, got {last}")
    window = _window(now, last)
    used: list[tuple[str, Mapping[str, Any]]] = []
    excluded: dict[str, str] = {}
    for day in window:
        reason, record = _day_value(_load(store, day), metric_id, key)
        if reason is None:
            used.append((day.isoformat(), record))
        else:
            excluded[day.isoformat()] = reason
    samples = [baseline._samples(record) for _, record in used]
    result: dict[str, Any] = {
        "metric": metric_ref, "kind": baseline.KIND[metric_id],
        "window": {"from": window[0].isoformat(), "to": window[-1].isoformat()},
        "days_total": len(window), "days_with_value": len(used),
        "used_days": [stamp for stamp, _ in used], "excluded_days": excluded,
        "samples_truncated": any(cut for _, cut in samples),
        "pooled_value": None, "sample_size": 0, "unknown_reason": UNKNOWN_WINDOW}
    if used and baseline.KIND[metric_id] == "quantile":
        pooled = [sample for day_samples, _ in samples for sample in day_samples]
        result.update(pooled_value=baseline.quantile(pooled, baseline._QUANTILE_Q[metric_id]),
                      sample_size=len(pooled), unknown_reason=None)
    elif used:
        result.update(pooled_value=statistics.fmean(float(r["value"]) for _, r in used),
                      sample_size=len(used), unknown_reason=None)
    return result


def run(args: Any) -> int:
    project_id, now, store = _resolve(args)
    if project_id is None:
        print("error: --project-id or VNX_PROJECT_ID is required", file=sys.stderr)
        return EXIT_USAGE
    if args.check:
        result = check(store, project_id, now)
        code = EXIT_OK if result["status"] == "ok" else EXIT_UNMET
    elif args.scan_zero_for_unknown:
        result = scan(store.root)
        clean = not (result["violations"] or result["unreadable_files"] or result["store_empty"])
        code = EXIT_OK if clean else EXIT_UNMET
    else:
        result, code = window_report(store, args.metric, args.last, now), EXIT_OK
    print(json.dumps(result, sort_keys=True))
    return code
