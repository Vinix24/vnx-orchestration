#!/usr/bin/env python3
"""CI check: CHANGELOG entries arrive as changelog.d/ fragments; a release PR matches the assembler.

Usage: check_changelog_fragments.py --base <rev> [--head <rev>] [--pr <number>]

Runs as its own pull-request-only step in the ``Lint Patterns`` job, with
``--base`` the merge base of the PR.  Both trees are read from git objects
(``git ls-tree``, ``git cat-file``), never from a working tree.  It fails when:

1. the PR changes the ``## [Unreleased]`` section of ``CHANGELOG.md`` at all
   (unless rule 3 applies): it adds a line (unless rule 4 applies), removes or
   changes one, reorders them, or removes the heading.  Blank lines never
   count;
2. a path the PR adds or changes under ``changelog.d/`` is an invalid
   fragment, a symlink, or no fragment name at all; or the PR removes
   ``changelog.d/README.md``; or ``CHANGELOG.md`` at the head has a
   ``## [label]`` heading more than once;
3. a release PR (one that adds an occurrence of a ``## [X.Y.Z]`` heading,
   counted per occurrence so a second ``## [1.0.0]`` is one, or deletes a
   fragment) does not match the assembler: exactly one heading, its version
   equal to the head's ``VERSION``, ``CHANGELOG.md`` equal to
   ``changelog_fragments.assemble`` run on the merge base (only prose may
   precede the first bullet under the new heading), and no fragment left at
   the head;
4. (grandfathering) a PR listed in ``scripts/ci/changelog_legacy_prs.txt`` at
   the merge base may add ``## [Unreleased]`` lines only while they hash to
   the listed value; it removes, changes or reorders none.  A missing list is
   an empty list.

The branch name is never read.  Without ``changelog.d/`` at both the merge base
and the head (a consumer project) the check passes and does nothing.

Exit 0 pass, 1 violations, 2 usage or git error.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import changelog_fragments as cf
from project_root import resolve_project_root

LEGACY_LIST_PATH = "scripts/ci/changelog_legacy_prs.txt"
VERSION_PATH = "VERSION"
FRAGMENT_HINT = (
    "add the entry as changelog.d/<dispatch-id>.md instead "
    "(changelog.d/<branch-slug>.md for a PR without a dispatch); "
    "the release PR assembles it. Rule: changelog.d/README.md"
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_NUMBERED_ITEM_RE = re.compile(r"\d+[.)]\s")

Entries = Dict[str, Tuple[str, str, str]]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.exit(2, f"{self.prog}: error: {message}\n")


# ---------------------------------------------------------------------------
# Rule 1 and rule 4: added Unreleased lines and the legacy list (pure)
# ---------------------------------------------------------------------------


def _unreleased_entries(text: Optional[str]) -> List[str]:
    """The non-blank lines under ``## [Unreleased]``, in order."""
    return [line for line in cf.unreleased_lines(text or "") if line.strip()]


def added_unreleased_lines(base_text: Optional[str], head_text: Optional[str]) -> List[str]:
    """The non-blank lines under ``## [Unreleased]`` at the head that the base
    did not have, counted as a multiset and sorted.  Blank lines never count."""
    base = Counter(_unreleased_entries(base_text))
    head = Counter(_unreleased_entries(head_text))
    return sorted((head - base).elements())


def removed_unreleased_lines(base_text: Optional[str], head_text: Optional[str]) -> List[str]:
    """The non-blank lines under ``## [Unreleased]`` at the base that the head
    no longer has, counted as a multiset and sorted: a changed line is one."""
    base = Counter(_unreleased_entries(base_text))
    head = Counter(_unreleased_entries(head_text))
    return sorted((base - head).elements())


def keeps_unreleased_order(base_text: Optional[str], head_text: Optional[str]) -> bool:
    """True when the base's non-blank ``## [Unreleased]`` lines occur at the
    head in the same order (other lines may sit between them)."""
    head = iter(_unreleased_entries(head_text))
    return all(line in head for line in _unreleased_entries(base_text))


def legacy_digest(lines: Iterable[str]) -> str:
    """sha256 over the lines, sorted and newline-joined: the legacy-list hash."""
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def legacy_entry(pr: int, base_text: Optional[str], head_text: Optional[str]) -> Optional[str]:
    """The ``<pr> <sha256>`` legacy-list line for a PR, or None when it adds no line."""
    lines = added_unreleased_lines(base_text, head_text)
    if not lines:
        return None
    return f"{pr} {legacy_digest(lines)}"


