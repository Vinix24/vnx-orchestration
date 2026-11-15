"""VNX adapter for the besluitenregister (ADR-077, B4): queue answers become decisions; the index block.

An ``answer`` line of the operator queue is a decision: it is Vincent's own words about an open ``antwoord:``
point. Points closed through their own source (a promote, a config change, a verdict) have no ``answer`` line
and write nothing here. The package never imports from scripts/; this module imports the package.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "besluitenregister"))
from besluitenregister.cli import _besluiten
from besluitenregister.lezer import lees
from besluitenregister.schrijver import Geweigerd, Register
from besluitenregister.stand import stand

import operator_queue as oq

TITEL_MAX = 100
CONTEXT = "vnx-orchestration"
LIMIT = 5
RECENT_TITEL = 40  # the index has a size budget


def _context(logdir: Path) -> str:
    """The context of the first valid register line; the project's own context when the file is new."""
    regels = lees(Path(logdir) / "besluiten.ndjson").regels
    return regels[0][1]["context"] if regels else CONTEXT


def _velden(context: str, vraag: str, ans: Dict[str, Any]) -> Dict[str, Any]:
    citaat = ans["citaat"]
    return {"event": "vastgelegd", "context": context, "besloten_op": str(ans["ts"])[:10],
            "vastgelegd_door": "agent:operator-queue", "titel": " ".join(str(vraag).split())[:TITEL_MAX] or "antwoord",
            "besluit": citaat, "waarom": "Antwoord van de operator op een vraag uit de operatorwachtrij.",
            "alternatieven": [], "besloten_door": "operator", "reikwijdte": "context", "vertrouwelijkheid": "intern",
            "terugweg": "geen: een antwoord van de operator wordt met een nieuw besluit vervangen",
            "bron": {"soort": "chat", "ref": ans["point_id"], "citaat": citaat}, "links": [], "tags": []}


def _titel_van(project_id: str, state_dir: Optional[Path]) -> Dict[str, str]:
    state = oq.read_state(project_id, state_dir=state_dir)
    if state.get("status") != "ok":
        raise oq.QueueUnreadable(str(state.get("reason")), state.get("line", 0))
    return {pid: p.get("vraag", "") for pid, p in state["points"].items()}


def sync_answers(project_id: str, logdir, *, state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Write the decision of every queue answer that has none; repeat, retry and torn tail are the writer's.

    Returns {"written", "present", "refused": [(point_id, reason)]}; a refusal never stops the others and its
    reason never holds the value. Raises QueueUnreadable on an unreadable queue ledger."""
    answers, vragen = oq.answers(project_id, state_dir=state_dir), _titel_van(project_id, state_dir)
    reg, out = Register(logdir), {"written": 0, "present": 0, "refused": []}
    for ans in answers:
        try:
            uit = reg.schrijf(_velden(_context(Path(logdir)), vragen.get(ans["point_id"], ""), ans))
        except Geweigerd as exc:
            out["refused"].append((ans["point_id"], str(exc)))
            continue
        out["written" if uit.geschreven else "present"] += 1
        ans["besluit_id"] = uit.besluit_id
    out["ids"] = {a["point_id"]: a["besluit_id"] for a in answers if "besluit_id" in a}
    return out


def _more(ids: List[str]) -> Dict[str, Any]:
    return {"ids": ids[:LIMIT], "more": max(0, len(ids) - LIMIT)}


def index_block(project_id: str, logdir, *, state_dir: Optional[Path] = None, nu: Optional[str] = None) -> Dict[str, Any]:
    """The ``besluiten`` block of t0_index.json, read-only. Never raises: a failure is status=error with a reason."""
    pad = Path(logdir) / "besluiten.ndjson"
    nu = nu or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not pad.exists():
        return {"status": "absent"}
    try:
        uit = lees(pad)
        regels = [r for _, r in uit.regels]
        besl = _besluiten(regels)
        standen = {bid: stand(regels, bid, nu).naam for bid in besl}
        gedekt = {b["v"]["bron"]["ref"] for b in besl.values()
                  if b["v"]["bron"]["soort"] == "chat" and b["v"]["vastgelegd_door"] == "agent:operator-queue"}
        gat = [a["point_id"] for a in oq.answers(project_id, state_dir=state_dir) if a["point_id"] not in gedekt]
        herzien = _more([bid for bid, s in standen.items() if s == "te_herzien"])
        recent = sorted((b["v"] for bid, b in besl.items() if standen[bid] in ("geldig", "te_herzien")),
                        key=lambda v: v["besloten_op"], reverse=True)[:LIMIT]
        return {"status": "ok", "nu": nu, "geldig": sum(s in ("geldig", "te_herzien") for s in standen.values()),
                "te_herzien": herzien["ids"], "te_herzien_more": herzien["more"],
                "recent": [{"id": v["besluit_id"], "titel": v["titel"][:RECENT_TITEL], "besloten_op": v["besloten_op"]}
                           for v in recent],
                "afgescheurd": len(uit.afgescheurd), "ongeldig": len(uit.ongeldig),
                "niet_vastgelegd": _more(gat)["ids"], "niet_vastgelegd_more": _more(gat)["more"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
