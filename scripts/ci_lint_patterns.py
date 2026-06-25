#!/usr/bin/env python3
"""CI lint gate for two recurring VNX anti-patterns.

Pattern A — silent exception:
  `except Exception:` or bare `except:` immediately followed by `pass`
  with no log or re-raise. Whitelist: a PLAIN marker comment on the `except`
  line containing `vnx-silent-except` (e.g. `# vnx-silent-except: <reason>`).
  Do NOT use the `# noqa:` prefix — Ruff rejects `# noqa: vnx-silent-except`
  as an invalid directive; this gate only substring-matches the token.

Pattern B — non-atomic state write:
  `open(path, "w"|"wb")` where path matches state file patterns,
  without os.replace / tempfile / state_writer in the following 10 lines.
  Whitelist: a plain marker comment containing `vnx-atomic-write`
  (e.g. `# vnx-atomic-write: <reason>`) — not the Ruff-rejected `# noqa:` form.

Exit codes:
  0 = clean (no findings)
  1 = findings present

This is a pure content scanner. It does NOT invoke git. The caller is
responsible for computing the diff and feeding either the added-line text
(`--scan-stdin`) or a list of file paths (`--files-from-stdin`).

Usage:
  # Local dev — full scan of scripts/ and dashboard/
  python3 scripts/ci_lint_patterns.py

  # CI — scan only added lines of a diff (recommended)
  git diff --unified=0 origin/main...HEAD -- 'scripts/*.py' \
    | grep -E '^\\+[^+]' | sed 's/^+//' \
    | python3 scripts/ci_lint_patterns.py --scan-stdin

  # CI — scan full content of changed files (pre-existing violations block)
  git diff --name-only origin/main...HEAD \
    | grep -E '^(scripts|dashboard)/.*\\.py$' \
    | python3 scripts/ci_lint_patterns.py --files-from-stdin
"""

from __future__ import annotations

import argparse
import os
import re
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


def scan_text_blob(text: str, label: str = "<added-lines>") -> list[Finding]:
    """Scan a raw text blob (e.g. concatenated added-lines from a diff).

    The blob has no real file paths or line numbers. Findings carry `label`
    as the path and the line index within the blob — informative for triage,
    but not a real source location.
    """
    lines = text.splitlines(keepends=True)
    findings = check_pattern_a(label, lines)
    findings += check_pattern_b(label, lines)
    return findings


def collect_files_from_dirs(root: Path) -> list[str]:
    result: list[str] = []
    for scan_dir in _SCAN_DIRS:
        target = root / scan_dir
        if not target.is_dir():
            continue
        for dirpath, dirs, files in os.walk(target):
            parts = Path(dirpath).parts
            # Skip vendored/cache dirs, and the benchmark task SEEDS under
            # field-tests/ — those are PLANTED fixtures (deliberate anti-patterns
            # reviewer-models must find); a suppression marker there would tip the
            # answer. Path-component match (not substring) so `field-tests-old/seed`
            # is NOT skipped. Real harness `runners/` + `verify.py` stay linted.
            dirs[:] = [
                d for d in dirs
                if d not in ("__pycache__", ".venv", "node_modules")
                and not (d == "seed" and "field-tests" in parts)
            ]
            for f in files:
                if f.endswith(".py"):
                    result.append(os.path.join(dirpath, f))
    return result


def _read_paths_from_stdin() -> list[str]:
    """Read one file path per line from stdin, ignoring blanks."""
    result: list[str] = []
    for raw in sys.stdin:
        path = raw.strip()
        if not path:
            continue
        result.append(path)
    return result


def _print_findings(findings: list[Finding]) -> None:
    print(f"VNX lint gate: {len(findings)} finding(s)\n")
    for finding in findings:
        print(f"  [{finding.pattern}] {finding.path}:{finding.line}: {finding.text.strip()}")
    print()
    print("Pattern codes:")
    print("  A = silent exception (except + pass, no log/re-raise)")
    print("  B = non-atomic state write (open(path,'w') without os.replace)")
    print()
    print("To suppress a line, add a PLAIN marker comment on it (NOT a # noqa: directive — Ruff rejects that):")
    print("  # vnx-silent-except: <reason>")
    print("  # vnx-atomic-write: <reason>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="VNX CI lint pattern gate. Exit codes: 0=clean, 1=findings.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--scan-stdin",
        action="store_true",
        help=(
            "Read concatenated added-line text from stdin and scan it for patterns. "
            "Caller is responsible for extracting added lines from the diff "
            "(e.g. `git diff --unified=0 ... | grep '^+[^+]' | sed 's/^+//'`). "
            "Findings have no real line numbers — only blob-relative indices."
        ),
    )
    mode.add_argument(
        "--files-from-stdin",
        action="store_true",
        help=(
            "Read one file path per line from stdin and scan each file in full. "
            "Pre-existing violations in unchanged code WILL block — use --scan-stdin "
            "in CI to limit detection to added lines only."
        ),
    )
    args = parser.parse_args(argv)

    if args.scan_stdin:
        blob = sys.stdin.read()
        all_findings = scan_text_blob(blob)
    elif args.files_from_stdin:
        files = _read_paths_from_stdin()
        all_findings = []
        for f in sorted(files):
            all_findings.extend(scan_file(f))
    else:
        root = Path(__file__).resolve().parent.parent
        files = collect_files_from_dirs(root)
        all_findings = []
        for f in sorted(files):
            all_findings.extend(scan_file(f))

    if not all_findings:
        return 0

    _print_findings(all_findings)
    return 1


if __name__ == "__main__":
    sys.exit(main())
