"""Permission events to proposals and notifications (permissieblokkades-leren L5b, ADR-065, ADR-075 decisions 8
and 9, ADR-082): a pure computation over its inputs, no I/O, no model, no store. Leren blijft voorstellen: no
code path here applies a rule. A hard stop (``permission_hard_stops.melding``) makes a group a notification
(``alleen_melding``) whatever its count; the stored ``vorm`` is never re-classified."""

from __future__ import annotations

import hashlib
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import permission_hard_stops as hard_stops
import steering_proposal_store as store

WINDOW = timedelta(days=7)
MIN_SESSIONS = 3
MELDING = "alleen_melding"
SCOPE = "project"
DOELPAD = {  # kind -> the file a proposal of that kind targets, relative to the project root
    "permissie": ".claude/settings.local.json",
    "bouwer_allowlist": ".vnx/worker_permissions.yaml",  # read by scripts/lib/worker_permissions.py
    "bouwgewoonte": ".claude/terminals/T0/role-orchestrator.md",  # § Worker Dispatch Standards
}
WRAPPERS = frozenset("bash sh zsh env timeout nohup xargs sudo command exec time python python3 node perl ruby".split())
K1_VERBS = frozenset(("ls", "cat", "head", "tail", "wc", "grep", "pwd", "date", "git status",
                      "gh pr view", "gh pr list", "gh pr checks", "gh run view", "gh run list"))  # frozen: read only
_PLACEHOLDER = re.compile(r"<|~/\.\.\.")  # every placeholder of permission_events.normalise
# The argument triggers of hard_stops.TABLE: ``prefix:*`` that one of them makes a hard stop allows it (rm:* -> rm -rf /)
COMPLETIONS = ("-rf x", "--delete x", ":x", "delete x", "secret x", "apply", "up", "--prod", "promote x")

def _ts(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError, TypeError):  # vnx-silent-except: None is counted as bad_ts by the caller
        return None
    return parsed if parsed.tzinfo is not None else None

def rule(vorm: str) -> Optional[str]:
    """The exact ``/permissions`` rule: the whole form, or the text before the first placeholder plus ``:*``."""
    cut = _PLACEHOLDER.search(vorm)
    prefix = (vorm[:cut.start()] if cut else vorm).strip()
    return (f"Bash({prefix}:*)" if cut else f"Bash({prefix})") if prefix else None

def handelingsklasse(vorm: str) -> str:
    """K1 for a verb in the frozen read-only table, K6 for anything else (never lower for an unknown verb)."""
    words = vorm.split()
    return "K1" if any(" ".join(words[:n]) in K1_VERBS for n in (1, 2, 3)) else "K6"

def _kind(origin: str, events: Sequence[Mapping[str, Any]]) -> str:
    if origin != "worker":
        return "permissie"
    refusals = [e["besluit"] for e in events if str(e.get("besluit", "")).startswith("refused_")]
    return "bouwer_allowlist" if refusals and all(b == "refused_config" for b in refusals) else "bouwgewoonte"

