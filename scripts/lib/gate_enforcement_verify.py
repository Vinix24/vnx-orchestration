#!/usr/bin/env python3
"""Artifact verification for scripts/t0_gate_enforcement.sh.

``review_gate_manager.py request-and-execute`` prints one JSON report: a
``gates`` list with one entry per gate it actually requested (and, for a gate
that could run, executed). This module reads that report and checks that the
artifacts of every reported gate are on disk, and that every seat of the
requested stack is accounted for by one of those gates.

Why the report and not the requested stack
------------------------------------------
The requested stack names SEATS. The manager may fill a seat with a different
gate: when codex_gate is unavailable the takeover chain has kimi_gate read in
its place, and a later seat that resolves to a gate already requested this round
is not requested a second time. Verifying artifacts for the raw seat names then
demands a file for a gate that was intentionally never asked, or trusts one left
by an earlier round. So the gates verified are the reported ones.

Fail-closed
-----------
A seat is accounted for when a reported gate is that seat, when the seat appears
in a reported gate's ``takeover_path`` (it was passed over and that gate read in
its place), or when its takeover chain was recorded as exhausted. A seat with none
of these has no request, no result and no takeover record: MISSING_ARTIFACT.

The requested stack is parsed from the same arguments the manager received, with
argparse, so ``--review-stack a,b`` and ``--review-stack=a,b`` and a quoted
``"a, b"`` all resolve to the seats the manager saw. Without ``--review-stack`` it
is the manager's ``DEFAULT_REVIEW_STACK``.

BILLING SAFETY: No Anthropic SDK. No network calls.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

_GATE_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_COMPLETED_STATUSES = ("completed", "passed")
_CHAIN_EXHAUSTED = "chain_exhausted"


class VerificationError(Exception):
    """The report or the arguments cannot be verified; the wrapper must fail."""


@dataclass
class Verification:
    """Outcome of verifying one request-and-execute report."""

    missing: List[str] = field(default_factory=list)
    not_completed: List[str] = field(default_factory=list)
    verified: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def parse_request_args(
    argv: Sequence[str],
    default_stack: Callable[[], Sequence[str]],
) -> "tuple[int, List[str]]":
    """Return ``(pr_number, seats)`` for the arguments the manager was given."""
    parser = argparse.ArgumentParser(add_help=False, prog="t0_gate_enforcement")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--review-stack", default=None)
    args, _unrecognised = parser.parse_known_args(list(argv))
    if args.review_stack is None:
        seats = [str(item).strip() for item in default_stack()]
    else:
        seats = [item.strip() for item in args.review_stack.split(",")]
    seats = [seat for seat in seats if seat]
    if not seats:
        raise VerificationError("review stack resolved empty; nothing to verify")
    for seat in seats:
        _check_gate_name(seat)
    return args.pr, seats


def extract_report(output: str) -> Dict[str, Any]:
    """The JSON report inside ``output``.

    The wrapper captures the manager's stdout and stderr together, so the report
    is surrounded by log lines and the ``ERROR:`` text the manager prints on a
    required failure. It is the top-level object that starts at the beginning of
    a line and carries a ``gates`` list.
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"^\{", output, re.MULTILINE):
        try:
            candidate, _end = decoder.raw_decode(output[match.start():])
        except ValueError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("gates"), list):
            return candidate
    raise VerificationError("request-and-execute printed no JSON report with a gates list")


def _check_gate_name(name: str) -> str:
    if not _GATE_NAME.match(name):
        raise VerificationError(f"gate name {name!r} is not a plain identifier")
    return name


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _hop_gates(path: Any) -> List[str]:
    if not isinstance(path, list):
        return []
    return [str(hop["gate"]) for hop in path if isinstance(hop, dict) and hop.get("gate")]


