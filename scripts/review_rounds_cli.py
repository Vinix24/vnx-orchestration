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

``rounds open`` on a PR that is already opened writes a second line in one
case only: the release of an interrupted publication (state contract rule 2,
R2a). A verdict reached the ledger and its result record did not; the gate
refuses every new run on that head until this line. It is a new boundary: the
cap starts at zero again from here, as for any attested opening.

``rounds show --pr N`` lists a PR's ledger: its ``opened`` line, its counted
verdicts, and every build-door ``--override-b3`` (``override`` line) recorded
against it.

``cap-verdict --pr N --for-dispatch <id>`` asks the tiebreaker at the B3 cap
(R2a, R2b, ADR-041) for the next step and records it in ``review_cap_verdicts``:
one outcome, never a findings list. ``verkleinen`` grants one extra codex-seat
round to the dispatch ``--for-dispatch`` names. ``terug-naar-plan`` (B3.1) is
carried out after its row stands (``review_cap_aftermath``): one open item per
remaining codex blocker, the deliverable back to ``proposed`` and the PR
closed with a comment; a PR that links to no deliverable is said loudly on
stderr and the other steps still run. A verdict that already stands is shown,
no model is asked again, and for ``terug-naar-plan`` only the missing steps of
its aftermath run. The verdict's ``review_cap_verdict`` receipt is written
first and its row only after it landed (ADR-005, OI-2201): a verdict without a
receipt grants nothing. A receipt that landed while its row did not is booked
from the receipt (``restored``).

``cap-verdict`` exit codes:
  0  a verdict stands, and for terug-naar-plan its whole aftermath is done (also
     ``outcome_unavailable_in_r2a`` for mergen-met-OI: the cap stays closed)
  1  the receipt could not be written: no row, no granted round, run it again
  2  nothing can be decided
  3  the verdict stands, its aftermath is incomplete (a step names what did not
     finish): run it again, it does only the missing steps

