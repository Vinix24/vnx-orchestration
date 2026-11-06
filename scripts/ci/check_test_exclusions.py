#!/usr/bin/env python3
"""CI check: every entry in scripts/ci/test_exclusions.txt carries a reason.

The full-suite CI sweep (profile-a in .github/workflows/vnx-ci.yml) excludes a
set of test files that are red on main; the explicit list lives in
scripts/ci/test_exclusions.txt. The file's own header promises "nothing is
excluded silently", but a path on its own does not say WHY it is excluded.

This gate enforces the invariant: every entry must carry an inline ``#
<reason>`` comment, the path must still exist (a de-quarantined file left
behind in the list is a stale reference), and no path may be listed twice.

Exit 0 when clean, exit 1 with violation details.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

EXCLUSIONS_REL = Path("scripts/ci/test_exclusions.txt")
WORKFLOW_REL = Path(".github/workflows/vnx-ci.yml")
TEST_DEF_RE = re.compile(r"^\s*(async )?def test_", re.MULTILINE)


def parse_entries(text: str) -> list[tuple[int, str, str]]:
    """Return ``(lineno, path, reason)`` for every non-comment entry line.

    Comment syntax matches the consumer, scripts/ci/profile_a_pytest_args.sh
    (called by Profile A in vnx-ci.yml), which strips ``[[:space:]]*#.*`` from
    each line before building the ``--ignore`` list.
    The first ``#`` (always preceded by optional whitespace) starts the reason;
    test paths never contain ``#``.
    """
    entries: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "#" in raw:
            path_part, _, reason = raw.partition("#")
            path = path_part.strip()
            reason = reason.strip()
        else:
            path = stripped
            reason = ""
        entries.append((lineno, path, reason))
    return entries


def check_file(exclusions_path: Path, repo_root: Path) -> list[str]:
    """Return violation strings for *exclusions_path* (empty when clean)."""
    try:
        text = exclusions_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{exclusions_path}: cannot read file: {exc}"]

    violations: list[str] = []
    seen: set[str] = set()
    for lineno, path, reason in parse_entries(text):
        label = f"{exclusions_path}:{lineno}"
        if not path:
            violations.append(f"{label}: empty entry path")
            continue
        if path in seen:
            violations.append(f"{label}: {path}: duplicate entry")
        seen.add(path)
        if not reason:
            violations.append(
                f"{label}: {path}: missing exclusion reason "
                "(append '# <reason>' to the entry)"
            )
        if not (repo_root / path).is_file():
            violations.append(
                f"{label}: {path}: path does not exist in the repo "
                "(stale exclusion — the test file is gone or was renamed)"
            )
    return violations


def profile_a_job(text: str) -> str:
    """The ``profile-a`` job block of a workflow, or "" when it has none."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"^  profile-a:\s*$", line)), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^  [\w-]+:\s*$", lines[i])), len(lines))
    return "\n".join(lines[start:end])


def other_deselections(root: Path) -> list[str]:
    try:
        job = profile_a_job((root / WORKFLOW_REL).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []
    job = "\n".join(line for line in job.splitlines() if not line.lstrip().startswith("#"))
    notes: list[str] = []
    if re.search(r'CI_PROFILE[^\n]*light', job):
        notes.append("also not swept when CI_PROFILE=light: the docs-only profile runs a fixed file list, not the tree")
    notes.extend(
        f"also not swept: {flag} {raw} deselects tests"
        for flag, raw in re.findall(r'(?<![\w-])(--deselect|--ignore|-m)[=\s]+("[^"]*"|\'[^\']*\'|\S+)', job)
    )
    return notes


def summarize(root: Path) -> list[str]:
    exclusions = root / EXCLUSIONS_REL
    try:
        if not exclusions.is_file():
            raise FileNotFoundError(EXCLUSIONS_REL)
        entries = parse_entries(exclusions.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return [f"test exclusions: unmeasured ({getattr(exc, 'strerror', None) or exc})"]
    functions = 0
    for _lineno, path, _reason in entries:
        try:
            functions += len(TEST_DEF_RE.findall((root / path).read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError) as exc:
            return [f"test exclusions: unmeasured (cannot read {path}: {getattr(exc, 'strerror', None) or exc})"]
    return [f"test exclusions: {len(entries)} files, {functions} test functions outside the Profile A sweep ({EXCLUSIONS_REL})"] + other_deselections(root)


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    summary = "--summary" in args
    root = Path(next((arg for arg in args if arg != "--summary"), ".")).resolve()
    if summary:
        print("\n".join(summarize(root)))
        return 0

    exclusions = root / EXCLUSIONS_REL
    if not exclusions.is_file():
        print(f"[test-exclusion-reason] skip — {EXCLUSIONS_REL} not present")
        return 0

    violations = check_file(exclusions, root)

    if not violations:
        print(
            "[test-exclusion-reason] PASS — every exclusion carries a reason, "
            "paths resolve, no duplicates."
        )
        return 0

    print(f"[test-exclusion-reason] FAIL — {len(violations)} finding(s):")
    for violation in violations:
        print(f"  {violation}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
