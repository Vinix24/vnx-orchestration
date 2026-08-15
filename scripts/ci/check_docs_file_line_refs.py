#!/usr/bin/env python3
"""CI check: fail when a ``docs/`` file:line reference drifts from the tree.

The lane-conformity-matrix and the other living docs cite source locations as
``file.py:123`` (or ``file.py:120-130``).  Every merge that moves a function a
few lines makes those citations a little more wrong, and nobody notices.  This
check makes the drift fail CI instead of silently rotting.

Scope — deliberately narrow, so it never false-alarms on a doc edit:

* **Only explicit line numbers.**  A bare filename (``file.py``) is never
  flagged: prose mentions files constantly, and a filename alone carries no
  line claim to drift.  The pattern matched is ``file.py:123`` (ranges and
  comma lists allowed: ``file.py:120-130``, ``file.py:120,123``).
* **Only living docs.**  Frozen point-in-time artifacts are skipped: they are
  historical records, and "correcting" their line numbers to today's values
  would falsify what they recorded.  Skipped: ``docs/_archive/``,
  ``docs/examples/`` (illustrative), ``docs/investigations/`` (dated triage
  reports), ``docs/governance/decisions/`` (Nygard ADRs, whose own survey cites
  are explicitly "not re-grepped to a line number"), and
  ``docs/internal/{plans,intelligence}/`` (historical proposals that cite an
  architecture that no longer exists).
* **Only tracked docs.**  Gitignored trees (``docs/internal/``,
  ``docs/orchestration/``) live on the maintainer's checkout but never ship,
  and their citations point at a local-only architecture CI has never seen.
  Scanning them made the check fail locally while CI stayed green.  They are
  not living repo docs, so untracked files are skipped outright.
* **Code fences are skipped.**  A fenced block is a literal listing or schema
  example (``scripts/lib/foo.py:10-20`` in a JSON example is a placeholder,
  not a citation).  Real citations live inline in backtick prose.

Resolution — robust enough to read the docs as written:

* ``dispatch_cli.py``        — bare basename, unique in the tree.
* ``scripts/lib/foo.py``     — exact repo-relative path.
* ``append_receipt_internals/payload.py`` — path relative to ``scripts/lib/``.

All three resolve through one rule: the cited path must equal, or be the
suffix of, exactly one tracked file.  Zero matches is "not found"; more than
one is "ambiguous"; both are violations, because the doc no longer names a
single, checkable location.

Exit 0 when every living citation resolves and its line numbers are in bounds.
Exit 1 with the drifted citations listed otherwise.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Living docs only — these subdirectories of docs/ are frozen artifacts.
_EXEMPT_DOC_SUBDIRS = frozenset({
    "_archive",
    "examples",
    "investigations",
    "governance/decisions",
    # Historical proposals and design notes (docs/internal/{plans,intelligence})
    # that cite an architecture that no longer exists (dispatcher_v8_minimal.sh,
    # unified_state_manager_v2.py, receipt_processor_v4.sh, ...).  "Correcting"
    # their line numbers to today's values would falsify what they recorded.
    # They are also gitignored, so they never ship; keep them carved out here so
    # the check never false-alarms if one is ever re-added to the tree.
    "internal/plans",
    "internal/intelligence",
})

# Extensions we treat as "a file" in a citation.  Covering the observed set
# (py, sh, yaml, yml, md, ts, toml) plus the obvious neighbours.
_KNOWN_EXTENSIONS = (
    "py|sh|yaml|yml|json|md|toml|ts|js|sql|txt|ndjson|tpl|go|rs"
)

# ``file.py:123`` / ``file.py:120-130`` / ``file.py:120,123`` — the file part
# must look like a path (word chars, dots, slashes, dashes) and end in a known
# extension.  A leading dot is allowed so hidden-directory citations
# (``.github/workflows/x.yml``, ``.vnx/x.yaml``) resolve instead of being
# misread as ``github/...``.  ``(?<![\w/])`` keeps ``https://host:8080`` and
# ``foo.py2:3`` out.
_REF_RE = re.compile(
    r"(?<![\w/])"
    r"(?P<file>[.]?[\w][\w./-]*\.(?:{ext}))"
    r":(?P<lines>\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)".format(ext=_KNOWN_EXTENSIONS)
)


def tracked_rel_paths(repo_root: Path) -> list[str]:
    """Return every tracked file path, repo-relative and posix-separated."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def resolve_ref(ref: str, paths: list[str]) -> tuple[str, str]:
    """Resolve a cited path against tracked files.

    Returns ``(status, resolved)`` where status is one of ``ok``,
    ``not_found``, or ``ambiguous``, and *resolved* is the matched
    repo-relative path (empty unless status is ``ok``).
    """
    candidates = [p for p in paths if p == ref or p.endswith("/" + ref)]
    if len(candidates) == 1:
        return "ok", candidates[0]
    if not candidates:
        return "not_found", ""
    return "ambiguous", ""


