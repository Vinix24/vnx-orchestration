#!/usr/bin/env python3
"""CI check: docs/core/SUBSYSTEMS.md deterministic columns match the live
cockpit generator (framework-status-audit-and-cockpit PR-3).

Diffs the DETERMINISTIC columns only — subsystem / what / flag / status / doc.
The `health` column is intentionally excluded: it is dynamic (live beacon or
seed fallback) and would trip on every probe run, forcing a ledger recommit
for no reason. Wired via `make subsystems-check` and
`.github/workflows/subsystems-drift.yml`.

Missing/invalid documentation and orphan module pages are blocking.
Exit codes: 0 = columns and docs in sync, 1 = drift detected, 2 = internal error.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for sub in (_REPO_ROOT / "scripts", _REPO_ROOT / "scripts" / "lib"):
    s = str(sub)
    if s not in sys.path:
        sys.path.insert(0, s)

sys.path.insert(0, str(_REPO_ROOT))

LEDGER_PATH = _REPO_ROOT / "docs" / "core" / "SUBSYSTEMS.md"


def _deterministic_rows(md_text: str) -> list[tuple[str, str, str, str, str]]:
    """Parse (subsystem, what, flag, status, doc) tuples out of a ledger table,
    dropping the dynamic `health` column."""
    rows = []
    for line in md_text.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 6:
            continue
        subsystem, what, flag, status, _health, doc = cells
        if subsystem in ("subsystem", "") or set(subsystem) <= {"-"}:
            continue
        rows.append((subsystem, what, flag, status, doc))
    return rows


def _module_pages(directory: Path):
    """Enumerate pages without suppressing directory-read errors."""
    for path in directory.iterdir():
        if path.is_dir() and not path.is_symlink():
            yield from _module_pages(path)
        elif path.suffix == ".md":
            yield path


def _check_docs(rows: list[dict], repo_root: Path) -> bool:
    reasons = {"docs-bloat": "geschrapt", "test-suite": "geen-module",
               "cross-project-federation": "niet-gebouwd",
               "effectiveness-probe-framework": "niet-gebouwd"}
    referenced = set()
    valid = True
    for row in rows:
        doc = row["doc"]
        if row["subsystem"] in reasons and doc == reasons[row["subsystem"]]:
            continue
        if (not isinstance(doc, str) or not doc.startswith("docs/modules/")
                or ".." in Path(doc).parts):
            print(f"INVALID DOC — {row['subsystem']}: {doc!r}", file=sys.stderr)
            valid = False
            continue
        path = repo_root / doc
        if path.resolve() != path.absolute():
            print(f"INVALID DOC — {row['subsystem']}: {doc} (symlink)", file=sys.stderr)
            valid = False
        elif not path.is_file():
            print(f"MISSING DOC — {row['subsystem']}: {doc}", file=sys.stderr)
            valid = False
        referenced.add(path)
    for page in sorted(_module_pages(repo_root / "docs/modules")):
        if page not in referenced:
            print(f"ORPHAN DOC — {page.relative_to(repo_root)}", file=sys.stderr)
            valid = False
    return valid


def main() -> int:
    try:
        from vnx_cli.commands.subsystems import build_rows, _render_md
    except ImportError as exc:
        print(f"internal error: cannot import subsystems generator: {exc}", file=sys.stderr)
        return 2

    try:
        committed_text = LEDGER_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"internal error: cannot read {LEDGER_PATH}: {exc}", file=sys.stderr)
        return 2

    rows = build_rows()
    try:
        docs_valid = _check_docs(rows, _REPO_ROOT)
    except OSError as exc:
        print(f"internal error: cannot check module documentation: {exc}", file=sys.stderr)
        return 2
    if not docs_valid:
        return 1
    for row in rows:
        row["health"] = ""  # dynamic column — excluded from the drift comparison
    generated_rows = _deterministic_rows(_render_md(rows))
    committed_rows = _deterministic_rows(committed_text)

    if generated_rows == committed_rows:
        print(f"OK — {LEDGER_PATH} deterministic columns match the live registry "
              f"({len(generated_rows)} subsystems).")
        return 0

    print("DRIFT — docs/core/SUBSYSTEMS.md generated columns (subsystem/what/flag/status/doc) "
          "do not match `vnx subsystems --md`. Regenerate with:\n"
          "  python3 -m vnx_cli.main subsystems --md   # then splice the table back in\n",
          file=sys.stderr)

    generated_map = {r[0]: r for r in generated_rows}
    committed_map = {r[0]: r for r in committed_rows}

    for subsystem in sorted(set(generated_map) | set(committed_map)):
        gen = generated_map.get(subsystem)
        com = committed_map.get(subsystem)
        if gen != com:
            print(f"  {subsystem}:", file=sys.stderr)
            print(f"    generated: {gen}", file=sys.stderr)
            print(f"    committed: {com}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
