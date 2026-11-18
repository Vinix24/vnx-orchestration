"""One learning chain for permission blockades (permissieblokkades-leren L6, ADR-075, ADR-065, ADR-084).

The order is fixed (plan-gate binding 11): scan, pair, build, persist. ``run_permission_chain`` carries all four and
never raises: a failing step is ``status: "error"`` with the step name, written to ``permission_chain.json`` for the
digest. Leren blijft voorstellen: ``persist`` writes only the proposal store, nothing here changes a permission.
The pairing (ADR-075): an ``asked`` event (B4a) and the first ``executed`` CLI invocation (B4b) of ``actor_class
operator`` with the same ``vorm`` from the ask up to 30 minutes later. A missing invocation is ``unpaired``, never
"Vincent did not do it"; two asks that both fit one invocation are ``ambiguous`` and pair none."""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli_invocations
from atomic_io import atomic_write_text
import permission_effect
import permission_events as pe
import permission_proposals as proposals_lib

REPORT_FILE = "permission_chain.json"
SCHEMA = "permission_chain/1"
PAIR_WINDOW = timedelta(minutes=30)
SCAN_SINCE = "7d"


def _ts(value: Any) -> Optional[datetime]:
    return proposals_lib._ts(value)


def read_invocations(project_id: str, state_dir: Path) -> Tuple[Optional[List[Dict[str, Any]]], int]:
    """``(lines, unparseable)``; ``None`` lines when the ledger cannot be read (missing is not an empty ledger)."""
    try:
        raw = (Path(pe._state_dir(project_id, state_dir)) / cli_invocations.LEDGER).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # vnx-silent-except: the caller reports it as an unreadable B4b, never as 0 lines
        return None, 0
    lines, bad = [], 0
    for text in (t for t in raw.splitlines() if t.strip()):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and obj.get("project_id") == project_id:
            lines.append(obj)
        else:
            bad += 1
    return lines, bad


def pair_key(vorm: Any) -> Optional[Tuple[str, ...]]:
    """The one key both sides pair on: the vnx CLI plus its known subcommand path, never placeholders, positionals or
    flags (B4b carries no argument values). ``None`` (never pairs) for a non-vnx first word or a non-string form."""
    words = vorm.split() if isinstance(vorm, str) else []
    if not words or not (words[0] == "vnx" or words[0].endswith("/vnx")):
        return None
    path: List[str] = []
    for word in words[1:]:
        if word not in cli_invocations.KNOWN_SUBCOMMANDS:
            break
        path.append(word)
    return ("vnx", *path)


