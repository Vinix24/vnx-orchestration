#!/usr/bin/env python3
"""Compress old event archives of one store; dry-run by default (ADR-081, eventarchief-compressie C2b).

``events_archive_compress.py --project-id <pid> [--days N] [--apply] [--json]``

The store is ``<central data dir>/events/archive`` of the project, including its
per-terminal subdirectories. Without ``--apply`` the CLI only lists what a round
would touch (``candidates``): it opens no file for writing and takes no lock.
With ``--apply`` it runs ``compress_archive_round(..., manual=True)``.

Exit codes:
  0  clean (a skipped outside_store entry is not divergent)
  1  any other failure (a failed read or write)
  2  usage or resolution error (unknown project, no archive directory)
  3  at least one pair is divergent (never removed, only reported)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import event_archive_compress as eac
from vnx_data_footprint_probe import DEFAULT_COMPRESS_DAYS
from vnx_paths import resolve_central_data_dir

OUTSIDE_STORE = "outside_store"

EPILOG = (
    "exit codes: 0 clean; 1 any other failure; 2 usage or resolution error; "
    "3 at least one divergent pair (never removed, only reported)"
)


def _skip_row(path: Path) -> Dict[str, Any]:
    print(f"skipped outside_store: {path} -> {os.path.realpath(path)}", file=sys.stderr)
    return {"path": str(path), "outcome": eac.SKIPPED, "reason": OUTSIDE_STORE,
            "bytes_before": 0, "bytes_after": 0}


def _directories(archive: Path) -> Tuple[List[Path], List[Dict[str, Any]]]:
    """Real directories whose resolved path stays under the store, plus the skipped entries.

    A symlinked directory, a directory resolving outside the store, and a directory
    holding a symlinked archive file (the library's glob would follow it and cannot
    be told to leave one file out) are skipped whole and never reach the library.
    """
    root, found, skipped = archive.resolve(), [], []
    walked = [archive]
    for cur, dirs, _files in os.walk(archive):
        dirs.sort()
        walked.extend(Path(cur) / d for d in dirs)
    for d in walked:
        if d.is_symlink() or not d.resolve().is_relative_to(root):
            skipped.append(_skip_row(d))
            continue
        links = [f for f in sorted(d.glob("*" + eac.ARCHIVE_SUFFIX + "*"))
                 if f.is_symlink() or not f.resolve().is_relative_to(root)]
        if links:
            skipped.extend(_skip_row(f) for f in links)
            continue
        found.append(d)
    return found, skipped


def _size(path: Optional[str]) -> int:
    return os.stat(path).st_size if path else 0


def _row(f: "eac.FileResult") -> Dict[str, Any]:
    done = f.outcome in (eac.COMPRESSED, eac.PAIR_RECOMPRESSED)
    return {"path": f.path, "outcome": f.outcome, "reason": f.reason,
            "bytes_before": f.plain_size, "bytes_after": (f.gz_size or 0) if done else 0}


def _report(rows: List[Dict[str, Any]], mode: str, stopped: str) -> Dict[str, Any]:
    ok = (eac.COMPRESSED, eac.PAIR_RECOMPRESSED, eac.PAIR_REMOVED_PLAIN)
    return {"mode": mode, "stopped": stopped, "files": rows, "totals": {
        "files": len(rows),
        "bytes_before": sum(r["bytes_before"] for r in rows if mode == "dry-run" or r["outcome"] in ok),
        "bytes_after": sum(r["bytes_after"] for r in rows),
        "skipped": sum(1 for r in rows if r["outcome"] == eac.SKIPPED),
        "divergent": sum(1 for r in rows if r["outcome"] == eac.DIVERGENT),
    }}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], epilog=EPILOG)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--days", type=float, default=float(DEFAULT_COMPRESS_DAYS),
                    help="compress plain archives older than this (default %(default)s, ADR-081)")
    ap.add_argument("--apply", action="store_true", help="compress; without it nothing changes")
    ap.add_argument("--json", action="store_true", help="one JSON object on stdout")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if not (math.isfinite(args.days) and args.days >= 0):
        print(f"--days must be a finite number, not negative: {args.days}", file=sys.stderr)
        return 2
    try:
        store_root = resolve_central_data_dir(args.project_id)
        archive = store_root / "events" / "archive"
    except ValueError as exc:
        print(f"unknown project: {exc}", file=sys.stderr)
        return 2
    link = eac.symlinked_root(store_root, archive)
    if link is not None:
        print(f"archive root refused: {link} -> {os.path.realpath(link)} (symlink or outside the store)",
              file=sys.stderr)
        return 2
    if not archive.is_dir():
        print(f"no archive directory for project {args.project_id}: {archive}", file=sys.stderr)
        return 2
    try:
        dirs, skipped_rows = _directories(archive)
        if args.apply:
            rnd = eac.compress_archive_round(dirs, args.days, manual=True)
            rows, stopped = [_row(f) for f in rnd.files] + skipped_rows, rnd.stopped
        else:
            rows = [{"path": str(p), "outcome": "would_compress", "reason": None,
                     "bytes_before": _size(str(p)), "bytes_after": 0}
                    for p in eac.candidates(dirs, args.days)] + skipped_rows
            stopped = "done"
        report = _report(rows, "apply" if args.apply else "dry-run", stopped)
    except Exception as exc:  # the failure is reported and exits 1, never a clean 0
        print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report))
    else:
        for r in rows:
            print(f"{r['path']}\t{r['outcome']}\t{r['reason'] or '-'}\t{r['bytes_before']}\t{r['bytes_after']}")
        print("totals: " + " ".join(f"{k}={v}" for k, v in report["totals"].items()) + f" stopped={stopped}")
    return 3 if report["totals"]["divergent"] else 0


if __name__ == "__main__":
    sys.exit(main())
