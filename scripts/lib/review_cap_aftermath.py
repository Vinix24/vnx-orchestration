"""The aftermath of a carried-out verdict at the B3 review cap (R2b, R2c, ADR-041).

Track ``review-stopregel-en-lichte-stoelen``, plan section 2. B3.1 says the PR
has a design problem; when the tiebreaker picks ``terug-naar-plan`` (or the
rule leaves only that after a failed extra round), the verdict's row stands
first (``review_cap_verdicts.record_verdict``: receipt, then row). Then three
steps run, in this order:

1. **Open items.** One open item per remaining codex blocker on the head the
   verdict decided on, with the severity codex gave, its title and its
   ``file:line``, read from the gate result record of that head: the record
   whose head and report are the codex-seat verdict the round ledger counted.
   Never from the model, which never sees or gives findings. The ids go into
   the verdict's ``oi_ids``.
2. **The deliverable goes back to ``proposed``.** The PR's branch
   ``dispatch/<id>`` names its dispatch, the dispatch's ``output_ref`` its
   deliverable (``planning_cli.linked_deliverable``); a ``ready`` deliverable
   moves back with a ``deliverable_returned_to_plan`` event whose actor is the
   verdict id (``planning_cli.return_deliverable_to_plan``). No linked
   deliverable is said loudly, in the output and in the receipt, and the next
   step still runs.
3. **The PR closes** with one comment naming the verdict, the cap rule, the
   open items and where the deliverable went (``gate_recorder.close_pr``). A
   PR that is closed or merged already is left as it is. The PR is looked up
   and closed in the repo of the store's project
   (``forge_project_target.resolve_forge_target``: store, project id,
   registered checkout, origin), never in the cwd's: a PR number names a PR
   only together with its repo. When that repo cannot be established nothing
   is closed and the step says why. The comment carries the verdict id in a
   marker line (:func:`comment_marker`), and the PR's comments are read
   before it is closed: ``gh`` posts the comment before it closes, so a close
   that failed can leave the comment behind, and the retry then closes
   without a second one.

**One receipt per step** (``review_cap_aftermath``, field ``step``), written
only when the ledger holds none for that step of this verdict and project, and
before the step's write to derived state (ADR-005): step 1's receipt names the
open-item ids before they go onto the verdict's row, and step 2's receipt is
written inside the deliverable's transaction, before it commits. A receipt
that does not land leaves no derived write. Step 3 closes the PR on GitHub
first and books the receipt after it. Per step and not one for the whole
aftermath: a crash between two steps then leaves every change that was made
with its receipt, and a step that keeps failing (the PR cannot be closed) does
not keep the receipts of the steps before it back.

**A receipted step is never run again.** A repeat of ``vnx review
cap-verdict`` restores it from its receipt and runs only the missing steps:
step 1's open-item ids go onto the row when they are not there yet, and the
result record is not read again (a later gate run replaces it); step 2's
journaled move is applied when its commit did not land; step 3 is done. A step
without a receipt runs, idempotent on the verdict id: an open item's
``dedup_key`` is ``review-cap:<project>:<verdict id>:blocker-<n>``, with ``n``
the finding's place in the result record of the counted verdict (a record of
another head or report is refused, never read as the source); the
deliverable's event names the verdict as its actor; the PR's state and its
comments are read before it is closed.

**``mergen-met-OI`` (R2c).** B3.2 says the findings did not converge. The same
step 1 runs (same dedup key, same receipt-first order, same idempotence), and
nothing else goes back: the deliverable stays where it is and the PR stays
open. A closing record with ``open_items_written`` says the aftermath is done;
the merge itself still needs an operator approval bound to this verdict, its
PR and its head (``review_cap_verdicts.merge_approval``).

The whole aftermath runs under one store lock, so two runs of the command do
not both close the PR. A step that does not finish raises
:class:`AftermathError` inside and ends the aftermath there: the verdict
stands, the steps before it stand, and a repeat finishes it.

BILLING SAFETY: no Anthropic SDK; the only network is ``gh`` for the PR.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import review_cap_verdicts as rcv
import review_rounds

AFTERMATH_EVENT = "review_cap_aftermath"
STEP_OPEN_ITEMS = "open_items"
STEP_DELIVERABLE = "deliverable"
STEP_PR_CLOSE = "pr_close"
STEP_MERGE_READY = "merge_ready"
OI_SOURCE = "review_cap_tiebreaker"
NO_LINKED_DELIVERABLE = "no_linked_deliverable"
NOT_APPLIED = "not_applied"
# What a step's receipt carries of its result, read back when a later run restores the step.
_STEP_FIELDS = {
    STEP_OPEN_ITEMS: ("oi_ids", "result_record"),
    STEP_DELIVERABLE: ("deliverable", "output_ref", "pr_dispatch_id", "from_state", "to_state", "branch", "track"),
    STEP_PR_CLOSE: ("pr_state", "repo", "comment"),
    STEP_MERGE_READY: ("open_items_written",),
}
_DISPATCH_BRANCH = "dispatch/"
_BRANCH_PREFIXES = ("refs/heads/", "origin/")
_SUMMARY_CHARS = 140
# The open-items scale (blocker | warn | info) of a severity codex gave. A
# blocking finding whose severity is none of these stays a blocker.
_OI_SEVERITY = {
    "blocker": "blocker", "blocking": "blocker", "critical": "blocker", "error": "blocker", "high": "blocker",
    "warn": "warn", "warning": "warn", "medium": "warn",
    "info": "info", "low": "info", "minor": "info", "note": "info",
}

class AftermathError(RuntimeError):
    """A step of the aftermath did not finish: the verdict and the steps before it stand, run it again."""


@dataclass(frozen=True)
class StepResult:
    """What one step did: ``result`` names it, ``fields`` go into the step's receipt.

    ``restored`` marks a step this run did not execute: its receipt stood, and
    the result is read back from it.
    """

    step: str
    result: str
    detail: str = ""
    fields: Dict[str, Any] = field(default_factory=dict)
    restored: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"step": self.step, "result": self.result, "detail": self.detail, "restored": self.restored, **self.fields}


@dataclass(frozen=True)
class Aftermath:
    """The steps that ran for one carried-out verdict; ``failed`` names the one that did not finish."""

    verdict: rcv.CapVerdict
    steps: Tuple[StepResult, ...] = ()
    failed: str = ""

    @property
    def complete(self) -> bool:
        return not self.failed

    def step(self, name: str) -> Optional[StepResult]:
        return next((result for result in self.steps if result.step == name), None)

    def to_dict(self) -> Dict[str, Any]:
        return {"complete": self.complete, "failed": self.failed, "steps": [step.to_dict() for step in self.steps]}


def oi_severity(codex_severity: Any) -> str:
    """The open-items severity of a blocking finding's ``severity``."""
    return _OI_SEVERITY.get(str(codex_severity or "").strip().lower(), "blocker")


