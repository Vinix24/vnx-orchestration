#!/usr/bin/env python3
"""Impact shadow record: the selector's choice held against the Profile A evidence.

ADR-074 (besluit 2, 3, 7, 8).  Pure and offline: it reads the selector JSON
(``impact_select.py``), the Profile A evidence directory (``junit-<phase>.xml``,
``manifest-<phase>.json``), the exclusions and the expected skips, and writes one
``impact_shadow/1`` record with sorted keys.  The record names files and test ids,
never only counts.  Nothing here decides anything: the shadow measures the
graph-only variant and the conftest variant side by side (OI-2788).

Exit 0 whenever a record was written, also an invalid attempt (``attempt_valid``
false, with the reasons).  A crash exits non-zero; the caller runs it under
``continue-on-error``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_xdist_parity as parity
import classify_ci_profile as classifier

SCHEMA = "impact_shadow/1"
NOT_INVESTIGATED = "niet onderzocht"
NOT_COVERED = "niet gedekt"
SELECTION_KEYS = ("selected", "selector_version", "graph_version", "conftest_closure_version", "conftest_reached")


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _read_phase(directory: Path, name: str, key, run: tuple, problems: list) -> dict:
    """One phase of the evidence: the files collected, the files that ran, the failing test ids."""
    phase = {"collected": set(), "required": set(), "ran": set(), "failed": set()}
    try:
        manifest = parity.load_manifest(directory / f"manifest-{name}.json")
    except parity.EvidenceError as exc:
        problems.append({"reason": exc.reason, "detail": exc.detail})
        return phase
    stale = parity.stale_evidence_reason(f"manifest-{name}.json", manifest, *run)
    if stale:
        problems.append({"reason": "stale_evidence", "detail": stale})
    if not manifest.get("phase_active"):
        return phase
    if manifest.get("collection_finished") is not True:
        problems.append({"reason": "collection_unfinished", "detail": name})
    phase["required"] = {_file_of(n) for n in manifest["nodeids"]}
    phase["collected"] = set(phase["required"])
    for outcome in manifest["collection_outcomes"]:
        phase["collected"].add(_file_of(str(outcome.get("nodeid"))))
        if outcome.get("outcome") != "skipped":
            problems.append({"reason": "collection_error", "detail": str(outcome.get("nodeid"))})
    _read_junit(directory / f"junit-{name}.xml", name, manifest["nodeids"], key, run, phase, problems)
    return phase


def _read_junit(path: Path, name: str, nodeids: list, key, run: tuple, phase: dict, problems: list) -> None:
    try:
        root = parity.load_junit(path)
    except parity.EvidenceError as exc:
        problems.append({"reason": exc.reason, "detail": exc.detail})
        return
    stale = parity.stale_evidence_reason(path.name, parity.junit_stamp(root), *run)
    if stale:
        problems.append({"reason": "stale_evidence", "detail": stale})
    nodeid_by_key = {key(n): n for n in nodeids}
    outside = []
    for case in root.iter("testcase"):
        nodeid = nodeid_by_key.get((case.get("classname") or "", case.get("name") or ""))
        if nodeid is None:
            outside.append(f"{case.get('classname')}::{case.get('name')}")
            continue
        phase["ran"].add(_file_of(nodeid))
        if parity.classify_record(case).cls == "fail":
            phase["failed"].add(nodeid)
    if outside:
        problems.append({"reason": "record_outside_manifest", "detail": f"{name}: {sorted(outside)[:5]}"})
    missing = phase["required"] - phase["ran"]
    if missing:
        problems.append({"reason": "junit_does_not_cover_e", "detail": f"{name}: {sorted(missing)[:5]}"})


def read_evidence(directory: Path, run: tuple) -> dict:
    """U, E, F and the reasons the attempt cannot count, from every ``manifest-<phase>.json``."""
    problems: list = []
    names = sorted(p.name[len("manifest-"):-len(".json")] for p in directory.glob("manifest-*.json"))
    if not names:
        problems.append({"reason": "missing_manifest", "detail": str(directory)})
    key = parity.junit_key_function()
    phases = [_read_phase(directory, n, key, run, problems) for n in names]
    return {
        "U": set().union(*(p["collected"] for p in phases)),
        "E": set().union(*(p["ran"] for p in phases)),
        "F": set().union(*(p["failed"] for p in phases)),
        "problems": sorted(problems, key=lambda p: (p["reason"], p["detail"])),
    }


def known_failure_files(exclusions_path: Path, skips_path: Path) -> tuple:
    """(X, K) on file level: the exclusions, and the exclusions plus the expected skips."""
    exclusions = set(classifier.read_exclusion_entries(exclusions_path.read_text(encoding="utf-8")))
    skips, problems = parity.read_expected_skips(skips_path)
    if problems:
        raise ValueError(f"expected skips unreadable: {problems[0]}")
    return exclusions, exclusions | {_file_of(s) for s in skips}


def _variant(selected: set, universe: set, excluded: set, failed: set) -> dict:
    misses = sorted(n for n in failed if _file_of(n) not in selected)
    return {"misses": misses, "reduced": len(selected) < len(universe - excluded)}


def build_record(selection: dict, evidence: dict, exclusions: set, known: set, identity: dict) -> dict:
    """The ``impact_shadow/1`` record; deterministic for the same input."""
    missing_keys = [k for k in SELECTION_KEYS if selection.get(k) is None]
    if missing_keys:
        raise ValueError(f"selection lacks {missing_keys}")
    universe, selected = evidence["U"], set(selection["selected"])
    conftest = bool(selection["conftest_reached"])
    graph_only = _variant(selected, universe, exclusions, evidence["F"])
    with_conftest = _variant(universe if conftest else selected, universe, exclusions, evidence["F"])
    paths = identity["changed_paths"]
    needed = (selection["selector_version"], selection["graph_version"], identity["base_sha"], identity["candidate_sha"], paths)
    problems = evidence["problems"]
    return {
        "schema": SCHEMA,
        "scope": "impact",
        "suite_complete": NOT_COVERED,
        **identity,
        "selector_version": selection["selector_version"],
        "graph_version": selection["graph_version"],
        "conftest_closure_version": selection["conftest_closure_version"],
        "reconstructible": all(needed),
        "attempt_valid": not problems,
        "invalid_reasons": problems,
        "sets": {"U": sorted(universe), "X": sorted(exclusions), "S": sorted(selected),
                 "E": sorted(evidence["E"]), "F": sorted(evidence["F"]), "misses": graph_only["misses"]},
        "reduced": graph_only["reduced"],
        "conftest_variant": {"reached": conftest, "selected_as": "universe" if conftest else "selection", **with_conftest},
        "known_failures": {"in_selection": sorted(known & selected),
                           "label_outside_selection": NOT_INVESTIGATED,
                           "outside_selection": sorted(known - selected)},
    }


def summary_line(record: dict) -> str:
    sets, variant = record["sets"], record["conftest_variant"]
    chosen = len(sets["U"]) if variant["reached"] else len(sets["S"])
    return (f"impact shadow: selected {len(sets['S'])} of {len(sets['U'])}, misses {len(sets['misses'])} "
            f"(conftest variant: selected {chosen}, misses {len(variant['misses'])}), "
            f"attempt_valid {str(record['attempt_valid']).lower()}")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run(args: argparse.Namespace) -> dict:
    selection = json.loads(Path(args.selection).read_text(encoding="utf-8"))
    if not isinstance(selection, dict):
        raise ValueError("the selection is not a JSON object")
    ci_dir = Path(__file__).resolve().parent
    exclusions, known = known_failure_files(
        Path(args.exclusions or ci_dir / "test_exclusions.txt"), Path(args.expected_skips or ci_dir / "xdist_expected_skips.txt"))
    paths = [p for p in Path(args.paths_file).read_text(encoding="utf-8").splitlines() if p.strip()]
    identity = {"run_id": args.run_id, "run_attempt": args.run_attempt, "base_sha": args.base,
                "candidate_sha": args.candidate, "changed_paths": paths}
    evidence = read_evidence(Path(args.evidence), (args.run_id, args.run_attempt))
    return build_record(selection, evidence, exclusions, known, identity)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for flag in ("--selection", "--evidence", "--run-id", "--run-attempt", "--base", "--candidate", "--paths-file", "--output"):
        parser.add_argument(flag, required=True)
    for flag in ("--summary", "--exclusions", "--expected-skips"):
        parser.add_argument(flag)
    args = parser.parse_args(argv)
    record = run(args)
    write_atomic(Path(args.output), json.dumps(record, indent=2, sort_keys=True) + "\n")
    if args.summary:
        write_atomic(Path(args.summary), summary_line(record) + "\n")
    print(summary_line(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
