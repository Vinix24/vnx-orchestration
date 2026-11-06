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
ARGS_SCRIPT_REL = Path("scripts/ci/profile_a_pytest_args.sh")
SERIAL_TESTS_REL = Path("scripts/ci/serial_tests.txt")
TEST_DEF_RE = re.compile(r"^\s*(async )?def test_", re.MULTILINE)
PYTEST_CMD_RE = re.compile(r"(?<![\w./-])pytest(?![\w.-])")
COMMAND_END_RE = re.compile(r"&&|\|\||[;|]")


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


def pytest_commands(job: str) -> list[str]:
    """The argument tail of every pytest command in *job*, continuations joined."""
    commands: list[str] = []
    lines, index = job.splitlines(), 0
    while index < len(lines):
        logical = lines[index]
        while logical.rstrip().endswith("\\") and index + 1 < len(lines):
            index += 1
            logical = logical.rstrip()[:-1] + " " + lines[index].strip()
        commands.extend(
            COMMAND_END_RE.split(logical[match.end():], maxsplit=1)[0]
            for match in PYTEST_CMD_RE.finditer(logical)
        )
        index += 1
    return commands


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
        for command in pytest_commands(job)
        for flag, raw in re.findall(r'(?<![\w-])(--deselect|--ignore|-m)[=\s]+("[^"]*"|\'[^\']*\'|\S+)', command)
    )
    return notes


def args_deselections(root: Path) -> tuple[list[str], str | None]:
    """The -k and extra --ignore/--deselect the Profile A args script builds."""
    try:
        script = (root / ARGS_SCRIPT_REL).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"cannot read {ARGS_SCRIPT_REL}: {getattr(exc, 'strerror', None) or exc}"
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    match = re.search(r'^\s*QUARANTINE_K=(?:"([^"]*)"|\'([^\']*)\'|(\S+))', code, re.MULTILINE)
    if match is None:
        match = re.search(r'(?<![\w-])-k\s+("[^"]*"|\'[^\']*\')', code)
    if match is None:
        return [], f"no -k quarantine expression in {ARGS_SCRIPT_REL}"
    expr = next(group for group in match.groups() if group is not None).strip("\"'")
    if "$" in expr:
        return [], f"unresolved -k quarantine expression in {ARGS_SCRIPT_REL}"
    notes = [f"also not swept: -k {expr} deselects tests"]
    for flag, raw in re.findall(r'(?<![\w-])(--deselect|--ignore)(?:=|\s+)("[^"]*"|\'[^\']*\'|\S+)', code):
        value = raw.strip("\"'")
        note = f"also not swept: {flag} {value} deselects tests"
        if "$" not in value and note not in notes:
            notes.append(note)
    if (serial := root / SERIAL_TESTS_REL).exists():
        try:
            lines = serial.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            return [], f"cannot read {SERIAL_TESTS_REL}: {getattr(exc, 'strerror', None) or exc}"
        for line in lines:
            entry = re.sub(r"(^|\s+)#.*$", "", line.lstrip()).strip()
            flag = "--deselect" if "::" in entry else "--ignore"
            note = f"also not swept: {flag} {entry} deselects tests"
            if entry and note not in notes:
                notes.append(note)
    return notes, None


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
    notes, error = args_deselections(root)
    if error:
        return [f"test exclusions: unmeasured ({error})"]
    return [f"test exclusions: {len(entries)} files, {functions} test functions outside the Profile A sweep ({EXCLUSIONS_REL})"] + notes + other_deselections(root)


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