def dedup_key(verdict: rcv.CapVerdict, index: int) -> str:
    """The open item of the ``index``-th (1-based) blocking finding of the verdict's head."""
    return f"review-cap:{verdict.project_id}:{verdict.verdict_id}:blocker-{index}"


def carry_out(state_dir: Path, verdict: rcv.CapVerdict) -> Aftermath:
    """Run what is missing of the aftermath of ``verdict``, a carried-out verdict.

    ``mergen-met-OI`` has its own step set (:func:`carry_out_merge_with_oi`).
    ``terug-naar-plan`` runs the steps below. Returns the :class:`Aftermath`,
    with ``failed`` set when a step did not finish. Raises ``ValueError`` for a
    verdict of another outcome: nothing else has an aftermath.
    """
    from atomic_io import slot_lock

    if verdict.outcome == rcv.MERGEN_MET_OI:
        return carry_out_merge_with_oi(state_dir, verdict)
    if verdict.outcome != rcv.TERUG_NAAR_PLAN:
        raise ValueError(
            f"{verdict.verdict_id} is {verdict.outcome}: only terug-naar-plan and mergen-met-OI "
            "have an aftermath"
        )
    done: List[StepResult] = []
    with slot_lock(Path(state_dir) / AFTERMATH_EVENT):
        try:
            booked = _booked_steps(state_dir, verdict)
            counted = _CountedRound(state_dir, verdict)
            items = booked.get(STEP_OPEN_ITEMS)
            done.append(
                _restore_open_items(state_dir, verdict, items) if items
                else _open_items_step(state_dir, verdict, counted.line())
            )
            verdict = replace(verdict, oi_ids=tuple(done[-1].fields["oi_ids"]))
            moved = booked.get(STEP_DELIVERABLE)
            done.append(
                _restore_deliverable(state_dir, verdict, moved) if moved
                else _deliverable_step(state_dir, verdict, counted.line())
            )
            closed = booked.get(STEP_PR_CLOSE)
            done.append(
                _restored(closed) if closed
                else _pr_close_step(state_dir, verdict, done[-1])
            )
        except AftermathError as exc:
            return Aftermath(verdict, tuple(done), failed=str(exc))
    return Aftermath(verdict, tuple(done))


