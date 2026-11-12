#!/usr/bin/env python3
"""Operator-steering RCA (ADR-072 decisions 8, 12, 13, 14, 16): ``stage --case <id>`` stages one RCA bundle and never fires;
``ingest --dispatch <id>`` books its ``steering_rca/1`` answer as a store revision (store first, ledger second) or refuses it.
Spec and instruction carry ids, note names, candidate paths and ``snapshot_sha256``, never prompt text. Order: the ledger
books ``rca_staged`` first, the bundle is published second; a staging a crash cut off is published again, booking nothing.
Exit codes stage: 0 staged (or printed by --dry-run), 1 refused (source, ledger, case, open RCA, cap), 2 usage. Ingest: 0
ingested, 4 refused and booked (--dry-run: same code, nothing booked), 1 no open case or report, 2 usage, 3 store or ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import operator_steering as steering_cli
import operator_steering_cases as ledger_lib
import operator_steering_report as report_lib
import operator_steering_source as source
import steering_proposal_store as store
from dispatch_bridge import stage_spec_bundle
from operator_steering_match import DISPATCH_ID, PR_NUMBER
from project_root import resolve_central_data_dir
from vnx_paths import project_id_from_state_dir

ROLE, MODEL, PROVIDER, SLOT = "research-analyst", "sonnet", "claude", "T1"
CANDIDATE_PATHS = ["CLAUDE.md", ".claude/terminals/T0/CLAUDE.md", ".claude/terminals/T0/role-orchestrator.md", ".claude/skills/", "agents/"]
OORZAKEN = tuple("ontbrekende_regel ontbrekende_skill ontbrekend_overzicht poort_te_streng tool_faalt_stil anders "
                 "insufficient_evidence".split())
SOORTEN, KINDS = ("skill", "claude_md", "rolbestand", "memory", "tool", "feature"), ("artefact_soort", "doelpad", "voorstel")
CASE_SCHEMA, RCA_SCHEMA, WINDOW, MASK = "steering_rca_case/1", "steering_rca/1", 40, "\x00"
_FENCE = re.compile(r"```json[ \t]*\n(.*?)\n```", re.DOTALL)
# Masked in prompt and report before the citation check: 64-hex (case ids, shas), 16-hex prompt ids, dispatch ids, PR numbers.
_IDS = re.compile("|".join((r"\b[0-9a-f]{64}\b", r"\b[0-9a-f]{16}\b", DISPATCH_ID.pattern, PR_NUMBER.pattern)))
_BROKEN = object()


class Refusal(Exception):
    """An answer ``ingest`` refuses; args: the ledger reason code and a detail of ids and counts, never text."""


def instruction(case_id: str, entry: dict, venster: dict, dispatch_id: str, snapshot: str, state_dir: Path, projects_root: Path) -> str:
    """Only what decision 12 allows: ids, note names, the context ids of decision 13, candidate paths, the snapshot."""
    context = [{"prompt_id": c["prompt_id"], "tool_call_ids": [t["id"] for t in c["tool_calls"]], "dispatch_ids": c["dispatch_ids"],
                "pr_nummers": c["pr_nummers"]} for c in entry["context"]]
    block = {"schema": "steering_rca_case/1", "case_id": case_id, "show_case_id": entry["case_id"], "dispatch_id": dispatch_id,
             "snapshot_sha256": snapshot, "venster": venster, "prompt_ids": entry["prompt_ids"], "notities": entry["notities"],
             "context": context, "kandidaat_paden": CANDIDATE_PATHS}
    show = shlex.join(["python3", "scripts/operator_steering.py", "--state-dir", str(state_dir), "--projects-root", str(projects_root),
                       "show", "--case", entry["case_id"], "--since", venster["since"], "--until", venster["until"]])
    return (f"# Oorzaakanalyse van steering-case {case_id}\n\nDeze opdracht draagt geen prompttekst (ADR-072 besluit 12). Lees "
            f"prompts en context live; dit print en schrijft niets:\n\n    {show}\n\nBepaal waarom de operator bijstuurde en welk "
            "artefact dat had voorkomen. Noem prompts alleen bij id, nooit hun tekst. Antwoord met precies één JSON-blok "
            "`steering_rca/1`: `case_id` en `snapshot_sha256` uit het blok hieronder, `oorzaak` (ontbrekende_regel, ontbrekende_skill, "
            "ontbrekend_overzicht, poort_te_streng, tool_faalt_stil, anders of insufficient_evidence), `artefact_soort` (skill, "
            "claude_md, rolbestand, memory, tool, feature), `doelpad`, `voorstel` en `bewijs` (prompt-id's, notities, receipts) met "
            "alleen id's uit dit blok. Bij insufficient_evidence zijn `artefact_soort`, `doelpad` en `voorstel` leeg.\n\n"
            f"```json\n{json.dumps(block, indent=2, ensure_ascii=False)}\n```\n")


def _resolve_state_dir(args: argparse.Namespace) -> Path:
    state_dir = Path(args.state_dir) if args.state_dir else (
        resolve_central_data_dir(args.project_id) / "state" if args.project_id else steering_cli._state_dir(args))
    if args.project_id and project_id_from_state_dir(state_dir) != args.project_id:
        raise ValueError(f"--state-dir hoort niet bij --project-id {args.project_id!r}")
    return state_dir


def _cmd_stage(args: argparse.Namespace, now: datetime) -> int:
    try:
        state_dir = _resolve_state_dir(args)
        report = report_lib.build_report(state_dir, Path(args.projects_root), steering_cli._parse_window(args.since, now), now, now=now)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    problem = steering_cli._source_problem(report)
    if problem:
        print(f"stage geweigerd: {problem}", file=sys.stderr)
        return 1

    def stage_fn(case: ledger_lib.LedgerCase, entry: dict, dispatch_id: str, snapshot: str) -> None:
        text = instruction(case.case_id, entry, report["venster"], dispatch_id, snapshot, state_dir, Path(args.projects_root))
        print(text if args.dry_run else "gestaged: " + str(stage_spec_bundle(
            instruction_text=text, dispatch_id=dispatch_id, role=ROLE, target_slot=SLOT, project_id=report["project_id"],
            provider=PROVIDER, model=MODEL, data_dir=state_dir.parent,
            tags=("steering-rca", f"steering_case:{case.case_id}", f"snapshot_sha256:{snapshot}"))) + "; vuren doet de T0")

    try:
        ledger_lib.stage_case(state_dir, report, args.case, now, stage_fn, dry_run=args.dry_run)
    except (ledger_lib.StageRefused, ValueError, OSError) as exc:
        print(f"stage geweigerd: {exc}", file=sys.stderr)
        return 1
    return 0


def _safe(value: object) -> str:
    """A value from the answer is printed only in the form of an id, anything else by its length (decision 12)."""
    text = str(value)
    return text if re.fullmatch(r"[\w#./:-]{1,80}", text) else f"<waarde van {len(text)} tekens>"


def _json(body: str) -> object:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):  # _BROKEN: the caller refuses it, never skips it; also nesting deeper than the decoder
        return _BROKEN


def _snapshot(state_dir: Path, case: ledger_lib.LedgerCase, dispatch_id: str) -> Tuple[dict, set]:
    """The case block of the one bundle (pending, completed, failed) and the ids it allows in ``bewijs``; else ``snapshot_onleesbaar``."""
    root = Path(state_dir).parent / "dispatches"
    bundles = [b for b in (root / s / dispatch_id for s in ("pending", "completed", "failed")) if (b / "dispatch-spec.json").is_file()]
    try:
        spec = json.loads((bundles[0] / "dispatch-spec.json").read_text(encoding="utf-8")) if len(bundles) == 1 else {}
        raw = (bundles[0] / "instruction.md").read_bytes() if spec else b""
        if not spec or spec.get("instruction_sha256") != hashlib.sha256(raw).hexdigest():
            raise ValueError(f"{len(bundles)} bundel(s); er moet er één zijn waarvan instruction.md past bij instruction_sha256")
        found = [b for b in map(_json, _FENCE.findall(raw.decode("utf-8"))) if isinstance(b, dict) and b.get("schema") == CASE_SCHEMA]
        snap = found[0] if len(found) == 1 else {}
        if (spec.get("dispatch_id"), snap.get("dispatch_id"), snap.get("case_id"), snap.get("snapshot_sha256")) != (
                dispatch_id, dispatch_id, case.case_id, case.staged[-1]["snapshot_sha256"]):
            raise ValueError(f"{len(found)} case-blok(ken), of spec of blok noemt een andere dispatch, case of snapshot dan rca_staged")
        allowed = set(snap["prompt_ids"]) | set(snap["notities"])
        for c in snap["context"]:
            allowed |= set(c["tool_call_ids"]) | set(c["dispatch_ids"]) | {f"{p}{n}" for n in c["pr_nummers"] for p in ("", "#")}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Refusal("snapshot_onleesbaar", str(exc)) from exc
    return snap, allowed


def _answer(text: str) -> dict:
    """The one fenced JSON block of the report that carries ``oorzaak``; an echo of the case block does not count."""
    blocks = [b for b in map(_json, _FENCE.findall(text)) if not (isinstance(b, dict) and b.get("schema") == CASE_SCHEMA)]
    answers = [b for b in blocks if isinstance(b, dict) and "oorzaak" in b]
    if _BROKEN in blocks:
        raise Refusal("blok_onleesbaar", f"{blocks.count(_BROKEN)} json-blok(ken) zonder geldige JSON")
    if len(answers) != 1:
        raise Refusal("meerdere_blokken" if answers else "geen_blok", f"{len(answers)} blokken met oorzaak, niet één")
    return answers[0]


def _live(state_dir: Path, projects_root: Path, prompt_ids: List[str]) -> Dict[str, object]:
    """The reader of ``show --case`` (its ``_live_texts`` drops the source state): not ``ok``, or a prompt missing, is ``bron_onleesbaar``."""
    collected = report_lib._Collected()
    result = source.read_sources(state_dir, projects_root, report_lib._consumer(collected))
    found = {p.prompt_id: p for p in collected.prompts}
    missing = [pid for pid in prompt_ids if pid not in found]
    if result.state != "ok" or missing:
        raise Refusal("bron_onleesbaar", f"bron {result.state} {result.reason}, {len(missing)} prompt(s) van de case niet gelezen")
    return {pid: found[pid] for pid in prompt_ids}


def _strings(value: object) -> List[str]:
    """Every string of a decoded JSON value, keys included, through objects and lists; a stack, so no depth recurses."""
    found, stack = [], [value]
    while stack:
        item = stack.pop()
        stack += [*item, *item.values()] if isinstance(item, dict) else item if isinstance(item, list) else []
        found += [item] if isinstance(item, str) else []
    return found


def _variants(text: str) -> List[str]:
    """As written and as NFC with each whitespace run one space, ids masked in both; keeping both, the refusal only grows."""
    plain = text.replace(MASK, " ")
    return [_IDS.sub(MASK, t) for t in (plain, re.sub(r"\s+", " ", unicodedata.normalize("NFC", plain)))]


def _cited(prompts: Dict[str, object], report: str) -> Optional[str]:
    """The first prompt id with a 40-character window free of masked ids that the prose or a decoded string of any JSON block of
    the report also holds (decision 12); each on its own, joined by MASK, so no window spans two of them."""
    side = MASK.join(v for t in (report, *(s for b in map(_json, _FENCE.findall(report)) for s in _strings(b))) for v in _variants(t))
    seen = {side[i:i + WINDOW] for i in range(len(side) - WINDOW + 1)}
    return next((pid for pid, p in prompts.items() if any(MASK not in w and w in seen for t in _variants(p.text)
                                                           for w in (t[i:i + WINDOW] for i in range(len(t) - WINDOW + 1)))), None)


def _validate(answer: dict, snap: dict, allowed: set) -> List[str]:
    """Decisions 8 and 13, each failure its own code, in this order; returns the prompt ids of ``bewijs``."""
    get, bewijs, none = answer.get, answer.get("bewijs"), answer.get("oorzaak") == "insufficient_evidence"
    items = bewijs if isinstance(bewijs, list) else sum(bewijs.values(), []) if isinstance(bewijs, dict) and all(
        isinstance(v, list) for v in bewijs.values()) else None
    foreign = [v for v in items or [] if str(v) not in allowed]
    for ok, code, detail in (
            (get("schema", RCA_SCHEMA) == RCA_SCHEMA, "schema_onbekend", f"schema {_safe(get('schema'))}"),
            (get("case_id") == snap["case_id"], "case_id_onjuist", "case_id is niet die van de snapshot"),
            (get("snapshot_sha256") == snap["snapshot_sha256"], "snapshot_sha256_onjuist", "snapshot_sha256 is niet die van de case"),
            (get("oorzaak") in OORZAKEN, "oorzaak_onbekend", f"oorzaak {_safe(get('oorzaak'))}"),
            (not none or all(get(k) in (None, "") for k in KINDS), "insufficient_evidence_niet_leeg", "artefact_soort, doelpad, voorstel"),
            (none or get("artefact_soort") in SOORTEN, "artefact_soort_onbekend", f"artefact_soort {_safe(get('artefact_soort'))}"),
            (none or all(isinstance(get(k), str) and get(k).strip() for k in KINDS[1:]), "voorstel_onvolledig", "doelpad of voorstel leeg"),
            (items is not None and all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in items), "bewijs_ongeldig",
             "bewijs is geen lijst of object van lijsten met id's"),
            (not foreign, "bewijs_buiten_snapshot", f"eerste id buiten de snapshot: {_safe(foreign[0]) if foreign else ''}")):
        if not ok:
            raise Refusal(code, detail)
    return list(dict.fromkeys(v for v in items if v in snap["prompt_ids"]))


def _judge(state_dir: Path, projects_root: Path, case: ledger_lib.LedgerCase, dispatch_id: str, text: str) -> Tuple[dict, List[dict]]:
    """(answer, evidence with live ts), else a Refusal; citations go first, so no message names a value of a quoting block."""
    snap, allowed = _snapshot(state_dir, case, dispatch_id)
    answer = _answer(text)
    prompts = _live(state_dir, projects_root, snap["prompt_ids"])
    cited = _cited(prompts, text)
    if cited:
        raise Refusal("citaat", f"40 tekens uit prompt {cited} staan letterlijk in het rapport")
    return answer, [{"prompt_id": pid, "ts": prompts[pid].ts.isoformat()} for pid in _validate(answer, snap, allowed)]


def _book(args: argparse.Namespace, state_dir: Path, project_id: str, cid: str, now: datetime, code: int, kind: str, **fields: str) -> int:
    event = ledger_lib.event(kind, cid, project_id, now, dispatch_id=args.dispatch, **fields)
    try:
        if not args.dry_run:
            ledger_lib.book(state_dir, project_id, [event])
    except (ValueError, OSError) as exc:  # LedgerUnreadable, or a parallel ingest closed this RCA first
        print(f"ingest gestopt, grootboek niet bijgewerkt: {exc}", file=sys.stderr)
        return 3
    print(" ".join(filter(None, (f"{kind} {args.dispatch}:", fields.get("reden") or fields["uitkomst"], fields.get("proposal_id"),
                                 "(dry-run: niets geschreven)" if args.dry_run else ""))))
    return code


def _cmd_ingest(args: argparse.Namespace, now: datetime) -> int:
    try:
        state_dir = _resolve_state_dir(args)
        project_id = project_id_from_state_dir(state_dir)
        if not project_id:
            raise ValueError(f"{state_dir} hoort bij geen project")
        case = next((c for c in ledger_lib.read_ledger(state_dir, project_id).values() if c.open_dispatch == args.dispatch), None)
    except ledger_lib.LedgerUnreadable as exc:
        print(f"ingest gestopt: {exc}", file=sys.stderr)
        return 3
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        text = (state_dir.parent / "unified_reports" / f"{args.dispatch}.md").read_text(encoding="utf-8") if case else None
    except (OSError, ValueError) as exc:  # absent: the RCA may still run; unreadable: nothing passes and nothing is booked
        print(f"ingest: geen leesbaar rapport ({exc}); niets geboekt", file=sys.stderr)
        return 1
    if text is None:
        print(f"ingest: geen case met een open rca_staged {args.dispatch}; niets geboekt", file=sys.stderr)
        return 1
    try:
        answer, evidence = _judge(state_dir, Path(args.projects_root), case, args.dispatch, text)
    except Refusal as refusal:
        print(f"ingest geweigerd: {refusal.args[0]}: {refusal.args[1]}", file=sys.stderr)
        return _book(args, state_dir, project_id, case.case_id, now, 4, "rca_refused", reden=refusal.args[0], detail=refusal.args[1])
    item = None if answer["oorzaak"] == "insufficient_evidence" else {**{k: answer[k] for k in ("oorzaak", *KINDS)}, "evidence": evidence}
    extra = {"proposal_id": store._normalise(project_id, item)["proposal_id"]} if item else {}
    uitkomst = "geldig" if item else "insufficient_evidence"
    if item and not args.dry_run:  # store first, ledger second: a re-ingest after a crash in between counts unchanged
        try:
            counts = store.reconcile(state_dir, project_id, [item], now=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        except (store.StoreUnreadable, OSError) as exc:
            print(f"ingest gestopt, voorstelstore onleesbaar: {exc}", file=sys.stderr)
            return 3
        uitkomst = next(k for k, v in counts.items() if v)
    return _book(args, state_dir, project_id, case.case_id, now, 0, "rca_ingested", uitkomst=uitkomst, **extra)


def main(argv: Optional[List[str]] = None, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="project state dir (default: central store of this project)")
    parser.add_argument("--projects-root", default=str(source.DEFAULT_PROJECTS_ROOT))
    common = argparse.ArgumentParser(add_help=False)  # also after the verb; SUPPRESS keeps a value given before it
    common.add_argument("--state-dir", default=argparse.SUPPRESS, help="project state dir")
    common.add_argument("--projects-root", default=argparse.SUPPRESS, help="root of the bound transcript dirs")
    common.add_argument("--project-id", help="project whose central store is read and written")
    common.add_argument("--dry-run", action="store_true", help="print the instruction or the outcome; stage, book and write nothing")
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", parents=[common], help="book the ledger first, then the bundle; never fires")
    stage.add_argument("--case", required=True, help="case id from the ledger or from steering_report/1 gevallen")
    stage.add_argument("--since", default="7d", help="window start: <N>d or an ISO time with zone (default 7d); the end is now")
    ingest = sub.add_parser("ingest", parents=[common], help="check the steering_rca/1 answer of an open RCA; store first, then the ledger")
    ingest.add_argument("--dispatch", required=True, help="dispatch id of an open rca_staged")
    args = parser.parse_args(argv)
    return (_cmd_ingest if args.command == "ingest" else _cmd_stage)(args, now or datetime.now(timezone.utc))


if __name__ == "__main__":
    sys.exit(main())
