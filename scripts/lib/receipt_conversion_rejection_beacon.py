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
``logger.warning`` on every ``AppendReceiptError`` whose code is in
``MODEL_REFUSAL_CODES``: ``missing_model`` and ``invalid_model_shape``) — is
parsed into a ``{dispatch_id, file, reason}`` record and written as this
scan's beacon detail. Any other line (INFO chatter, the per-scan summary
line, a different WARNING) is ignored: it is not this beacon's job to
duplicate the processing log, only to make the REJECTED reason traceable.

``details.rejections`` is a snapshot, matching
``report_to_receipt_converter._write_scan_heartbeat``'s own convention: it
lists ONLY the rejections seen in the scan that just ran, not an ever-growing
history. A report rejected every cycle until its cause is fixed would
otherwise make it grow without bound.

The STATUS is not a snapshot. The converter quarantines a report the moment it
is refused (``_deadletter_report``), so the next scan no longer sees it and a
snapshot-only verdict would flip back to ``ok`` one scan later, and a refusal
nobody has read would evaporate. The converter's own beacon already keeps the
accumulated history (``details.rejected``, each entry stamped ``rejected_at``);
this module reads that history and applies ONE rule to it,
``recent_rejections``: a rejection younger than ``REJECTION_ALARM_WINDOW_SECONDS``
keeps the beacon at ``fail``. The converter's beacon imports the same rule, so
the two beacons give the same verdict for the same history instead of one
saying ``fail`` while the other says ``ok``.

Component name is a module-level string constant so
``scripts/lib/beacon_register.py``'s AST scan discovers it as an EXPECTED
writer (mirrors every other ``HealthBeacon(...)`` call site in this repo) —
if this beacon ever stops being written, ``all_beacons(expected=...)`` marks
it ``absent`` (not silently missing), the same "absence-is-loud" contract
every other component here already has.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from health_beacon import HealthBeacon  # noqa: E402

_COMPONENT = "receipt_conversion_rejections"
_EXPECTED_INTERVAL_SECONDS = 3600

# The converter beacon that carries the accumulated rejection history
# (report_to_receipt_converter._HEALTH_COMPONENT; a test pins the two equal).
CONVERTER_COMPONENT = "report_to_receipt_converter"

# How long a refusal keeps a beacon at fail. The converter quarantines the
# report on the first refusal, so without a window the alarm would clear on the
# very next scan and an unread refusal would disappear. One day is long enough
# for the operator to open the health digest at least once, and short enough
# that a refusal already handled does not pin the beacon at fail for good (the
# retry-forever behavior this window replaces).
REJECTION_ALARM_WINDOW_SECONDS = 24 * 3600

_REJECTED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

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


def load_rejected_history(state_dir: Path, component: str) -> List[Dict[str, Any]]:
    """Load the accumulated ``details.rejected`` history of *component*'s beacon.

    ``HealthBeacon.heartbeat()`` atomically REPLACES the whole payload on every
    call (tmp + os.replace) and has no append mode, so a writer that wants to
    accumulate must read its prior history back first, and a reader that wants
    the history reads the same field.

    Best-effort: a missing, unreadable, or malformed health file yields an
    empty history rather than raising. A corrupt beacon must never block a
    scan from completing.
    """
    path = state_dir.parent / "health" / f"{component}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    details = payload.get("details") if isinstance(payload, dict) else None
    prior = details.get("rejected") if isinstance(details, dict) else None
    return [entry for entry in prior if isinstance(entry, dict)] if isinstance(prior, list) else []


def _rejected_at_epoch(entry: Dict[str, Any]) -> Optional[float]:
    try:
        parsed = datetime.strptime(entry.get("rejected_at"), _REJECTED_AT_FORMAT)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def recent_rejections(history: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The entries of *history* whose ``rejected_at`` is inside the alarm window.

    An entry without a parseable ``rejected_at`` counts as old: its age cannot
    be shown, and treating it as young would pin the alarm at ``fail`` for as
    long as that entry stays in the history.
    """
    cutoff = time.time() - REJECTION_ALARM_WINDOW_SECONDS
    recent: List[Dict[str, Any]] = []
    for entry in history:
        epoch = _rejected_at_epoch(entry)
        if epoch is not None and epoch > cutoff:
            recent.append(entry)
    return recent


def _newest_per_report(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per (dispatch_id, file, reason), the newest, oldest first.

    A report the converter re-refused on every scan (before it was
    quarantined, or when the move failed) sits in the history once per scan;
    the beacon names each report once.
    """
    newest: Dict[tuple, Dict[str, Any]] = {}
    for entry in entries:
        key = (entry.get("dispatch_id"), entry.get("file"), entry.get("reason"))
        held = newest.get(key)
        if held is None or entry["rejected_at"] > held["rejected_at"]:
            newest[key] = entry
    return sorted(newest.values(), key=lambda e: e["rejected_at"])


def record_rejections(state_dir: Path, rejections: List[Dict[str, str]]) -> None:
    """Write the rejection beacon under ``<data_dir>/health/``.

    ``state_dir`` is ``$VNX_STATE_DIR`` (``<data_root>/state``) — the same
    convention ``report_to_receipt_converter._write_scan_heartbeat`` documents
    for its own beacon.

    ``status`` is ``fail`` when this scan saw at least one rejection OR the
    converter beacon's history holds one inside the alarm window (see the
    module docstring: the converter quarantines a refused report, so the scan
    after a rejection sees nothing, and the refusal must stay visible until
    the window has passed). ``ok`` otherwise. ``details.rejections`` is this
    scan's snapshot; ``details.recent_rejected`` names the reports holding the
    alarm, one entry each.
    """
    data_dir = state_dir.parent
    beacon = HealthBeacon(data_dir, _COMPONENT, expected_interval_seconds=_EXPECTED_INTERVAL_SECONDS)
    recent = recent_rejections(load_rejected_history(state_dir, CONVERTER_COMPONENT))
    status = "fail" if rejections or recent else "ok"
    details: Dict[str, Any] = {
        "count": len(rejections),
        "rejections": rejections,
        "recent_rejected": _newest_per_report(recent),
        "alarm_window_seconds": REJECTION_ALARM_WINDOW_SECONDS,
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