def carry_out_merge_with_oi(state_dir: Path, verdict: rcv.CapVerdict) -> Aftermath:
    """The aftermath of ``mergen-met-OI``: the open items, then the record that it finished (R2c).

    Nothing goes back: the deliverable stands and the PR stays open. Both steps
    are receipted and idempotent on the verdict id, as the R2b steps are.
    """
    from atomic_io import slot_lock

    if verdict.outcome != rcv.MERGEN_MET_OI:
        raise ValueError(f"{verdict.verdict_id} is {verdict.outcome}: only mergen-met-OI has this aftermath")
    done: List[StepResult] = []
    with slot_lock(Path(state_dir) / AFTERMATH_EVENT):
        try:
            booked = _booked_steps(state_dir, verdict)
            counted = _CountedRound(state_dir, verdict)
            items = booked.get(STEP_OPEN_ITEMS)
            done.append(
                _restore_open_items(state_dir, verdict, items) if items
                else _open_items_step(state_dir, verdict, counted.line())
            )
            verdict = replace(verdict, oi_ids=tuple(done[-1].fields["oi_ids"]))
            ready = booked.get(STEP_MERGE_READY)
            done.append(_restored(ready) if ready else _booked(state_dir, verdict, _merge_ready(verdict)))
        except AftermathError as exc:
            return Aftermath(verdict, tuple(done), failed=str(exc))
    return Aftermath(verdict, tuple(done))


def _merge_ready(verdict: rcv.CapVerdict) -> StepResult:
    """The closing record of the merge-with-open-items aftermath: its ``open_items_written``.

    A half-written aftermath has no such record; the merge door counts the
    verdict by it.
    """
    written = len(verdict.oi_ids)
    return StepResult(
        STEP_MERGE_READY, "ready",
        detail=(
            f"{written} remaining codex blocker(s) of {verdict.verdict_id} on head {verdict.head_sha[:12]} are "
            f"open items; the merge still needs an operator approval bound to this verdict, PR "
            f"#{verdict.pr_number} and head {verdict.head_sha[:12]}"
        ),
        fields={"open_items_written": written},
    )


# ---------------------------------------------------------------------------
# The counted verdict the aftermath reads from
# ---------------------------------------------------------------------------


class _CountedRound:
    """The counted round of :func:`_counted_round`, read once and only when a step that runs needs it."""

    def __init__(self, state_dir: Path, verdict: rcv.CapVerdict) -> None:
        self._state_dir, self._verdict = state_dir, verdict
        self._line: Optional[review_rounds.RoundLine] = None

    def line(self) -> review_rounds.RoundLine:
        if self._line is None:
            self._line = _counted_round(self._state_dir, self._verdict)
        return self._line


