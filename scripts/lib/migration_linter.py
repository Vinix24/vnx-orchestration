"""migration_linter.py — CI guard: reject a new column DEFAULT on the dev tenant name.

WHAT IT DOES
------------
Finds every ``DEFAULT`` clause whose value is the quoted dev tenant name
(single or double quotes, any case of DEFAULT) and fails on a clause the
operator-name guard does not list.

It owns no list and no scope of its own. The matcher is
``scripts/ci/operator_names.py`` (``scan``): tracked files under scripts/,
vnx_cli/, schemas/, bin/ and hooks/ (including the packaged worker default), tests and
markdown excluded. The list is the guard's per-finding table
``tests/fixtures/operator_names/findings.tsv``. A finding is listed by
(path, name, fingerprint of the normalised line) and a count; a new line, an
edited line or an extra copy of a listed line is a violation. There is no
whole-file allowance.

WHY
---
Phase 3 of the W1 tenant-stamping fix (ADR-007) drops the DEFAULT clauses so
that an unqualified INSERT into a project-scoped table fails loudly instead of
stamping the wrong tenant. New migrations and DDL helpers must stamp
project_id explicitly (``resolve_init_project_id()`` from
``project_id_migration.py``).

USAGE
-----
From project root:

    python3 scripts/lib/migration_linter.py
    python3 scripts/lib/migration_linter.py --strict   # fail on any occurrence, listed or not

Exit codes:
    0 — no new violations found
    1 — one or more new violations found (or --strict: any occurrence)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci"))
import operator_names as _guard  # the one matcher and the one table

# A DEFAULT clause on the quoted dev tenant name; built from halves so this file
# carries no finding of its own.
_DEV_NAME = "vnx-" + "dev"
_PATTERN = re.compile(r"DEFAULT\s+['\"]" + re.escape(_DEV_NAME) + r"['\"]", re.IGNORECASE)
_DEFAULT_CLAUSE = f"DEFAULT '{_DEV_NAME}'"


def _find_project_root(start: Path) -> Path:
    """Walk up from ``start`` looking for pyproject.toml or .git."""
    current = start.resolve()
    for ancestor in [current, *current.parents]:
        if (ancestor / "pyproject.toml").is_file() or (ancestor / ".git").is_dir():
            return ancestor
    return current


def scan(
    project_root: Path,
    *,
    strict: bool = False,
    table: list[dict] | None = None,
) -> list[dict]:
    """Violations: DEFAULT clauses on the dev tenant name that the guard's table does not list.

    Returns {"file": rel_path_str, "line": int, "text": str} dicts. The guard's matcher
    runs over its own scope; a root that is not a git checkout raises, never passes.
    ``strict`` reports every occurrence. Otherwise a finding is a violation when its
    (path, name, fingerprint) is not in the table, and each line beyond the table's count
    of a listed finding is one too. ``table`` defaults to the guard's table.
    """
    rows = _guard.load_table() if table is None else table
    allowed = {(p, n, f): c for p, n, f, c in _guard.generate(rows)["entries"]}
    violations: list[dict] = []
    for (rel, name, fp), (count, _line) in sorted(_guard.scan(project_root).items()):
        if name != _DEV_NAME:
            continue
        hits = [(no, text) for no, text in _lines_of(project_root / rel, fp) if _PATTERN.search(text)]
        if not hits:
            continue
        # strict: every hit. Normal: a missing key allows 0, so every hit; else the surplus.
        skip = 0 if strict else allowed.get((rel, name, fp), 0)
        for no, text in hits[skip:]:
            violations.append({"file": rel, "line": no, "text": text.rstrip()})
    return violations


def _lines_of(path: Path, fp: str) -> list[tuple[int, str]]:
    """(line number, text) of every line of ``path`` with fingerprint ``fp``; an unreadable file raises."""
    text = path.read_bytes().decode("utf-8", "replace")
    return [(no, line) for no, line in enumerate(text.split("\n"), start=1)
            if _guard.fingerprint(line) == fp]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CI linter: fail on a new DEFAULT clause on the dev tenant name that the operator-name guard does not list.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on ANY occurrence, including findings the guard's table lists.",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Project root directory. Defaults to auto-detected root (pyproject.toml / .git).",
    )
    args = parser.parse_args(argv)

    root = Path(args.project_root) if args.project_root else _find_project_root(Path(__file__))
    violations = scan(root, strict=args.strict)

    if violations:
        print(
            f"[migration-linter] FAIL — {len(violations)} new {_DEFAULT_CLAUSE} occurrence(s) found.",
            file=sys.stderr,
        )
        print(
            f"[migration-linter] New migrations and DDL helpers MUST NOT use {_DEFAULT_CLAUSE}.",
            file=sys.stderr,
        )
        print(
            "[migration-linter] Use resolve_init_project_id() from project_id_migration.py instead.",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v['file']}:{v['line']}: {v['text']}", file=sys.stderr)
        return 1

    mode_label = "strict" if args.strict else "normal (guard table active)"
    print(f"[migration-linter] OK — no new {_DEFAULT_CLAUSE} occurrences found ({mode_label}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