def _takeover_hops(entry: Dict[str, Any], request_file: Path) -> List[str]:
    """The seats ``entry``'s gate read in place of: the takeover path the report
    carries, else the one on the request record (a guarded result write can leave
    the result without the annotation the request holds)."""
    detail = entry.get("detail")
    hops = _hop_gates(detail.get("takeover_path") if isinstance(detail, dict) else None)
    if hops:
        return hops
    request = _load_json(request_file)
    return _hop_gates(request.get("takeover_path") if request else None)


def verify_report(
    report: Dict[str, Any],
    *,
    pr_number: int,
    seats: Sequence[str],
    state_dir: Path,
) -> Verification:
    """Verify the artifacts of every reported gate and account for every seat."""
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    outcome = Verification()

    entries = [
        entry for entry in report.get("gates", [])
        if isinstance(entry, dict) and entry.get("gate")
    ]
    answered_by: Dict[str, List[str]] = {}

    for entry in entries:
        gate = _check_gate_name(str(entry["gate"]))
        request_file = requests_dir / f"pr-{pr_number}-{gate}.json"

        if entry.get("request_status") == _CHAIN_EXHAUSTED:
            record = results_dir / f"pr-{pr_number}-{gate}-chain-exhausted.json"
            if not record.is_file():
                outcome.missing.append(str(record))
            else:
                outcome.verified.append(gate)
                outcome.not_completed.append(f"{gate} status={_CHAIN_EXHAUSTED}")
            chain_detail = entry.get("detail")
            hops: List[str] = _hop_gates(
                chain_detail.get("takeover_path") if isinstance(chain_detail, dict) else None
            )
        else:
            result_file = results_dir / f"pr-{pr_number}-{gate}.json"
            if not request_file.is_file():
                outcome.missing.append(str(request_file))
            if not result_file.is_file():
                outcome.missing.append(str(result_file))
            if request_file.is_file() and result_file.is_file():
                outcome.verified.append(gate)
                result = _load_json(result_file)
                status = str((result or {}).get("status", "unknown"))
                if status not in _COMPLETED_STATUSES:
                    outcome.not_completed.append(f"{gate} status={status}")
            hops = _takeover_hops(entry, request_file)

        for seat in [gate, *hops]:
            answered_by.setdefault(seat, []).append(gate)

    for seat in seats:
        if seat not in answered_by:
            outcome.missing.append(
                f"seat {seat}: no gate in the request-and-execute report ran it, "
                f"took it over or recorded its takeover chain as exhausted"
            )
    return outcome


def _default_stack() -> Sequence[str]:
    """The manager's own default stack, resolved the way the manager resolves it."""
    scripts_dir = str(Path(__file__).resolve().parent.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import review_gate_manager
    return list(review_gate_manager.DEFAULT_REVIEW_STACK)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if "--" not in args_list:
        print("GATE_ENFORCEMENT_FAILED: usage: gate_enforcement_verify.py --state-dir DIR -- <manager args>", file=sys.stderr)
        return 2
    split = args_list.index("--")
    own, manager_args = args_list[:split], args_list[split + 1:]

    parser = argparse.ArgumentParser(add_help=False, prog="gate_enforcement_verify")
    parser.add_argument("--state-dir", required=True)
    state_dir = Path(parser.parse_args(own).state_dir)

    try:
        pr_number, seats = parse_request_args(manager_args, _default_stack)
        report = extract_report(sys.stdin.read())
        outcome = verify_report(report, pr_number=pr_number, seats=seats, state_dir=state_dir)
    except VerificationError as exc:
        print(f"GATE_ENFORCEMENT_FAILED: {exc}", file=sys.stderr)
        return 1

    for name in outcome.missing:
        print(f"MISSING_ARTIFACT: {name}", file=sys.stderr)
    if not outcome.ok:
        return 1
    for name in outcome.not_completed:
        # Report but let T0 decide: a gate that ran and did not complete is T0's call.
        print(f"GATE_NOT_COMPLETED: {name}", file=sys.stderr)
    print("GATE_ENFORCEMENT_COMPLETE: all artifacts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