def _counted_round(state_dir: Path, verdict: rcv.CapVerdict) -> review_rounds.RoundLine:
    """The codex-seat round the round ledger counted on the verdict's head: its reviewer, report and branch."""
    review_gates = Path(state_dir) / "review_gates"
    try:
        ledger = review_rounds.read_ledger(review_rounds.ledger_path(review_gates))
    except review_rounds.RoundLedgerError as exc:
        raise AftermathError(f"the round ledger cannot be read: {exc}") from exc
    unreadable = review_rounds.unreadable_text(ledger, verdict.pr_number)
    if unreadable:
        raise AftermathError(unreadable)
    lines = ledger.for_pr(review_rounds.store_project_id(review_gates), verdict.pr_number)
    found = next(
        (line for line in review_rounds.codex_rounds(lines) if line.head_sha == verdict.head_sha), None,
    )
    if found is None:
        raise AftermathError(
            f"the round ledger counts no codex-seat verdict of PR #{verdict.pr_number} on head "
            f"{verdict.head_sha[:12]}: the remaining blockers of {verdict.verdict_id} have no record to come from"
        )
    return found


def _blocking_findings(state_dir: Path, verdict: rcv.CapVerdict, line: review_rounds.RoundLine) -> Sequence[Any]:
    """The ``blocking_findings`` of the result record of the counted verdict on the verdict's head."""
    path = Path(state_dir) / "review_gates" / "results" / f"pr-{verdict.pr_number}-{line.reviewer}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AftermathError(f"the result record {path} cannot be read ({exc}): no open items") from exc
    if not isinstance(record, dict):
        raise AftermathError(f"the result record {path} is no JSON object: no open items")
    head, report = str(record.get("commit_sha") or ""), Path(str(record.get("report_path") or "")).name
    if (head, report) != (verdict.head_sha, line.report_name):
        raise AftermathError(
            f"the result record {path} holds head {head[:12] or 'none'}, report {report or 'none'}; the round "
            f"ledger counted head {verdict.head_sha[:12]}, report {line.report_name}. The remaining blockers are "
            f"not read from another verdict: no open items"
        )
    findings = record.get("blocking_findings")
    if not isinstance(findings, list):
        raise AftermathError(f"the result record {path} has no blocking_findings list: no open items")
    return findings


# ---------------------------------------------------------------------------
# Step 1: one open item per remaining codex blocker
# ---------------------------------------------------------------------------


def _finding_parts(finding: Any) -> Tuple[str, str, str, str]:
    """``(severity, title, message, location)`` of one blocking finding; ``location`` is ``file:line`` or ``file``."""
    if not isinstance(finding, dict):
        return "", "", str(finding).strip(), ""
    message = str(finding.get("message") or finding.get("description") or "").strip()
    where = str(finding.get("file_path") or finding.get("file") or "").strip()
    line = finding.get("line")
    location = f"{where}:{line}" if where and line not in (None, "") else where
    return str(finding.get("severity") or ""), str(finding.get("title") or "").strip(), message, location


def _oi_titles(verdict: rcv.CapVerdict, index: int, total: int, title: str, message: str, location: str) -> Tuple[str, str]:
    """The item's title, and the plain one used when the open-items guard reads the first as a passed check."""
    plain = f"PR #{verdict.pr_number} codex blocker {index}/{total}" + (f" at {location}" if location else "")
    summary = (title or message).splitlines()[0].strip() if (title or message).strip() else ""
    if len(summary) > _SUMMARY_CHARS:
        summary = summary[: _SUMMARY_CHARS - 3].rstrip() + "..."
    return (f"{plain}: {summary}" if summary else plain), f"{plain} ({verdict.verdict_id})"


def _add_open_item(oim: Any, verdict: rcv.CapVerdict, line: review_rounds.RoundLine, finding: Any,
                   index: int, total: int) -> Tuple[str, bool]:
    severity, title, message, location = _finding_parts(finding)
    first, plain = _oi_titles(verdict, index, total, title, message, location)
    details = (
        f"Remaining blocker of the codex seat ({line.reviewer}) on head {verdict.head_sha[:12]} of PR "
        f"#{verdict.pr_number}, carried into an open item by the review-cap tiebreaker: verdict "
        f"{verdict.verdict_id} ({verdict.cap_rule} -> {verdict.outcome}). Severity codex gave: {severity or 'none'}. "
        f"Location: {location or 'none'}. Report: {line.report_path}.\n\n{message}"
    )
    fields = dict(
        severity=oi_severity(severity), dispatch_id=verdict.verdict_id, report_path=line.report_path,
        pr_id=str(verdict.pr_number), details=details, dedup_key=dedup_key(verdict, index), source=OI_SOURCE,
    )
    try:
        return oim.add_item_programmatic(title=first, **fields)
    except ValueError:
        return oim.add_item_programmatic(title=plain, **fields)


