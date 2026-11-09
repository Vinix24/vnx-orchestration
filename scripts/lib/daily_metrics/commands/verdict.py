"""``verdict``: judge approved proposals on their seven settled post days.

Decision 26: the approval day ``a`` comes with the approval. The command refuses
while ``a+7`` is not settled, then compares ``a+1`` to ``a+7`` against the
frozen baseline; ``unknown`` never reads as ``not_helped``, and a day whose
``schema`` nobody recorded (``schema_missing``) is undetermined too.

With a proposal id it judges that one; without one, and from ``daily``, it
judges every approved proposal whose ``a+7`` is settled and leaves the others
alone. Both print ``{"judged": [...], "waiting": [...]}``. Discovered like every
other command (decision 4); both call shapes are normalised by ``propose``'s
helpers so they cannot drift.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from daily_metrics import days, proposal_ledger, verdict
from daily_metrics.commands.propose import _resolve
from daily_metrics.store import DayStore

#: Decision 14: ``daily`` runs the stepped commands ordered by ``DAILY_STEP``.
DAILY_STEP = 20

EXIT_OK = 0
EXIT_UNREADABLE = 1
EXIT_REFUSED = 2


def register(subparsers: Any) -> None:
    """Add the ``verdict`` subcommand; the shared parser is not passed to commands."""
    parser = subparsers.add_parser(
        "verdict", help="judge approved proposals on their seven post days")
    parser.add_argument("proposal_id", nargs="?", default=None, metavar="PROPOSAL_ID")
    for option in ("--project-id", "--now", "--store", "--state-dir"):
        parser.add_argument(option, dest=option[2:].replace("-", "_"), default=None)


def _days_after(approved: Mapping[str, Any], approval: date, now: datetime) -> int:
    """Whole days from ``approved_at`` to ``judged_at``; the dates when the stamp is unreadable."""
    try:
        stamp = datetime.fromisoformat(str(approved.get("approved_at")).replace("Z", "+00:00"))
        return (now - stamp).days
    except (TypeError, ValueError):
        return (now.date() - approval).days


def _event(approved: Mapping[str, Any], project_id: str, now: datetime, approval: date,
           result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event": proposal_ledger.VERDICT,
        "proposal_id": approved["proposal_id"],
        "project_id": approved.get("project_id") or project_id,
        "key": approved.get("key"),
        "metric_id": approved.get("metric_id"),
        "seat": approved.get("seat"),
        "metric_day": days.latest_settled_day(now).isoformat(),
        "verdict": result["verdict"],
        "reason": result["reason"],
        "approval_day": approval.isoformat(),
        "window": {"from": (approval + timedelta(days=1)).isoformat(),
                   "to": (approval + timedelta(days=verdict.POST_DAYS)).isoformat()},
        "final_days": result["final_days"],
        "post_value": result["post_value"],
        "post_n": result["post_n"],
        "frozen_baseline": verdict.frozen_value(approved),
        "improvement": result["improvement"],
        "judged_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days_after_approval": _days_after(approved, approval, now),
    }


def _judge_due(path: Any, events: Sequence[Mapping[str, Any]], proposal_id: str | None,
               project_id: str, now: datetime, store: DayStore) -> int:
    """Judge the named proposal, or every due one; ``a+7`` not settled is refused."""
    approved = [event for event in events
                if event.get("event") == proposal_ledger.APPROVED
                and event.get("project_id") in (None, project_id)]
    if proposal_id is not None:
        approved = [event for event in approved if event.get("proposal_id") == proposal_id]
        if not approved:
            print(f"error: no approved event for {proposal_id}", file=sys.stderr)
            return EXIT_REFUSED
        due = verdict.approval_day(approved[0]) + timedelta(days=verdict.POST_DAYS)
        if not days.is_settled(due, now):
            print(f"error: {proposal_id}: {due.isoformat()} is not settled yet", file=sys.stderr)
            return EXIT_REFUSED
    answered = {event.get("proposal_id") for event in events
                if event.get("event") == proposal_ledger.VERDICT}
    judged: list[str] = []
    waiting: list[str] = []
    for event in approved:
        proposal_id_value = event.get("proposal_id")
        if not isinstance(proposal_id_value, str):
            continue
        # An explicit id is judged through the ledger, so a second run is refused
        # there as ``already_judged`` instead of quietly reporting nothing.
        if proposal_id is None and proposal_id_value in answered:
            continue
        approval = verdict.approval_day(event)
        if not days.is_settled(approval + timedelta(days=verdict.POST_DAYS), now):
            waiting.append(proposal_id_value)
            continue
        post_days = {day: doc for day in verdict.window(event)
                     if (doc := store.load(day.isoformat())) is not None}
        result = verdict.judge(event, post_days)
        proposal_ledger.append_verdict(
            path, _event(event, project_id, now, approval, result))
        judged.append(proposal_id_value)
    print(json.dumps({"judged": judged, "waiting": waiting}))
    return EXIT_OK


def run(args: Any) -> int:
    """Judge one approved proposal or every due one; see the module doc."""
    project_id, now, store = _resolve(args)
    if project_id is None:
        print("error: --project-id or VNX_PROJECT_ID is required", file=sys.stderr)
        return EXIT_REFUSED
    path = proposal_ledger.ledger_path(store.root)
    try:
        return _judge_due(path, proposal_ledger.read_events(path),
                          getattr(args, "proposal_id", None), project_id, now, store)
    except proposal_ledger.LedgerUnreadable as exc:
        print(f"error: unreadable ledger {path}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    except proposal_ledger.ApprovalRefused as exc:
        print(f"refused: {exc.reason}", file=sys.stderr)
        return EXIT_REFUSED
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
