#!/usr/bin/env python3
"""CI check: documentation must not point at paths or links that no longer exist.

Rule 1 of the documentation-follows-the-code track (ADR-079).  Two selectable
rule names share one grammar:

* ``paths`` -- an inline backtick token that
  ``check_architecture_doc_paths.py`` accepts as a repo path must resolve.
* ``links`` -- a markdown link ``[text](target)`` must point at a tracked file
  or directory.

Ground truth is ``git ls-files``.  The scanset is ``docs/**/*.md``,
``README.md`` and ``CONTRIBUTING.md``, minus the frozen trees in
``FROZEN_DOC_DIRS``.  Fenced code blocks are never scanned.

Path normalisation, in this order: strip ``#anchor``, then ``::symbol``, then a
line suffix (``:12``, ``:12-20``, ``:12-20`` with an en-dash, comma lists),
then a ``:name`` identifier suffix.  A token whose last segment is only digits
and dots is a schema id (``pr_queue/1.1``), not a path.

Path resolution, in this order: ``./`` or ``../`` is relative to the document's
directory; otherwise an exact tracked file or directory; otherwise exactly one
tracked file or directory ending in ``/<token>``.  Zero or several matches is
unresolved.  Only an unresolved token is checked against ``PATH_EXCEPTIONS``,
so an exception can never mask a path that exists.

Findings inside generated content (a ``BEGIN``/``END`` marker pair containing
the word ``generated``, case-insensitive, or a file in ``GENERATED_FILES``) are
printed with the label ``generated``: they are fixed in the generator, not by
hand.

Usage:
  python3 scripts/ci/check_docs_stale_refs.py                 # exit 1 on a finding
  python3 scripts/ci/check_docs_stale_refs.py --report        # print, exit 0
  python3 scripts/ci/check_docs_stale_refs.py --rule paths    # repeatable
  python3 scripts/ci/check_docs_stale_refs.py --root DIR

Exit 2 means the check could not run (git failed, a tracked doc is unreadable);
it is never a pass, with or without ``--report``.
"""
from __future__ import annotations

import argparse
import importlib.util
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

_HERE = Path(__file__).resolve().parent


def _load_path_grammar():
    """Import the token grammar from the sibling checker instead of copying it."""
    spec = importlib.util.spec_from_file_location(
        "_check_architecture_doc_paths", _HERE / "check_architecture_doc_paths.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load check_architecture_doc_paths.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_grammar = _load_path_grammar()
_BACKTICK_PATH_RE = _grammar._BACKTICK_PATH_RE
_looks_like_repo_path = _grammar._looks_like_repo_path

RULES = ("paths", "links")

FROZEN_DOC_DIRS = (
    "docs/_archive/",
    "docs/archive/",
    "docs/investigations/",
    "docs/compliance/",
    "docs/governance/decisions/",
)
# Illustrative paths: the path rule skips these files, the link rule does not.
PATH_RULE_SKIP_DIRS = ("docs/examples/",)
GENERATED_FILES = frozenset({"docs/core/DAEMON_LIVENESS.md"})

# ADR-079 part 8: a change here needs a code round with a review gate, and the
# reason goes in a comment next to the entry.  A cleanup round never adds one.
PATH_EXCEPTIONS = (
    # Runtime-store and local directories: documented, never shipped.
    ".vnx-data", ".vnx", ".vnx-attest", "state", "logs", "events", "health",
    "review_gates", "receipt_deadletter", "governance", "shadow", "artifacts",
    "raw_output", "unified_reports", "dispatches", "daily-log",
    "build-log-bucket", "claudedocs",
)
# The install path in a consumer repo; this repo does not ship it.
PATH_EXCEPTION_PREFIXES = (".claude/vnx-system/",)

_DASH = "-–"
_ANCHOR_SPLIT = "#"
_LINE_SUFFIX_RE = re.compile(rf":\d+(?:[{_DASH}]\d+)?(?:,\d+(?:[{_DASH}]\d+)?)*$")
_NAME_SUFFIX_RE = re.compile(r":[A-Za-z_][A-Za-z0-9_]*$")
_SCHEMA_ID_RE = re.compile(r"^[0-9.]+$")
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]*(?:\s+\"[^\"]*\"|\s+'[^']*')?)\)")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_BEGIN_RE = re.compile(r"<!--\s*BEGIN\b[^>]*generated[^>]*-->", re.IGNORECASE)
_END_RE = re.compile(r"<!--\s*END\b[^>]*generated[^>]*-->", re.IGNORECASE)
_SKIPPED_SCHEMES = ("http:", "https:", "mailto:")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    text: str
    generated: bool = False

    def render(self) -> str:
        label = " generated" if self.generated else ""
        return f"{self.path}:{self.line} {self.rule}{label} {self.text}"


class CheckError(Exception):
    """The check could not run; never a pass."""


def normalise_token(token: str) -> str:
    """Apply the four normalisation steps, in order."""
    token = token.split(_ANCHOR_SPLIT, 1)[0]
    token = token.split("::", 1)[0]
    token = _LINE_SUFFIX_RE.sub("", token)
    token = _NAME_SUFFIX_RE.sub("", token)
    return token


def is_schema_id(token: str) -> bool:
    return bool(_SCHEMA_ID_RE.match(token.rsplit("/", 1)[-1]))