def _open_items_step(state_dir: Path, verdict: rcv.CapVerdict, line: review_rounds.RoundLine) -> StepResult:
    """Step 1: the open items, the step's receipt naming their ids, then the ids on the verdict's row (ADR-005)."""
    import open_items_manager as oim
    from plan_gate_tiebreaker import _open_items_store_override

    findings = _blocking_findings(state_dir, verdict, line)
    added: List[Tuple[str, bool]] = []
    try:
        with _open_items_store_override(oim, str(state_dir)):
            for index, finding in enumerate(findings, start=1):
                added.append(_add_open_item(oim, verdict, line, finding, index, len(findings)))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AftermathError(
            f"open item {len(added) + 1} of {len(findings)} could not be written ({type(exc).__name__}: {exc}); "
            f"the ones before it stand and are found again by their key"
        ) from exc
    oi_ids = [item_id for item_id, _created in added]
    created = sum(1 for _item_id, was_new in added if was_new)
    result = StepResult(
        STEP_OPEN_ITEMS, "created",
        detail=f"{len(oi_ids)} remaining codex blocker(s) on head {verdict.head_sha[:12]}"
        + (f", {len(oi_ids) - created} found again by their key" if created < len(oi_ids) else ""),
        fields={"oi_ids": oi_ids, "result_record": f"pr-{verdict.pr_number}-{line.reviewer}.json"},
    )
    _receipt_once(state_dir, verdict, result)
    _oi_ids_on_row(state_dir, verdict, oi_ids)
    return result


def _restore_open_items(state_dir: Path, verdict: rcv.CapVerdict, receipt: Dict[str, Any]) -> StepResult:
    """Step 1 as its receipt booked it: the ids it names, onto the row when they are not there yet.

    The result record is not read again: a later gate run on the PR replaces
    it, and the open items stand already under the ids the receipt names.
    """
    result = _restored(receipt)
    oi_ids = result.fields.get("oi_ids")
    if not isinstance(oi_ids, list) or not all(isinstance(item_id, str) for item_id in oi_ids):
        raise AftermathError(
            f"the {AFTERMATH_EVENT} receipt of step {STEP_OPEN_ITEMS} of {verdict.verdict_id} names no list of "
            f"open-item ids ({oi_ids!r}): the row cannot be booked from it"
        )
    if tuple(oi_ids) != verdict.oi_ids:
        _oi_ids_on_row(state_dir, verdict, oi_ids)
    return result


def _oi_ids_on_row(state_dir: Path, verdict: rcv.CapVerdict, oi_ids: Sequence[str]) -> None:
    try:
        rcv.set_oi_ids(state_dir, verdict, oi_ids)
    except rcv.CapVerdictError as exc:
        raise AftermathError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Step 2: the deliverable goes back to proposed
# ---------------------------------------------------------------------------


def branch_dispatch(branch: str) -> str:
    """The dispatch a PR branch ``dispatch/<id>`` names; ``""`` for any other branch."""
    name = (branch or "").strip()
    for prefix in _BRANCH_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name[len(_DISPATCH_BRANCH):].strip() if name.startswith(_DISPATCH_BRANCH) else ""


