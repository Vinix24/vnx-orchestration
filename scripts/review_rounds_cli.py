#!/usr/bin/env python3
"""vnx review — the round ledger of the PR review (R1).

``vnx review rounds open --pr N --reason "..."`` opens PR N in the project's
round ledger (``state/review_gates/rounds.ndjson``) as the operator: an
``opened`` line with ``basis`` ``attested``, the actor (the OS user) and the
reason. Run it for a PR reviewed before the ledger counted it: the gate refuses
its seats (``round_history_missing``) and the merge door its PASS until then.
Its earlier verdicts are not reconstructed; the B3 cap starts at zero from this
line. The line is written first, under the ledger lock; then one
``review_round_opened`` receipt goes into the project's receipt ledger with the
earlier-review evidence that was found. A PR that is already opened gets no
second line and no second receipt.

An unreadable ledger is refused with the same error the gate gives. Nothing
removes, moves or rewrites a ledger line: the way out is to set the file
aside, then reopen every open PR with a reason.

``--data-dir`` opens the PR in ``<data-dir>/state``. When ``VNX_STATE_DIR``
overrides the state dir with another one, the gate reads that ledger instead,
so the command refuses and names both paths rather than writing a line nobody
reads (``review_rounds.store_state_dir``).

Exit codes:
  0  opened, or already opened (the message names the basis and recorded_at that stand)
  1  opened, but the receipt could not be written: the ledger line stands
  2  bad input, --data-dir and VNX_STATE_DIR name two ledgers, or the ledger could not be read or written

``rounds show --pr N`` lists a PR's ledger: its ``opened`` line, its counted
verdicts, and every build-door ``--override-b3`` (``override`` line) recorded
against it.

Usage:
  python3 scripts/review_rounds_cli.py rounds open --pr 2089 --reason "reviewed before R1: B3.1 in round 1, operator merged"
  python3 scripts/review_rounds_cli.py rounds open --pr 2089 --reason "..." --data-dir /path/to/.vnx-data --json
  python3 scripts/review_rounds_cli.py rounds show --pr 2089 [--data-dir /path/to/.vnx-data] [--json]
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (str(SCRIPT_DIR / "lib"), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

EXIT_OK = 0
EXIT_RECEIPT_FAILED = 1
EXIT_ERROR = 2

RECEIPT_EVENT = "review_round_opened"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx review", description="The round ledger of the PR review (R1)")
    sub = parser.add_subparsers(dest="noun", required=True)
    rounds = sub.add_parser("rounds", help="the round ledger: one line per review verdict, per seat, per head")
    verbs = rounds.add_subparsers(dest="verb", required=True)
    opener = verbs.add_parser("open", help="open a PR with review history from before the ledger, with a reason")
    opener.add_argument("--pr", type=int, required=True, help="GitHub PR number")
    opener.add_argument("--reason", required=True, help="why this PR opens without its earlier verdicts counted")
    opener.add_argument("--data-dir", default=None, help="the project's .vnx-data (default: this project's)")
    opener.add_argument("--json", action="store_true", help="machine-readable output")
    show = verbs.add_parser("show", help="show a PR's round ledger: verdicts, its opened line and any build-door override")
    show.add_argument("--pr", type=int, required=True, help="GitHub PR number")
    show.add_argument("--data-dir", default=None, help="the project's .vnx-data (default: this project's)")
    show.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _data_dir(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from vnx_paths import ensure_env

    return Path(ensure_env()["VNX_DATA_DIR"])


def _emit_receipt(state_dir: Path, line, *, ledger: Path, evidence) -> None:
    """One ``review_round_opened`` receipt through the receipt API, into this store's receipt ledger."""
    from governance_receipts import emit_governance_receipt
    from review_rounds import RECEIPTS_NAME

    emit_governance_receipt(
        RECEIPT_EVENT, receipt_kind="state_mutation", receipts_file=str(state_dir / RECEIPTS_NAME),
        pr_number=line.pr_number, basis=line.basis, actor=line.actor, reason=line.reason,
        ledger=str(ledger), evidence=list(evidence), opened_at=line.recorded_at,
        **({"project_id": line.project_id} if line.project_id else {}),
    )


