#!/usr/bin/env python3
"""ADR-003 enforcement: scan for forbidden Anthropic SDK imports.

Scans scripts/, dashboard/, and tests/ for Python files that import the
Anthropic Python SDK or Claude Agent SDK. Exits 1 if any violations found.

Magic-comment opt-in: place the following comment on the line IMMEDIATELY
preceding the import to permit it (rare structural exceptions only):
    # vnx:allow-anthropic-sdk-import-with-justification

See: docs/governance/decisions/ADR-003-oauth-only-claude-routing.md
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCAN_DIRS = ("scripts", "dashboard", "tests")

# Forbidden imports per ADR-003 (no Anthropic SDK / Claude Agent SDK in VNX runtime).
# Pattern catches:
#   import anthropic                 -> match
#   import anthropic, os             -> match (anthropic first in multi-import)
#   import anthropic.types           -> match (\b boundary before .)
#   from anthropic import X          -> match
#   from anthropic.types import Y    -> match
#   import anthropic_utils           -> NOT match (\b boundary requires word break)
# Limitation: `import os, anthropic` (anthropic second in multi-import) is NOT caught.
# That style is uncommon (PEP 8 discourages multi-imports). If it lands, raise OI to
# upgrade this scanner to ast.parse() for full coverage.
FORBIDDEN_PATTERN = re.compile(
    r"^(?:import\s+(?:anthropic|claude_agent_sdk)\b"
    r"|from\s+(?:anthropic|claude_agent_sdk)[\s.])"
)

ALLOWLIST_COMMENT = "# vnx:allow-anthropic-sdk-import-with-justification"

ADR_URL = "docs/governance/decisions/ADR-003-oauth-only-claude-routing.md"

# ---------------------------------------------------------------------------
# Core gate logic
# ---------------------------------------------------------------------------


def check_file(path: Path) -> list[tuple[int, str]]:
    """Return list of (lineno, line) violations for a single file."""
    violations: list[tuple[int, str]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return violations

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not FORBIDDEN_PATTERN.match(stripped):
            continue
        preceding = lines[i - 1].strip() if i > 0 else ""
        if preceding == ALLOWLIST_COMMENT:
            continue
        violations.append((i + 1, stripped))

    return violations


def scan_repo(root: Path) -> list[tuple[Path, int, str]]:
    """Return all violations as (path, lineno, line) for the repo."""
    all_violations: list[tuple[Path, int, str]] = []
    for dir_name in SCAN_DIRS:
        scan_dir = root / dir_name
        if not scan_dir.is_dir():
            continue
        for py_file in sorted(scan_dir.rglob("*.py")):
            for lineno, line in check_file(py_file):
                all_violations.append((py_file, lineno, line))
    return all_violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _annotation(path: Path, lineno: int, line: str, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return (
        f"::error file={rel},line={lineno}::ADR-003 violation: forbidden import '{line}'.\n"
        f"VNX is locked to OAuth-only Claude routing via subprocess.\n"
        f"See: {ADR_URL}\n"
        f"To opt in (rare): add '{ALLOWLIST_COMMENT}' on the preceding line."
    )


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0]).resolve() if args else Path.cwd()

    violations = scan_repo(root)

    if not violations:
        print("[ADR-003] PASS — no forbidden SDK imports found.")
        return 0

    print(f"[ADR-003] FAIL — {len(violations)} violation(s) found:\n")
    for path, lineno, line in violations:
        print(_annotation(path, lineno, line, root))
        print()

    return 1


if __name__ == "__main__":
    sys.exit(main())