def _deliverable_step(state_dir: Path, verdict: rcv.CapVerdict, line: review_rounds.RoundLine) -> StepResult:
    """Step 2: the PR's deliverable back to ``proposed``, its receipt before the move commits; or loudly none."""
    import planning_cli

    dispatch_id = branch_dispatch(line.branch)
    if not dispatch_id:
        return _booked(state_dir, verdict, StepResult(
            STEP_DELIVERABLE, NO_LINKED_DELIVERABLE,
            detail=f"the PR's branch {line.branch!r} is no dispatch/<id> branch, so it names no dispatch",
            fields={"branch": line.branch},
        ))
    try:
        link = planning_cli.linked_deliverable(state_dir, verdict.project_id, dispatch_id)
        if not link.dispatch_id:
            return _booked(state_dir, verdict, StepResult(
                STEP_DELIVERABLE, NO_LINKED_DELIVERABLE, detail=link.reason,
                fields={"pr_dispatch_id": dispatch_id, "track": link.track},
            ))
        return _return(state_dir, verdict, link, dispatch_id, planning_cli)
    except sqlite3.Error as exc:
        raise AftermathError(f"the deliverable of PR #{verdict.pr_number} could not be read or moved: {exc}") from exc


def _return(state_dir: Path, verdict: rcv.CapVerdict, link: Any, dispatch_id: str, planning_cli: Any) -> StepResult:
    """The move back to plan, journaled: the step's receipt lands inside the move's transaction, before its commit.

    ADR-005. A receipt that does not land rolls the move back; a commit that
    does not land after it leaves the receipt, and the next run applies the
    move it journals (:func:`_restore_deliverable`).
    """
    journaled: List[StepResult] = []

    def journal(status: str, from_state: str) -> None:
        journaled.append(_returned_result(link, dispatch_id, status, from_state))
        _receipt_once(state_dir, verdict, journaled[-1])

    planning_cli.return_deliverable_to_plan(
        state_dir, verdict.project_id, link.dispatch_id, actor=verdict.verdict_id, reason=_return_reason(verdict),
        metadata=_return_metadata(verdict, dispatch_id), journal=journal,
    )
    return journaled[-1]


def _return_reason(verdict: rcv.CapVerdict) -> str:
    return (
        f"review-cap tiebreaker {verdict.cap_rule} -> terug-naar-plan on PR #{verdict.pr_number} "
        f"(verdict {verdict.verdict_id}): back to plan, it needs the operator's promote again"
    )


def _return_metadata(verdict: rcv.CapVerdict, dispatch_id: str) -> Dict[str, Any]:
    return {
        "verdict_id": verdict.verdict_id, "pr_number": verdict.pr_number, "cap_rule": verdict.cap_rule,
        "head_sha": verdict.head_sha, "pr_dispatch_id": dispatch_id, "oi_ids": list(verdict.oi_ids),
    }


def _restore_deliverable(state_dir: Path, verdict: rcv.CapVerdict, receipt: Dict[str, Any]) -> StepResult:
    """Step 2 as its receipt journaled it; a move whose commit did not land is applied from the receipt.

    Never decided again: only the deliverable the receipt names moves, and
    only when the receipt journals a move. A deliverable that no longer stands
    where the receipt left it is not moved, and the step says so (result
    ``not_applied``), loudly in the CLI and in the PR's comment.
    """
    import planning_cli

    result = _restored(receipt)
    deliverable = str(result.fields.get("deliverable") or "")
    if result.result != "returned" or not deliverable:
        return result
    try:
        status, now = planning_cli.return_deliverable_to_plan(
            state_dir, verdict.project_id, deliverable, actor=verdict.verdict_id, reason=_return_reason(verdict),
            metadata=_return_metadata(verdict, str(result.fields.get("pr_dispatch_id") or "")),
        )
    except sqlite3.Error as exc:
        raise AftermathError(
            f"deliverable {deliverable} could not be moved as the receipt of {verdict.verdict_id} journals: {exc}"
        ) from exc
    if status in ("returned", "already_returned", "already_proposed"):
        return result
    return replace(result, result=NOT_APPLIED, detail=(
        f"the receipt of {verdict.verdict_id} journals ready -> proposed, but the move did not commit and the "
        f"deliverable stands {now or 'nowhere'} now ({status}): not moved"
    ))


