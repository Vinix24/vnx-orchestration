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

import sys
from pathlib import Path

EXCLUSIONS_REL = Path("scripts/ci/test_exclusions.txt")


def parse_entries(text: str) -> list[tuple[int, str, str]]:
    """Return ``(lineno, path, reason)`` for every non-comment entry line.

    Comment syntax matches the workflow consumer in vnx-ci.yml, which strips
    ``[[:space:]]*#.*`` from each line before building the ``--ignore`` list.
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


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0]).resolve() if args else Path.cwd()

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
