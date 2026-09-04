#!/usr/bin/env python3
"""CI check: every bare repo-path citation in ``00_VNX_ARCHITECTURE.md`` resolves.

``scripts/ci/check_docs_file_line_refs.py`` already guards ``file.py:123``
style citations (a path *plus a line number*) across all living docs, but it
deliberately skips a bare filename — "prose mentions files constantly, and a
filename alone carries no line claim to drift" (see its docstring). That
carve-out is correct for casual filename mentions, but ``00_VNX_ARCHITECTURE.md``
also cites full repo-relative *paths* (``docs/core/technical/X.md``,
``scripts/lib/foo.py``, ``claudedocs/some-prd.md``) as pointers to a specific
file's location -- exactly the kind of claim that goes stale silently when a
file moves, is renamed, or is deleted, because nothing re-checks it.

This check closes that gap for the one document it is scoped to: it extracts
every backtick-quoted, ``/``-containing token from the doc's inline prose
(fenced code blocks excluded -- a JSON/YAML example inside a fence is a
placeholder, not a citation) and asserts each one names a file that is
actually tracked by git. A token is treated as a citation, not a repo path,
and skipped when it:

* ends in ``/`` -- a generic directory-prefix mention (``state/``,
  ``.claude/skills/``), not a claim about one specific file;
* starts with ``~`` -- a user-home path (``~/.claude/skills/``) that ``vnx
  init`` creates on demand, not a path this repo ships;
* starts with ``/`` -- an HTTP route (``/api/events``) or a provider
  slash-command (``/model``, ``/clear``), never a filesystem path in this doc;
* sits under ``.vnx-data/`` or ``.vnx/`` -- the doc's own File System Layout
  section marks both roots "(gitignored)": runtime/config artifacts the doc
  documents as *not* shipped, so an example filename under them is a
  designed illustration, not a broken reference;
* starts with ``state/`` or ``logs/`` -- shorthand the doc uses throughout for
  files nested one level under that same gitignored runtime-data root (e.g.
  ``state/t0_receipts.ndjson`` names a file inside it); same reasoning as the
  carve-out above, just one path segment shorter;
* carries a trailing ``:123`` / ``:120-130`` line-number suffix -- that is a
  ``file.py:123`` citation, already covered precisely (including line-bounds)
  by ``check_docs_file_line_refs.py``; the suffix is stripped before the
  existence check so this script does not re-flag it under a different rule;
* has no extension on its final ``/``-separated segment and does not start
  with a recognized top-level repo root (``bin/``, ``scripts/``, ``docs/``,
  ``tests/``, ``dashboard/``, ``schemas/``, ``templates/``, ``.claude/``,
  ``.github/``, ``.gemini/``) -- catches settings-key shorthand like
  ``permissions.allow/deny`` or ``allow/deny`` (a slash meaning "either", not
  a path separator: the final segment ``deny`` has no extension) without a
  hand-maintained denylist of every such phrase.

Ground truth is ``git ls-files`` (repo-relative, POSIX-separated), the same
source the sibling checker uses -- so a path that exists only in an
untracked/gitignored local directory (``claudedocs/`` is fully gitignored)
correctly fails here too: a citation from a shipped doc into a directory that
never ships cannot be verified by anyone who clones the repo fresh.

Usage:
  python3 scripts/ci/check_architecture_doc_paths.py            # exit 1 on drift
  python3 scripts/ci/check_architecture_doc_paths.py --root DIR # explicit repo root
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

DOC_REL = "docs/core/00_VNX_ARCHITECTURE.md"

# Backtick-quoted token containing a ``/``, no whitespace or shell/template
# metacharacters -- the same "does this look like a path, not a placeholder"
# filter used when this check was first measured against the live doc.
_BACKTICK_PATH_RE = re.compile(r"`([^`\s]*?/[^`\s]*?)`")
_DISALLOWED_CHARS = set("<>{}$()*|&;")
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
_RUNTIME_SHORTHAND_PREFIXES = ("state/", "logs/")
_KNOWN_ROOT_PREFIXES = (
    "bin/", "scripts/", "docs/", "tests/", "dashboard/", "schemas/",
    "templates/", ".claude/", ".github/", ".gemini/",
)


def _strip_line_suffix(token: str) -> str:
    return _LINE_SUFFIX_RE.sub("", token)


def _looks_like_repo_path(token: str) -> bool:
    if any(ch in _DISALLOWED_CHARS for ch in token):
        return False
    if token.endswith("/"):
        return False
    if token.startswith("~") or token.startswith("/"):
        return False
    if token.startswith(".vnx-data/") or token.startswith(".vnx/"):
        return False
    if token.startswith(_RUNTIME_SHORTHAND_PREFIXES):
        return False
    checkable = _strip_line_suffix(token)
    last_segment = checkable.rsplit("/", 1)[-1]
    if "." not in last_segment and not checkable.startswith(_KNOWN_ROOT_PREFIXES):
        return False
    return True


def scan_doc(text: str) -> list[tuple[int, str]]:
    """Return ``(line_no, path)`` for every citation candidate outside fences.

    A trailing ``:123``/``:120-130`` line-number suffix is stripped from the
    returned path -- ``check_docs_file_line_refs.py`` already verifies that
    part precisely (including line-bounds); this check only needs the file
    itself to exist.
    """
    citations: list[tuple[int, str]] = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in _BACKTICK_PATH_RE.finditer(line):
            token = match.group(1)
            if _looks_like_repo_path(token):
                citations.append((line_no, _strip_line_suffix(token)))
    return citations


def tracked_rel_paths(repo_root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def check(doc_text: str, tracked: set[str]) -> list[str]:
    """Return violation strings for every citation that does not resolve."""
    violations: list[str] = []
    for line_no, token in scan_doc(doc_text):
        if token not in tracked:
            violations.append(f"  {DOC_REL}:{line_no} — `{token}` not tracked in the repo")
    return violations


def resolve_repo_root(argv: list[str]) -> Path:
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
    doc_path = repo_root / DOC_REL
    if not doc_path.is_file():
        print(f"ERROR: {DOC_REL} not found under {repo_root}")
        return 2

    doc_text = doc_path.read_text(encoding="utf-8")
    tracked = tracked_rel_paths(repo_root)
    violations = check(doc_text, tracked)

    if not violations:
        print(f"OK: every bare path citation in {DOC_REL} resolves to a tracked file.")
        return 0

    print(f"ERROR: drifted path citations in {DOC_REL}:\n")
    for line in violations:
        print(line)
    print()
    print("Fix the citation to match the current tree, or drop it if the path")
    print("no longer names a shipped file.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