def _returned_result(link: Any, dispatch_id: str, status: str, from_state: str) -> StepResult:
    moved = status in ("returned", "already_returned")
    detail = {
        "returned": "ready -> proposed",
        "already_returned": "ready -> proposed (moved by an earlier run of this verdict)",
        "already_proposed": "it stood proposed already: nothing to move",
        "not_returnable": f"it stands {from_state}: only a ready deliverable goes back to plan, not moved",
        "not_found": "its row is gone: not moved",
    }[status]
    return StepResult(
        STEP_DELIVERABLE, "returned" if moved else status, detail=detail,
        fields={
            "deliverable": link.dispatch_id, "output_ref": link.output_ref, "pr_dispatch_id": dispatch_id,
            "from_state": from_state, "to_state": "proposed" if moved or status == "already_proposed" else from_state,
        },
    )


# ---------------------------------------------------------------------------
# Step 3: the PR closes
# ---------------------------------------------------------------------------


def close_comment(verdict: rcv.CapVerdict, deliverable: StepResult) -> str:
    """The one comment the PR closes with: the verdict, the cap rule, the open items and the deliverable."""
    items = ", ".join(verdict.oi_ids) or "none"
    if deliverable.result == "returned" or deliverable.result == "already_proposed":
        where = (
            f"Deliverable {deliverable.fields['deliverable']} went back to plan "
            f"({deliverable.fields['from_state']} -> proposed): it needs the operator's promote again."
        )
    elif deliverable.result == NO_LINKED_DELIVERABLE:
        where = f"No deliverable is linked to this PR, so none went back to plan: {deliverable.detail}."
    else:
        where = f"Deliverable {deliverable.fields.get('deliverable')} did not go back to plan: {deliverable.detail}."
    return (
        f"Closed by the B3 review-cap tiebreaker: verdict {verdict.verdict_id} ({verdict.cap_rule} -> "
        f"terug-naar-plan). The remaining codex blockers on head {verdict.head_sha[:12]} are open items: {items}. "
        f"{where}\n\n{comment_marker(verdict)}"
    )


def comment_marker(verdict: rcv.CapVerdict) -> str:
    """The line of the close comment that names its verdict: one comment per verdict id, found again by it."""
    return f"<!-- vnx-review-cap-verdict: {verdict.verdict_id} -->"


def _pr_close_step(state_dir: Path, verdict: rcv.CapVerdict, deliverable: StepResult) -> StepResult:
    """Step 3: in the store's repo, close the PR with its comment unless it is closed already, then the receipt."""
    import gate_recorder

    repo = _store_repo(state_dir, verdict)
    state = gate_recorder.get_pr_state(verdict.pr_number, repo=repo)
    if state in ("CLOSED", "MERGED"):
        result = StepResult(
            STEP_PR_CLOSE, f"already_{state.lower()}",
            detail=f"PR #{verdict.pr_number} in {repo} stands {state}: left as it is",
            fields={"pr_state": state, "repo": repo},
        )
    elif state != "OPEN":
        raise AftermathError(
            f"the state of PR #{verdict.pr_number} in {repo} cannot be read from GitHub ({state or 'no answer'}): "
            f"it is not closed"
        )
    else:
        result = _close(verdict, deliverable, repo)
    _receipt_once(state_dir, verdict, result)
    return result


def _store_repo(state_dir: Path, verdict: rcv.CapVerdict) -> str:
    """The ``owner/name`` of the store's project: the repo the PR is looked up and closed in, never the cwd's."""
    from forge_check_run import ForgePublishRefused
    from forge_project_target import resolve_forge_target

    try:
        return resolve_forge_target(Path(state_dir) / "review_gates" / "results").owner_repo
    except ForgePublishRefused as exc:
        raise AftermathError(
            f"the repo of PR #{verdict.pr_number} cannot be established from the store {state_dir}, so it is "
            f"not looked up and not closed: {exc}"
        ) from exc


