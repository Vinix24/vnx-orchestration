"""``propose``: the rule table over the newest settled day, into the ledger.

Decision 26: only the newest settled day yields proposals, so a catch-up over
five days proposes for one day and never for fifteen. The metric day must be
``final`` in its base file; anything else (an ``incomplete`` day, a missing
day, an unreadable status) prints one JSON line with ``reason
"day_not_final"`` and writes nothing.

The command is discovered like every other command module (decision 4): this
file exposes ``register(subparsers)``, ``run(args)`` and ``DAILY_STEP = 10``.
``run`` accepts both call shapes: the CLI (``store`` a path string or ``None``,
``now`` an ISO string or ``None``) and ``daily`` (``store`` a ``DayStore``,
``now`` a ``datetime``). The ledger's own retry-safe limit stays where it is;
this module only hands it the candidates of the one metric day.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from daily_metrics import days, proposal_ledger, proposal_rules
from daily_metrics.store import DayStore, default_store_dir

#: Decision 14: ``daily`` runs the stepped commands ordered by ``DAILY_STEP``.
DAILY_STEP = 10

#: Decision 26: the baseline window before the metric day.
WINDOW_DAYS = 7

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


def register(subparsers: Any) -> None:
    """Add the ``propose`` subcommand; the shared parser is not passed to commands."""
    parser = subparsers.add_parser(
        "propose", help="propose metric improvements from the newest settled day")
    parser.add_argument("--project-id", dest="project_id", default=None)
    parser.add_argument("--now", dest="now", default=None)
    parser.add_argument("--store", dest="store", default=None)
    parser.add_argument("--state-dir", dest="state_dir", default=None)


def _parse_now(value: Any) -> datetime:
    """An aware UTC datetime from the ``daily`` context or an ISO string, else now."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if value is None:
        return datetime.now(timezone.utc)
    text = value.strip()
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"--now must be an ISO datetime, got {value!r}") from exc
    if parsed.utcoffset() is None:
        raise ValueError("--now must be an aware datetime (Z accepted)")
    return parsed.astimezone(timezone.utc)


def _resolve(args: Any) -> tuple[str | None, datetime | None, DayStore | None]:
    """Normalise both call shapes to ``(project_id, now, store)``; no id is all ``None``."""
    project_id = ((getattr(args, "project_id", None)
                   or os.environ.get("VNX_PROJECT_ID") or "").strip() or None)
    if project_id is None:
        return None, None, None
    store = getattr(args, "store", None)
    if not isinstance(store, DayStore):
        store = DayStore(Path(store) if store else default_store_dir(project_id), project_id)
    return project_id, _parse_now(getattr(args, "now", None)), store


def _window(store: DayStore, day: date) -> dict[date, dict[str, Any]]:
    """The metric day and the seven days before it; a missing day is left out."""
    documents: dict[date, dict[str, Any]] = {}
    for offset in range(WINDOW_DAYS, -1, -1):
        window_day = day - timedelta(days=offset)
        document = store.load(window_day.isoformat())
        if document is not None:
            documents[window_day] = document
    return documents


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def run(args: Any) -> int:
    """Propose for the newest settled day and print one JSON line; see the module doc."""
    project_id, now, store = _resolve(args)
    if project_id is None:
        print("error: --project-id or VNX_PROJECT_ID is required", file=sys.stderr)
        return EXIT_USAGE
    day = days.latest_settled_day(now)
    if store.base_status(day.isoformat()) != days.FINAL_STATUS:
        _emit({"metric_day": day.isoformat(), "proposed": [], "reason": proposal_rules.DAY_NOT_FINAL})
        return EXIT_OK
    evaluation = proposal_rules.evaluate(day, _window(store, day))
    path = proposal_ledger.ledger_path(store.root)
    try:
        written = proposal_ledger.append_proposals(
            path, project_id=project_id, metric_day=day,
            candidates=evaluation.candidates, rule_version=proposal_rules.RULE_VERSION,
            recorded_at=now.isoformat())
    except proposal_ledger.LedgerUnreadable as exc:
        print(f"error: unreadable ledger {path}: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    _emit({"metric_day": day.isoformat(), "proposed": written,
           "candidates": len(evaluation.candidates),
           "skipped": dict(evaluation.skipped)})
    return EXIT_OK