def _show(args) -> int:
    """``rounds show``: a PR's ledger in file order — its ``opened`` line, its verdicts, its build-door overrides."""
    import review_rounds

    if args.pr <= 0:
        print("vnx review rounds show: --pr must be a positive PR number", file=sys.stderr)
        return EXIT_ERROR
    data_dir = _data_dir(args.data_dir)
    try:
        state_dir = review_rounds.store_state_dir(data_dir if args.data_dir else None)
    except review_rounds.StoreConflict as exc:
        print(f"vnx review rounds show: refused: {exc}", file=sys.stderr)
        return EXIT_ERROR
    review_gates = state_dir / "review_gates"
    ledger_path = review_rounds.ledger_path(review_gates)
    try:
        ledger = review_rounds.read_ledger(ledger_path)
    except review_rounds.RoundLedgerError as exc:
        print(f"vnx review rounds show: refused: {exc}", file=sys.stderr)
        return EXIT_ERROR
    project_id = review_rounds.store_project_id(review_gates)
    opened = ledger.opened_line(project_id, args.pr)
    overrides = ledger.overrides_for(project_id, args.pr)
    lines = ledger.for_pr(project_id, args.pr)
    if args.json:
        print(json.dumps({
            "pr": args.pr,
            "ledger": str(ledger_path),
            "opened": opened.to_dict() if opened is not None else None,
            "overrides": [line.to_dict() for line in overrides],
            "lines": [line.to_dict() for line in lines],
        }, indent=2))
        return EXIT_OK
    print(f"PR #{args.pr} round ledger {ledger_path}:")
    if opened is None:
        print("  opened: none")
    else:
        print(f"  opened: basis {opened.basis}, recorded_at {opened.recorded_at}, source {opened.source}")
    for line in overrides:
        print(
            f"  override: head {line.head_sha[:12]} reason {line.reason!r} set_by {line.set_by} "
            f"total {line.total}" + (f" rule {line.rule}" if line.rule else "")
        )
    for line in lines:
        print(
            f"  {line.seat} {line.verdict.upper()}({line.blocking_count}) on {line.head_sha[:12]} "
            f"reviewer {line.reviewer}"
        )
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    # After the sys.path bootstrap above: review_rounds lives in scripts/lib.
    import review_rounds
    from governance_receipts import AppendReceiptError

    args = _build_parser().parse_args(argv)
    if args.verb == "show":
        return _show(args)
    reason = args.reason.strip()
    if args.pr <= 0 or not reason:
        print('vnx review rounds open: --pr must be a positive PR number and --reason "..." must say why', file=sys.stderr)
        return EXIT_ERROR
    data_dir = _data_dir(args.data_dir)
    try:
        state_dir = review_rounds.store_state_dir(data_dir if args.data_dir else None)
    except review_rounds.StoreConflict as exc:
        print(f"vnx review rounds open: refused: {exc}", file=sys.stderr)
        return EXIT_ERROR
    review_gates = state_dir / "review_gates"
    ledger = review_rounds.ledger_path(review_gates)
    try:
        evidence = review_rounds.prior_evidence(data_dir, args.pr, state_dir=state_dir)
        status, line = review_rounds.open_pr(
            ledger, review_rounds.store_project_id(review_gates), args.pr, basis=review_rounds.BASIS_ATTESTED,
            evidence=len(evidence), actor=getpass.getuser(), reason=reason,
        )
    except review_rounds.RoundLedgerError as exc:
        print(f"vnx review rounds open: refused: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if status == "present":
        print(
            f"PR #{args.pr} is already opened (basis {line.basis}, recorded_at {line.recorded_at}) in {ledger}: "
            f"no second line"
        )
        return EXIT_OK
    try:
        _emit_receipt(state_dir, line, ledger=ledger, evidence=evidence)
    except (AppendReceiptError, OSError, ValueError) as exc:
        print(
            f"vnx review rounds open: PR #{args.pr} is opened and the ledger line stands, but the "
            f"{RECEIPT_EVENT} receipt was not written: {exc}", file=sys.stderr,
        )
        return EXIT_RECEIPT_FAILED
    if args.json:
        print(json.dumps({**line.to_dict(), "ledger": str(ledger), "evidence": list(evidence)}, indent=2))
    else:
        print(
            f"PR #{args.pr} opened in {ledger} (basis attested, actor {line.actor}): the cap starts at zero here; "
            f"{len(evidence)} earlier-review entr{'y' if len(evidence) == 1 else 'ies'} not counted"
            + (f", first: {evidence[0]}" if evidence else "")
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