def _close(verdict: rcv.CapVerdict, deliverable: StepResult, repo: str) -> StepResult:
    """Close the open PR, with the comment only when no comment of this verdict stands on it yet."""
    import gate_recorder

    bodies = gate_recorder.get_pr_comment_bodies(verdict.pr_number, repo=repo)
    if bodies is None:
        raise AftermathError(
            f"the comments of PR #{verdict.pr_number} in {repo} cannot be read from GitHub: it is not closed, "
            f"so no second comment of {verdict.verdict_id} is posted"
        )
    marker = comment_marker(verdict)
    posted = any(line.strip() == marker for body in bodies for line in body.splitlines())
    closed, why = gate_recorder.close_pr(
        verdict.pr_number, "" if posted else close_comment(verdict, deliverable), repo=repo,
    )
    if not closed:
        raise AftermathError(f"gh pr close {verdict.pr_number} --repo {repo} failed: {why}")
    detail = f"PR #{verdict.pr_number} in {repo} closed with a comment naming {verdict.verdict_id}"
    if posted:
        detail += ", posted by an earlier close that failed after it: not posted again"
    return StepResult(
        STEP_PR_CLOSE, "closed", detail=detail,
        fields={"pr_state": "CLOSED", "repo": repo, "comment": "already_posted" if posted else "posted"},
    )


# ---------------------------------------------------------------------------
# One receipt per step
# ---------------------------------------------------------------------------


def step_receipts(state_dir: Path, verdict: rcv.CapVerdict) -> List[Dict[str, Any]]:
    """The ``review_cap_aftermath`` receipts of ``verdict`` in its own project (ADR-007), in file order."""
    return [
        receipt for receipt in rcv.receipts_naming(state_dir, AFTERMATH_EVENT, verdict.verdict_id)
        if (receipt.get("verdict_id"), receipt.get("project_id"), receipt.get("pr_number"))
        == (verdict.verdict_id, verdict.project_id, verdict.pr_number)
    ]


def _booked_steps(state_dir: Path, verdict: rcv.CapVerdict) -> Dict[str, Dict[str, Any]]:
    """The first receipt of each step of ``verdict`` that the ledger holds: those steps are not run again."""
    try:
        receipts = step_receipts(state_dir, verdict)
    except rcv.CapVerdictError as exc:
        raise AftermathError(str(exc)) from exc
    booked: Dict[str, Dict[str, Any]] = {}
    for receipt in receipts:
        if receipt.get("step") in _STEP_FIELDS:
            booked.setdefault(receipt["step"], receipt)
    return booked


def _restored(receipt: Dict[str, Any]) -> StepResult:
    """A step's result as its receipt booked it."""
    step = receipt["step"]
    return StepResult(
        step, str(receipt.get("result") or ""), detail=str(receipt.get("detail") or ""),
        fields={name: receipt[name] for name in _STEP_FIELDS[step] if name in receipt}, restored=True,
    )


def _booked(state_dir: Path, verdict: rcv.CapVerdict, result: StepResult) -> StepResult:
    """``result`` once its receipt stands: for a step whose result writes no derived state."""
    _receipt_once(state_dir, verdict, result)
    return result


def _receipt_once(state_dir: Path, verdict: rcv.CapVerdict, result: StepResult) -> None:
    """The step's receipt, unless the ledger already holds one for this step of ``verdict``."""
    from governance_receipts import AppendReceiptError, emit_governance_receipt

    try:
        if any(receipt.get("step") == result.step for receipt in step_receipts(state_dir, verdict)):
            return
        # ``section`` is one of the appender's idempotency fields: without it two steps
        # booked in the same second share one key and the second is dropped as a duplicate.
        emit_governance_receipt(
            AFTERMATH_EVENT, receipt_kind="state_mutation",
            receipts_file=str(Path(state_dir) / review_rounds.RECEIPTS_NAME),
            project_id=verdict.project_id, pr_number=verdict.pr_number, verdict_id=verdict.verdict_id,
            head_sha=verdict.head_sha, cap_rule=verdict.cap_rule, step=result.step, section=result.step,
            result=result.result, detail=result.detail, **result.fields,
        )
    except rcv.CapVerdictError as exc:
        raise AftermathError(str(exc)) from exc
    except (AppendReceiptError, OSError, ValueError) as exc:
        raise AftermathError(
            f"the {AFTERMATH_EVENT} receipt of step {result.step} of {verdict.verdict_id} was not written ({exc}); "
            f"the step's change stands"
        ) from exc