def pair(events: List[Dict[str, Any]], invocations: Optional[List[Dict[str, Any]]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """``(events, counts)``: every event comes back, a paired ``asked`` carries ``paired_invocation_id``.
    Counts: ``asked``, ``pairs``, ``ambiguous`` (invocations that fit two or more asks), ``unpaired``, ``bad_ts``."""
    counts = dict.fromkeys(("asked", "pairs", "ambiguous", "unpaired", "bad_ts"), 0)
    asked = [e for e in events if e.get("besluit") == "asked"]
    counts["asked"] = len(asked)
    taken: Dict[int, str] = {}
    dead: set = set()  # asks that an ambiguous invocation already fit: no later invocation pairs them
    stamped = [(_ts(i.get("ts")), i) for i in invocations or [] if i.get("actor_class") == "operator"]
    counts["bad_ts"] = sum(1 for t, _ in stamped if t is None)
    for when, inv in sorted(((t, i) for t, i in stamped if t is not None), key=lambda p: p[0]):
        fits = [k for k, a in enumerate(asked) if k not in taken and k not in dead and (key := pair_key(inv.get("vorm"))) is not None
                and pair_key(a.get("vorm")) == key
                and (at := _ts(a.get("ts"))) is not None and at <= when <= at + PAIR_WINDOW]
        if len(fits) == 1:
            taken[fits[0]] = str(inv.get("invocation_id") or "")
        elif fits:
            counts["ambiguous"] += 1
            dead.update(fits)
    counts["pairs"] = len(taken)
    counts["unpaired"] = len(asked) - len(taken) - len(dead)
    index = {id(asked[k]): inv_id for k, inv_id in taken.items()}
    return [{**e, "paired_invocation_id": index[id(e)]} if id(e) in index else e for e in events], counts


def _write_report(state_dir: Path, report: Dict[str, Any]) -> None:
    path = Path(state_dir) / REPORT_FILE
    atomic_write_text(path, json.dumps(report, sort_keys=True, indent=2) + "\n")


def run_permission_chain(state_dir: Path, project_id: str, projects_root: Path, now: Optional[datetime] = None) -> Dict[str, Any]:
    """scan, pair, build, persist; returns and writes the report. Never raises."""
    stamp = now or datetime.now(timezone.utc)
    report: Dict[str, Any] = {"schema": SCHEMA, "project_id": project_id, "gegenereerd": stamp.isoformat(), "status": "ok"}
    step = "scan"
    try:
        import permission_events_scan as scan
        scan.run_scan(project_id, SCAN_SINCE, Path(projects_root), state_dir, now=stamp, out=io.StringIO())
        step = "pair"
        events, skipped = pe.read_events(project_id, state_dir)
        invocations, unparseable = read_invocations(project_id, state_dir)
        paired, counts = pair(events, invocations)
        cli_invocations.coverage(project_id, state_dir)  # B4b: unknown with a reason when the ledger is unreadable
        step = "build"
        coverage = pe._load_coverage(Path(pe._state_dir(project_id, state_dir)) / "permission_coverage.json")[0]
        built = proposals_lib.build(paired, coverage, stamp)
        step = "persist"
        outcome = proposals_lib.persist(Path(state_dir), project_id, built, now=stamp.isoformat())
        report.update(bronnen={b: str(c.get("state")) for b, c in sorted(coverage.items()) if isinstance(c, dict)}, events=len(events), skipped_events=sum(skipped.values()), unparseable_invocations=unparseable,
                      invocations_readable=invocations is not None, **counts, proposals=len(built[0]), notifications=len(built[1]),
                      build_counts=built[2], persist=outcome)
    except Exception as exc:  # the chain must never fail dream: the failure is written as status error with the step
        report.update(status="error", reden=f"{step}: {type(exc).__name__}")
    try:  # ADR-086: the effect per applied permissie rule; its failure is one error entry, never the chain's
        import permission_effect
        report["effect"] = permission_effect.section(Path(state_dir), project_id, stamp)
    except Exception as exc:  # an import or an unexpected raise still leaves the rest of the report written
        report["effect"] = [{"outcome": "error", "reden": type(exc).__name__}]
    try:
        _write_report(state_dir, report)
    except OSError as exc:  # vnx-silent-except: dream still must not fail; the error goes to stderr
        print(f"permission chain: report not written ({type(exc).__name__})", file=sys.stderr)
    return report


INDEX_LIMIT = 5
OPEN_STATUS = ("pending", "approved")
MELDING = proposals_lib.MELDING
CHAIN_KINDS = (permission_effect.KIND, MELDING)  # the steering store also holds feature and skill proposals


def _span(text: str) -> str:
    """The first backtick span of a proposal text: the exact rule, or the form for a habit."""
    parts = (text or "").split("`")
    return parts[1] if len(parts) > 2 else ""


def open_view(state_dir: Path, project_id: str, limit: int = INDEX_LIMIT) -> Dict[str, Any]:
    """Open proposals and notifications, ``limit`` items and the rest as ``more``. Read only; an unreadable store is
    ``available: false`` with a reason, never an empty list."""
    try:
        entries, _decisions = proposals_lib.store.read_proposals(Path(state_dir), project_id)
    except Exception as exc:  # the section reports the failure; the build carries on
        return {"available": False, "reason": f"proposal store unreadable: {type(exc).__name__}"}
    items = []
    for entry in (e for e in entries if e["status"] in OPEN_STATUS and e.get("revisions") and e.get("artefact_soort") in CHAIN_KINDS):
        text, kind = entry["revisions"][-1]["voorstel"], entry["artefact_soort"]
        item = {"proposal_id": entry["proposal_id"], "kind": kind, "form": _span(text)}
        item.update(reason=text[:160] if kind == MELDING else None, rule=None if kind == MELDING else item["form"])
        items.append({k: v for k, v in item.items() if v is not None})
    return {"available": True, "total": len(items), "more": max(0, len(items) - limit), "items": items[:limit]}


def digest_section(state_dir: Path, project_id: Optional[str]) -> str:
    """The "Permissievoorstellen" section of the nightly digest: a line and the rule in a copy block per proposal, a
    notification with its reason, ``geen`` when there are none, ``onbekend`` with the reason when a source is not ok."""
    head = "─── Permissievoorstellen ───\n"
    try:
        report = json.loads((Path(state_dir) / REPORT_FILE).read_text(encoding="utf-8"))
        unknown = None if report.get("status") == "ok" else f"chain {report.get('status')}: {report.get('reden', 'no reason')}"
        unknown = unknown or "; ".join(f"bron {b} {s}" for b, s in sorted((report.get("bronnen") or {}).items()) if s != "ok") or None
    except (OSError, ValueError, AttributeError) as exc:
        unknown = f"no readable chain report ({type(exc).__name__})"
    view = open_view(state_dir, project_id) if project_id else {"available": False, "reason": "no project id"}
    if not view["available"]:
        unknown = view["reason"] if not unknown else f"{unknown}; {view['reason']}"
    lines = [f"onbekend: {unknown}"] if unknown else []
    for item in view.get("items", []):
        if item["kind"] == MELDING:
            lines.append(f"- melding {item['proposal_id']}: {item['reason']}")
        else:
            lines += [f"- {item['kind']} {item['proposal_id']}", "  ```", f"  {item['rule']}", "  ```"]
    if view.get("more"):
        lines.append(f"- en nog {view['more']}")
    if not lines:
        lines = ["geen"]
    return head + "\n".join(lines) + "\n"