def parse_legacy_list(text: str) -> Tuple[Dict[int, str], List[str]]:
    """``({pr: sha256}, errors)``.  ``#`` starts a comment; a malformed or
    duplicate line is an error, never silently skipped."""
    entries: Dict[int, str] = {}
    errors: List[str] = []
    for line_no, raw in enumerate(text.split("\n"), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or not _SHA256_RE.fullmatch(parts[1]):
            errors.append(f"{LEGACY_LIST_PATH}:{line_no}: expected '<pr-number> <sha256>': {raw!r}")
            continue
        pr = int(parts[0])
        if pr in entries:
            errors.append(f"{LEGACY_LIST_PATH}:{line_no}: PR #{pr} is listed twice")
            continue
        entries[pr] = parts[1]
    return entries, errors


# ---------------------------------------------------------------------------
# Rule 3: compare a release PR with the assembler (pure)
# ---------------------------------------------------------------------------


def is_prose(line: str) -> bool:
    """True for a blank line or an unindented line that is no bullet, no
    numbered item and no heading: what a release intro paragraph may hold."""
    if not line.strip():
        return True
    if line[0] in " \t-*+#":
        return False
    return _NUMBERED_ITEM_RE.match(line) is None


def _strip_intro(lines: List[str], version: str) -> Tuple[List[str], List[str]]:
    """Split off the lines between the ``## [version]`` heading and its first
    bullet (or the next ``## `` heading): ``(rest, intro)``."""
    for index, line in enumerate(lines):
        parsed = cf.parse_heading(line)
        if parsed and parsed[0] == version:
            end = index + 1
            while end < len(lines) and not lines[end].startswith(("- ", "## ")):
                end += 1
            return lines[: index + 1] + lines[end:], lines[index + 1 : end]
    return list(lines), []


def compare_release(expected: str, actual: str, version: str) -> Optional[str]:
    """Why *actual* differs from the assembler's *expected*, or None.

    The only allowed difference is prose between the new heading and its
    first bullet: the hand-written release intro.  On the *expected* side
    that region holds blank lines only, because ``assemble`` refuses an
    ``## [Unreleased]`` body with a line that is no bullet, continuation or
    blank; so stripping it there drops nothing that should be compared.
    """
    expected_rest, _ = _strip_intro(expected.split("\n"), version)
    actual_rest, intro = _strip_intro(actual.split("\n"), version)
    for line in intro:
        if not is_prose(line):
            return (f"under '## [{version}]' only prose may precede the first bullet; "
                    f"found {line!r}")
    if expected_rest == actual_rest:
        return None
    diff = list(difflib.unified_diff(
        expected_rest, actual_rest, "assembled from the merge base", "CHANGELOG.md at the head",
        lineterm="", n=1,
    ))
    shown = "\n    ".join(diff[:24])
    more = "\n    ..." if len(diff) > 24 else ""
    return ("CHANGELOG.md does not match what assemble_changelog.py builds from the merge base "
            f"(edit a fragment in an earlier PR instead):\n    {shown}{more}")


def heading_counts(text: Optional[str]) -> Counter:
    """How often each ``## [label]`` heading occurs, Unreleased included."""
    return Counter(
        parsed[0] for parsed in (cf.parse_heading(line) for line in (text or "").split("\n")) if parsed
    )


def added_versions(base_text: Optional[str], head_text: Optional[str]) -> List[str]:
    """The version headings the head has more often than the base, one entry
    per added occurrence, sorted: a second ``## [1.0.0]`` counts."""
    added = heading_counts(head_text) - heading_counts(base_text)
    return sorted(label for label in added.elements() if label != "Unreleased")


def duplicate_headings(text: Optional[str]) -> List[str]:
    """Rule 2: one violation per ``## [label]`` heading that occurs more than once."""
    return [
        f"CHANGELOG.md has the heading '## [{label}]' {count} times; every '## [...]' "
        "heading appears once (a second one hides a release from this check)"
        for label, count in sorted(heading_counts(text).items())
        if count > 1
    ]


def heading_date(text: str, version: str) -> Optional[str]:
    """The date of the ``## [version] - YYYY-MM-DD`` heading, or None when the
    heading does not read exactly that way."""
    for line in text.split("\n"):
        parsed = cf.parse_heading(line)
        if parsed and parsed[0] == version:
            rest = parsed[1]
            if rest.startswith(" - ") and cf.is_valid_date(rest[3:]):
                return rest[3:]
            return None
    return None


# ---------------------------------------------------------------------------
# The check (reads git objects)
# ---------------------------------------------------------------------------


def _text(data: Optional[bytes]) -> Optional[str]:
    return None if data is None else data.decode("utf-8", "replace")


def _check_paths(repo: Path, head: str, base_entries: Entries, head_entries: Entries) -> List[str]:
    """Rule 2: every path the PR adds or changes under changelog.d/, and README."""
    violations = []
    for path in sorted(head_entries):
        entry = head_entries[path]
        if base_entries.get(path) == entry:
            continue
        if path == cf.README_PATH:
            if not cf.is_regular_blob(entry):
                violations.append(f"{path}: must be a regular file, not a symlink")
        elif not cf.is_fragment_path(path):
            violations.append(
                f"{path}: not a fragment name, so it would never be assembled; a fragment is "
                "changelog.d/<name>.md with <name> matching [A-Za-z0-9][A-Za-z0-9._-]*"
            )
        elif not cf.is_regular_blob(entry):
            violations.append(f"{path}: a fragment must be a regular file, not a symlink")
        else:
            reason = cf.validate_fragment(path.split("/", 1)[1], cf.read_blob(repo, head, path) or b"")
            if reason:
                violations.append(f"invalid fragment {reason}")
    if cf.README_PATH in base_entries and cf.README_PATH not in head_entries:
        violations.append(f"{cf.README_PATH} was removed; it holds the rule and stays")
    return violations


def _check_unreleased_kept(base_cl: Optional[str], head_cl: Optional[str]) -> List[str]:
    """Rule 1 outside the added lines: nothing under Unreleased is removed,
    changed or reordered, and the heading stays."""
    if cf.has_unreleased(base_cl or "") and not cf.has_unreleased(head_cl or ""):
        return [f"CHANGELOG.md lost its '{cf.UNRELEASED_HEADING}' heading; the release assembler needs it"]
    removed = removed_unreleased_lines(base_cl, head_cl)
    if removed:
        shown = "\n    ".join(removed[:5]) + ("\n    ..." if len(removed) > 5 else "")
        return [f"removes or changes {len(removed)} line(s) under '{cf.UNRELEASED_HEADING}' in "
                f"CHANGELOG.md:\n    {shown}\n  outside a release PR that section does not change; "
                f"the release PR folds it. Rule: {cf.README_PATH}"]
    if not keeps_unreleased_order(base_cl, head_cl):
        return [f"reorders the lines under '{cf.UNRELEASED_HEADING}' in CHANGELOG.md; outside a "
                f"release PR that section does not change. Rule: {cf.README_PATH}"]
    return []


def _check_unreleased(repo: Path, base: str, base_cl: Optional[str], head_cl: Optional[str],
                      pr: Optional[int]) -> Tuple[List[str], List[str]]:
    """Rule 1 with rule 4: ``(violations, notes)``."""
    violations = _check_unreleased_kept(base_cl, head_cl)
    added = added_unreleased_lines(base_cl, head_cl)
    if not added:
        return violations, []
    legacy, errors = parse_legacy_list(_text(cf.read_blob(repo, base, LEGACY_LIST_PATH)) or "")
    violations.extend(errors)
    digest = legacy_digest(added)
    if pr is not None and pr in legacy:
        if legacy[pr] == digest:
            return violations, [f"PR #{pr} is grandfathered: its {len(added)} Unreleased line(s) match {LEGACY_LIST_PATH}"]
        violations.append(
            f"PR #{pr} is grandfathered for the CHANGELOG.md lines it had when the fragment rule "
            f"landed (sha256 {legacy[pr]}), but its lines under '{cf.UNRELEASED_HEADING}' now hash "
            f"to {digest}: a new or changed bullet goes to a fragment; {FRAGMENT_HINT}"
        )
        return violations, []
    shown = "\n    ".join(added[:5]) + ("\n    ..." if len(added) > 5 else "")
    violations.append(
        f"adds {len(added)} line(s) under '{cf.UNRELEASED_HEADING}' in CHANGELOG.md:\n    {shown}\n"
        f"  {FRAGMENT_HINT}"
    )
    return violations, []


def _base_fragments(repo: Path, base: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """The merge base's fragments in introduction order, and refusals."""
    names = cf.fragment_names_at(repo, base)
    blobs = {name: cf.read_blob(repo, base, cf.fragment_path(name)) or b"" for name in names}
    reasons = [cf.validate_fragment(name, data) for name, data in blobs.items()]
    reasons = [f"invalid fragment at the merge base {reason}" for reason in reasons if reason]
    if reasons:
        return [], reasons
    try:
        ordered = cf.introduction_order(repo, base, names)
    except cf.ChangelogError as exc:
        return [], [str(exc)]
    return [(name, blobs[name].decode("utf-8")) for name in ordered], []


def _check_inventory(base_entries: Entries, head_entries: Entries) -> List[str]:
    """Rule 3 inventory: a release PR consumes every fragment and adds none."""
    violations = []
    for path in sorted(p for p in head_entries if cf.is_fragment_path(p)):
        if path in base_entries:
            violations.append(f"{path}: a consumed fragment is left in place; the release deletes it")
        else:
            violations.append(f"{path}: a release PR adds no new fragment; it lands in its own PR")
    return violations


def _check_release(repo: Path, base: str, head: str, base_cl: Optional[str], head_cl: Optional[str],
                   base_entries: Entries, head_entries: Entries) -> List[str]:
    """Rule 3: the release PR equals the assembler's output from the merge base."""
    added = added_versions(base_cl, head_cl)
    if len(added) != 1:
        return [f"a release PR adds exactly one version heading to CHANGELOG.md; this one adds "
                f"{len(added)}{': ' + ', '.join(added) if added else ''}"]
    version = added[0]
    head_version = (_text(cf.read_blob(repo, head, VERSION_PATH)) or "").strip()
    if version != head_version:
        return [f"the new heading '## [{version}]' does not match {VERSION_PATH} at the head ({head_version or 'missing'})"]
    violations = _check_inventory(base_entries, head_entries)
    date = heading_date(head_cl or "", version)
    if date is None:
        violations.append(f"the new heading must read '## [{version}] - YYYY-MM-DD'")
        return violations
    fragments, refusals = _base_fragments(repo, base)
    if refusals:
        return violations + refusals
    try:
        expected, _names = cf.assemble(base_cl or "", fragments, version, date)
    except cf.ChangelogError as exc:
        return violations + [f"the merge base cannot be assembled: {exc}"]
    mismatch = compare_release(expected, head_cl or "", version)
    if mismatch:
        violations.append(mismatch)
    return violations


def check(repo: Path, base: str, head: str, pr: Optional[int]) -> Tuple[List[str], List[str]]:
    """Every rule for the PR from *base* to *head*: ``(violations, notes)``."""
    base_entries = cf.tree_entries(repo, base, cf.FRAGMENT_DIR)
    head_entries = cf.tree_entries(repo, head, cf.FRAGMENT_DIR)
    if not base_entries and not head_entries:
        return [], [f"no {cf.FRAGMENT_DIR}/ at the merge base or the head: nothing to check"]
    violations = _check_paths(repo, head, base_entries, head_entries)
    base_cl = _text(cf.read_blob(repo, base, cf.CHANGELOG_PATH))
    head_cl = _text(cf.read_blob(repo, head, cf.CHANGELOG_PATH))
    violations += duplicate_headings(head_cl)
    deleted = [p for p in base_entries if cf.is_fragment_path(p) and p not in head_entries]
    if deleted or added_versions(base_cl, head_cl):
        violations += _check_release(repo, base, head, base_cl, head_cl, base_entries, head_entries)
        return violations, ["release PR: compared with the assembler on the merge base"]
    unreleased_violations, notes = _check_unreleased(repo, base, base_cl, head_cl, pr)
    return violations + unreleased_violations, notes


def _resolve(repo: Path, rev: str) -> str:
    return cf.run_git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def main(argv: List[str] | None = None) -> int:
    parser = _Parser(description="CHANGELOG fragments check")
    parser.add_argument("--base", required=True, help="the merge base of the PR")
    parser.add_argument("--head", default="HEAD", help="head ref (default HEAD)")
    parser.add_argument("--pr", type=int, default=None, help="PR number, for the legacy list")
    args = parser.parse_args(argv)
    try:
        repo = resolve_project_root()
        base, head = _resolve(repo, args.base), _resolve(repo, args.head)
        print(f"changelog fragments: base {base} head {head} pr {'#' + str(args.pr) if args.pr else 'none'}")
        violations, notes = check(repo, base, head, args.pr)
    except (RuntimeError, cf.ChangelogError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(note)
    if not violations:
        print("changelog fragments: OK")
        return 0
    for violation in violations:
        print(f"[changelog] {violation}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