def _hard_stop(events: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The strictest notification reason in the group: a category, then indirection, then evidence_unavailable."""
    reasons = {r for r in (hard_stops.melding(e) for e in events) if r is not None}
    return min(reasons, key=lambda r: (r.startswith(hard_stops.EVIDENCE_UNAVAILABLE),
                                       r.startswith(hard_stops.INDIRECTION), r)) if reasons else None

def _blocked(vorm: str, kind: str, events: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Why the group can only be a notification (hard stop first), or None."""
    stop = _hard_stop(events)
    if stop:
        return stop
    if vorm.split()[0].rsplit("/", 1)[-1].lower() in WRAPPERS:  # APFS is case-insensitive: ``ENV`` runs env
        return "wrapper_prefix: a rule for a wrapper or interpreter lets any command through"
    regel = rule(vorm) or ""
    if regel.endswith(":*)") and handelingsklasse(vorm) != "K1":  # rg is K6: ``rg --pre <command>`` runs one per file
        return "wildcard_not_read_only: a wildcard rule for a verb outside the read-only table passes any argument, a hard stop included"
    if regel.endswith(":*)") and any(hard_stops.classify(f"{regel[5:-3]} {c}")[0] for c in COMPLETIONS):
        return "wildcard_reaches_hard_stop: the rule's prefix completes into a hard stop, so it is never an allow rule"
    if any(e.get("bron") == "B4b" for e in events):
        return "b4b_no_arguments: the CLI ledger keeps no arguments, so there is no safety evidence"
    if kind != "bouwgewoonte" and not regel:
        return "no_rule: no permission rule can be built from this form"
    return None

def _evidence(events: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """One {prompt_id, ts} per session: the event_id of that session's latest event."""
    latest: Dict[str, Tuple[datetime, str, str]] = {}
    for e in events:
        latest[e["session_id"]] = max(latest.get(e["session_id"], (cur := (_ts(e["ts"]), e["event_id"], e["ts"]))), cur)
    return [{"prompt_id": eid, "ts": ts} for _, eid, ts in sorted(latest.values())]

def _voorstel(kind: str, vorm: str, account: str, sessions: int) -> Tuple[str, Dict[str, str]]:
    if kind == "bouwgewoonte":
        return (f"Workers ran into `{vorm}` in {sessions} sessions: name the allowed route for it in the "
                "dispatch footer (§ Worker Dispatch Standards)."), {}
    regel, klasse = rule(vorm), handelingsklasse(vorm)
    if kind == "bouwer_allowlist":
        return (f"Add `{regel}` to the role's allowed_tools in {DOELPAD[kind]} ({sessions} sessions refused "
                "by config); never a grant for the operator."), {}
    risk = ("a read-only verb, the rule passes every argument to it" if klasse == "K1" else
            "not a known read-only verb, so it counts as changing a running system until the table says otherwise")
    return (f"Add `{regel}` via /permissions, scope {SCOPE} ({DOELPAD[kind]}), account {account}, class {klasse}: "
            f"{risk} ({sessions} sessions). Way back: remove the rule from {DOELPAD[kind]}."), {"handelingsklasse": klasse}

def _item(key: Tuple[str, ...], events: Sequence[Mapping[str, Any]], reason: Optional[str]) -> Dict[str, Any]:
    vorm, _origin, account, scope, kind = key
    digest = hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:16]
    item: Dict[str, Any] = {"oorzaak": f"permission_form:{kind}:{account}:{scope}:{digest}", "doelpad": DOELPAD[kind],
                            "evidence": _evidence(events), "account": account, "bereik": scope}
    if reason:
        return {**item, "artefact_soort": MELDING, "voorstel": reason}
    text, extra = _voorstel(kind, vorm, account, len(item["evidence"]))
    return {**item, **extra, "artefact_soort": kind, "voorstel": text}

def _groups(events: Sequence[Mapping[str, Any]], now: datetime, counts: Dict[str, int]) -> Dict[Tuple[str, str, str], list]:
    groups: Dict[Tuple[str, str, str], list] = {}
    for e in events:
        ts = _ts(e.get("ts"))
        if ts is None or not all(isinstance(e.get(f), str) for f in ("vorm", "origin", "account", "session_id", "event_id")):
            counts["bad_event"] += 1
        elif now - WINDOW < ts <= now:
            groups.setdefault((e["vorm"], e["origin"], e["account"]), []).append(e)
    return groups

def build(events: Sequence[Mapping[str, Any]], coverage: Mapping[str, Any], now: datetime) -> tuple:
    """(proposals, meldingen, counts). Identity (vorm, origin, account, scope, audience); count = distinct sessions."""
    counts = dict.fromkeys(("bad_event", "unknown_identity", "coverage_not_ok", "below_threshold"), 0)
    proposals: List[Dict[str, Any]] = []
    meldingen: List[Dict[str, Any]] = []
    for (vorm, origin, account), group in sorted(_groups(events, now, counts).items()):
        if origin not in ("t0", "operator", "worker") or account not in ("vnx", "sm") or not vorm.strip():
            counts["unknown_identity"] += 1
            continue
        if not all(isinstance(coverage.get(e.get("bron")), Mapping) and coverage[e["bron"]].get("state") == "ok" for e in group):
            counts["coverage_not_ok"] += 1
            continue
        if len({e["session_id"] for e in group}) < MIN_SESSIONS:
            counts["below_threshold"] += 1
            continue
        kind = _kind(origin, group)
        reason = _blocked(vorm, kind, group)
        (meldingen if reason else proposals).append(_item((vorm, origin, account, SCOPE, kind), group, reason))
    return proposals, meldingen, counts

def persist(state_dir: Path, project_id: str, built: tuple, now: Optional[str] = None) -> Dict[str, int]:
    """Write the output of ``build`` in one ``reconcile``, notifications first so a hard stop holds back its
    proposal in the same batch (ADR-084); returns the outcome counts. Applies nothing."""
    proposals, meldingen, _counts = built
    return store.reconcile(state_dir, project_id, [*meldingen, *proposals], now=now)
