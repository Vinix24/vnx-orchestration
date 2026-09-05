#!/usr/bin/env python3
"""guard_reachability_audit.py — CI-runnable wachter for the golf-4 bug class.

golf-4 (2026-09-05): eight independently-discovered defects shared one shape
— a guard condition that gates a raise/return/skip or a gate decision reads
a field, column, or spec attribute that in practice is never filled. The
code looks fully wired. The branch it protects is unreachable. Nothing
raises, so nothing alerts.

This CLI combines the two halves built for this dispatch:
  - ``guard_reachability_scanner`` — AST-finds every guard that probes a
    named field via ``dict.get(str)`` / ``obj[str]`` / a known dataclass
    attribute.
  - ``guard_reachability_store`` — measures that field's real fill rate
    against the store registered for it in ``guard_reachability_registry``.

Subcommands:
  scan      — list every guarded-field reference found repo-wide (no store
              measurement; informational, always exits 0).
  audit     — scan + measure every field that has a FIELD_STORE_MAP entry;
              exits 1 if any UNSUPPRESSED zero-fill/missing-column finding
              exists, 0 otherwise. Suppressed findings (ACCEPTED_GAPS) are
              still printed, with their reason, never silently dropped.
  selftest  — re-confirm the calibration case(s) in
              ``guard_reachability_calibration`` against their frozen,
              real pre-fix source. Exits 1 (loud) if the detector can no
              longer find a single known-bad case — see that module's
              docstring for why this exists.

Deliberately NOT wired into CI yet (see the dispatch report's Open Items):
the first repo-wide `audit` run surfaces genuine, currently-unfixed findings
(new discoveries, not the calibration case). Wiring this into a blocking CI
gate today would force either a red pipeline or laundering those findings
into ACCEPTED_GAPS without real review — precisely the anti-pattern this
tool exists to catch.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPTS_DIR / "lib"
for _p in (str(_SCRIPTS_DIR), str(_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import project_root  # noqa: E402
import vnx_paths  # noqa: E402
from guard_reachability_calibration import (  # noqa: E402
    CALIBRATION_CASES,
    SelfTestFailure,
    run_selftest,
)
from guard_reachability_registry import (  # noqa: E402
    ACCEPTED_GAPS,
    FIELD_STORE_MAP,
    AcceptedGap,
    StoreTarget,
    validate_registry,
)
from guard_reachability_scanner import GuardedFieldRef, scan_repo_guarded_fields  # noqa: E402
from guard_reachability_store import (  # noqa: E402
    FillRate,
    measure_json_dir_fill_rate,
    measure_ndjson_fill_rate,
    measure_sqlite_column_fill_rate,
)


@dataclass(frozen=True)
class FieldFinding:
    field: str
    refs: Tuple[GuardedFieldRef, ...]
    target: StoreTarget
    rate: FillRate

    @property
    def is_defect_shape(self) -> bool:
        return (not self.rate.exists) or self.rate.is_zero_fill


def _measure_target(data_dir: Path, mapping_field: str, target: StoreTarget) -> FillRate:
    key = target.dict_key or mapping_field
    if target.kind == "sqlite":
        return measure_sqlite_column_fill_rate(
            data_dir / target.db_relpath, target.table, target.column,
        )
    if target.kind == "ndjson":
        paths = [data_dir / rel for rel in target.ndjson_relpaths]
        return measure_ndjson_fill_rate(paths, field=key)
    if target.kind == "json_dir":
        return measure_json_dir_fill_rate(data_dir / target.dir_relpath, target.glob, field=key)
    raise ValueError(f"unknown StoreTarget.kind={target.kind!r}")  # pragma: no cover - guarded in __post_init__


def _matching_gap(field: str, file: str, gaps: Tuple[AcceptedGap, ...]) -> Optional[AcceptedGap]:
    for gap in gaps:
        if gap.field != field:
            continue
        if gap.file is None or gap.file == file:
            return gap
    return None


def build_findings(
    root: Path, data_dir: Path,
) -> Tuple[List[FieldFinding], List[Tuple[FieldFinding, AcceptedGap]], List[FieldFinding], Dict[str, List[GuardedFieldRef]]]:
    """Returns (violations, suppressed, ok, unmeasured_by_field).

    ``unmeasured_by_field`` holds every guarded field the scanner found that
    has no ``FIELD_STORE_MAP`` entry — informational candidates for a human
    to triage into the registry, never treated as a violation on their own.
    """
    validate_registry()
    refs = scan_repo_guarded_fields(root)

    by_field: Dict[str, List[GuardedFieldRef]] = {}
    for r in refs:
        by_field.setdefault(r.field, []).append(r)

    mapping_by_field = {m.field: m for m in FIELD_STORE_MAP}

    violations: List[FieldFinding] = []
    suppressed: List[Tuple[FieldFinding, AcceptedGap]] = []
    ok: List[FieldFinding] = []
    unmeasured: Dict[str, List[GuardedFieldRef]] = {}

    for field, field_refs in sorted(by_field.items()):
        mapping = mapping_by_field.get(field)
        if mapping is None:
            unmeasured[field] = field_refs
            continue
        for target in mapping.targets:
            rate = _measure_target(data_dir, mapping.field, target)
            finding = FieldFinding(field=field, refs=tuple(field_refs), target=target, rate=rate)
            if finding.is_defect_shape:
                gap = None
                for ref in field_refs:
                    gap = _matching_gap(field, ref.file, ACCEPTED_GAPS)
                    if gap is not None:
                        break
                if gap is not None:
                    suppressed.append((finding, gap))
                else:
                    violations.append(finding)
            else:
                ok.append(finding)

    return violations, suppressed, ok, unmeasured


def _print_finding(finding: FieldFinding, *, label: str) -> None:
    t = finding.target
    where = (
        f"{t.table}.{t.column}" if t.kind == "sqlite"
        else (t.dict_key or finding.field)
    )
    store_desc = {
        "sqlite": f"sqlite:{t.db_relpath}::{where}",
        "ndjson": f"ndjson:{','.join(t.ndjson_relpaths)}::{where}",
        "json_dir": f"json_dir:{t.dir_relpath}/{t.glob}::{where}",
    }[t.kind]
    exists_desc = "MISSING" if not finding.rate.exists else f"{finding.rate.filled}/{finding.rate.total}"
    print(f"[{label}] field={finding.field!r} store={store_desc} fill={exists_desc}")
    for ref in finding.refs[:5]:
        print(f"    guarded at {ref.file}:{ref.lineno} ({ref.access_kind}) if {ref.test_source}")
    if len(finding.refs) > 5:
        print(f"    ... and {len(finding.refs) - 5} more guard site(s)")


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    refs = scan_repo_guarded_fields(root)
    by_field: Dict[str, List[GuardedFieldRef]] = {}
    for r in refs:
        by_field.setdefault(r.field, []).append(r)
    print(f"[guard-reachability scan] {len(refs)} guard site(s), {len(by_field)} distinct field(s)\n")
    for field, field_refs in sorted(by_field.items()):
        locations = ", ".join(f"{r.file}:{r.lineno}" for r in field_refs[:3])
        more = f" (+{len(field_refs) - 3} more)" if len(field_refs) > 3 else ""
        print(f"  {field}: {locations}{more}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else Path(vnx_paths.resolve_paths()["VNX_DATA_DIR"])
    violations, suppressed, ok, unmeasured = build_findings(root, data_dir)

    print(f"[guard-reachability audit] data_dir={data_dir}\n")
    for finding in violations:
        _print_finding(finding, label="VIOLATION")
    for finding, gap in suppressed:
        _print_finding(finding, label="SUPPRESSED")
        print(f"    reason: {gap.reason} (decided_by={gap.decided_by}, decided_on={gap.decided_on})")
    for finding in ok:
        _print_finding(finding, label="OK")

    print(
        f"\n{len(violations)} violation(s), {len(suppressed)} suppressed, "
        f"{len(ok)} ok, {len(unmeasured)} unmeasured field(s) (no registry mapping)"
    )
    if unmeasured and args.show_unmeasured:
        print("\nUnmeasured fields (candidates to triage into FIELD_STORE_MAP):")
        for field, refs in sorted(unmeasured.items()):
            print(f"  {field}: {len(refs)} guard site(s), e.g. {refs[0].file}:{refs[0].lineno}")

    return 1 if violations else 0


def cmd_selftest(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    accepted_fields = frozenset(g.field for g in ACCEPTED_GAPS)
    try:
        confirmations = run_selftest(root, accepted_fields)
    except SelfTestFailure as exc:
        print("[guard-reachability selftest] FAIL")
        print(str(exc))
        return 1
    print(f"[guard-reachability selftest] PASS ({len(CALIBRATION_CASES)} calibration case(s))")
    for line in confirmations:
        print(f"  {line}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(project_root.resolve_project_root(__file__)))
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="list guarded-field references, no measurement")
    p_scan.set_defaults(func=cmd_scan)

    p_audit = sub.add_parser("audit", help="scan + measure registered fields")
    p_audit.add_argument("--data-dir", default=None)
    p_audit.add_argument("--show-unmeasured", action="store_true")
    p_audit.set_defaults(func=cmd_audit)

    p_selftest = sub.add_parser("selftest", help="re-confirm calibration cases")
    p_selftest.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
