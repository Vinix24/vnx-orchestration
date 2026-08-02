#!/usr/bin/env python3
"""guard_stats.py — guard-fired counter: how often does each guard fire?

A guard that evaluates to False (does not fire) for 30 straight days is either
a healthy guard or a dead guard — and from the outside both look identical,
because a non-firing guard disappears into ``logger.debug``. This module makes
the difference visible by recording every evaluation of an instrumented guard
to ``<state_dir>/guard_evaluations.ndjson``:

    {"timestamp", "event_type": "guard_evaluation", "guard", "fired", "detail"}

Hard contract: the counter is OBSERVE-ONLY. ``record_guard_evaluation`` never
raises and never alters the guard's verdict — instrumentation wraps a guard's
return value, it does not touch the decision.

``summarize`` turns the stream into per-guard stats, flagging
``suspect_silent``: guards with a long evaluation span that NEVER fired (the
"100% False for 30 days" case), and ``never_evaluated`` is impossible to say
from data — which is precisely why evaluation, not just firing, is recorded.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ndjson_io import fsync_fileno, iter_ndjson

_LOG = logging.getLogger(__name__)

GUARD_EVALUATIONS_FILENAME = "guard_evaluations.ndjson"
# A guard with an evaluation span of at least this many days and zero firings
# is flagged suspect_silent.
SUSPECT_SILENT_DAYS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_state_dir() -> Optional[Path]:
    env = os.environ.get("VNX_STATE_DIR")
    if env:
        return Path(env)
    data_dir = os.environ.get("VNX_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "state"
    return None


def guard_evaluations_path(state_dir: Optional[Path] = None) -> Optional[Path]:
    base = Path(state_dir) if state_dir else _default_state_dir()
    return base / GUARD_EVALUATIONS_FILENAME if base else None


def record_guard_evaluation(
    guard: str,
    fired: bool,
    *,
    detail: Optional[Dict[str, Any]] = None,
    state_dir: Optional[Path] = None,
) -> bool:
    """Append one guard evaluation. OBSERVE-ONLY: never raises, so it can sit
    on any guard's return path without changing semantics. Returns True when
    the record was written."""
    try:
        path = guard_evaluations_path(state_dir)
        if path is None:
            _LOG.debug("guard_stats: no state dir resolvable — evaluation of %s not recorded", guard)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        record: Dict[str, Any] = {
            "timestamp": _now_iso(),
            "event_type": "guard_evaluation",
            "guard": guard,
            "fired": bool(fired),
        }
        if detail:
            record["detail"] = detail
        with open(path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(json.dumps(record, ensure_ascii=False, default=str))
                fh.write("\n")
                fh.flush()
                fsync_fileno(fh, context=str(path))
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True
    except Exception as exc:  # noqa: BLE001 — observe-only: a counter failure must NEVER break a guard
        _LOG.warning("guard_stats: could not record evaluation of %s: %s", guard, exc)
        return False


def _parse_ts(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def summarize(
    state_dir: Optional[Path] = None,
    *,
    now: Optional[float] = None,
    suspect_silent_days: int = SUSPECT_SILENT_DAYS,
) -> Dict[str, Any]:
    """Per-guard evaluation stats from the NDJSON stream.

    Per guard: evaluations, fired count/pct, first/last evaluation, last
    firing, and ``suspect_silent`` — True when the guard has been evaluated
    over a span of >= ``suspect_silent_days`` days without EVER firing.
    """
    now = time.time() if now is None else now
    path = guard_evaluations_path(state_dir)
    guards: Dict[str, Dict[str, Any]] = {}
    if path is not None:
        for record in iter_ndjson(path):
            if not isinstance(record, dict) or record.get("event_type") != "guard_evaluation":
                continue
            name = record.get("guard")
            if not name:
                continue
            ts = _parse_ts(record.get("timestamp"))
            stats = guards.setdefault(
                name,
                {"evaluations": 0, "fired": 0, "first_eval_ts": None, "last_eval_ts": None, "last_fired_ts": None},
            )
            stats["evaluations"] += 1
            if record.get("fired"):
                stats["fired"] += 1
                if ts is not None and (stats["last_fired_ts"] is None or ts > stats["last_fired_ts"]):
                    stats["last_fired_ts"] = ts
            if ts is not None:
                if stats["first_eval_ts"] is None or ts < stats["first_eval_ts"]:
                    stats["first_eval_ts"] = ts
                if stats["last_eval_ts"] is None or ts > stats["last_eval_ts"]:
                    stats["last_eval_ts"] = ts

    out: Dict[str, Any] = {"guards": {}, "suspect_silent_days": suspect_silent_days}
    for name in sorted(guards):
        stats = guards[name]
        evaluations = stats["evaluations"]
        fired = stats["fired"]
        span_days = 0.0
        if stats["first_eval_ts"] is not None and stats["last_eval_ts"] is not None:
            span_days = (stats["last_eval_ts"] - stats["first_eval_ts"]) / 86400.0
        suspect = fired == 0 and span_days >= suspect_silent_days
        out["guards"][name] = {
            "evaluations": evaluations,
            "fired": fired,
            "fired_pct": round(100.0 * fired / evaluations, 1) if evaluations else 0.0,
            "span_days": round(span_days, 1),
            "last_fired": (
                datetime.fromtimestamp(stats["last_fired_ts"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if stats["last_fired_ts"] is not None
                else None
            ),
            "days_since_last_fired": (
                round((now - stats["last_fired_ts"]) / 86400.0, 1)
                if stats["last_fired_ts"] is not None
                else None
            ),
            "suspect_silent": suspect,
        }
    return out


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize guard-fired counters")
    parser.add_argument("--summary", action="store_true", help="Print per-guard stats as JSON")
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args(argv)
    result = summarize(Path(args.state_dir) if args.state_dir else None)
    print(json.dumps(result, indent=2, sort_keys=True))
    suspect = [g for g, s in result["guards"].items() if s["suspect_silent"]]
    return 11 if suspect else 0


if __name__ == "__main__":
    raise SystemExit(main())
