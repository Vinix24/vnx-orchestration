#!/usr/bin/env python3
"""Find the newest mtime (integer seconds) across two report directories.

Usage:
    python3 rp_find_newest_report_mtime.py <unified_dir> <headless_dir> <fallback_ts>

Outputs a single integer to stdout: the highest mtime found among all *.md files
in <unified_dir> and <headless_dir>.  If no files are found (or both dirs are
absent/empty), outputs <fallback_ts> instead.

Stat failures are reported to stderr and skipped; the scan continues so that one
unreadable entry does not discard all other mtimes.

This module was extracted from the inline Python heredoc inside
_rp_apply_bootstrap_protection() in scripts/receipt_processor.sh (OI-1525/1524).
Behaviour is intentionally identical to the original.
"""
from __future__ import annotations

import sys
from pathlib import Path


def find_newest_report_mtime(unified: str, headless: str, fallback: int) -> int:
    """Return the highest mtime across *.md files in *unified* and *headless*.

    Args:
        unified:  Path to the unified reports directory.
        headless: Path to the headless reports directory.
        fallback: Value returned when no *.md files are found.

    Returns:
        Highest integer mtime found, or *fallback* if no files exist.
    """
    max_mtime = 0
    for d in (unified, headless):
        p = Path(d)
        if not p.is_dir():
            continue
        for f in p.glob("*.md"):
            try:
                mtime = int(f.stat().st_mtime)
                if mtime > max_mtime:
                    max_mtime = mtime
            except OSError as e:
                print(f"warning: stat failed for {f}: {e}", file=sys.stderr)
    return max_mtime if max_mtime > 0 else fallback


def main() -> None:
    if len(sys.argv) != 4:
        print(
            f"Usage: {sys.argv[0]} <unified_dir> <headless_dir> <fallback_ts>",
            file=sys.stderr,
        )
        sys.exit(1)

    unified, headless, fallback_raw = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        fallback = int(fallback_raw)
    except ValueError:
        print(f"error: fallback_ts must be an integer, got {fallback_raw!r}", file=sys.stderr)
        sys.exit(1)

    result = find_newest_report_mtime(unified, headless, fallback)
    print(result)


if __name__ == "__main__":
    main()
