#!/usr/bin/env python3
"""router_baseline.py — repeatable nulmeting over the dispatch receipts ledger.

Measures what traffic does WITHOUT the smart router, over the existing receipt
ledger, so a post-rollout run can be placed next to it and the two compared.
Not a one-off script and not a number in a markdown file: the same command, run
against the same data, yields the same output — and the same command run after
the canary rollout yields the delta.

Usage:
  python3 scripts/lib/router_baseline.py
  python3 scripts/lib/router_baseline.py --receipts ~/.vnx-data/vnx-dev/state/t0_receipts.ndjson
  python3 scripts/lib/router_baseline.py --since 2026-08-03 --json

Window: --since 2026-08-03 (default). Before that the model field is dirty
(5416 `unknown` values) and poisons every distribution.

Exit codes:
  0  measurement printed
  2  fatal (receipts file missing/unreadable, no matching receipts)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_RECEIPTS = "~/.vnx-data/vnx-dev/state/t0_receipts.ndjson"
DEFAULT_SINCE = "2026-08-03"

# Outcome buckets (status -> bucket). The ledger is known to carry exactly
# these; anything else lands in "other" so a new status cannot silently inflate
# success or failure.
_SUCCESS = frozenset({"success", "done"})
_FAILURE = frozenset({
    "failed", "failure", "timeout", "contract_invalid",
    "not_executable", "guard_error",
})


@dataclass
class BaselineReport:
    since: str
    total: int
    providers: dict[str, int] = field(default_factory=dict)
    models: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    durations_by_lane: dict[str, dict[str, float]] = field(default_factory=dict)


def _norm_ts(ts) -> Optional[str]:
    """Normalise a receipt timestamp to an ISO-8601 string.

    The ledger mixes string timestamps and epoch numbers; both collapse to the
    same comparable form so the ``since`` cutoff is correct either way.
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(ts)


def _lane_key(record: dict) -> str:
    """Grouping key for doorlooptijd.

    The ledger split schema mid-stream: ``task_complete`` receipts carry
    ``duration_seconds`` + ``provider`` but no ``lane``; ``subprocess_completion``
    receipts carry ``lane`` but no duration. Group doorlooptijd by lane when
    present, else by provider, so the duration-carrying receipts land under a
    meaningful key instead of a shared "?".
    """
    return record.get("lane") or record.get("provider") or "?"


def load_receipts(path: Path, since: str) -> list[dict]:
    """Read the NDJSON ledger, keeping receipts with timestamp >= since.

    Unparseable lines are skipped (the ledger is append-only and may carry a
    torn last write); a missing file raises FileNotFoundError for the CLI.
    """
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _norm_ts(record.get("timestamp"))
            if ts and ts >= since:
                out.append(record)
    return out


def _outcome(status: Optional[str]) -> str:
    s = (status or "").strip().lower()
    if s in _SUCCESS:
        return "success"
    if s in _FAILURE:
        return "failed"
    return "other"


def _duration_stats(values: list[float]) -> dict[str, float]:
    values = sorted(values)
    n = len(values)
    p95_idx = min(n - 1, int(n * 0.95))
    return {
        "n": n,
        "mean": round(sum(values) / n, 1),
        "median": round(values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2, 1),
        "p95": round(values[p95_idx], 1),
        "min": round(values[0], 1),
        "max": round(values[-1], 1),
    }


def baseline(records: list[dict], since: str) -> BaselineReport:
    """Aggregate the nulmeting over a window of receipts (pure, deterministic)."""
    providers: Counter = Counter()
    models: Counter = Counter()
    outcomes: Counter = Counter()
    durations: dict[str, list[float]] = defaultdict(list)

    for r in records:
        providers[r.get("provider") or "?"] += 1
        models[r.get("model") or "?"] += 1
        outcomes[_outcome(r.get("status"))] += 1
        d = r.get("duration_seconds")
        if d is not None:
            try:
                durations[_lane_key(r)].append(float(d))
            except (TypeError, ValueError):
                continue

    return BaselineReport(
        since=since,
        total=len(records),
        providers=dict(providers),
        models=dict(models),
        outcomes=dict(outcomes),
        durations_by_lane={
            lane: _duration_stats(vals) for lane, vals in sorted(durations.items())
        },
    )


def _sorted_items(d: dict) -> list[tuple[str, int]]:
    # Deterministic ordering: count desc, then name asc — identical every run.
    return sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))


def render(report: BaselineReport) -> str:
    lines = [
        "Router baseline — nulmeting over de receipts (zonder smart router)",
        f"window: >= {report.since} | receipts: {report.total}",
        "",
        "Provider distribution:",
    ]
    for name, count in _sorted_items(report.providers):
        lines.append(f"  {name:<30} {count}")
    lines += ["", "Model distribution:"]
    for name, count in _sorted_items(report.models):
        lines.append(f"  {name:<30} {count}")
    lines += ["", "Outcome distribution (success/fail):"]
    for bucket in ("success", "failed", "other"):
        lines.append(f"  {bucket:<30} {report.outcomes.get(bucket, 0)}")
    lines += ["", "Doorlooptijd (duration_seconds) per lane:"]
    if not report.durations_by_lane:
        lines.append("  (no receipts carry duration_seconds in this window)")
    for lane, stats in report.durations_by_lane.items():
        lines.append(
            f"  {lane:<30} n={stats['n']:<5} mean={stats['mean']:>8}s "
            f"median={stats['median']:>8}s p95={stats['p95']:>8}s "
            f"min={stats['min']:>6}s max={stats['max']:>8}s"
        )
    lines += [
        "",
        "note: duration_seconds is recorded on task_complete receipts (provider-",
        "lane dispatches, no `lane` field); subprocess_completion receipts carry a",
        "`lane` but no duration. Grouping falls back to provider when lane is absent.",
    ]
    return "\n".join(lines)


def _as_json(report: BaselineReport) -> str:
    payload = {
        "since": report.since,
        "total": report.total,
        "providers": dict(_sorted_items(report.providers)),
        "models": dict(_sorted_items(report.models)),
        "outcomes": {
            "success": report.outcomes.get("success", 0),
            "failed": report.outcomes.get("failed", 0),
            "other": report.outcomes.get("other", 0),
        },
        "durations_by_lane": {
            lane: report.durations_by_lane[lane]
            for lane in sorted(report.durations_by_lane)
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="router_baseline",
        description="Repeatable nulmeting over the dispatch receipts ledger (pre-rollout baseline).",
    )
    parser.add_argument(
        "--receipts",
        default=DEFAULT_RECEIPTS,
        help=f"path to the receipts NDJSON (default: {DEFAULT_RECEIPTS})",
    )
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help=f"only receipts with timestamp >= this (default: {DEFAULT_SINCE})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the human-readable table",
    )
    args = parser.parse_args(argv)

    path = Path(args.receipts).expanduser()
    if not path.is_file():
        print(f"router_baseline: receipts file not found: {path}", file=sys.stderr)
        return 2

    records = load_receipts(path, args.since)
    if not records:
        print(
            f"router_baseline: no receipts with timestamp >= {args.since} in {path}",
            file=sys.stderr,
        )
        return 2

    report = baseline(records, args.since)
    print(_as_json(report) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
