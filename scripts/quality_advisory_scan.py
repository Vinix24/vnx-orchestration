#!/usr/bin/env python3
"""Whole-repo quality advisory backlog scanner.

Advisory-only companion to the diff-scoped BLOCKING file-size gate in
``scripts/lib/quality_advisory.py``. Walks the whole repository tree and writes
a JSON backlog of every source file that exceeds the warning threshold. The
output is written atomically and the script always exits 0 — it produces a
backlog, it never fails a gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from atomic_io import atomic_write_json
from project_root import resolve_data_dir, resolve_project_root
from quality_advisory import build_whole_repo_file_size_backlog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a whole-repo file-size advisory backlog (advisory only; "
            "never blocks CI or gates)."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root to scan (default: git-resolved project root).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Output JSON path (default: "
            "$VNX_DATA_DIR/quality_advisory_backlog.json)."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root or resolve_project_root(__file__)
    output_path = args.output or (
        resolve_data_dir(__file__) / "quality_advisory_backlog.json"
    )

    backlog = build_whole_repo_file_size_backlog(repo_root)
    atomic_write_json(output_path, backlog)

    print(
        f"quality_advisory_scan: wrote {backlog['total_backlog']} backlog item(s) "
        f"({backlog['blocking_count']} blocking, {backlog['allowlisted_count']} "
        f"allowlisted, {backlog['warning_count']} warning) to {output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
