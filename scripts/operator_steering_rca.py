#!/usr/bin/env python3
"""Operator-steering RCA (ADR-072 decisions 8, 12, 16): ``stage --case <id>`` stages one RCA bundle and never fires.

Spec and instruction carry ids, note names, candidate paths and ``snapshot_sha256``, never prompt text. Order: the ledger
books ``rca_staged`` first, the bundle is published second; a staging a crash cut off is published again, booking nothing.
Exit codes: 0 staged (or printed by --dry-run), 1 refused (source, ledger, case, open RCA, cap), 2 usage.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import operator_steering as steering_cli
import operator_steering_cases as ledger_lib
import operator_steering_report as report_lib
import operator_steering_source as source
from dispatch_bridge import stage_spec_bundle
from project_root import resolve_central_data_dir
from vnx_paths import project_id_from_state_dir

ROLE, MODEL, PROVIDER, SLOT = "research-analyst", "sonnet", "claude", "T1"
CANDIDATE_PATHS = ["CLAUDE.md", ".claude/terminals/T0/CLAUDE.md", ".claude/terminals/T0/role-orchestrator.md", ".claude/skills/", "agents/"]


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


def publish_bundle(state_dir: Path, projects_root: Path, report: dict, case: ledger_lib.LedgerCase, entry: dict,
                   dispatch_id: str, snapshot: str) -> Path:
    text = instruction(case.case_id, entry, report["venster"], dispatch_id, snapshot, state_dir, projects_root)
    return stage_spec_bundle(instruction_text=text, dispatch_id=dispatch_id, role=ROLE, target_slot=SLOT, project_id=report["project_id"],
                             provider=PROVIDER, model=MODEL, data_dir=state_dir.parent,
                             tags=("steering-rca", f"steering_case:{case.case_id}", f"snapshot_sha256:{snapshot}"))


def _cmd_stage(args: argparse.Namespace, now: datetime) -> int:
    try:
        state_dir = Path(args.state_dir) if args.state_dir else (
            resolve_central_data_dir(args.project_id) / "state" if args.project_id else steering_cli._state_dir(args))
        if args.project_id and project_id_from_state_dir(state_dir) != args.project_id:
            raise ValueError(f"--state-dir hoort niet bij --project-id {args.project_id!r}")
        report = report_lib.build_report(state_dir, Path(args.projects_root), steering_cli._parse_window(args.since, now), now, now=now)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    problem = steering_cli._source_problem(report)
    if problem:
        print(f"stage geweigerd: {problem}", file=sys.stderr)
        return 1

    def stage_fn(case: ledger_lib.LedgerCase, entry: dict, dispatch_id: str, snapshot: str) -> None:
        if args.dry_run:
            print(instruction(case.case_id, entry, report["venster"], dispatch_id, snapshot, state_dir, Path(args.projects_root)))
        else:
            print("gestaged: " + str(publish_bundle(state_dir, Path(args.projects_root), report, case, entry, dispatch_id, snapshot))
                  + "; vuren doet de T0")

    try:
        ledger_lib.stage_case(state_dir, report, args.case, now, stage_fn, dry_run=args.dry_run)
    except (ledger_lib.StageRefused, ValueError, OSError) as exc:
        print(f"stage geweigerd: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[List[str]] = None, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="project state dir (default: central store of this project)")
    parser.add_argument("--projects-root", default=str(source.DEFAULT_PROJECTS_ROOT))
    stage = parser.add_subparsers(dest="command", required=True).add_parser("stage", help="book the ledger first, then the bundle; never fires")
    stage.add_argument("--case", required=True, help="case id from the ledger or from steering_report/1 gevallen")
    stage.add_argument("--project-id", help="project whose central store is read and staged into")
    stage.add_argument("--since", default="7d", help="window start: <N>d or an ISO time with zone (default 7d); the end is now")
    stage.add_argument("--dry-run", action="store_true", help="print the instruction; stage and book nothing")
    return _cmd_stage(parser.parse_args(argv), now or datetime.now(timezone.utc))


if __name__ == "__main__":
    sys.exit(main())
