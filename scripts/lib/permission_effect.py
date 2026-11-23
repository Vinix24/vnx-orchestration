"""The effect of an applied ``permissie`` rule (permissieblokkades-leren L7a2, ADR-086 decisions 1, 3 and 4).

Anchor: ``applied_at`` of the one ``applied`` record of the proposal that carries ``applied_evidence`` (L7a's checked
settings record); any other applied record anchors nothing. Window: 7 days before and 7 days after the anchor, read
over the live ledger and the archive. A count is distinct sessions in which the proposal's form was asked: an event
whose (vorm, origin, account, scope, kind) hashes to the digest in the proposal's ``oorzaak`` (the identity
``permission_proposals._item`` built it from) and whose ``besluit`` is a blockade, so every besluit but ``executed``.
Read only; a drop is always both numbers, never a percentage."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import permission_events as pe
import steering_proposal_store as store

WINDOW = timedelta(days=7)
KIND = "permissie"
ASKED = pe.BESLUITEN - {"executed"}
EVENT_SOURCES = pe.BRONNEN - {"B4b"}  # the sources an ask comes from, checked when no event names its own


def _ts(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError, TypeError):  # vnx-silent-except: None is no anchor, the caller says so
        return None
    return parsed if parsed.tzinfo is not None else None


def anchor(pid: str, project_id: str, decisions: List[Mapping[str, Any]]) -> Optional[datetime]:
    """``applied_at`` of the single applied record of ``pid`` in ``project_id`` with a non-empty ``applied_evidence``;
    ``None`` for no record, two records, a record without evidence or an ``applied_at`` that is not a timezone-aware time."""
    applied = [r for r in decisions if r.get("decision") == "applied" and r.get("proposal_id") == pid
               and r.get("project_id") == project_id]
    if len(applied) != 1 or not isinstance(applied[0].get("applied_evidence"), dict) or not applied[0]["applied_evidence"]:
        return None
    return _ts(applied[0].get("applied_at"))


def _form(oorzaak: str):
    """``(account, scope, digest)`` of a ``permission_form:permissie:<account>:<scope>:<digest>`` cause; raises otherwise."""
    parts = oorzaak.split(":")
    if len(parts) != 5 or parts[:2] != ["permission_form", KIND] or not all(parts[2:]):
        raise ValueError("oorzaak is not a permissie form")
    return parts[2], parts[3], parts[4]


def _matches(event: Mapping[str, Any], account: str, scope: str, digest: str) -> bool:
    key = "|".join((event["vorm"], event["origin"], account, scope, KIND))
    return event["account"] == account and event["besluit"] in ASKED and hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] == digest


def measure(entry: Mapping[str, Any], decisions: List[Mapping[str, Any]], coverage: Mapping[str, Any],
            project_id: str, state_dir: Path, now: datetime) -> Dict[str, Any]:
    """One measurement: ``pending``, ``unknown`` with a ``reden``, or ``measured`` with both session counts."""
    out: Dict[str, Any] = {"proposal_id": entry["proposal_id"]}
    when = anchor(entry["proposal_id"], project_id, decisions)
    if when is None:
        return {**out, "outcome": "unknown", "reden": "no_anchor"}
    out["applied_at"] = when.isoformat()
    if now - when < WINDOW:
        return {**out, "outcome": "pending"}
    account, scope, digest = _form(entry["oorzaak"])
    events, skipped, held_from = pe.read_window(project_id, "permission_events", when - WINDOW, when + WINDOW, state_dir)
    if held_from is None or held_from > when - WINDOW:
        return {**out, "outcome": "unknown", "reden": "retention_expired"}
    hits = [e for e in events if _matches(e, account, scope, digest)]
    sources = {e["bron"] for e in hits} or EVENT_SOURCES
    if not all(isinstance(coverage.get(b), Mapping) and coverage[b].get("state") == "ok" for b in sources):
        return {**out, "outcome": "unknown", "reden": "coverage_not_ok"}
    before = {e["session_id"] for e in hits if _ts(e["ts"]) < when}
    after = {e["session_id"] for e in hits if _ts(e["ts"]) >= when}
    return {**out, "outcome": "measured", "sessions_before": len(before), "sessions_after": len(after),
            "skipped_lines": sum(skipped.values())}


def section(state_dir: Path, project_id: str, now: datetime) -> List[Dict[str, Any]]:
    """One entry per applied ``permissie`` proposal; a failure is one error entry, never raised."""
    try:
        entries, decisions = store.read_proposals(Path(state_dir), project_id)
        coverage = pe._load_coverage(Path(state_dir) / "permission_coverage.json")[0]
    except Exception as exc:  # the chain report carries the failure as one entry; the chain goes on
        return [{"outcome": "error", "reden": f"store or coverage unreadable: {type(exc).__name__}"}]
    result = []
    for entry in (e for e in entries if e["artefact_soort"] == KIND and e["status"] == "applied"):
        try:
            result.append(measure(entry, decisions, coverage, project_id, Path(state_dir), now))
        except Exception as exc:  # one failing measurement is its own error entry; the others are still measured
            result.append({"proposal_id": entry["proposal_id"], "outcome": "error", "reden": type(exc).__name__})
    return result
