#!/usr/bin/env python3
"""receipt_conversion_rejection_beacon.py — per-rejection detail for the
``report_to_receipt_converter.py`` scan (golf3b / F1-2).

Bug this closes: ``report_to_receipt_converter``'s OWN health beacon
(``health/report_to_receipt_converter.json``, owned by that module — not
touched here) already carries a ``rejected_count`` integer, but no per-report
detail. A count of 27 with no names is not actionable: nobody can tell WHICH
report was refused or WHY without re-running the converter by hand and
watching stderr, and that stderr is exactly what
``scripts/receipt_processor.sh`` used to throw away with ``2>/dev/null``
(both the polling-loop call and, before OI-1753, the catchup call too).

This module is the Bash caller's own health writer, not a change to the
converter: ``receipt_processor.sh`` now captures the converter's stderr (see
``_run_receipt_converter_scan``), and pipes the raw text here on stdin. Every
line matching the converter's own fail-closed log format —

    "REJECTED (fail-closed) dispatch=<id> file=<name> reason=<msg>"

(``report_to_receipt_converter.py``'s ``_convert_one_detailed``, logged via
``logger.warning`` on ``AppendReceiptError.code == "missing_model"``) — is
parsed into a ``{dispatch_id, file, reason}`` record and written as this
scan's beacon detail. Any other line (INFO chatter, the per-scan summary
line, a different WARNING) is ignored: it is not this beacon's job to
duplicate the processing log, only to make the REJECTED reason traceable.

Snapshot semantics, matching ``report_to_receipt_converter._write_scan_heartbeat``'s
own convention: each heartbeat reflects ONLY the rejections seen in the scan
that just ran, not an ever-growing history — a report rejected every cycle
until its cause is fixed would otherwise make ``details.rejections`` grow
without bound. History lives in the processing log (now that it is no longer
discarded); this beacon answers "what is wrong RIGHT NOW", the question a
human or ``vnx doctor`` actually asks.

Component name is a module-level string constant so
``scripts/lib/beacon_register.py``'s AST scan discovers it as an EXPECTED
writer (mirrors every other ``HealthBeacon(...)`` call site in this repo) —
if this beacon ever stops being written, ``all_beacons(expected=...)`` marks
it ``absent`` (not silently missing), the same "absence-is-loud" contract
every other component here already has.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from health_beacon import HealthBeacon  # noqa: E402

_COMPONENT = "receipt_conversion_rejections"
_EXPECTED_INTERVAL_SECONDS = 3600

# Mirrors report_to_receipt_converter.py's own log line exactly:
#   "report_to_receipt_converter: REJECTED (fail-closed) dispatch=%s file=%s reason=%s"
_REJECTED_PATTERN = re.compile(
    r"REJECTED \(fail-closed\) dispatch=(\S+) file=(\S+) reason=(.*)$"
)


def parse_rejections(raw_stderr: str) -> List[Dict[str, str]]:
    """Extract ``{dispatch_id, file, reason}`` records from raw stderr text.

    Never raises on malformed input — a line that doesn't match the pattern
    is simply not a rejection line and is skipped, not an error.
    """
    rejections: List[Dict[str, str]] = []
    for line in raw_stderr.splitlines():
        match = _REJECTED_PATTERN.search(line)
        if not match:
            continue
        dispatch_id, file_name, reason = match.groups()
        rejections.append({
            "dispatch_id": dispatch_id,
            "file": file_name,
            "reason": reason.strip(),
        })
    return rejections


def record_rejections(state_dir: Path, rejections: List[Dict[str, str]]) -> None:
    """Write this scan's rejections as a heartbeat under ``<data_dir>/health/``.

    ``state_dir`` is ``$VNX_STATE_DIR`` (``<data_root>/state``) — the same
    convention ``report_to_receipt_converter._write_scan_heartbeat`` documents
    for its own beacon. ``status`` is ``fail`` when this scan saw at least one
    rejection, ``ok`` otherwise (an empty scan, or a scan with only
    new/duplicate outcomes, is healthy — the same "attempted with zero
    success" distinction the converter's own beacon makes, narrowed to just
    the rejected outcome since that's the one this beacon exists to detail).
    """
    data_dir = state_dir.parent
    beacon = HealthBeacon(data_dir, _COMPONENT, expected_interval_seconds=_EXPECTED_INTERVAL_SECONDS)
    status = "fail" if rejections else "ok"
    details: Dict[str, Any] = {
        "count": len(rejections),
        "rejections": rejections,
    }
    beacon.heartbeat(status=status, details=details)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir", required=True,
        help="$VNX_STATE_DIR (<data_root>/state) — the beacon lands at its parent's health/ dir",
    )
    args = parser.parse_args(argv)

    raw_stderr = sys.stdin.read()
    rejections = parse_rejections(raw_stderr)
    record_rejections(Path(args.state_dir), rejections)
    return 0


if __name__ == "__main__":
    sys.exit(main())
