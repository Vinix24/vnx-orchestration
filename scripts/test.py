#!/usr/bin/env python3
"""Fast syntax/config smoke-check for the repo.

Compiles every tracked Python file (py_compile) and parses every tracked
YAML/JSON config file. Catches syntax errors and malformed config before
they reach CI. Only inspects files known to git (``git ls-files``), so
untracked scratch/build output is never flagged.

Usage:
    python3 scripts/test.py            # check the whole repo
    python3 scripts/test.py --quiet    # only print failures + summary
"""
from __future__ import annotations

import argparse
import json
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from project_root import resolve_project_root  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None


def _tracked_files(root: Path, *suffixes: str) -> list[Path]:
    out = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", *[f"*{s}" for s in suffixes]],
        text=True,
    )
    return [root / line for line in out.splitlines() if line]


def _check_python(path: Path) -> str | None:
    with tempfile.TemporaryDirectory() as tmp_cfile_dir:
        cfile = Path(tmp_cfile_dir) / "out.pyc"
        try:
            py_compile.compile(str(path), cfile=str(cfile), doraise=True)
        except py_compile.PyCompileError as exc:
            return str(exc).strip()
        except (SyntaxError, ValueError) as exc:
            return f"{type(exc).__name__}: {exc}"
    return None


def _check_yaml(path: Path) -> str | None:
    if yaml is None:
        return None
    try:
        list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except yaml.YAMLError as exc:
        return str(exc).strip()
    except (OSError, UnicodeDecodeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _check_json(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"{type(exc).__name__}: {exc}"

    try:
        json.loads(text)
        return None
    except json.JSONDecodeError as exc:
        single_doc_error = str(exc).strip()

    # Some fixtures/receipts ship NDJSON under a .json extension (this repo's
    # governance ledgers are append-only NDJSON). Accept "one JSON object per
    # non-blank line" as a second valid shape before reporting a failure.
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return single_doc_error
    for line in lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return single_doc_error
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="only print failures and the final summary line",
    )
    args = parser.parse_args()

    root = resolve_project_root(__file__)

    checks: list[tuple[str, list[Path], object]] = [
        ("python", _tracked_files(root, ".py"), _check_python),
        ("yaml", _tracked_files(root, ".yaml", ".yml"), _check_yaml),
        ("json", _tracked_files(root, ".json"), _check_json),
    ]

    total = 0
    failures: list[tuple[Path, str]] = []

    for kind, files, checker in checks:
        for path in files:
            total += 1
            error = checker(path)
            if error is not None:
                failures.append((path, error))
            elif not args.quiet:
                print(f"ok    {kind:6s} {path.relative_to(root)}")

    for path, error in failures:
        print(f"FAIL         {path.relative_to(root)}: {error}", file=sys.stderr)

    checked = total - (0 if yaml is not None else len(_tracked_files(root, ".yaml", ".yml")))
    print(f"\n{checked}/{total} files checked, {len(failures)} failure(s)")
    if yaml is None:
        print("note: PyYAML not installed — .yaml/.yml files were skipped", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
