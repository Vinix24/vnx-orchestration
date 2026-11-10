#!/usr/bin/env python3
"""Recurring dream liveness probe (ADR-066, dream PR-7).

Runs the same ``assess()`` as the doctor, once, and leaves two traces when
asked to (D15, the whole write surface of this probe):

* one NDJSON line in ``<data_root>/state/dream_liveness.ndjson`` (``--append``);
* the beacon ``<data_root>/state/health/dream_liveness.json`` whose
  ``expected_interval_seconds`` is 90000 (25h): a probe that stops running
  turns the beacon stale, which makes its own death visible.

Exit codes: 0 measured (and ``--strict``: state ok), 1 a write failed or
``--strict`` found ``state != ok``, 2 the project id was rejected.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import dream_liveness
from health_beacon import HealthBeacon
from project_root import resolve_central_data_dir

BEACON_COMPONENT = "dream_liveness"
BEACON_INTERVAL_SECONDS = 90000
LEDGER_NAME = "dream_liveness.ndjson"


def _append_line(path: Path, document: dict) -> None:
    """One NDJSON line, O_APPEND and fsync; raises OSError when it cannot land."""
    line = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        written = os.write(fd, line)
        if written != len(line):
            raise OSError(f"short write to {path}: {written} of {len(line)} bytes")
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(state_dir: Path, document: dict, append: bool) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    if append:
        _append_line(state_dir / LEDGER_NAME, document)
    # heartbeat_strict raises on I/O failure; the best-effort heartbeat would swallow it.
    HealthBeacon(state_dir, BEACON_COMPONENT,
                 expected_interval_seconds=BEACON_INTERVAL_SECONDS).heartbeat_strict(
        status="ok" if document["state"] == "ok" else "fail",
        details={"state": document["state"], "project_id": document["project_id"],
                 "finding_ids": sorted({f["id"] for f in document["findings"]})},
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dream liveness probe (ADR-066)")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--json", action="store_true", help="print the document")
    parser.add_argument("--append", action="store_true", help="append one NDJSON line and write the beacon")
    parser.add_argument("--strict", action="store_true", help="exit 1 when state is not ok")
    args = parser.parse_args(argv)

    try:
        data_root = resolve_central_data_dir(args.project_id)
        document = dream_liveness.assess(
            dream_liveness.load_sources(args.project_id, data_root=data_root),
            project_id=args.project_id)
    except ValueError as exc:
        print(f"dream_liveness_probe: project id rejected: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True))
    if args.append:
        try:
            _write(data_root / "state", document, append=True)
        except OSError as exc:
            print(f"dream_liveness_probe: write failed: {exc}", file=sys.stderr)
            return 1
    if args.strict and document["state"] != "ok":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