Usage:
  python3 scripts/review_rounds_cli.py rounds open --pr 2089 --reason "reviewed before R1: B3.1 in round 1, operator merged"
  python3 scripts/review_rounds_cli.py rounds open --pr 2089 --reason "..." --data-dir /path/to/.vnx-data --json
  python3 scripts/review_rounds_cli.py rounds show --pr 2089 [--data-dir /path/to/.vnx-data] [--json]
  python3 scripts/review_rounds_cli.py cap-verdict --pr 2089 --for-dispatch 20261020-fix-x [--json]
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
EXIT_AFTERMATH_INCOMPLETE = 3

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
    verdict = sub.add_parser("cap-verdict", help="the tiebreaker at the B3 cap: one outcome for a capped PR")
    verdict.add_argument("--pr", type=int, required=True, help="GitHub PR number")
    verdict.add_argument("--for-dispatch", default="", help="the dispatch that carries out a verkleinen")
    verdict.add_argument("--data-dir", default=None, help="the project's .vnx-data (default: this project's)")
    verdict.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _data_dir(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from vnx_paths import ensure_env

    return Path(ensure_env()["VNX_DATA_DIR"])


def _emit_receipt(state_dir: Path, line, *, ledger: Path, evidence, released: bool = False) -> None:
    """One ``review_round_opened`` receipt through the receipt API, into this store's receipt ledger."""
    from governance_receipts import emit_governance_receipt
    from review_rounds import RECEIPTS_NAME

    emit_governance_receipt(
        RECEIPT_EVENT, receipt_kind="state_mutation", receipts_file=str(state_dir / RECEIPTS_NAME),
        pr_number=line.pr_number, basis=line.basis, actor=line.actor, reason=line.reason,
        ledger=str(ledger), evidence=list(evidence), opened_at=line.recorded_at,
        **({"released": True} if released else {}),
        **({"project_id": line.project_id} if line.project_id else {}),
    )


def _state_dir(args, command: str) -> Optional[Path]:
    import review_rounds

    try:
        return review_rounds.store_state_dir(_data_dir(args.data_dir) if args.data_dir else None)
    except review_rounds.StoreConflict as exc:
        print(f"vnx review {command}: refused: {exc}", file=sys.stderr)
        return None


def _cap_verdict(args) -> int:
    """``cap-verdict``: the tiebreaker's verdict on a capped PR, booked once (receipt, then row), then carried out."""
    import review_cap_aftermath
    import review_cap_tiebreaker
    import review_cap_verdicts

    if args.pr <= 0:
        print("vnx review cap-verdict: --pr must be a positive PR number", file=sys.stderr)
        return EXIT_ERROR
    state_dir = _state_dir(args, "cap-verdict")
    if state_dir is None:
        return EXIT_ERROR
    try:
        decision = review_cap_tiebreaker.decide(
            state_dir, args.pr, for_dispatch=args.for_dispatch.strip(), data_dir=str(_data_dir(args.data_dir)),
        )
    except review_cap_verdicts.CapVerdictReceiptError as exc:
        print(f"vnx review cap-verdict: PR #{args.pr}: {exc}. Run it again", file=sys.stderr)
        return EXIT_RECEIPT_FAILED
    except (review_cap_tiebreaker.CapTiebreakerError, review_cap_tiebreaker.TiebreakerParseError,
            review_cap_tiebreaker.TiebreakerNoAnswerError, review_cap_verdicts.CapVerdictError,
            review_cap_verdicts.CapVerdictRefused) as exc:
        print(f"vnx review cap-verdict: no verdict recorded for PR #{args.pr}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    verdict, aftermath = decision.verdict, None
    if verdict.outcome == review_cap_verdicts.TERUG_NAAR_PLAN:
        aftermath = review_cap_aftermath.carry_out(state_dir, verdict)
        verdict = aftermath.verdict
    if args.json:
        extra = {"aftermath": aftermath.to_dict()} if aftermath is not None else {}
        print(json.dumps({"status": decision.status, **verdict.to_dict(), **extra}, indent=2))
    else:
        _print_cap_verdict(args.pr, decision.status, verdict, aftermath)
    if aftermath is None:
        return EXIT_OK
    _warn_unreturned(args.pr, aftermath)
    if aftermath.complete:
        return EXIT_OK
    print(
        f"vnx review cap-verdict: PR #{args.pr}: {verdict.verdict_id} stands, its aftermath is incomplete: "
        f"{aftermath.failed}. Run it again: only the missing steps run", file=sys.stderr,
    )
    return EXIT_AFTERMATH_INCOMPLETE


def _print_cap_verdict(pr: int, status: str, verdict, aftermath) -> None:
    """The verdict in one line; for ``terug-naar-plan`` one more line per aftermath step."""
    import review_cap_aftermath
    import review_cap_verdicts

    head = f"PR #{pr} {status} {verdict.verdict_id}: {verdict.cap_rule} -> {verdict.outcome}"
    if verdict.outcome == review_cap_verdicts.VERKLEINEN:
        print(f"{head}, one extra codex-seat round for dispatch {verdict.granted_dispatch_id}: {verdict.intervention}")
        return
    if aftermath is None:
        print(f"{head} (chosen {verdict.chosen_outcome}; the cap stays closed, defer with an OI by hand)")
        return
    print(f"{head}: the PR goes back to plan; aftermath {'complete' if aftermath.complete else 'INCOMPLETE'}")
    for step in aftermath.steps:
        ids = step.fields.get("oi_ids") if step.step == review_cap_aftermath.STEP_OPEN_ITEMS else None
        listed = f" {', '.join(ids)}" if ids else ""
        print(f"  {step.step}: {step.result}{listed}" + (f" ({step.detail})" if step.detail else ""))
    if not aftermath.complete:
        print(f"  not done: {aftermath.failed}")


def _warn_unreturned(pr: int, aftermath) -> None:
    """Say loudly on stderr when the PR's deliverable did not go back to plan."""
    import review_cap_aftermath

    step = aftermath.step(review_cap_aftermath.STEP_DELIVERABLE)
    if step is None or step.result in ("returned", "already_proposed"):
        return
    what = "links to no deliverable" if step.result == review_cap_aftermath.NO_LINKED_DELIVERABLE else (
        f"has deliverable {step.fields.get('deliverable')}, which did not go back to plan"
    )
    print(
        f"vnx review cap-verdict: WARNING: PR #{pr} {what}: {step.detail}. Move the deliverable by hand", file=sys.stderr,
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
    if args.noun == "cap-verdict":
        return _cap_verdict(args)
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
            evidence=len(evidence), actor=getpass.getuser(), reason=reason, results_dir=review_gates / "results",
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
        _emit_receipt(state_dir, line, ledger=ledger, evidence=evidence, released=status == "released")
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
            f"PR #{args.pr} {status} in {ledger} (basis attested, actor {line.actor}): the cap starts at zero here; "
            f"{len(evidence)} earlier-review entr{'y' if len(evidence) == 1 else 'ies'} not counted"
            + (f", first: {evidence[0]}" if evidence else "")
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
