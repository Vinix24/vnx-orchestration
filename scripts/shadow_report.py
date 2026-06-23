#!/usr/bin/env python3
"""CLI: aggregated divergence report from shadow_divergence.ndjson.

Usage:
  scripts/shadow_report.py --since 24h
  scripts/shadow_report.py --since 7d --project-id mc
  scripts/shadow_report.py --severity hard
  scripts/shadow_report.py --by-table
  scripts/shadow_report.py --by-metric
  scripts/shadow_report.py --json
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from shadow_logger import LEDGER_FILENAME, LOCK_FILENAME  # noqa: E402

SEVERITY_ORDER = ["hard", "soft", "aggregate", "advisory"]


def _resolve_ledger_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    try:
        from project_root import resolve_state_dir

        return resolve_state_dir(__file__) / LEDGER_FILENAME
    except Exception:
        return Path(".vnx-data/state") / LEDGER_FILENAME


def _parse_duration(s: str) -> datetime.timedelta:
    s = s.strip()
    if s.endswith("h"):
        return datetime.timedelta(hours=int(s[:-1]))
    if s.endswith("d"):
        return datetime.timedelta(days=int(s[:-1]))
    raise ValueError(f"Unrecognized duration format: {s!r}. Use Xh or Xd.")


def _load_events(ledger_path: Path) -> tuple[list[dict[str, Any]], int]:
    """Return (events, skipped_count). Holds LOCK_SH during read to avoid partial-write races."""
    if not ledger_path.exists():
        return [], 0
    events: list[dict[str, Any]] = []
    skipped = 0
    with ledger_path.open("r", encoding="utf-8", errors="replace") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        raw = fh.read()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            skipped += 1
    return events, skipped


def _parse_ts(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _filter_events(
    events: list[dict],
    *,
    since: datetime.timedelta,
    project_id: str | None,
    severity: str | None,
) -> list[dict]:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - since
    out = []
    for ev in events:
        ts = _parse_ts(ev.get("timestamp_iso"))
        if ts is None or ts < cutoff:
            continue
        if project_id and ev.get("project_id") != project_id:
            continue
        if severity and ev.get("severity") != severity:
            continue
        out.append(ev)
    return out


def _count_by_severity(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for ev in events:
        counts[ev.get("severity", "unknown")] += 1
    return dict(counts)


def _count_by_metric(events: list[dict]) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    for ev in events:
        mid = ev.get("metric_id")
        if isinstance(mid, int):
            counts[mid] += 1
    return dict(counts)


def _count_by_project(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for ev in events:
        pid = ev.get("project_id", "unknown")
        counts[pid] += 1
    return dict(counts)


def _group_by_table(events: list[dict]) -> dict[tuple[str, str], int]:
    groups: dict[tuple[str, str], int] = defaultdict(int)
    for ev in events:
        pid = ev.get("project_id", "unknown")
        table = (ev.get("detail") or {}).get("table", "unknown")
        groups[(pid, table)] += 1
    return dict(groups)


def _human_report(events: list[dict], since_label: str, skipped_count: int = 0) -> str:
    lines: list[str] = []
    lines.append(f"Shadow divergence report — last {since_label}")
    lines.append("=" * 38)

    if skipped_count:
        lines.append(f"WARNING: {skipped_count} malformed line(s) skipped (partial writes or corruption)")

    if not events:
        lines.append("Total events: 0")
        lines.append("(no divergences in window)")
        return "\n".join(lines)

    by_sev = _count_by_severity(events)
    by_met = _count_by_metric(events)
    by_proj = _count_by_project(events)

    lines.append(f"Total events: {len(events)}")

    sev_parts = ", ".join(
        f"{s}={by_sev.get(s, 0)}" for s in SEVERITY_ORDER if s in by_sev or s in ("hard", "soft")
    )
    lines.append(f"By severity: {sev_parts}")

    met_parts = ", ".join(f"{m}={by_met.get(m, 0)}" for m in range(1, 7))
    lines.append(f"By metric: {met_parts}")

    proj_parts = ", ".join(f"{p}={c}" for p, c in sorted(by_proj.items()))
    lines.append(f"By project: {proj_parts}")

    hard_events = [ev for ev in events if ev.get("severity") == "hard"]
    if hard_events:
        lines.append("")
        lines.append("HARD violations:")
        for ev in hard_events:
            detail = ev.get("detail") or {}
            rid = ev.get("read_site", "")
            missing = detail.get("missing_in_central", [])
            dispatch = detail.get("dispatch_id", ev.get("project_id", ""))
            if missing:
                for m in missing[:3]:
                    lines.append(f"  - PR-scoped blocking finding mismatch: dispatch={dispatch}, missing={m}")
            else:
                lines.append(f"  - metric={ev.get('metric_id')} {rid}: legacy={ev.get('legacy_count')} central={ev.get('central_count')}")

    soft_events = [ev for ev in events if ev.get("severity") == "soft"]
    if soft_events:
        by_table = _group_by_table(soft_events)
        lines.append("")
        lines.append("Top SOFT violations:")
        for (pid, table), count in sorted(by_table.items(), key=lambda x: -x[1])[:5]:
            sample = next((ev for ev in soft_events if (ev.get("project_id") == pid and (ev.get("detail") or {}).get("table") == table)), None)
            drift = (sample.get("detail") or {}).get("drift_pct", 0) if sample else 0
            drift_pct = f"{drift * 100:.4f}%" if drift else ""
            if drift_pct:
                lines.append(f"  - count drift on ({pid}, {table}): {count} events (max drift {drift_pct})")
            else:
                lines.append(f"  - divergence on ({pid}, {table}): {count} events")

    return "\n".join(lines)


def _json_report(events: list[dict], since_label: str, skipped_count: int = 0) -> str:
    by_sev = _count_by_severity(events)
    by_met = _count_by_metric(events)
    by_proj = _count_by_project(events)

    summary = {
        "since": since_label,
        "total_events": len(events),
        "skipped_lines": skipped_count,
        "by_severity": {s: by_sev.get(s, 0) for s in SEVERITY_ORDER},
        "by_metric": {str(m): by_met.get(m, 0) for m in range(1, 7)},
        "by_project": dict(sorted(by_proj.items())),
        "hard_count": by_sev.get("hard", 0),
        "soft_count": by_sev.get("soft", 0),
    }
    return json.dumps(summary, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow divergence report from NDJSON ledger")
    parser.add_argument("--since", default="24h", help="Duration window: Xh or Xd (default 24h)")
    parser.add_argument("--project-id", default=None, help="Filter to one project")
    parser.add_argument("--severity", choices=["hard", "soft", "aggregate", "advisory"], default=None)
    parser.add_argument("--by-table", action="store_true", help="Group by (project_id, table)")
    parser.add_argument("--by-metric", action="store_true", help="Group by metric_id")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON summary")
    parser.add_argument("--ledger", default=None, help="Override ledger path")
    args = parser.parse_args()

    try:
        since_delta = _parse_duration(args.since)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    ledger_path = _resolve_ledger_path(args.ledger)
    all_events, skipped_count = _load_events(ledger_path)

    events = _filter_events(
        all_events,
        since=since_delta,
        project_id=args.project_id,
        severity=args.severity,
    )

    if args.by_table:
        by_table = _group_by_table(events)
        if args.json_output:
            out = {f"{p}:{t}": c for (p, t), c in sorted(by_table.items())}
            out["skipped_lines"] = skipped_count
            print(json.dumps(out, indent=2))
        else:
            if skipped_count:
                print(f"WARNING: {skipped_count} malformed line(s) skipped")
            print(f"By (project, table) — last {args.since}:")
            for (pid, table), count in sorted(by_table.items(), key=lambda x: -x[1]):
                print(f"  ({pid}, {table}): {count}")
        return 0

    if args.by_metric:
        by_met = _count_by_metric(events)
        if args.json_output:
            out = {str(m): by_met.get(m, 0) for m in range(1, 7)}
            out["skipped_lines"] = skipped_count
            print(json.dumps(out, indent=2))
        else:
            if skipped_count:
                print(f"WARNING: {skipped_count} malformed line(s) skipped")
            print(f"By metric — last {args.since}:")
            for m in range(1, 7):
                print(f"  metric {m}: {by_met.get(m, 0)}")
        return 0

    if args.json_output:
        print(_json_report(events, args.since, skipped_count))
    else:
        print(_human_report(events, args.since, skipped_count))

    return 0


if __name__ == "__main__":
    sys.exit(main())
