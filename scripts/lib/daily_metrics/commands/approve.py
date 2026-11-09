"""``approve``: the operator's gate that freezes the baseline into an approved event.

Decision 27: an approval is a handling an agent cannot perform through a tool
call or a headless lane. It refuses when ``VNX_AGENT_ID`` is set, when stdin or
stdout is not a terminal, and when the operator does not type the proposal id
back. Decision 26: the baseline is frozen here, so the later verdict compares
against what the operator saw. There is no ``DAILY_STEP``: approval never runs
from ``daily``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from daily_metrics import proposal_ledger as ledger
from daily_metrics import store as store_mod
from daily_metrics.baseline import WINDOW_DAYS, baseline
from vnx_identity import ENV_AGENT

EXIT_OK = 0
EXIT_UNREADABLE = 1
EXIT_REFUSED = 2
OPERATOR_ENV = "VNX_OPERATOR_ID"


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("approve", help="approve one proposal (operator terminal only)")
    parser.add_argument("proposal_id", metavar="PROPOSAL_ID")
    parser.add_argument("--action", required=True, metavar="TEXT")
    for option in ("--project-id", "--store", "--now"):
        parser.add_argument(option, dest=option[2:].replace("-", "_"), default=None)


def _refuse(message: str) -> int:
    print(f"refused: {message}", file=sys.stderr)
    return EXIT_REFUSED


def _is_tty(stream: Any) -> bool:
    try:
        return os.isatty(stream.fileno())
    except (ValueError, OSError):  # vnx-silent-except: a stream without a descriptor is no terminal, so the gate refuses
        return False


def _parse_now(value: str | None) -> datetime:
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


def _frozen(store: store_mod.DayStore, metric_id: str, seat: str | None,
            approval_day: date) -> tuple[dict[str, Any], int | None]:
    window = {}
    for offset in range(WINDOW_DAYS, 0, -1):
        day = approval_day - timedelta(days=offset)
        doc = store.load(day.isoformat())
        if doc is not None:
            window[day] = doc
    result = baseline(metric_id, approval_day, window, key=seat)
    used = result.details.get("used_days", [])
    finals = [window[day] for day in sorted(window) if window[day].get("status") == "final"]
    schema = finals[-1].get("schema") if finals else None
    frozen = {"value": result.value, "sample_size": result.sample_size,
              "unknown_reason": result.unknown_reason, "used_days": used}
    return frozen, schema


def _show(proposal: dict[str, Any], frozen: dict[str, Any]) -> None:
    print(f"proposal {proposal['proposal_id']}")
    for name in ("key", "value", "baseline", "metric_day"):
        print(f"  {name}: {proposal.get(name)}")
    print(f"  frozen baseline: {json.dumps(frozen, sort_keys=True)}")
    print("  the verdict reads days a+1 to a+7, a being the approval day")
    print("Type the proposal id to approve: ", end="", flush=True)


def _event(proposal: dict[str, Any], args: argparse.Namespace, now: datetime,
           frozen: dict[str, Any], schema: int | None) -> dict[str, Any]:
    return {
        "event": ledger.APPROVED,
        "proposal_id": proposal["proposal_id"],
        "project_id": proposal.get("project_id"),
        "key": proposal.get("key"),
        "metric_id": proposal.get("metric_id"),
        "seat": proposal.get("seat"),
        "metric_day": proposal.get("metric_day"),
        "approved_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "approval_day": now.date().isoformat(),
        "approved_by": "operator",
        "action": args.action,
        "evidence": {"tty": True, "user": getpass.getuser(), "host": socket.gethostname(),
                     "vnx_operator_id": os.environ.get(OPERATOR_ENV) or None},
        "frozen_baseline": frozen,
        "schema": schema,
    }


def _gate_refusal(args: argparse.Namespace) -> str | None:
    if os.environ.get(ENV_AGENT):
        return f"{ENV_AGENT} is set; an agent may not approve"
    if not (_is_tty(sys.stdin) and _is_tty(sys.stdout)):
        return "stdin and stdout must both be a terminal"
    if not (args.project_id or os.environ.get("VNX_PROJECT_ID") or "").strip():
        return "--project-id or VNX_PROJECT_ID is required"
    return None


def _open_proposal(events: list[dict[str, Any]], proposal_id: str) -> tuple[dict | None, str | None]:
    reason = ledger.approval_refusal(events, proposal_id)
    if reason is not None:
        return None, reason
    proposal = next(e for e in events
                    if e.get("event") == ledger.PROPOSED and e.get("proposal_id") == proposal_id)
    return proposal, None


def run(args: argparse.Namespace) -> int:
    reason = _gate_refusal(args)
    if reason is not None:
        return _refuse(reason)
    project_id = (args.project_id or os.environ.get("VNX_PROJECT_ID")).strip()
    root = Path(args.store) if args.store else store_mod.default_store_dir(project_id)
    path = ledger.ledger_path(root)
    try:
        proposal, reason = _open_proposal(ledger.read_events(path), args.proposal_id)
        if proposal is None:
            return _refuse(reason or "unknown_proposal")
        now = _parse_now(args.now)
        frozen, schema = _frozen(store_mod.DayStore(root, project_id), proposal["metric_id"],
                                 proposal.get("seat"), now.date())
        _show(proposal, frozen)
        if sys.stdin.readline().strip() != args.proposal_id:
            return _refuse("the typed id does not match the proposal id")
        ledger.append_approval(path, _event(proposal, args, now, frozen, schema))
    except ledger.ApprovalRefused as exc:
        return _refuse(exc.reason)
    except ledger.LedgerUnreadable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    print(json.dumps({"approved": args.proposal_id, "approval_day": now.date().isoformat()}))
    return EXIT_OK
