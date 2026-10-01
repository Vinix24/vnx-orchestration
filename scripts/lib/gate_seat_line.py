#!/usr/bin/env python3
"""Per-seat display line for ``vnx gate`` (OI-1888).

``scripts/commands/gate.sh`` used to print ``Gate '<seat>': PASS`` from the raw
seat name and ``request-and-execute``'s exit code alone. When a seat's own
reader is unavailable (quota, 403) and the takeover chain hands it to a
different gate (``kimi_gate`` unavailable -> ``glm_gate`` reads instead), the
exit code still reflects the SUCCESSOR's verdict correctly, but the line kept
naming the ORIGINAL seat as if it had read -- #1952 merged on 2026-09-27 on
exactly that misreading, with kimi unavailable and glm's PASS shown as kimi's.

This module resolves, per requested seat, which gate actually answered it and
what it decided -- reusing :func:`gate_enforcement_verify.resolve_seat_entries`
(the same takeover interpretation ``scripts/t0_gate_enforcement.sh`` already
relies on) rather than re-deriving the takeover walk a second time.

CLI: reads the ``request-and-execute`` JSON report from stdin (merged
stdout+stderr, exactly as ``gate_enforcement_verify.extract_report`` already
expects it), prints one ``TAG\\tline`` pair per ``--seats`` entry (in the given
order) to stdout -- ``TAG`` is ``PASS`` or ``FAIL`` so the bash caller can pick
``log``/``err`` styling without re-parsing the line text -- and exits 0 only
when every seat is a decided PASS.

BILLING SAFETY: No Anthropic SDK. No network calls.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gate_enforcement_verify import (  # noqa: E402
    VerificationError,
    _raw_takeover_path,
    extract_report,
    resolve_seat_entries,
)
from gate_status import FAIL_STATES, has_complete_evidence, is_pass, is_terminal  # noqa: E402


def _reason_text(entry: Dict[str, Any]) -> str:
    """The most specific human-readable cause this entry carries.

    ``reason_detail`` (a cause) beats ``reason`` (a classification) beats the
    generic ``pass_reason`` gate_executor always stamps -- same precedence
    ``gate_recorder.derive_failure_reason`` documents for the same three
    fields, applied here to whichever of ``entry`` or its nested ``detail``
    (the raw gate-run payload for an executed seat) carries the value.
    """
    detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
    for source in (entry, detail):
        for key in ("reason_detail", "reason"):
            value = source.get(key)
            if value:
                return str(value)
    pass_reason = entry.get("pass_reason")
    return str(pass_reason) if pass_reason else "no verdict recorded"


def _diff_truncated(entry: Dict[str, Any]) -> bool:
    detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
    return bool(detail.get("diff_truncated") or entry.get("diff_truncated"))


def _decided_fail_reason(detail: Dict[str, Any]) -> str:
    """The reason the seat's own record gives for not passing, e.g. ``1 blocking finding(s)``."""
    return is_pass(detail)[1]


def _verdict_label(entry: Dict[str, Any]) -> str:
    """PASS / FAIL (reason) / UNAVAILABLE (reason) for the gate that answered.

    FAIL is reserved for a real, decided rejection: a recognised fail status
    (:data:`gate_status.FAIL_STATES`), or the seat's own record being terminal,
    carrying complete evidence and not passing (a review fail booked as
    ``completed`` plus blocking findings, OI-1938). Everything
    else that is not a decided pass (unavailable, not_executable,
    chain_exhausted, an in-flight status, an unrecognised one) reads as
    UNAVAILABLE: no verdict was produced, which must never display as a
    quieter kind of FAIL.
    """
    if entry.get("passed"):
        return "PASS"
    status = str(entry.get("execution_status") or entry.get("request_status") or "").strip().lower()
    if status in FAIL_STATES:
        return f"FAIL ({_reason_text(entry)})"
    detail = entry.get("detail")
    if (
        isinstance(detail, dict)
        and is_terminal(detail)
        and has_complete_evidence(detail)
        and not is_pass(detail)[0]
    ):
        return f"FAIL ({_decided_fail_reason(detail)})"
    return f"UNAVAILABLE ({_reason_text(entry)})"


def _hop_reason_for_seat(entry: Dict[str, Any], seat: str, request_file: Path) -> str:
    for hop in _raw_takeover_path(entry, request_file):
        if str(hop.get("gate")) == seat:
            reason = hop.get("reason")
            if reason:
                return str(reason)
    return "takeover"


def format_seat_line(
    seat: str,
    *,
    pr_number: int,
    state_dir: Path,
    seat_entries: Dict[str, Dict[str, Any]],
) -> Tuple[str, bool]:
    """Return ``(line, seat_passed)`` for one requested seat."""
    entry = seat_entries.get(seat)
    if entry is None:
        return f"Gate '{seat}': UNAVAILABLE (no result recorded for this seat)", False

    label = _verdict_label(entry)
    if _diff_truncated(entry):
        label = f"{label} (diff truncated)"
    passed = bool(entry.get("passed"))

    answering_gate = str(entry.get("gate") or seat)
    if answering_gate == seat:
        return f"Gate '{seat}': {label}", passed

    request_file = state_dir / "review_gates" / "requests" / f"pr-{pr_number}-{answering_gate}.json"
    reason = _hop_reason_for_seat(entry, seat, request_file)
    return f"Gate '{seat}' -> {answering_gate} (takeover, {reason}): {label}", passed


def format_seat_lines(
    seats: Sequence[str],
    report: Dict[str, Any],
    *,
    pr_number: int,
    state_dir: Path,
) -> List[Tuple[str, bool]]:
    """Return ``(line, seat_passed)`` for every requested seat, in order."""
    seat_entries = resolve_seat_entries(report, pr_number=pr_number, state_dir=state_dir)
    return [
        format_seat_line(seat, pr_number=pr_number, state_dir=state_dir, seat_entries=seat_entries)
        for seat in seats
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="gate_seat_line", add_help=False)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--seats", required=True, help="Comma-separated seat names, in requested order")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    seats = [item.strip() for item in args.seats.split(",") if item.strip()]
    if not seats:
        print("gate_seat_line: --seats resolved empty", file=sys.stderr)
        return 2

    try:
        report = extract_report(sys.stdin.read())
    except VerificationError as exc:
        print(f"gate_seat_line: {exc}", file=sys.stderr)
        return 2

    results = format_seat_lines(seats, report, pr_number=args.pr, state_dir=Path(args.state_dir))
    for line, passed in results:
        print(f"{'PASS' if passed else 'FAIL'}\t{line}")
    return 0 if all(passed for _, passed in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
