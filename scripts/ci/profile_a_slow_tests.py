#!/usr/bin/env python3
"""Profile A slow-test profiler: the slow tail of a pytest JUnit XML (TSK-0).

Usage: profile_a_slow_tests.py <junit.xml> [<junit.xml> ...] [--threshold 2.0]
       [--top N] [--json] [--baseline <junit.xml>] [--exclude-module <module> ...]

Per file: total time, test count, and the count plus summed time of the tests
above the threshold (exactly on it is not above); with several files, the
median of the slow sum and of its count. --baseline fixes the set to the tests
above the threshold in that file by test id (classname and name); every other
file reports that set's summed time and count, a missing test counts 0 s and is
listed, and the median uses the fixed-set sum. --exclude-module removes a
module (classname without trailing uppercase class names) from every sum, count,
median and baseline set and reports its seconds. --top lists the N slowest
modules of the last file; --json prints one object with all of the above.
Exit 2 on a missing or unreadable file or XML without testcase elements; no partial number.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


class InputError(Exception):
    """Input the profiler refuses: exit 2 with this message on stderr."""


@dataclass(frozen=True)
class TestCase:
    test_id: str
    module: str
    seconds: float


def module_of(classname: str) -> str:
    parts = classname.split(".")
    while parts and parts[-1][:1].isupper():
        parts.pop()
    return ".".join(parts)


def _parse_seconds(raw: str | None, path: Path) -> float:
    if raw is None:
        raise InputError(f"{path}: testcase without a time attribute")
    try:
        seconds = float(raw)
    except ValueError:
        raise InputError(f"{path}: testcase time {raw!r} is not a number") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise InputError(f"{path}: testcase time {raw!r} is not finite and non-negative")
    return seconds


def load_cases(path: Path) -> list[TestCase]:
    if not path.is_file():
        raise InputError(f"{path}: file not found")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise InputError(f"{path}: XML does not parse: {exc}") from exc
    except OSError as exc:
        raise InputError(f"{path}: cannot be read: {exc}") from exc
    cases = []
    for node in root.iter("testcase"):
        classname, name = node.get("classname"), node.get("name")
        if not classname or not name:
            raise InputError(f"{path}: testcase without classname/name ({classname!r}, {name!r})")
        cases.append(TestCase(f"{classname}::{name}", module_of(classname),
                              _parse_seconds(node.get("time"), path)))
    if not cases:
        raise InputError(f"{path}: XML contains no testcase elements")
    return cases


def file_report(path: Path, cases: list[TestCase], threshold: float) -> dict:
    slow = [case for case in cases if case.seconds > threshold]
    return {"path": str(path), "test_count": len(cases),
            "total_time": round(sum(c.seconds for c in cases), 3),
            "slow_count": len(slow), "slow_time": round(sum(c.seconds for c in slow), 3)}


def baseline_ids(baseline: list[TestCase], threshold: float, excluded: set[str]) -> list[str]:
    return [c.test_id for c in baseline if c.module not in excluded and c.seconds > threshold]


def baseline_report(cases: list[TestCase], ids: list[str]) -> dict:
    wanted = set(ids)
    found: dict[str, float] = {}
    for case in cases:
        if case.test_id in wanted:
            found[case.test_id] = found.get(case.test_id, 0.0) + case.seconds
    return {"baseline_total": len(wanted), "baseline_count": len(found),
            "baseline_time": round(sum(found.values()), 3),
            "missing": sorted(wanted - set(found))}


def excluded_report(loaded: list[tuple[Path, list[TestCase]]], modules: list[str]) -> list[dict]:
    rows = []
    for module in modules:
        per_file = {str(path): round(sum(c.seconds for c in cases if c.module == module), 3)
                    for path, cases in loaded}
        rows.append({"module": module, "per_file_seconds": per_file,
                     "total_seconds": round(sum(per_file.values()), 3)})
    return rows


def top_report(cases: list[TestCase], limit: int) -> list[dict]:
    totals: dict[str, float] = {}
    for case in cases:
        totals[case.module] = totals.get(case.module, 0.0) + case.seconds
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{"module": module, "seconds": round(seconds, 3)} for module, seconds in ranked]


def collect(args: argparse.Namespace) -> dict:
    loaded = [(Path(raw), load_cases(Path(raw))) for raw in args.files]
    baseline = load_cases(Path(args.baseline)) if args.baseline else None
    excluded = set(args.exclude_module)
    kept = [(path, [c for c in cases if c.module not in excluded]) for path, cases in loaded]
    report: dict = {"threshold": args.threshold,
                    "files": [file_report(path, cases, args.threshold) for path, cases in kept]}
    if baseline is not None:
        ids = baseline_ids(baseline, args.threshold, excluded)
        for row, (_path, cases) in zip(report["files"], kept):
            row.update(baseline_report(cases, ids))
    key = "baseline" if baseline is not None else "slow"
    rows = report["files"]
    report["median"] = {
        key + "_time": round(statistics.median(row[key + "_time"] for row in rows), 3),
        key + "_count": statistics.median(row[key + "_count"] for row in rows),
    }
    if args.top is not None:
        report["top"] = top_report(kept[-1][1], args.top)
    if excluded:
        report["excluded"] = excluded_report(loaded, args.exclude_module)
    return report


def render_text(report: dict) -> str:
    threshold, lines = report["threshold"], []
    for row in report["files"]:
        lines.append(row["path"])
        lines.append(f"  tests: {row['test_count']}  total: {row['total_time']:.1f} s")
        lines.append(f"  above {threshold:.1f} s: {row['slow_count']} tests, {row['slow_time']:.1f} s")
        if "baseline_count" in row:
            lines.append(f"  baseline set: {row['baseline_count']}/{row['baseline_total']} found,"
                         f" {row['baseline_time']:.1f} s")
            lines.extend(f"    missing: {test_id}" for test_id in row["missing"])
    if len(report["files"]) > 1:
        median, key = report["median"], "baseline" if "baseline_time" in report["median"] else "slow"
        lines.append(f"median over {len(report['files'])} files ({key} set): "
                     f"{median[key + '_time']:.1f} s over {median[key + '_count']:g} tests")
    if "top" in report:
        lines.append(f"slowest modules in {report['files'][-1]['path']}:")
        lines += [f"  {i}. {r['module']}  {r['seconds']:.1f} s" for i, r in enumerate(report["top"], 1)]
    if "excluded" in report:
        lines.append("excluded modules:")
        lines += [f"  {row['module']}: "
                  + ", ".join(f"{p} {s:.1f} s" for p, s in row["per_file_seconds"].items())
                  + f" (total {row['total_seconds']:.1f} s)" for row in report["excluded"]]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Profile A slow tail from JUnit XML.")
    parser.add_argument("files", nargs="+", metavar="junit.xml")
    parser.add_argument("--threshold", type=float, default=2.0, help="slow above this many seconds")
    parser.add_argument("--top", type=int, metavar="N", help="N slowest modules of the last file")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    parser.add_argument("--baseline", metavar="junit.xml", help="fix the slow set from this file")
    parser.add_argument("--exclude-module", action="append", default=[], metavar="MODULE",
                        help="leave this module out of every number (repeatable)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.top is not None and args.top < 1:
        parser.error("--top must be at least 1")
    if not math.isfinite(args.threshold) or args.threshold < 0:
        parser.error("--threshold must be finite and non-negative")
    try:
        report = collect(args)
    except InputError as exc:
        print(f"profile_a_slow_tests: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
