#!/usr/bin/env python3
"""guard_reachability_calibration.py — the detector's own tripwire.

golf-4's valkuil #4: a detector whose positive signal depends on a field
that is itself never filled will report a permanently clean "0 findings" and
nobody will notice it stopped working. The fix is the same shape as the bug
it hunts: a condition (here, ``run_selftest`` passing) must be shown to
depend on something that actually varies.

``CALIBRATION_CASES`` freezes ONE real, historical instance of the bug class
(OI-1632 / #1774, 2026-09-05): before that fix, ``stage_spec_bundle()`` never
accepted a ``track_id`` parameter, so ``spec.track_id`` was always ``None``
for every bridge-staged dispatch and the plan-gate enforcement guard in
``dispatch_cli._check_track_link_verdict`` could never take its blocking
branch (measured: 0 of 681 non-deliverable dispatch rows had a track). The
fix landed on main, so the LIVE repo no longer reproduces the bug — which is
exactly why the self-test cannot just re-run the scanner against the current
tree. Instead it fetches the real PRE-FIX source straight from git history
(never retyped, so it cannot silently drift from what the bug actually was)
and re-runs the scanner against that frozen snapshot.

``run_selftest`` fails loud, not quiet, when:
  1. the scanner no longer finds a guard on the calibration field in the
     frozen historical source (a scanner regression), or
  2. the calibration field has been added to ``ACCEPTED_GAPS`` (a confirmed
     historical bug can never legitimately become a "designed" gap — if this
     fires, someone used the escape hatch to launder a real defect).
"""
from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from guard_reachability_scanner import find_guarded_field_refs

# fix(dispatch): stage_spec_bundle draagt geen track_id, dus geen enkele
# dispatch heeft een track (OI-1632) (#1774) — merged into main 2026-09-05.
PRE_FIX_COMMIT = "ee818845"
PRE_FIX_FILE = "scripts/lib/dispatch_cli.py"
PRE_FIX_FUNCTION = "_check_track_link_verdict"


@dataclass(frozen=True)
class CalibrationCase:
    name: str
    field: str
    pre_fix_commit: str
    pre_fix_file: str
    pre_fix_function: str
    description: str


CALIBRATION_CASES: Tuple[CalibrationCase, ...] = (
    CalibrationCase(
        name="OI-1632-track_id",
        field="track_id",
        pre_fix_commit=PRE_FIX_COMMIT,
        pre_fix_file=PRE_FIX_FILE,
        pre_fix_function=PRE_FIX_FUNCTION,
        description=(
            "stage_spec_bundle() never accepted track_id, so spec.track_id "
            "was always None for every bridge-staged dispatch (0 of 681 "
            "non-deliverable rows had a track); the plan-gate enforcement "
            "guard in _check_track_link_verdict could never take its "
            "blocking branch. Fixed in ee818845 (#1774, 2026-09-05)."
        ),
    ),
)


class CalibrationFetchError(RuntimeError):
    pass


class SelfTestFailure(RuntimeError):
    pass


def fetch_pre_fix_function_source(root: Path, case: CalibrationCase) -> str:
    """The REAL historical guard source, read from git — never retyped.

    Fetches the full pre-fix file via ``git show <commit>^:<path>`` (the
    parent of the fix commit, i.e. the last commit BEFORE the fix) and
    extracts only ``case.pre_fix_function`` via ``ast`` so the fixture stays
    small and unambiguously tied to the exact function the bug lived in.
    """
    ref = f"{case.pre_fix_commit}^:{case.pre_fix_file}"
    try:
        result = subprocess.run(
            ["git", "show", ref],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CalibrationFetchError(f"git show {ref} failed to run: {exc}") from exc
    if result.returncode != 0:
        raise CalibrationFetchError(
            f"git show {ref} exited {result.returncode}: {result.stderr.strip()}"
        )
    full_source = result.stdout
    try:
        tree = ast.parse(full_source, filename=case.pre_fix_file)
    except SyntaxError as exc:
        raise CalibrationFetchError(f"pre-fix {case.pre_fix_file} did not parse: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == case.pre_fix_function:
            segment = ast.get_source_segment(full_source, node)
            if segment:
                return segment
    raise CalibrationFetchError(
        f"{case.pre_fix_function} not found in pre-fix {case.pre_fix_file} at {ref}"
    )


def run_selftest(root: Path, accepted_gap_fields: frozenset) -> List[str]:
    """Re-confirm every calibration case; raise :class:`SelfTestFailure` on
    ANY regression. Returns the list of per-case confirmation strings on
    success (for a human-readable ``selftest`` CLI report).
    """
    confirmations: List[str] = []
    failures: List[str] = []

    for case in CALIBRATION_CASES:
        if case.field in accepted_gap_fields:
            failures.append(
                f"{case.name}: field {case.field!r} is a CONFIRMED historical "
                "bug (see description below) but appears in ACCEPTED_GAPS — a "
                "real defect must never be marked as a deliberate, designed "
                "gap.\n    " + case.description
            )
            continue
        try:
            source = fetch_pre_fix_function_source(root, case)
        except CalibrationFetchError as exc:
            failures.append(f"{case.name}: could not fetch calibration source: {exc}")
            continue
        refs = find_guarded_field_refs(
            source, case.pre_fix_file, known_attr_fields={case.field},
        )
        matches = [r for r in refs if r.field == case.field]
        if not matches:
            failures.append(
                f"{case.name}: scanner found ZERO guards on field "
                f"{case.field!r} in the known-broken historical source "
                f"({case.pre_fix_commit}^:{case.pre_fix_file}:{case.pre_fix_function}) "
                "— this is a detector regression. Do not trust a clean audit "
                "report until this is fixed."
            )
            continue
        confirmations.append(
            f"{case.name}: found {len(matches)} guard(s) on field "
            f"{case.field!r} in the frozen pre-fix source — OK"
        )

    if not confirmations and not failures:
        # Cannot happen while CALIBRATION_CASES is non-empty, but an empty
        # CALIBRATION_CASES tuple must never read as "self-test passed" —
        # zero known cases confirmed is itself the failure this guards
        # against (valkuil #4).
        failures.append("CALIBRATION_CASES is empty — self-test cannot confirm anything")

    if failures:
        raise SelfTestFailure("\n".join(failures))
    return confirmations
