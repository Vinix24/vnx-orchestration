#!/usr/bin/env python3
"""Whole-tree function size at a git ref (golf-2-hardenen, G2-0).

Counting, scope and the ``git show`` reading are the per-PR ratchet's own
(``function_size_gate``); the walk over ``git ls-tree -r`` is all that is new.

Usage: check_function_size_tree.py [--ref HEAD] [--prefix scripts/] [--max 200] [--json]
Text: `function size tree: <A> above <max>, <B> above 70, <F> functions in <N> files
at <ref> (<sha8>)`, then `unparsed <path>` per file that does not parse, then
`<lines> <path>::<qualname> (line <lineno>)` per function above --max, longest first.
JSON: one object with the same numbers; `unparsed` there is the count of those paths
and `unparsed_paths` the sorted list itself.
Exit 0 clean, 1 something above --max or a file that does not parse (an unmeasured
file is no proof that it is small), 2 usage or git error, or a measurement of zero
files (a prefix that matches nothing proves nothing, OI-2570).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from function_size_gate import FUNCTION_SIZE_LIMIT, GitError, in_scope, measure_at
from function_size_gate import paths_at, resolve_ref


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.exit(2, f"{self.prog}: error: {message}\n")


def _summary(payload: dict) -> str:
    return (
        f"function size tree: {payload['above_max']} above {payload['max']}, "
        f"{payload['above_70']} above {FUNCTION_SIZE_LIMIT}, {payload['functions']} functions "
        f"in {payload['files']} files at {payload['ref']} ({payload['sha'][:8]})"
    )


def render(payload: dict, unparsed_paths: list[str]) -> list[str]:
    """Summary, the unparsed paths, then the offenders longest first."""
    lines = [_summary(payload), *(f"unparsed {path}" for path in unparsed_paths)]
    lines += [f"{o['executable_lines']} {o['path']}::{o['qualname']} (line {o['lineno']})"
              for o in payload["offenders"]]
    return lines


def measure_tree(repo: Path, ref: str, prefix: str, max_lines: int) -> tuple[dict, list[str]]:
    """(JSON payload, unparsed paths) for every in-scope file under `prefix` at `ref`."""
    sha = resolve_ref(repo, ref)
    paths = [path for path in paths_at(repo, ref) if path.startswith(prefix) and in_scope(path)]
    functions = above_70 = 0
    unparsed: list[str] = []
    offenders: list[dict] = []
    for path in paths:
        measured = measure_at(repo, ref, path)
        if measured is None:
            unparsed.append(path)
            continue
        functions += len(measured)
        above_70 += sum(1 for fn in measured if fn.executable_lines > FUNCTION_SIZE_LIMIT)
        offenders += [
            {"path": path, "qualname": fn.qualname, "lineno": fn.lineno, "executable_lines": fn.executable_lines}
            for fn in measured if fn.executable_lines > max_lines
        ]
    offenders.sort(key=lambda offender: (-offender["executable_lines"], offender["path"]))
    payload = {"ref": ref, "sha": sha, "prefix": prefix, "max": max_lines, "files": len(paths),
               "functions": functions, "above_max": len(offenders), "above_70": above_70,
               "unparsed": len(unparsed), "unparsed_paths": sorted(unparsed),
               "offenders": offenders}
    return payload, sorted(unparsed)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Function size over the whole tree at a git ref")
    parser.add_argument("--ref", default="HEAD", help="git ref to measure (default HEAD)")
    parser.add_argument("--prefix", default="scripts/", help="only paths under this prefix")
    parser.add_argument("--max", type=int, default=200, help="report functions above this")
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    args = parser.parse_args(argv)
    try:
        payload, unparsed_paths = measure_tree(Path.cwd(), args.ref, args.prefix, args.max)
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if payload["files"] == 0:
        print(
            f"error: measured 0 files under prefix {args.prefix!r} at {args.ref}; "
            "an empty measurement proves nothing",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, indent=2) if args.json else "\n".join(render(payload, unparsed_paths)))
    return 0 if not payload["offenders"] and payload["unparsed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