def is_path_exception(token: str) -> bool:
    if token.startswith(PATH_EXCEPTION_PREFIXES):
        return True
    return token.split("/", 1)[0] in PATH_EXCEPTIONS


class Tree:
    """Tracked files and directories, from ``git ls-files``."""

    def __init__(self, files: set[str]):
        self.files = files
        self.dirs: set[str] = set()
        for f in files:
            parent = posixpath.dirname(f)
            while parent and parent not in self.dirs:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)
        self.entries = files | self.dirs

    def exists(self, rel: str) -> bool:
        return rel in self.entries

    def resolve(self, token: str, doc_path: str) -> bool:
        """True when the token names one tracked entry (resolution steps 1-3)."""
        if token.startswith(("./", "../")):
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(doc_path), token))
            if joined.startswith("../") or joined == "..":
                return False
            return self.exists(joined)
        if self.exists(token):
            return True
        suffix = "/" + token
        return sum(1 for e in self.entries if e.endswith(suffix)) == 1


def in_scanset(rel: str) -> bool:
    if rel in ("README.md", "CONTRIBUTING.md"):
        return True
    if not (rel.startswith("docs/") and rel.endswith(".md")):
        return False
    return not rel.startswith(FROZEN_DOC_DIRS)


def generated_lines(rel: str, lines: list[str]) -> set[int]:
    """1-based line numbers inside a closed generated block (markers included)."""
    if rel in GENERATED_FILES:
        return set(range(1, len(lines) + 1))
    result: set[int] = set()
    start: int | None = None
    for no, line in enumerate(lines, start=1):
        if start is None:
            if _BEGIN_RE.search(line):
                start = no
        elif _END_RE.search(line):
            result.update(range(start, no + 1))
            start = None
    return result


def prose_lines(lines: list[str]):
    """Yield ``(line_no, line)`` outside fenced code blocks."""
    fence: str | None = None
    for no, line in enumerate(lines, start=1):
        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
            continue
        if fence is None:
            yield no, line


def check_document(rel: str, text: str, tree: Tree, rules: set[str]) -> list[Finding]:
    lines = text.splitlines()
    generated = generated_lines(rel, lines)
    findings: list[Finding] = []
    check_paths = "paths" in rules and not rel.startswith(PATH_RULE_SKIP_DIRS)
    for no, line in prose_lines(lines):
        gen = no in generated
        if check_paths:
            for m in _BACKTICK_PATH_RE.finditer(line):
                raw = m.group(1)
                if not _looks_like_repo_path(raw):
                    continue
                token = normalise_token(raw)
                if not token or is_schema_id(token) or tree.resolve(token, rel):
                    continue
                if is_path_exception(token):
                    continue
                findings.append(Finding(rel, no, "paths", f"`{raw}` does not resolve to a tracked path", gen))
        if "links" in rules:
            for m in _LINK_RE.finditer(line):
                finding = _check_link(rel, no, m.group(1), tree, gen)
                if finding:
                    findings.append(finding)
    return findings


def _check_link(rel: str, no: int, raw: str, tree: Tree, gen: bool) -> Finding | None:
    target = raw.strip()
    if target.startswith("<") and ">" in target:
        target = target[1:target.index(">")]
    else:
        target = target.split(None, 1)[0] if target else ""
    if target.lower().startswith(_SKIPPED_SCHEMES) or target.startswith("#"):
        return None
    path = target.split("#", 1)[0]
    if not path:
        return Finding(rel, no, "links", f"link `({raw})` has an empty target", gen)
    candidates = {path, unquote(path)}
    base = posixpath.dirname(rel)
    for cand in candidates:
        if cand.startswith("/"):
            continue
        joined = posixpath.normpath(posixpath.join(base, cand))
        if not (joined == ".." or joined.startswith("../")) and tree.exists(joined):
            return None
    return Finding(rel, no, "links", f"link target `{path}` is not a tracked file or directory", gen)


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CheckError(f"git {' '.join(args)} failed: {exc}") from exc
    return result.stdout


def run_check(root: Path, rules: set[str]) -> list[Finding]:
    files = {ln for ln in _git(root, "ls-files").splitlines() if ln}
    if not files:
        raise CheckError("git ls-files returned no tracked files")
    tree = Tree(files)
    findings: list[Finding] = []
    for rel in sorted(f for f in files if in_scanset(f)):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CheckError(f"cannot read tracked doc {rel}: {exc}") from exc
        findings.extend(check_document(rel, text, tree, rules))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--report", action="store_true", help="print findings and exit 0")
    parser.add_argument("--rule", action="append", choices=RULES, help="rule to run (repeatable)")
    parser.add_argument("--root", help="repository root (default: git toplevel)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    rules = set(args.rule) if args.rule else set(RULES)
    try:
        root = Path(args.root).resolve() if args.root else Path(
            _git(Path.cwd(), "rev-parse", "--show-toplevel").strip()
        ).resolve()
        findings = run_check(root, rules)
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for f in findings:
        print(f.render())
    counts = {r: sum(1 for f in findings if f.rule == r) for r in sorted(rules)}
    generated = sum(1 for f in findings if f.generated)
    summary = ", ".join(f"{r}={n}" for r, n in counts.items())
    print(f"{len(findings)} finding(s): {summary}; generated={generated}", file=sys.stderr)
    if args.report:
        return 0
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
