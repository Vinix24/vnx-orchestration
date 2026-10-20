"""The aftermath of a ``terug-naar-plan`` verdict at the B3 review cap (R2b, ADR-041).

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
   PR that is closed or merged already is left as it is.

**Every step is idempotent on the verdict id**, so a repeat of ``vnx review
cap-verdict`` after a crash only does what is missing: an open item's
``dedup_key`` is ``review-cap:<project>:<verdict id>:blocker-<n>``, with ``n``
the finding's place in the result record of that head (the record of one
counted verdict, so the order does not move); the deliverable's event names
the verdict as its actor; the PR's state is read before it is closed.

**One receipt per step** (``review_cap_aftermath``, field ``step``), written
once the step's change stands and only when the ledger holds none for that
step of this verdict and project. Per step and not one for the whole
aftermath: a crash between two steps then leaves every change that was made
with its receipt, and a step that keeps failing (the PR cannot be closed) does
not keep the receipts of the steps before it back.

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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import review_cap_verdicts as rcv
import review_rounds

AFTERMATH_EVENT = "review_cap_aftermath"
STEP_OPEN_ITEMS = "open_items"
STEP_DELIVERABLE = "deliverable"
STEP_PR_CLOSE = "pr_close"
OI_SOURCE = "review_cap_tiebreaker"
NO_LINKED_DELIVERABLE = "no_linked_deliverable"
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

PrStateFn = Callable[[int], str]
ClosePrFn = Callable[[int, str], Tuple[bool, str]]


class AftermathError(RuntimeError):
    """A step of the aftermath did not finish: the verdict and the steps before it stand, run it again."""


@dataclass(frozen=True)
class StepResult:
    """What one step did: ``result`` names it, ``fields`` go into the step's receipt."""

    step: str
    result: str
    detail: str = ""
    fields: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"step": self.step, "result": self.result, "detail": self.detail, **self.fields}


@dataclass(frozen=True)
class Aftermath:
    """The steps that ran for one ``terug-naar-plan`` verdict; ``failed`` names the one that did not finish."""

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


def carry_out(
    state_dir: Path, verdict: rcv.CapVerdict, *, pr_state: Optional[PrStateFn] = None,
    close_pr: Optional[ClosePrFn] = None,
) -> Aftermath:
    """Run what is missing of the aftermath of ``verdict``, a stored ``terug-naar-plan``.

    ``pr_state`` and ``close_pr`` answer the two ``gh`` questions; without them
    ``gate_recorder`` asks ``gh``. Returns the :class:`Aftermath`, with
    ``failed`` set when a step did not finish. Raises ``ValueError`` for a
    verdict of another outcome: nothing else has an aftermath.
    """
    from atomic_io import slot_lock

    if verdict.outcome != rcv.TERUG_NAAR_PLAN:
        raise ValueError(f"{verdict.verdict_id} is {verdict.outcome}: only terug-naar-plan has an aftermath")
    done: List[StepResult] = []
    with slot_lock(Path(state_dir) / AFTERMATH_EVENT):
        try:
            line = _counted_round(state_dir, verdict)
            done.append(_open_items_step(state_dir, verdict, line))
            verdict = replace(verdict, oi_ids=tuple(done[-1].fields["oi_ids"]))
            done.append(_deliverable_step(state_dir, verdict, line))
            done.append(_pr_close_step(state_dir, verdict, done[-1], pr_state=pr_state, close_pr=close_pr))
        except AftermathError as exc:
            return Aftermath(verdict, tuple(done), failed=str(exc))
    return Aftermath(verdict, tuple(done))


