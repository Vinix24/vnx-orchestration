#!/usr/bin/env python3
"""CI lint gate for two recurring VNX anti-patterns.

Pattern A — silent exception:
  `except Exception:` or bare `except:` immediately followed by `pass`
  with no log or re-raise. Whitelist: `# noqa: vnx-silent-except`.

Pattern B — non-atomic state write:
  `open(path, "w"|"wb")` where path matches state file patterns,
  without os.replace / tempfile / state_writer in the following 10 lines.
  Whitelist: `# noqa: vnx-atomic-write`.

Exit codes: 0 = clean, 1 = findings found.

Usage:
  python3 scripts/ci_lint_patterns.py
  python3 scripts/ci_lint_patterns.py --diff <base-ref>
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_SCAN_DIRS = ("scripts", "dashboard")

_STATE_FILE_RE = re.compile(
    r"state/[^\"']*\.json"
    r"|receipts[^\"']*\.ndjson"
    r"|state\.json"
)

_OPEN_WRITE_RE = re.compile(
    r"""open\s*\([^)]*['"](w|wb)['"]\s*\)"""
    r"""|open\s*\([^)]+,\s*['"](w|wb)['"]\s*\)"""
)

_EXCEPT_RE = re.compile(r"^\s*except(\s+Exception)?\s*:")


@dataclass
class Finding:
    pattern: str
    path: str
    line: int
    text: str

    def __str__(self) -> str:
        return f"{self.pattern} {self.path}:{self.line}: {self.text.strip()}"


def _next_meaningful_line(lines: list[str], after: int) -> str | None:
    """Return the first non-blank, non-comment line after index `after`."""
    for i in range(after + 1, min(after + 10, len(lines))):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return None


def check_pattern_a(path: str, lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for i, line in enumerate(lines):
        if not _EXCEPT_RE.match(line):
            continue
        if "vnx-silent-except" in line:
            continue
        next_line = _next_meaningful_line(lines, i)
        if next_line == "pass":
            findings.append(Finding("A", path, i + 1, line))
    return findings


def check_pattern_b(path: str, lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for i, line in enumerate(lines):
        if "vnx-atomic-write" in line:
            continue
        if not _OPEN_WRITE_RE.search(line):
            continue
        if not _STATE_FILE_RE.search(line):
            continue
        window = "".join(lines[i + 1 : i + 11])
        if "os.replace" in window or "tempfile" in window or "state_writer." in window:
            continue
        findings.append(Finding("B", path, i + 1, line))
    return findings


def scan_file(path: str) -> list[Finding]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    findings = check_pattern_a(path, lines)
    findings += check_pattern_b(path, lines)
    return findings


def collect_files_from_dirs(root: Path) -> list[str]:
    result: list[str] = []
    for scan_dir in _SCAN_DIRS:
        target = root / scan_dir
        if not target.is_dir():
            continue
        for dirpath, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv", "node_modules")]
            for f in files:
                if f.endswith(".py"):
                    result.append(os.path.join(dirpath, f))
    return result


def collect_files_from_diff(base_ref: str, root: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", base_ref],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        changed = proc.stdout.splitlines()
    except (subprocess.SubprocessError, OSError):
        changed = []

    result: list[str] = []
    allowed_prefixes = tuple(f"{d}/" for d in _SCAN_DIRS)
    for rel in changed:
        if not rel.endswith(".py"):
            continue
        if not rel.startswith(allowed_prefixes):
            continue
        abs_path = root / rel
        if abs_path.is_file():
            result.append(str(abs_path))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VNX CI lint pattern gate")
    parser.add_argument(
        "--diff",
        metavar="BASE_REF",
        help="Scan only files changed vs BASE_REF (git diff --name-only)",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent

    if args.diff:
        files = collect_files_from_diff(args.diff, root)
    else:
        files = collect_files_from_dirs(root)

    all_findings: list[Finding] = []
    for f in sorted(files):
        all_findings.extend(scan_file(f))

    if not all_findings:
        return 0

    print(f"VNX lint gate: {len(all_findings)} finding(s)\n")
    for finding in all_findings:
        print(f"  [{finding.pattern}] {finding.path}:{finding.line}: {finding.text.strip()}")
    print()
    print("Pattern codes:")
    print("  A = silent exception (except + pass, no log/re-raise)")
    print("  B = non-atomic state write (open(path,'w') without os.replace)")
    print()
    print("To suppress a specific line add the appropriate noqa comment:")
    print("  # noqa: vnx-silent-except")
    print("  # noqa: vnx-atomic-write")
    return 1


if __name__ == "__main__":
    sys.exit(main())
