#!/usr/bin/env python3
"""Classify the ADR-079 scanset at --sha (default HEAD) into documentation layers.

Print one category per file. Unassigned files exit 1, or 0 with --report.
Unreadable snapshots exit 2 in both modes. No working-tree content is read.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from project_root import resolve_project_root


# ADR-079 part 8: changing these rules requires a code round and review gate.
LAYER_RULES = (
    ("Ingang", (
        "README.md", "CONTRIBUTING.md",  # Regel 4 row 1: closed entrance list.
        "docs/DOCS_INDEX.md", "docs/README.md",  # Regel 4 row 1.
        "docs/operations/README.md", "docs/applications/README.md",  # Regel 4 row 1.
        "docs/features/README.md",  # Regel 4 row 1.
    )),
    ("Verhaal", (
        "docs/manifesto/*.md",  # Regel 4 row 3: narrative.
        "docs/applications/coding-agents.md", "docs/applications/finance.md",  # Regel 4 row 3.
    )),
    ("A", ("docs/core/00_VNX_ARCHITECTURE.md",)),  # Regel 4 row 4: architecture.
    ("C", ("docs/EXIT_CODES.md", "docs/SCRIPTS_INDEX.md")),  # Regel 4 row 5: commands.
    ("M-index", ("docs/modules/*.md", "docs/core/SUBSYSTEMS.md")),  # Regel 4 row 6: module index.
    ("F", ("docs/features/*.md",)),  # Regel 4 row 7: features.
    ("R", (
        "docs/operations/*.md", "docs/QUICKSTART.md",  # Regel 4 row 8: runbooks.
        "docs/MIGRATION_GUIDE.md", "docs/DISPATCH_GUIDE.md",  # Regel 4 row 8.
        "docs/core/00_GETTING_STARTED.md", "docs/onboarding/*.md",  # Regel 4 row 8.
        "docs/guides/*.md", "docs/examples/*.md",  # Regel 4 row 8.
    )),
    ("M-achtergrond", (
        "docs/core/*.md", "docs/core/technical/*.md",  # Regel 4 row 9: mechanism background.
        "docs/contracts/*.md", "docs/intelligence/*.md",  # Regel 4 row 9.
        "docs/governance/*.md", "docs/gates/*.md",  # Regel 4 row 9.
        "docs/project-root-resolution.md",  # Regel 4 row 9.
    )),
)
_SCANSET = re.compile(r"(?:docs/.*\.md|README\.md|CONTRIBUTING\.md)\Z")
_FROZEN = re.compile(r"docs/(?:_archive|archive|investigations|compliance|governance/decisions)/")
_LINK = re.compile(r'''(?<!!)\[[^\]]*\]\(\s*(<[^<>\n]+>|[^\s()]+?)(?:\s+["'][^"']*["'])?\s*\)''')


class CheckError(Exception):
    """The selected snapshot could not be read; never a pass."""


def matches_pattern(path: str, pattern: str) -> bool:
    return path.count("/") == pattern.count("/") and fnmatch.fnmatchcase(path, pattern)


def path_category(path: str) -> str:
    for category, patterns in LAYER_RULES:
        if any(matches_pattern(path, pattern) for pattern in patterns):
            return category
    return "no category"


def is_redirect_stub(text: str) -> bool:
    """Regel 4 row 2: <=5 nonempty lines, one title, a relative Markdown link."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    headings = [line for line in lines if re.match(r"#{1,6}(?:\s|$)", line)]
    if len(lines) > 5 or len(headings) != 1 or not re.match(r"#(?:\s|$)", headings[0]):
        return False
    # Code samples do not supply a redirect link or a document title.
    if any(re.match(r"`{3,}|~{3,}", line) for line in lines):
        return False
    for match in _LINK.finditer(text):
        target = match.group(1).removeprefix("<").removesuffix(">")
        if target and not target.startswith(("/", "#", "\\")) and not re.match(r"[\w+.-]+:", target):
            return True
    return False


def classify_document(path: str, text: str) -> str:
    if path in LAYER_RULES[0][1]:
        return "Ingang"
    if is_redirect_stub(text):
        return "Verwijsstub"
    return path_category(path)


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              encoding="utf-8", check=True).stdout
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        raise CheckError(f"git {' '.join(args)} failed: {exc}") from exc


def run_check(root: Path, sha: str = "HEAD") -> list[tuple[str, str]]:
    # Pin the commit once: a moving ref cannot mix paths and content snapshots.
    commit = _git(root, "rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}").strip()
    paths = _git(root, "ls-tree", "-r", "--name-only", "-z", commit).split("\0")
    docs = sorted({path for path in paths if _SCANSET.fullmatch(path) and not _FROZEN.match(path)})
    if not docs:
        raise CheckError(f"no documents in scanset at {commit}")
    entries = _git(root, "ls-tree", "-r", "-z", commit).split("\0")
    modes = {path: metadata.split(" ", 1)[0] for metadata, path in
             (entry.split("\t", 1) for entry in entries if entry)}
    for path in docs:
        if modes[path] not in ("100644", "100755"):
            raise CheckError(f"cannot read document {path}: not a regular Git blob")
    # Read Git blobs, never join document paths to the filesystem or follow symlinks.
    return [(path, classify_document(path, _git(root, "show", f"{commit}:{path}"))) for path in docs]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--report", action="store_true", help="print all categories and exit 0 on findings")
    parser.add_argument("--sha", default="HEAD", help="commit to scan (default: HEAD)")
    parser.add_argument("--root", type=Path, help="repository root (default: git toplevel)")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve() if args.root is not None else resolve_project_root()
        results = run_check(root, args.sha)
    except (CheckError, OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for path, category in results:
        print(f"{path}: {category}")
    return 1 if not args.report and any(category == "no category" for _, category in results) else 0


if __name__ == "__main__":
    sys.exit(main())