# ---------------------------------------------------------------------------
# The counted verdict the aftermath reads from
# ---------------------------------------------------------------------------


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
        f"{verdict.verdict_id} ({verdict.cap_rule} -> terug-naar-plan). Severity codex gave: {severity or 'none'}. "
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
    """Step 1: the open items, their ids on the verdict's row, then the step's receipt."""
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
    try:
        rcv.set_oi_ids(state_dir, verdict, oi_ids)
    except rcv.CapVerdictError as exc:
        raise AftermathError(str(exc)) from exc
    created = sum(1 for _item_id, was_new in added if was_new)
    result = StepResult(
        STEP_OPEN_ITEMS, "created",
        detail=f"{len(oi_ids)} remaining codex blocker(s) on head {verdict.head_sha[:12]}"
        + (f", {len(oi_ids) - created} found again by their key" if created < len(oi_ids) else ""),
        fields={"oi_ids": oi_ids, "result_record": f"pr-{verdict.pr_number}-{line.reviewer}.json"},
    )
    _receipt_once(state_dir, verdict, result)
    return result


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
    """Step 2: the PR's deliverable back to ``proposed``, or loudly none, then the step's receipt."""
    import planning_cli

    dispatch_id = branch_dispatch(line.branch)
    try:
        if not dispatch_id:
            result = StepResult(
                STEP_DELIVERABLE, NO_LINKED_DELIVERABLE,
                detail=f"the PR's branch {line.branch!r} is no dispatch/<id> branch, so it names no dispatch",
                fields={"branch": line.branch},
            )
        else:
            result = _return(state_dir, verdict, dispatch_id, planning_cli)
    except sqlite3.Error as exc:
        raise AftermathError(f"the deliverable of PR #{verdict.pr_number} could not be read or moved: {exc}") from exc
    _receipt_once(state_dir, verdict, result)
    return result


def _return(state_dir: Path, verdict: rcv.CapVerdict, dispatch_id: str, planning_cli: Any) -> StepResult:
    link = planning_cli.linked_deliverable(state_dir, verdict.project_id, dispatch_id)
    if not link.dispatch_id:
        return StepResult(
            STEP_DELIVERABLE, NO_LINKED_DELIVERABLE, detail=link.reason,
            fields={"pr_dispatch_id": dispatch_id, "track": link.track},
        )
    status, from_state = planning_cli.return_deliverable_to_plan(
        state_dir, verdict.project_id, link.dispatch_id, actor=verdict.verdict_id,
        reason=(
            f"review-cap tiebreaker {verdict.cap_rule} -> terug-naar-plan on PR #{verdict.pr_number} "
            f"(verdict {verdict.verdict_id}): back to plan, it needs the operator's promote again"
        ),
        metadata={
            "verdict_id": verdict.verdict_id, "pr_number": verdict.pr_number, "cap_rule": verdict.cap_rule,
            "head_sha": verdict.head_sha, "pr_dispatch_id": dispatch_id, "oi_ids": list(verdict.oi_ids),
        },
    )
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
        f"{where}"
    )


def _pr_close_step(
    state_dir: Path, verdict: rcv.CapVerdict, deliverable: StepResult, *,
    pr_state: Optional[PrStateFn], close_pr: Optional[ClosePrFn],
) -> StepResult:
    """Step 3: close the PR with its comment unless it is closed already, then the step's receipt."""
    import gate_recorder

    state = (pr_state or gate_recorder.get_pr_state)(verdict.pr_number)
    if state in ("CLOSED", "MERGED"):
        result = StepResult(
            STEP_PR_CLOSE, f"already_{state.lower()}", detail=f"PR #{verdict.pr_number} stands {state}: left as it is",
            fields={"pr_state": state},
        )
    elif state != "OPEN":
        raise AftermathError(
            f"the state of PR #{verdict.pr_number} cannot be read from GitHub ({state or 'no answer'}): it is not "
            f"closed"
        )
    else:
        closed, why = (close_pr or gate_recorder.close_pr)(verdict.pr_number, close_comment(verdict, deliverable))
        if not closed:
            raise AftermathError(f"gh pr close {verdict.pr_number} failed: {why}")
        result = StepResult(
            STEP_PR_CLOSE, "closed", detail=f"PR #{verdict.pr_number} closed with a comment naming {verdict.verdict_id}",
            fields={"pr_state": "CLOSED"},
        )
    _receipt_once(state_dir, verdict, result)
    return result


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