def _in_bounds(linespec: str, file_lines: int) -> bool:
    """True when every line number in *linespec* is within the file."""
    for part in linespec.split(","):
        if "-" in part:
            start, _, end = part.partition("-")
            start_n, end_n = int(start), int(end)
            # A reversed range (``1702-1671``) is malformed, not merely shifted.
            if start_n > end_n or end_n > file_lines:
                return False
        elif int(part) > file_lines:
            return False
    return True


def check_ref(ref: str, linespec: str, repo_root: Path, paths: list[str]) -> str | None:
    """Return a violation string for one citation, or None when it checks out."""
    status, resolved = resolve_ref(ref, paths)
    if status == "not_found":
        return f"{ref}:{linespec} — file not found"
    if status == "ambiguous":
        return f"{ref}:{linespec} — ambiguous (matches {_ambiguous_count(ref, paths)} files)"
    file_lines = _count_lines(repo_root / resolved)
    if not _in_bounds(linespec, file_lines):
        return f"{resolved}:{linespec} — line out of bounds (file has {file_lines} lines)"
    return None


def _ambiguous_count(ref: str, paths: list[str]) -> int:
    return sum(1 for p in paths if p == ref or p.endswith("/" + ref))


def _count_lines(path: Path) -> int:
    return sum(1 for _ in path.open(encoding="utf-8", errors="ignore"))


def scan_markdown(text: str) -> list[tuple[int, str, str]]:
    """Return ``(line_no, ref, linespec)`` for inline citations only.

    Lines inside fenced code blocks (````` ``` ``` ``) are skipped: a fence
    marks a literal listing or schema example, not a prose citation.
    """
    citations: list[tuple[int, str, str]] = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in _REF_RE.finditer(line):
            citations.append((line_no, match.group("file"), match.group("lines")))
    return citations


def is_live_doc(doc_rel: str) -> bool:
    """True when *doc_rel* (relative to docs/) is a living reference doc."""
    return not any(
        doc_rel == prefix or doc_rel.startswith(prefix + "/")
        for prefix in _EXEMPT_DOC_SUBDIRS
    )


def iter_live_docs(docs_dir: Path, paths: list[str] | None = None) -> list[Path]:
    """Return every living .md under *docs_dir*, untracked and frozen subdirs excluded.

    *paths* is the tracked-file list (repo-relative posix paths) when known.
    When provided, a doc is scanned only if it is tracked: gitignored trees such
    as ``docs/internal/`` and ``docs/orchestration/`` live on the maintainer's
    checkout but never ship, and their line numbers describe a local-only
    architecture that CI has never seen.
    """
    tracked = None
    if paths is not None:
        tracked = {p for p in paths if p.startswith("docs/") and p.endswith(".md")}
    docs: list[Path] = []
    for md in sorted(docs_dir.rglob("*.md")):
        rel = md.relative_to(docs_dir).as_posix()
        if tracked is not None and "docs/" + rel not in tracked:
            continue
        if is_live_doc(rel):
            docs.append(md)
    return docs


def check_docs(docs_dir: Path, repo_root: Path, paths: list[str]) -> list[str]:
    """Return violation strings for every drifted citation in living docs."""
    violations: list[str] = []
    for md in iter_live_docs(docs_dir, paths):
        text = md.read_text(encoding="utf-8", errors="ignore")
        for line_no, ref, linespec in scan_markdown(text):
            violation = check_ref(ref, linespec, repo_root, paths)
            if violation is not None:
                rel = md.relative_to(repo_root).as_posix()
                violations.append(f"  {rel}:{line_no} — {violation}")
    return violations


def resolve_repo_root(argv: list[str]) -> Path:
    """Return the repo root from ``--root`` or ``git rev-parse``."""
    if "--root" in argv:
        return Path(argv[argv.index("--root") + 1]).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip()).resolve()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repo_root = resolve_repo_root(args)
    docs_dir = repo_root / "docs"
    if not docs_dir.is_dir():
        print(f"ERROR: docs/ not found under {repo_root}")
        return 1

    paths = tracked_rel_paths(repo_root)
    violations = check_docs(docs_dir, repo_root, paths)

    if not violations:
        print("OK: every docs/ file:line reference resolves and is in bounds.")
        return 0

    print("ERROR: drifted docs/ file:line references:\n")
    for line in violations:
        print(line)
    print()
    print("Fix the citation to match the current tree (or drop the line number")
    print("if the reference is meant to be illustrative rather than exact).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
