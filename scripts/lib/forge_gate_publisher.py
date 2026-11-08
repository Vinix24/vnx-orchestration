#!/usr/bin/env python3
"""Gate verdict -> GitHub check-run: the judgment layer (Golf B, B2b).

``forge_check_run.py`` (B2a) is the CLIENT: it authenticates as the
``vnx-gate`` GitHub App and posts whatever ``conclusion`` it is handed. It
knows nothing about review gates on purpose. This module is the other half —
which record on disk may become which conclusion, which publications are
refused outright, and the CLI that republishes from a record.

**Why a separate module and not the bottom of forge_check_run.py.** B2a
states, and ``tests/test_forge_check_run_client.py::
test_module_has_no_gate_knowledge`` enforces, that the client file carries no
gate vocabulary at all — that separation is what lets this policy change
without touching a line of transport. Appending the mapping to that file would
have made the claim false and the test red, and a test that goes red because
of a design choice is the design choice being wrong, not the test. So: one
import edge, from here to there, and never back.

**Three conclusions, and the asymmetry is the whole point.** On a required
check GitHub treats ``neutral`` and ``skipped`` as SATISFIED. A review gate
that could not speak must therefore never book either one; it books
``action_required``, which blocks. ``success`` is reserved for a verdict that
is a proven pass on THIS EXACT HEAD, ``failure`` for a gate that ran and said
no. Absence of evidence gets the blocking conclusion, never the permissive
one.

BILLING SAFETY: No Anthropic SDK. No direct API calls to api.anthropic.com.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
_SCRIPTS_DIR = _LIB_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
# scripts/forge holds apply_branch_protection.py, whose build_put_payload the
# pending-promotion rehearsal reuses. Importing it beats rebuilding the PUT
# body here: a second copy would show the operator an object the real apply
# does not send.
_FORGE_DIR = _SCRIPTS_DIR / "forge"
if str(_FORGE_DIR) not in sys.path:
    sys.path.insert(0, str(_FORGE_DIR))

from forge_check_run import (  # noqa: E402
    RUNBOOK_PATH,
    AppConfig,
    ForgeAPIError,
    ForgeAppConfigError,
    ForgeCheckRunError,
    ForgeKeychainError,
    ForgePublishRefused,
    list_check_runs,
    load_app_config,
    patch_check_run,
    publish_check_run,
)
from forge_project_target import ForgeTarget, resolve_forge_target
from governance_receipts import emit_governance_receipt
# The door splits its own runs from foreign ones with the reader's rule itself
# (ADR-061 §3), imported and not copied: a copy could drift from the reader and
# turn a state the reader blocks on into a permit again.
from ci_contexts import _split_by_app
from gate_status import (  # noqa: E402
    ALL_KNOWN_STATES,
    FAIL_STATES,
    INCOMPLETE_STATES,
    PARTIAL_REVIEW_STATES,
    PASS_STATES,
    UNAVAILABLE_STATES,
    canonical_status,
    is_test_run_record,
)

# Re-exported so a caller that already imports this module never needs a second
# import of the client just to catch what this module can raise.
__all__ = [
    "AppConfig",
    "CHECK_RUN_NAME_PREFIX",
    "CONCLUSION_ACTION_REQUIRED",
    "CONCLUSION_FAILURE",
    "CONCLUSION_SUCCESS",
    "FORGE_CONCLUSIONS",
    "FORGE_EVENT_LANE",
    "RECOVERY_COMMAND_TEMPLATE",
    "REVIEW_RECOVERY_COMMAND_TEMPLATE",
    "REVIEW_SUMMARY_CHECK_NAME",
    "REVIEW_SUMMARY_SLUG",
    "ForgeAPIError",
    "ForgeAppConfigError",
    "ForgeCheckRunError",
    "ForgeKeychainError",
    "ForgePublishRefused",
    "ForgeStatusUnmapped",
    "ForgeVerdict",
    "auto_merge_is_armed",
    "check_run_name",
    "classify_record",
    "conclusion_for",
    "gates_with_a_record",
    "main",
    "pending_promotion_put_payload",
    "pr_base_branch",
    "publish_for_record",
    "publish_review_summary",
    "read_result_record",
    "read_result_record_at",
    "refresh_review_summary",
    "refuse_if_auto_merge_is_armed",
    "result_record_path",
    "review_verdict",
]

logger = logging.getLogger(__name__)

CONCLUSION_SUCCESS = "success"
CONCLUSION_FAILURE = "failure"
CONCLUSION_ACTION_REQUIRED = "action_required"

#: Every conclusion this module will ever emit. Deliberately excludes
#: ``neutral`` and ``skipped`` — see the module docstring.
FORGE_CONCLUSIONS = (CONCLUSION_SUCCESS, CONCLUSION_FAILURE, CONCLUSION_ACTION_REQUIRED)

#: Branch protection matches a required check by NAME, so the name is a
#: contract, not a label. One check per gate: ``vnx-gate/glm_gate``,
#: ``vnx-gate/codex_gate``. WHICH of them becomes required is B3's decision
#: (``pending_checks`` in ``scripts/forge/branch_protection.yaml``); this
#: module publishes them all and requires none.
CHECK_RUN_NAME_PREFIX = "vnx-gate"

#: The slug of the SUMMARY check (B3), and the one name in this namespace that
#: is NOT a gate. ``vnx-gate/<gate>`` answers "what did THIS gate say"; the
#: summary answers the only question branch protection needs answered — is
#: there a valid review accord on this head at all. Branch protection matches
#: on the name, so a gate that ever came to be called ``review`` would publish
#: over the summary check and satisfy it with a single gate's opinion.
#: :func:`check_run_name` refuses the slug for exactly that reason.
REVIEW_SUMMARY_SLUG = "review"
REVIEW_SUMMARY_CHECK_NAME = f"{CHECK_RUN_NAME_PREFIX}/{REVIEW_SUMMARY_SLUG}"

#: The slug of the door permit (ADR-061, deur D3). Reserved like ``review``:
#: ``vnx-gate/door`` is the one check only the door's permit path may publish,
#: so :func:`check_run_name` refuses it for every gate.
DOOR_SLUG = "door"
DOOR_CHECK_NAME = f"{CHECK_RUN_NAME_PREFIX}/{DOOR_SLUG}"

#: The command that republishes a check-run from the record already on disk.
#: Carried in every failure log, so a publication the recorder swallowed is
#: always one copy-paste away from repair.
RECOVERY_COMMAND_TEMPLATE = (
    "python3 scripts/lib/forge_gate_publisher.py publish --pr {pr} --gate {gate}"
)

#: The same, for the summary check — it names no gate, because it is about
#: every review peer at once.
REVIEW_RECOVERY_COMMAND_TEMPLATE = (
    "python3 scripts/lib/forge_gate_publisher.py review --pr {pr}"
)

_GH_TIMEOUT_SECONDS = 15

#: The event stream a publication attempt leaves its NDJSON line in (ADR-005).
#: Its OWN lane, never ``T{n}``: ``.vnx-data/events/T{n}.ndjson`` is a
#: per-dispatch ring buffer, and appending a publication to it under the
#: record's ``dispatch_id`` would trip ``EventStore``'s dispatch-boundary
#: rotation and archive a running terminal's stream out from under it.
FORGE_EVENT_LANE = "forge"


class ForgeStatusUnmapped(ForgeCheckRunError):
    """A gate result carries a status this module has no mapping for.

    Raised, never defaulted. A permissive fallback would silently pick a
    conclusion for a verdict nobody classified — and whichever way it fell it
    would be wrong for half the unknown statuses that will ever exist. A status
    outside :data:`gate_status.ALL_KNOWN_STATES` means a writer and this
    mapping have drifted apart; that is an operator's problem to see, not a
    default's to hide.
    """


def check_run_name(gate: str) -> str:
    """``vnx-gate/<gate>`` — the name branch protection matches on.

    Refuses :data:`REVIEW_SUMMARY_SLUG`. That name belongs to the summary
    check, which asks a strictly stronger question than any single gate; a
    gate publishing under it would satisfy a required ``vnx-gate/review`` with
    one gate's verdict. No gate is called ``review`` today
    (``test_no_gate_in_the_enum_is_named_review`` keeps it that way), so this
    is a guard against a future enum member, not against a current one.
    """
    if not gate or not gate.strip():
        raise ForgeCheckRunError("gate is leeg: een check-run zonder poortnaam matcht niets")
    resolved = gate.strip()
    if resolved == REVIEW_SUMMARY_SLUG:
        raise ForgeCheckRunError(
            f"{REVIEW_SUMMARY_CHECK_NAME} is de naam van de samenvattende check en niet "
            f"van een poort: een poort die onder deze naam publiceert zou de "
            "samenvattende eis met één poortoordeel vervullen"
        )
    if resolved == DOOR_SLUG:
        raise ForgeCheckRunError(
            f"{DOOR_CHECK_NAME} is de naam van de toestemming van de deur en niet van een "
            "poort: een poort die onder deze naam publiceert zou de toestemming vervangen"
        )
    return f"{CHECK_RUN_NAME_PREFIX}/{resolved}"


@dataclass(frozen=True)
class ForgeVerdict:
    """A conclusion plus the reason it was reached.

    The reason is not decoration: it becomes the check-run's summary, which is
    the only thing an operator sees on a blocked PR. An ``action_required``
    without a reason is a red cross with no instruction attached.
    """

    conclusion: str
    reason: str


# ---------------------------------------------------------------------------
# The disqualifiers, each defined once
# ---------------------------------------------------------------------------


def _require_head(head_sha: str) -> str:
    """The head this publication is about, or raise.

    Deliberately NOT ``gate_recorder.result_is_for_head`` semantics. That
    function returns True for an EMPTY ``head_sha`` on purpose: it serves the
    merge door, where a failed ``gh`` lookup must never silently reclassify
    existing evidence. Here the opposite holds — a check-run IS a statement
    about one commit, and with no commit to name there is nothing to state.
    Leniency there and refusal here are the same rule applied to two different
    questions.
    """
    resolved = (head_sha or "").strip()
    if not resolved:
        raise ForgeCheckRunError(
            "head_sha is leeg: een conclusie hangt aan een kop, en zonder kop valt "
            "er niets te concluderen"
        )
    return resolved


def _head_binding_reason(record: Dict[str, Any], head_sha: str) -> Optional[str]:
    """Why this record is not about ``head_sha``, or None when it is.

    One phrase for all three ways of missing the head — absent record, empty
    ``commit_sha``, other ``commit_sha`` — because the operator's next action
    is identical in all three: run the gate on this head.
    """
    record_sha = (record.get("commit_sha") or "").strip()
    if not record_sha:
        return (
            "poort niet gedraaid op deze kop: het record draagt geen commit_sha, "
            "dus het oordeelt over geen enkele commit"
        )
    if record_sha != head_sha:
        return (
            f"poort niet gedraaid op deze kop: het record oordeelt over "
            f"{record_sha[:12]}, de kop is {head_sha[:12]}"
        )
    return None


def _test_run_reason(record: Dict[str, Any]) -> Optional[str]:
    """Offline test runs are not production evidence (``gate_status``' own rule)."""
    if is_test_run_record(record):
        return (
            "poort niet gedraaid op deze kop: het record is gemarkeerd als test_run, "
            "een offline proefrun en geen oordeel over deze PR"
        )
    return None


def _evidence_source_reason(record: Dict[str, Any]) -> Optional[str]:
    """Why this record's provenance disqualifies it as a live verdict.

    Measured 2026-09-08 over 1148 result records across every project store:
    only ``glm_gate`` (89 live / 5 reprocessed) and ``kimi_gate`` (26 / 1)
    write this field at all. codex_gate (599 records), gemini_review (161),
    ci_gate (147), claude_github_optional (147) and deepseek_gate (3) never do.

    So an ABSENT field is not a provenance claim to distrust — it is the normal
    shape of a plain live run, and demanding the literal string would make
    ``success`` structurally unreachable for the fleet's most-used review gate.
    A PRESENT field IS a claim, and then only ``live`` will do: ``reprocessed``
    (``glm_gate --reprocess``) and ``reanchored`` (``gate_reanchor_cli``) both
    mean the verdict was not produced by a run against this head, and any
    future value nobody has taught this function about lands in the same
    bucket. Unknown provenance fails closed.
    """
    raw = record.get("evidence_source")
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value or value == "live":
        return None
    return (
        f"poort niet live gedraaid op deze kop: evidence_source is {value!r}, "
        "geen live poortrun"
    )


def _proven_pass_on_head(record: Optional[Dict[str, Any]], head_sha: str) -> Tuple[bool, str]:
    """THE definition of "this record is a proven pass on this head".

    Returns ``(ok, reason)``. This is the predicate :func:`classify_record`
    consults for a pass, and the one :func:`publish_for_record` re-asks
    immediately before a green check leaves the machine — one definition read
    twice, never two definitions that have to agree.

    "Proven pass" is not restated here. It is what the merge door already
    enforces on one record: ``closure_verifier._merge_door_record_verdict``,
    the exact chain ``check_review_gate_for_merge`` runs (terminal, complete
    evidence per ``gate_status.has_complete_evidence``, the report exists and
    is readable, the verdict is a pass, and the report does not contradict it).
    Calling it rather than re-deriving it is the point: a second, weaker copy
    of those invariants would be a check that says GO where the merge door says
    NO-GO, and the entire purpose of this check-run is that GitHub and the door
    agree.

    On top of the door's chain, three conditions the door has no reason to care
    about but a published check does: the record judges THIS head, it is not an
    offline test run, and its provenance is a live run.
    """
    resolved_head = _require_head(head_sha)
    if record is None:
        return False, "poort niet gedraaid op deze kop: er is geen schijf-record voor deze poort"

    status = canonical_status(record)
    if status not in PASS_STATES:
        return False, f"geen pass-status (status={status or 'ontbreekt'!r})"

    for reason in (
        _head_binding_reason(record, resolved_head),
        _test_run_reason(record),
        _evidence_source_reason(record),
    ):
        if reason:
            return False, reason

    # Imported here, not at module scope: gate_recorder reaches this module
    # after every result write, and closure_verifier's own import tail
    # (review_contract, codex_final_gate, dispatch_spec, ...) has no business
    # being loaded by every gate run that merely records a result.
    from closure_verifier import _merge_door_record_verdict  # noqa: PLC0415

    label = str(record.get("gate") or record.get("gate_type") or "poort")
    pr_ref = str(record.get("pr_id") or record.get("pr_number") or "?")
    door = _merge_door_record_verdict(record, label, pr_ref)
    if door.get("verdict") != "GO":
        return False, str(door.get("message") or "merge-deur weigert dit record")
    return True, str(door.get("message") or "bewezen pass op deze kop")


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def classify_record(record: Optional[Dict[str, Any]], head_sha: str) -> ForgeVerdict:
    """Map one gate-result record onto a check-run conclusion, with its reason.

    Exhaustive over ``gate_status.ALL_KNOWN_STATES`` and raising outside it.
    :func:`conclusion_for` is the same decision without the reason.

    Order matters, and the head comes first. A verdict about another commit is
    not a lenient verdict about this one; it is the absence of any verdict here
    (OI-1668), whichever way it went on the commit it did judge. So a stale
    fail books ``action_required`` ("run the gate on this head"), not
    ``failure`` — publishing the old answer on a new head is precisely what
    this layer exists to prevent.
    """
    resolved_head = _require_head(head_sha)

    if record is None:
        return ForgeVerdict(
            CONCLUSION_ACTION_REQUIRED,
            "poort niet gedraaid op deze kop: er is geen schijf-record voor deze poort",
        )

    status = canonical_status(record)
    if not status:
        # A REACHABLE state, not an unknown one: measured 2026-09-08, 147
        # claude_github_optional records carry neither status, verdict, nor a
        # completed state/result_status pair. The gate has said nothing yet.
        return ForgeVerdict(
            CONCLUSION_ACTION_REQUIRED,
            "poort heeft geen uitspraak gedaan: het record draagt geen status of verdict",
        )
    if status not in ALL_KNOWN_STATES:
        raise ForgeStatusUnmapped(
            f"onbekende poortstatus {status!r} in het record voor "
            f"{record.get('gate') or 'onbekende poort'} — deze afbeelding kent alleen "
            f"{sorted(ALL_KNOWN_STATES)}. Een onbekende status krijgt bewust geen "
            "conclusie toegewezen: schrijver en afbeelding zijn uit elkaar gelopen."
        )

    for reason in (
        _head_binding_reason(record, resolved_head),
        _test_run_reason(record),
        _evidence_source_reason(record),
    ):
        if reason:
            return ForgeVerdict(CONCLUSION_ACTION_REQUIRED, reason)

    if status in PASS_STATES:
        proven, reason = _proven_pass_on_head(record, resolved_head)
        # A pass that does not survive the merge door's chain is a
        # CONTRADICTED pass, not a missing one: the gate ran on this head and
        # what it left behind does not hold up. That is a failure, not a
        # "please run it".
        return ForgeVerdict(CONCLUSION_SUCCESS if proven else CONCLUSION_FAILURE, reason)
    if status in FAIL_STATES:
        return ForgeVerdict(CONCLUSION_FAILURE, f"poort wees deze kop af (status={status})")
    if status in UNAVAILABLE_STATES:
        return ForgeVerdict(
            CONCLUSION_ACTION_REQUIRED,
            f"poort kwam niet tot een uitspraak (status={status}): provider-uitval is "
            "afwezigheid van bewijs, geen afkeuring — draai de poort opnieuw",
        )
    if status in PARTIAL_REVIEW_STATES:
        # OI-1851: nothing rejected, nothing reviewed in full.
        return ForgeVerdict(
            CONCLUSION_ACTION_REQUIRED,
            "poort zag maar een deel van de diff (status=partial_review): laat een "
            "tweede review de hele diff lezen, of splits de PR op",
        )
    if status == "not_executable":
        return ForgeVerdict(
            CONCLUSION_ACTION_REQUIRED,
            "poort was niet uitvoerbaar (status=not_executable): er is niets beoordeeld "
            "— draai de poort opnieuw",
        )
    if status in INCOMPLETE_STATES:
        return ForgeVerdict(
            CONCLUSION_ACTION_REQUIRED,
            f"poort loopt nog (status={status}): er is nog geen uitspraak",
        )

    # Only reachable when gate_status grows a state set this mapping was never
    # taught about. Fail closed and name it, rather than let a new canonical
    # status inherit whichever branch happened to be last.
    raise ForgeStatusUnmapped(
        f"poortstatus {status!r} staat in gate_status.ALL_KNOWN_STATES maar heeft hier "
        "geen afbeelding — voeg hem expliciet toe"
    )


def conclusion_for(record: Optional[Dict[str, Any]], head_sha: str) -> str:
    """``success`` / ``failure`` / ``action_required`` for one record."""
    return classify_record(record, head_sha).conclusion


# ---------------------------------------------------------------------------
# The summary check — one conclusion per head, over every review peer (B3)
# ---------------------------------------------------------------------------


def review_verdict(
    pr_number: int,
    head_sha: str,
    *,
    results_dir: Optional[Path] = None,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    checkout: Optional[Path] = None,
    base_branch: Optional[str] = None,
    b3_override: str = "",
    b3_override_label: str = "B3 override",
) -> ForgeVerdict:
    """Is every required review seat filled on THIS head? One verdict for the whole review.

    Where :func:`classify_record` answers "what did gate X say", this answers
    the question branch protection actually needs answered, and it is the only
    one of the two that can be made required: a per-gate check would force
    whichever gate happened to be named, on every PR.

    **The rule is read, never restated.** The answer comes from
    ``review_seat_evaluation.evaluate_review_seats``, the one seat evaluation
    the merge door (``closure_verifier.check_review_gate_for_merge``),
    ``vnx pr-ready`` and track closure use too (OI-2058, OI-2064, R5b). Before
    it, this check stopped at the first seat with a GO: on PR #2058 head
    ``71f92de1`` it said ``success`` while ``vnx-gate/codex_gate`` said
    ``failure`` with a blocking finding on the same head.

    On top of the door, :func:`_proven_pass_on_head` for every record that
    fills a seat — the SAME predicate the per-gate publisher consults before a
    green check leaves the machine. The door does not read
    ``evidence_source``, so without this the summary would be MORE permissive
    than the per-gate checks it summarizes.

    The three conclusions:

    ``failure``          a review peer on this head rejected it (a FAIL
                         verdict or a blocking finding, in its JSON or in its
                         report), whatever the other seats say; or a seat's reviewer
                         spoke on this head and what it left does not hold up
                         (a contradicted pass, incomplete evidence, a pass the
                         per-gate predicate refuses).
    ``action_required``  a required seat has not spoken on this head: no
                         record, an outage, a run still going, a verdict about
                         another commit, or a seat list that cannot be
                         established. Absence of evidence, which on a required
                         check must block — never ``neutral`` or ``skipped``,
                         both of which GitHub reads as SATISFIED.
    ``success``          every required seat is filled by a distinct reviewer
                         with a proven pass on this head, and nothing blocks.

    ``branch``/``project_id`` are passed straight through. Handing the
    evaluation less scope than the merge door gets would make this check the
    more lenient of the two, which is the one direction that costs something.
    Which seats are required follows the risk class of the head as the door
    reads it (R3b), in ``checkout`` (``None``: the store's own checkout) against
    ``base_branch``, the PR's ``baseRefName`` (``None``: not known, so the class
    is high, as at the door).

    ``b3_override`` is the ``--override-b3`` reason the merge door would use, or
    ``""``. It reaches :func:`evaluate_review_seats`, so a capped PR the door may
    merge publishes the verdict the door gives (OI-2143), with the reason, the
    cap rule and the threshold named in the verdict a reviewer reads (RG-2).

    ``VNX_REVIEW_SEAT_RULE`` is read here through
    ``review_seat_evaluation.active_seat_rule`` — the same reader the merge door
    uses — so the published check and the door decide by the same rule for the
    same inputs (track ``tweede-stoel-op-signaal``, PR 4). With the key off no
    rule keyword reaches the evaluation at all, so the call is the one on main.
    """
    resolved_head = _require_head(head_sha)
    directory = results_dir if results_dir is not None else _default_results_dir()

    if not directory.exists():
        return ForgeVerdict(
            CONCLUSION_ACTION_REQUIRED,
            f"geen review-akkoord op deze kop: de resultatenmap {directory} bestaat niet, "
            "dus geen enkele review-poort heeft hier een record achtergelaten",
        )

    # Imported at call time: closure_verifier's import tail (which the
    # evaluation reaches) has no business loading on every gate run that
    # merely records a result.
    from review_seat_evaluation import RISK_SEAT_RULE, active_seat_rule, evaluate_review_seats

    seat_rule = active_seat_rule()
    rule_kwargs = (
        {"rule": seat_rule.rule, "rule_value": seat_rule.value}
        if seat_rule.rule != RISK_SEAT_RULE or seat_rule.value else {}
    )
    evaluation = evaluate_review_seats(
        str(pr_number), directory, branch=branch, project_id=project_id, head_sha=resolved_head,
        checkout=checkout, base_branch=base_branch, b3_override=b3_override, **rule_kwargs,
    )
    note = _b3_override_note(b3_override, evaluation, label=b3_override_label)

    def verdict(conclusion: str, reason: str) -> ForgeVerdict:
        return ForgeVerdict(conclusion, f"{reason}\n\n{note}" if note else reason)

    seats = ", ".join(evaluation.seats) or "geen"
    if evaluation.required.error:
        return verdict(CONCLUSION_ACTION_REQUIRED, f"geen review-akkoord op deze kop: {evaluation.message()}")
    if evaluation.blocking:
        return verdict(
            CONCLUSION_FAILURE,
            f"geen review-akkoord op deze kop (seats: {seats}): {evaluation.message()}",
        )
    unproven = _unproven_fills(evaluation, resolved_head)
    judged = [fill for fill in evaluation.unfilled if fill.spoke]
    if judged or unproven:
        reasons = [f"seat {fill.seat}: {fill.message}" for fill in judged] + unproven
        return verdict(
            CONCLUSION_FAILURE,
            f"geen geldig review-akkoord op deze kop (seats: {seats}): " + "; ".join(reasons),
        )
    if evaluation.unfilled:
        return verdict(
            CONCLUSION_ACTION_REQUIRED,
            f"geen review-akkoord op deze kop: een vereiste seat heeft hier nog niet gesproken "
            f"(seats: {seats}). Draai de review-poorten op {resolved_head[:12]}. {evaluation.message()}",
        )
    return verdict(CONCLUSION_SUCCESS, f"geldig review-akkoord op deze kop: {evaluation.message()}")


def _b3_override_note(b3_override: str, evaluation: Any, *, label: str = "B3 override") -> str:
    """The evidence line a reviewer reads: label, reason, cap rule, and the cap record's threshold.

    ``""`` without a reason. ``label`` says what the evidence is: ``B3 override``
    for an actual ``--override-b3``, the verdict's own name when the merge door
    refreshed the check under a standing ``mergen-met-OI`` verdict (RG-4 round 3,
    OI-2506). The threshold (RG-1's ``blockers`` or ``rounds``) is named only
    when the cap record carries one.
    """
    reason = (b3_override or "").strip()
    if not reason:
        return ""
    rounds = getattr(evaluation, "rounds", None)
    cap = getattr(rounds, "cap", None)
    if cap is None or not cap.capped:
        return (
            f"{label}: {reason}. Deze kop staat niet op de B3-cap, dus de override "
            "verandert hier niets aan de telling"
        )
    threshold = str(cap.to_record().get("threshold") or "").strip()
    line = f"{label}: {reason}. De kop staat op de {cap.rule}-cap"
    if threshold:
        line += f" (drempel: {threshold})"
    return f"{line}. {cap.summary()}"


def _unproven_fills(evaluation, head_sha: str) -> List[str]:
    """Every filled seat whose record the per-gate predicate does not call a proven pass.

    The record is the one the door's route accepted for the seat
    (``SeatFill.record``, OI-2058 R5b round 3), never a second lookup of the
    reviewer's slot, which can hold another record than the one a booking
    pointed at. The door accepts it, this layer does not. Never silent: the two
    disagreeing is exactly the state an operator has to see, because the merge
    would go through while the check stays red.

    The codex seat filled by a standing ``mergen-met-OI`` verdict (RG-4) has no
    pass to prove: its evidence is the verdict, the operator's per-use approval
    and a complete aftermath, all of which ``evaluate_review_seats`` re-read for
    this head above. The record beside it is the FAIL round the OIs came from,
    which the per-gate predicate rightly refuses — so it is not asked.
    """
    from review_seat_evaluation import ROUTE_MERGE_WITH_OI

    reasons: List[str] = []
    for fill in evaluation.fills:
        if not fill.filled:
            continue
        if fill.route == ROUTE_MERGE_WITH_OI:
            continue
        proven, why = _proven_pass_on_head(fill.record, head_sha)
        if proven:
            continue
        logger.warning(
            "forge_gate_publisher: de merge-deur telt %s als reviewer voor seat %s op %s, maar "
            "het record is hier geen bewezen pass (%s) — de samenvattende check blijft rood",
            fill.reviewer, fill.seat, head_sha[:12], why,
        )
        reasons.append(f"seat {fill.seat}: {fill.reviewer} is hier geen bewezen pass ({why})")
    return reasons


# ---------------------------------------------------------------------------
# Reading the record off disk
# ---------------------------------------------------------------------------


def _default_results_dir() -> Path:
    """``${VNX_STATE_DIR}/review_gates/results`` — the same slot the recorder
    writes and ``pr_merge`` reads."""
    from vnx_paths import ensure_env  # noqa: PLC0415

    return Path(ensure_env()["VNX_STATE_DIR"]) / "review_gates" / "results"


def result_record_path(results_dir: Path, pr_number: int, gate: str) -> Optional[Path]:
    """The existing result slot for ``pr_number`` + ``gate``, or None.

    Both of ``gate_recorder.result_file_path``'s conventions are tried: the
    ``pr-<N>-<gate>.json`` form (pr_number keyed) and the
    ``<pr_id>-<gate>-contract.json`` form. Which one a gate used depends on
    whether its writer had a ``pr_id``, and a reader that knew only one of them
    would report a perfectly good record as absent.
    """
    for candidate in (
        results_dir / f"pr-{pr_number}-{gate}.json",
        results_dir / f"{pr_number}-{gate}-contract.json",
    ):
        if candidate.exists():
            return candidate
    return None


def read_result_record_at(record_path: Path) -> Dict[str, Any]:
    """The record at an EXPLICIT path — that file or nothing, never the store.

    The path variant of :func:`read_result_record`, and the only one a WRITER
    may use. A writer already knows which file it just wrote; re-deriving that
    file from ``${VNX_STATE_DIR}`` would answer a different question ("what
    does the default store hold for this PR and gate") whose answer is a
    different record whenever the writer used a store that is not the default
    one — which in this project is every gate run, since they all run with
    ``VNX_DATA_DIR=~/.vnx-data/vnx-dev``. Publishing that other record can turn
    a fresh failure into a stale green check.

    So a missing or unreadable path REFUSES instead of returning None. Absent
    is a legitimate answer to "does the store have a record" (it publishes
    ``action_required``) and never a legitimate answer to "read the file I just
    wrote" — there the absence is a plumbing fault, and falling back to the
    store would hide it behind whatever that store happens to hold.
    """
    path = Path(record_path)
    if not path.exists():
        raise ForgePublishRefused(
            f"het opgegeven schijf-record {path} bestaat niet — publicatie geweigerd. "
            "Een expliciet pad wordt nooit vervangen door de standaardopslag: dat zou "
            "een ander record publiceren dan de schrijver bedoelde."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForgePublishRefused(
            f"het schijf-record {path} bestaat maar is onleesbaar ({exc}) — een "
            "beschadigd record levert geen conclusie op, en 'afwezig' zou hier liegen"
        ) from exc
    if not isinstance(data, dict):
        raise ForgePublishRefused(
            f"het schijf-record {path} is geen object maar {type(data).__name__}"
        )
    return data


def read_result_record(
    pr_number: int,
    gate: str,
    *,
    results_dir: Optional[Path] = None,
    record_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """The gate result record on disk, or None when there is none.

    A file that exists but does not parse as a JSON object is NOT None: absent
    and unreadable are different states (the distinction
    ``gate_recorder._read_existing_result`` already draws), and reading a torn
    write as "never ran" would publish ``action_required`` while hiding that
    something corrupted the evidence.

    ``record_path`` names the file directly and is handed to
    :func:`read_result_record_at`, which refuses rather than returns None when
    that file is absent. Only the store-resolved route can answer None, because
    only there is "no record" a real state of the world.
    """
    if record_path is not None:
        return read_result_record_at(Path(record_path))
    path = result_record_path(
        results_dir if results_dir is not None else _default_results_dir(), pr_number, gate
    )
    if path is None or not path.exists():
        return None
    return read_result_record_at(path)


def gates_with_a_record(results_dir: Path, pr_number: int) -> List[str]:
    """Every gate that has a result slot for this PR, sorted."""
    gates = set()
    for path in results_dir.glob(f"pr-{pr_number}-*.json"):
        gates.add(path.stem[len(f"pr-{pr_number}-"):])
    for path in results_dir.glob(f"{pr_number}-*-contract.json"):
        gates.add(path.stem[len(f"{pr_number}-"): -len("-contract")])
    return sorted(g for g in gates if g)


# ---------------------------------------------------------------------------
# The auto-merge refusal
# ---------------------------------------------------------------------------


def _gh_pr_json(pr_number: int, field: str, repo: str) -> Dict[str, Any]:
    """One ``gh pr view --repo <repo> --json <field>`` object, or refuse.

    ``repo`` is the project's ``owner/repo`` (:func:`forge_project_target.
    resolve_forge_target`), never inferred from the cwd: the auto-merge state
    read here must belong to the same PR the check-run is posted on.

    Deliberately NOT ``gate_recorder._gh_pr_view_field``, which returns "" on
    every failure. That leniency is right for stamping identity onto a record
    (a missing branch is logged, the record is still written) and wrong here:
    the single question this function answers is "is auto-merge armed", and
    answering "no" because ``gh`` was absent, rate-limited or unauthenticated
    would let a ``success`` land on a PR that merges itself the moment it does.
    A lookup that did not happen is not a negative answer.
    """
    if shutil.which("gh") is None:
        raise ForgePublishRefused(
            f"`gh` staat niet op PATH, dus {field} van PR #{pr_number} is niet op te "
            "vragen — publicatie geweigerd in plaats van te gokken dat auto-merge uit "
            f"staat (runbook: {RUNBOOK_PATH}, faalmodus 'launchd-runner met kaal PATH')"
        )
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", field],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ForgePublishRefused(
            f"`gh pr view {pr_number} --json {field}` faalde ({exc}) — publicatie geweigerd"
        ) from exc
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise ForgePublishRefused(
            f"`gh pr view {pr_number} --json {field}` gaf exit {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:200]} — publicatie geweigerd"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ForgePublishRefused(
            f"`gh pr view {pr_number} --json {field}` gaf geen JSON: {proc.stdout[:200]!r}"
        ) from exc
    if not isinstance(data, dict):
        raise ForgePublishRefused(
            f"`gh pr view {pr_number} --json {field}` gaf geen object maar "
            f"{type(data).__name__}"
        )
    return data


def pr_base_branch(pr_number: int, repo: str) -> Any:
    """The PR's base branch (``baseRefName``) in ``repo``, as GitHub gives it; ``None`` when GitHub cannot say.

    The summary check reads the risk class against it, as the merge door does
    (R3b). ``None`` is no guess and no default: the class of a head without a
    base is ``high`` (``review_risk_class.base_ref_for``, which also refuses a
    value that is no plain branch name), so a lookup that failed asks for more
    seats, never fewer. The failure is logged with its cause.
    """
    try:
        return _gh_pr_json(pr_number, "baseRefName", repo).get("baseRefName")
    except ForgePublishRefused as exc:
        logger.warning(
            "forge_gate_publisher: base branch of PR #%s in %s not readable (%s); the risk class reads high",
            pr_number, repo, exc,
        )
        return None


def auto_merge_is_armed(pr_number: int, repo: str) -> bool:
    """Whether this PR will merge itself once its checks go green.

    Raises :class:`ForgePublishRefused` when the answer cannot be established —
    see :func:`_gh_pr_json`. "I could not look" is not "no".
    """
    data = _gh_pr_json(pr_number, "autoMergeRequest", repo)
    return bool(data.get("autoMergeRequest"))


def refuse_if_auto_merge_is_armed(pr_number: int, repo: str) -> None:
    """Refuse a ``success`` publication on a PR that has auto-merge queued.

    ``pr_merge.py:430`` adds ``--auto`` as soon as the repo allows it, and the
    repo does not (``allow_auto_merge: false``, held there by B1's drift
    check). If that ever flips, an armed PR turns a GREEN check-run into the
    thing that performs the merge: GitHub merges the moment the last required
    check passes, with no human between the check-run and ``main``.

    **Only green.** The refusal used to cover every conclusion, on the reading
    that a queued auto-merge means a human handed the decision to a robot and
    this robot declines to pull the trigger. That reading inverts on a red
    conclusion. Withholding a ``failure`` does not stop the merge — it leaves
    whatever the head already carries standing, and if that is a stale
    ``success`` from an earlier run, branch protection stays satisfied and the
    auto-merge proceeds on evidence this very gate just contradicted. The red
    check-run is the brake, so refusing to publish it removes the brake.

    ``success`` is the only conclusion that can COMPLETE a merge, and it is the
    only one refused here. Callers publish red regardless (and say so out loud:
    :func:`_note_red_over_armed_auto_merge`).
    """
    if auto_merge_is_armed(pr_number, repo):
        raise ForgePublishRefused(
            f"PR #{pr_number} heeft een actieve auto-merge (autoMergeRequest): een "
            "success-publicatie is geweigerd, want een groene check zou hier de merge "
            "zelf uitvoeren. Zet auto-merge uit (`gh pr merge --disable-auto`) en "
            "publiceer daarna opnieuw."
        )


def _note_red_over_armed_auto_merge(
    pr_number: int, check_name: str, conclusion: str, repo: str
) -> None:
    """Say out loud that a red conclusion is going out onto a self-merging PR.

    Advisory only, and deliberately unable to stop anything: the auto-merge
    state cannot change what a red publication does, so a ``gh`` outage here
    must never become the reason a failing gate stays invisible on the PR. That
    would rebuild the stale-green hole this narrowing just closed, one level
    further down.

    Warning level either way. "Auto-merge armed" is precisely the state in
    which an operator wants to know a machine just wrote a verdict onto that
    PR, and an unanswerable lookup on the path where the answer no longer
    blocks is worth one line too — otherwise a permanently broken ``gh`` would
    be indistinguishable from a permanently unarmed PR.
    """
    try:
        armed = auto_merge_is_armed(pr_number, repo)
    except ForgePublishRefused as exc:
        logger.warning(
            "forge_gate_publisher: auto-merge-status van PR #%s niet vast te stellen (%s); "
            "%s voor %s wordt tóch gepubliceerd — een rode uitkomst houdt een eventuele "
            "auto-merge juist tegen, dus deze onbekende blokkeert niets",
            pr_number, exc, conclusion, check_name,
        )
        return
    if armed:
        logger.warning(
            "forge_gate_publisher: PR #%s heeft een actieve auto-merge en krijgt tóch %s "
            "voor %s — een rode check houdt die auto-merge tegen; alleen een success "
            "wordt hier geweigerd",
            pr_number, conclusion, check_name,
        )


# ---------------------------------------------------------------------------
# The publisher
# ---------------------------------------------------------------------------


def _check_run_summary(gate: str, verdict: ForgeVerdict, head_sha: str, pr_number: int) -> str:
    lines = [
        f"**{check_run_name(gate)}** — {verdict.conclusion}",
        "",
        verdict.reason,
        "",
        f"Kop: `{head_sha}`",
    ]
    if verdict.conclusion != CONCLUSION_SUCCESS:
        lines += [
            "",
            "Opnieuw publiceren vanaf het schijf-record:",
            "",
            "```",
            RECOVERY_COMMAND_TEMPLATE.format(pr=pr_number, gate=gate),
            "```",
        ]
    lines += ["", f"Runbook: `{RUNBOOK_PATH}`"]
    return "\n".join(lines)


def _emit_publication_event(
    *,
    pr_number: int,
    gate: str,
    head_sha: str,
    outcome: str,
    detail: str,
    conclusion: str = "",
    record: Optional[Dict[str, Any]] = None,
    check_name: Optional[str] = None,
) -> None:
    """One NDJSON line per publication attempt (ADR-005), best-effort.

    A check-run mutates state on GitHub, so the attempt belongs in the audit
    trail whichever way it went — ``published`` and ``failed`` both write a
    line. Never raises: this is a trace OF the publication, and a trace that
    can break the thing it traces is worse than a missing line (the same rule
    ``gate_recorder.publish_forge_check_run`` applies one level up).

    ``check_name`` is passed by the summary-check path, whose name is not
    derivable from a gate (:func:`check_run_name` refuses ``review`` on
    purpose). Absent, the name is derived — which is what every per-gate
    caller wants.

    ``dispatch_id`` is deliberately left empty on the envelope and carried in
    ``data`` instead. The envelope field is what drives ``EventStore``'s
    per-dispatch ring-buffer rotation; stamping it here would make every
    publication from a new dispatch archive and truncate the lane, so the
    producing dispatch is recorded as data rather than as a rotation key.
    """
    try:
        from event_store import EventStore  # noqa: PLC0415

        EventStore().append(
            FORGE_EVENT_LANE,
            {
                "type": f"forge_check_run_{outcome}",
                "dispatch_id": "",
                "data": {
                    "pr_number": pr_number,
                    "gate": gate,
                    "check_run_name": check_name or check_run_name(gate),
                    "head_sha": head_sha,
                    "outcome": outcome,
                    "conclusion": conclusion,
                    "detail": detail,
                    "record_dispatch_id": str((record or {}).get("dispatch_id") or ""),
                },
            },
        )
    # vnx-broad-except: the audit line may never take down the publication it
    # describes. Event-store resolution reaches the data-dir resolver, the
    # filesystem and a lock — an open-ended surface, and every failure in it is
    # a missing line, not a wrong check-run.
    except Exception as exc:  # noqa: BLE001
        # WARNING, not debug. A check-run mutates state on GitHub; if the
        # ADR-005 line behind it is lost, the ledger silently disagrees with
        # what the world now looks like. Swallowing that at debug level makes
        # an audit gap indistinguishable from a quiet success — the trace is
        # allowed to fail, it is not allowed to fail invisibly.
        logger.warning(
            "forge_gate_publisher: ADR-005-gebeurtenis NIET weggeschreven (%s) voor "
            "gate=%s pr=%s: %s — de check-run-mutatie staat wél op GitHub, dus het "
            "gebeurtenissenspoor mist hier een regel",
            outcome, gate, pr_number, exc,
        )


def _publication_target(record_path: Optional[Path], results_dir: Optional[Path]) -> ForgeTarget:
    """The project a publication is for, from the store the record lives in.

    ``record_path`` is the file itself, ``results_dir`` the CLI's store, neither
    the default store: the same three-way choice as the record lookup, so the
    repo is always the one the record was read from.
    """
    if record_path is not None:
        store = Path(record_path).parent
    elif results_dir is not None:
        store = Path(results_dir)
    else:
        store = _default_results_dir()
    return resolve_forge_target(store)


def publish_for_record(
    pr_number: int,
    gate: str,
    head_sha: str,
    *,
    results_dir: Optional[Path] = None,
    record_path: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Publish ``vnx-gate/<gate>`` for ``pr_number`` from the record on disk.

    Reads the record, maps it, and posts. Returns the payload that was sent
    (or, under ``dry_run``, the payload that would have been) — the mapping's
    decision is always visible to the caller, never only to GitHub.

    **Which record.** Three ways to name it, and a writer may only use the
    first: ``record_path`` is THE file, read as given
    (:func:`read_result_record_at`, which refuses a missing one rather than
    falling back); ``results_dir`` is the CLI's store; neither is the default
    store. The recorder hook passes the path it just wrote, because every gate
    run in this project runs against a non-default store
    (``VNX_DATA_DIR=~/.vnx-data/vnx-dev``) and a re-resolved lookup would then
    read a different file than the one that was written — publishing a stale
    ``success`` over a verdict that had just failed.

    **Two refusals, and both are about ``success`` alone.** An armed auto-merge
    (:func:`refuse_if_auto_merge_is_armed`) and a record that is not a proven
    pass on this head. The second one re-asks :func:`_proven_pass_on_head`
    rather than trusting the conclusion it was just handed. That is not
    distrust of :func:`classify_record` — it is the one place where a mistake
    anywhere upstream turns into a green light on ``main``, so the predicate
    that DEFINES "proven" is consulted at the exact moment the green light
    would be given.

    Which is why the conclusion is computed BEFORE either refusal runs.
    ``failure`` and ``action_required`` are published unconditionally: they
    cannot complete a merge, they are what holds an armed auto-merge back, and
    withholding one would leave a stale ``success`` on the head as the last
    thing branch protection sees.

    **Which repo.** The one the store holding the record belongs to
    (:func:`forge_project_target.resolve_forge_target`): every ``gh`` lookup and
    the POST itself use that repo. When the store cannot be attributed to a
    project the publication is refused (:class:`ForgePublishRefused`), because
    the only other candidate, the engine's own checkout, is a different repo.
    """
    resolved_head = _require_head(head_sha)

    # FIRST, and before any network call: is this feature switched on at all?
    # Until the operator has registered the App and filled ``app_id`` (runbook
    # §1/§3) there is nothing to publish with, and the gate recorder calls this
    # after EVERY result write. Asking the local YAML first means a pending
    # operator step costs zero ``gh`` round-trips and zero keychain prompts,
    # instead of one of each per gate run for as long as the step is open. A
    # ``--dry-run`` deliberately skips this: showing which conclusion a record
    # maps to needs no App, and that is exactly the rehearsal an operator wants
    # BEFORE registering one.
    if not dry_run:
        load_app_config()

    record: Optional[Dict[str, Any]] = None
    try:
        # The record and its conclusion FIRST, before any ``gh`` round-trip.
        # Not merely cheaper (a publication that names a file nobody wrote
        # costs nothing and fails where the fault is): the conclusion is what
        # decides whether the auto-merge question is even asked, so it cannot be
        # asked before the conclusion exists.
        if record_path is not None:
            record = read_result_record_at(Path(record_path))
        else:
            record = read_result_record(pr_number, gate, results_dir=results_dir)
        verdict = classify_record(record, resolved_head)
        target = _publication_target(record_path, results_dir)

        if verdict.conclusion == CONCLUSION_SUCCESS:
            refuse_if_auto_merge_is_armed(pr_number, target.owner_repo)
            proven, why = _proven_pass_on_head(record, resolved_head)
            if not proven:
                raise ForgePublishRefused(
                    f"weiger success voor {check_run_name(gate)} op {resolved_head[:12]}: "
                    f"het record is geen bewezen pass op deze kop — {why}"
                )
        elif not dry_run:
            # Red goes out whatever the auto-merge state is; the operator still
            # gets told. A rehearsal posts nothing, so it has nothing to note.
            _note_red_over_armed_auto_merge(
                pr_number, check_run_name(gate), verdict.conclusion, target.owner_repo
            )

        name = check_run_name(gate)
        payload: Dict[str, Any] = {
            "pr_number": pr_number,
            "gate": gate,
            "name": name,
            "head_sha": resolved_head,
            "conclusion": verdict.conclusion,
            "reason": verdict.reason,
            "summary": _check_run_summary(gate, verdict, resolved_head, pr_number),
            "dry_run": dry_run,
        }
        # A rehearsal posts nothing, so there is no publication to record.
        if dry_run:
            return payload

        response = publish_check_run(
            resolved_head,
            name,
            verdict.conclusion,
            payload["summary"],
            project_root=target.project_root,
        )
    # vnx-broad-except: every way this can end without a check-run — a refusal,
    # a corrupt record, GitHub saying no — is one line in the trail, and the
    # exception is re-raised unchanged. Catching only ForgeCheckRunError would
    # leave the surprises, which are the ones worth a trace, untraced.
    except Exception as exc:  # noqa: BLE001
        _emit_publication_event(
            pr_number=pr_number,
            gate=gate,
            head_sha=resolved_head,
            outcome="failed",
            detail=f"{type(exc).__name__}: {exc}",
            record=record,
        )
        raise

    payload["check_run_id"] = response.get("id")
    _emit_publication_event(
        pr_number=pr_number,
        gate=gate,
        head_sha=resolved_head,
        outcome="published",
        detail=str(payload["reason"]),
        conclusion=str(payload["conclusion"]),
        record=record,
    )
    return payload


def _review_summary_body(verdict: ForgeVerdict, head_sha: str, pr_number: int) -> str:
    lines = [
        f"**{REVIEW_SUMMARY_CHECK_NAME}** — {verdict.conclusion}",
        "",
        verdict.reason,
        "",
        f"Kop: `{head_sha}`",
    ]
    if verdict.conclusion != CONCLUSION_SUCCESS:
        lines += [
            "",
            "Opnieuw beoordelen vanaf de schijf-records:",
            "",
            "```",
            REVIEW_RECOVERY_COMMAND_TEMPLATE.format(pr=pr_number),
            "```",
        ]
    lines += ["", f"Runbook: `{RUNBOOK_PATH}`"]
    return "\n".join(lines)


def publish_review_summary(
    pr_number: int,
    head_sha: str,
    *,
    results_dir: Optional[Path] = None,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    dry_run: bool = False,
    base_branch: Optional[str] = None,
    b3_override: str = "",
    b3_override_label: str = "B3 override",
) -> Dict[str, Any]:
    """Publish ``vnx-gate/review`` for one head. Returns the payload sent.

    The summary sibling of :func:`publish_for_record`, and deliberately the
    same shape: the App config is asked for first so a pending operator step
    costs no network round-trip, ``success`` is refused while auto-merge is
    armed (a green check on an armed PR performs the merge), and red goes out
    regardless because the red check IS the brake.

    One difference, and it is not an omission: there is no second re-ask of the
    proving predicate before the green light. :func:`publish_for_record` re-asks
    :func:`_proven_pass_on_head` because :func:`classify_record` reaches its
    conclusion by a different route than the predicate does. Here
    :func:`review_verdict` IS that predicate — it returns ``success`` only by
    calling :func:`_proven_pass_on_head` and getting True — so asking again
    would run the identical call twice and prove nothing the first one did not.

    ``base_branch`` is the PR's base; ``None`` reads it from the PR in the
    store's repo (:func:`pr_base_branch`), so the risk class is read against the
    base the door reads it against (R3b).

    ``b3_override`` is the ``--override-b3`` reason the merge door used, or
    ``""`` (RG-2): a capped head the door merges under an override gets a green
    required check with the reason on it, not the red one branch protection
    keeps blocking (OI-2143). ``b3_override_label`` names the evidence: the
    override itself, or the ``mergen-met-OI`` verdict the door acted on
    (RG-4 round 3, OI-2506).
    """
    resolved_head = _require_head(head_sha)

    # Before any network call: is the App registered at all? Same reason as in
    # publish_for_record — and a --dry-run skips it, because rehearsing the
    # conclusion is exactly what an operator wants BEFORE registering one.
    if not dry_run:
        load_app_config()

    try:
        # The same store, so the same repo, as the per-gate check of this record.
        target = _publication_target(None, results_dir)
        base = base_branch if base_branch is not None else pr_base_branch(pr_number, target.owner_repo)
        verdict = review_verdict(
            pr_number, resolved_head, results_dir=results_dir, branch=branch,
            project_id=project_id, base_branch=base, b3_override=b3_override,
            b3_override_label=b3_override_label,
        )
        if verdict.conclusion == CONCLUSION_SUCCESS:
            refuse_if_auto_merge_is_armed(pr_number, target.owner_repo)
        elif not dry_run:
            _note_red_over_armed_auto_merge(
                pr_number, REVIEW_SUMMARY_CHECK_NAME, verdict.conclusion, target.owner_repo
            )

        payload: Dict[str, Any] = {
            "pr_number": pr_number,
            "gate": REVIEW_SUMMARY_SLUG,
            "name": REVIEW_SUMMARY_CHECK_NAME,
            "head_sha": resolved_head,
            "conclusion": verdict.conclusion,
            "reason": verdict.reason,
            "summary": _review_summary_body(verdict, resolved_head, pr_number),
            "dry_run": dry_run,
        }
        if dry_run:
            return payload

        response = publish_check_run(
            resolved_head,
            REVIEW_SUMMARY_CHECK_NAME,
            verdict.conclusion,
            payload["summary"],
            project_root=target.project_root,
        )
    # vnx-broad-except: every way this ends without a check-run is one line in
    # the trail, and the exception is re-raised unchanged.
    except Exception as exc:  # noqa: BLE001
        _emit_publication_event(
            pr_number=pr_number,
            gate=REVIEW_SUMMARY_SLUG,
            head_sha=resolved_head,
            outcome="failed",
            detail=f"{type(exc).__name__}: {exc}",
            check_name=REVIEW_SUMMARY_CHECK_NAME,
        )
        raise

    payload["check_run_id"] = response.get("id")
    _emit_publication_event(
        pr_number=pr_number,
        gate=REVIEW_SUMMARY_SLUG,
        head_sha=resolved_head,
        outcome="published",
        detail=str(payload["reason"]),
        conclusion=str(payload["conclusion"]),
        check_name=REVIEW_SUMMARY_CHECK_NAME,
    )
    return payload


def refresh_review_summary(
    pr_number: int,
    head_sha: str,
    *,
    b3_override: str,
    results_dir: Optional[Path] = None,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    base_branch: Optional[str] = None,
    b3_override_label: str = "B3 override",
    dry_run: bool = False,
) -> Optional[Dict[str, Any]]:
    """Republish ``vnx-gate/review`` for one head with an explicit override reason (RG-2).

    The entry point the merge door uses (RG-4) when it merges under
    ``--override-b3`` or the verdict allowed a merge with too few PASSes: that
    path writes no gate record, so the required check would stay red on a head
    the door is about to merge. This republishes from the records already on
    disk and returns the payload that went out — including its ``conclusion``,
    which is not necessarily ``success`` (RG-4 round 2: publishing a red
    summary is still a publication). ``None`` when nothing went out at all.

    The caller must read ``conclusion``: a check-run that went out red is not a
    green door. Treating "published" as "passed" is exactly the hole this
    return value closes.

    ``b3_override_label`` says what the evidence is: the merge door passes
    ``B3 override`` for an actual ``--override-b3`` and the verdict's own name
    for a standing ``mergen-met-OI`` verdict (RG-4 round 3, OI-2506).
    ``dry_run`` rehearses the same mapping — ``publish_review_summary`` returns
    the payload it would have sent and no check-run leaves the machine, so a
    ``--dry-run`` merge never touches the forge.

    **A head that moved publishes nothing.** The check is bound to one commit;
    the PR's current head is read first (:func:`_resolve_head_sha`) and when it
    cannot be read, or differs from ``head_sha``, nothing is published and the
    reason is logged. The reason is required: an override without one is not an
    override. Every way the publication can fail is a logged ``None``, never an
    exception — the caller is mid-merge and needs an answer.
    """
    reason = (b3_override or "").strip()
    if not reason:
        raise ForgeCheckRunError(
            "een B3-override zonder reden is geen override: geef de reden mee waarmee "
            "de kop langs de cap mag"
        )
    resolved_head = _require_head(head_sha)

    try:
        target = _publication_target(None, results_dir)
        current_head = _resolve_head_sha(pr_number, target.owner_repo)
        if not current_head:
            logger.warning(
                "forge_gate_publisher: de kop van PR #%s is niet op te vragen; er wordt "
                "niets gepubliceerd voor %s en de check blijft zoals hij staat",
                pr_number, resolved_head[:12],
            )
            return None
        if current_head != resolved_head:
            logger.warning(
                "forge_gate_publisher: de kop van PR #%s is verschoven (%s -> %s); er wordt "
                "niets gepubliceerd voor de gevraagde kop — ververs de check voor de nieuwe kop",
                pr_number, resolved_head[:12], current_head[:12],
            )
            return None
        payload = publish_review_summary(
            pr_number, resolved_head, results_dir=results_dir, branch=branch,
            project_id=project_id, base_branch=base_branch, b3_override=reason,
            b3_override_label=b3_override_label, dry_run=dry_run,
        )
    # vnx-silent-except: the caller is mid-merge and needs an answer, not a
    # traceback; the failure is logged loudly and every one of them leaves the
    # check unpublished, which the caller must treat as a refusal.
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "forge_gate_publisher: verversen van %s voor PR #%s op %s MISLUKT — %s: %s. "
            "De check staat er niet; de deur moet de merge weigeren tot dit lukt. Herstel: %s",
            REVIEW_SUMMARY_CHECK_NAME, pr_number, resolved_head[:12], type(exc).__name__, exc,
            REVIEW_RECOVERY_COMMAND_TEMPLATE.format(pr=pr_number),
        )
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# The rehearsal: what the apply WOULD send once the entry is promoted
# ---------------------------------------------------------------------------


def pending_promotion_put_payload(yaml_path: Optional[Path] = None) -> Dict[str, Any]:
    """The exact PUT body ``apply_branch_protection.py`` would send if every
    ``pending_checks`` entry were moved into ``checks[]``. Writes nothing.

    ``pending_checks`` is a parking slot: ``apply_branch_protection.py`` leaves
    it out of the PUT and ``forge_protection_drift.py`` never compares it, so
    an entry there requires nothing of ``main`` (B1, pinned by
    ``test_apply_branch_protection.py``). This is the rehearsal of the step
    that ends that: it shows the operator the object that closes the trap
    BEFORE it is closed, and it never touches ``gh``.

    **Where the app_id comes from.** ``app.app_id`` in the same YAML — the
    identity ``forge_check_run`` signs with, and therefore by construction the
    only App that can satisfy a ``vnx-gate/*`` check. Repeating the number
    inside each pending entry would be a second copy of one fact, free to
    drift from the one that actually signs.

    Two refusals, both fail-closed:

    - a pending entry that is not ``vnx-gate/*`` has no App to bind to, and
      emitting it unbound would put a check into ``checks[]`` that ANY app can
      satisfy — a weakening dressed up as a preview. Declare it in ``checks[]``
      with its own ``app_id`` instead.
    - a pending entry already present in ``checks[]`` would produce the
      duplicate context the schema reader itself refuses.
    """
    from ci_contexts import RequiredCheck  # noqa: PLC0415
    from forge_check_run import DEFAULT_YAML_PATH  # noqa: PLC0415
    from forge_protection_drift import (  # noqa: PLC0415
        ProtectionConfigError,
        load_protection_config,
    )

    path = Path(yaml_path) if yaml_path is not None else DEFAULT_YAML_PATH
    # Translated at this boundary, not left to escape. ``load_protection_config``
    # lets an OSError through untouched and raises ProtectionConfigError (a
    # ValueError) for a malformed file — neither is a ForgeCheckRunError, so
    # the CLI's own handler would miss both and hand the operator a traceback
    # where the whole point of this command is a readable rehearsal.
    try:
        config = load_protection_config(path)
    except OSError as exc:
        raise ForgePublishRefused(f"kan {path} niet lezen: {exc}") from exc
    except ProtectionConfigError as exc:
        raise ForgePublishRefused(f"{path} voldoet niet aan het schema: {exc}") from exc
    app = load_app_config(path)

    required = {check.context for check in config.checks}
    promoted = list(config.checks)
    for context in config.pending_checks:
        if not context.startswith(f"{CHECK_RUN_NAME_PREFIX}/"):
            raise ForgePublishRefused(
                f"pending_checks bevat '{context}', geen {CHECK_RUN_NAME_PREFIX}/*-check: "
                f"deze proefdraai kan alleen aan de App '{app.slug}' binden, en een entry "
                "zonder gebonden app_id zou elke app deze status laten zetten. Zet hem "
                "met zijn eigen app_id rechtstreeks in required_status_checks.checks."
            )
        if context in required:
            raise ForgePublishRefused(
                f"'{context}' staat zowel in pending_checks als in "
                "required_status_checks.checks — de promotie zou een dubbele context "
                "opleveren, precies wat de schemalezer weigert"
            )
        promoted.append(RequiredCheck(context, app.app_id))
        required.add(context)

    from apply_branch_protection import build_put_payload  # noqa: PLC0415

    return build_put_payload(replace(config, checks=tuple(promoted), pending_checks=()))


# ---------------------------------------------------------------------------
# CLI — republish from the record on disk
# ---------------------------------------------------------------------------

#: Published, and the record really is about the current head.
EXIT_OK = 0
#: Something refused or failed outright; nothing was published.
EXIT_ERROR = 1
#: Published (as ``action_required``), but the slot holds a decided verdict for
#: an OLDER head. Non-zero because the operator has work to do — the check
#: stays red until the gate runs again — while the check-run itself IS posted,
#: so the PR carries a visible reason instead of a missing check.
EXIT_STALE_VERDICT = 2


def _resolve_head_sha(pr_number: int, repo: str) -> str:
    """The PR head from GitHub (in ``repo``), via the fleet's single source of truth.

    ``gate_recorder.get_pr_head_sha`` — never ``git rev-parse HEAD``, which
    resolves against the process cwd and is not the PR head (OI-1307).
    """
    from gate_recorder import get_pr_head_sha  # noqa: PLC0415

    return get_pr_head_sha(pr_number, repo=repo)


def _resolve_head_branch(pr_number: int, repo: str) -> str:
    """The PR's head branch, or refuse.

    Uses this module's strict :func:`_gh_pr_json`, not
    ``gate_recorder._gh_pr_view_field`` (which returns "" on every failure).
    The leniency is right when stamping identity onto a record; here an empty
    branch would silently widen the scope handed to the merge door, and this
    check would then accept evidence the door itself rejects as stale — the
    one direction a summary check may never fall.
    """
    branch = str(_gh_pr_json(pr_number, "headRefName", repo).get("headRefName") or "").strip()
    if not branch:
        raise ForgePublishRefused(
            f"`gh pr view {pr_number} --json headRefName` gaf geen branch terug — zonder "
            "branch is de scope ruimer dan die van de merge-deur, en dan zou deze check "
            "bewijs kunnen goedkeuren dat de deur als verouderd afwijst"
        )
    return branch


def _publish_one(
    pr_number: int, gate: str, head_sha: str, results_dir: Path, *, dry_run: bool
) -> int:
    record = read_result_record(pr_number, gate, results_dir=results_dir)
    verdict = classify_record(record, head_sha)
    payload = publish_for_record(
        pr_number, gate, head_sha, results_dir=results_dir, dry_run=dry_run
    )

    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}{payload['name']} -> {payload['conclusion']}")
    print(f"  kop:    {head_sha}")
    print(f"  reden:  {payload['reason']}")
    if dry_run:
        print("  payload:")
        print(
            json.dumps(
                {
                    "name": payload["name"],
                    "head_sha": payload["head_sha"],
                    "status": "completed",
                    "conclusion": payload["conclusion"],
                    "output": {"title": payload["name"], "summary": payload["summary"]},
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    # A slot holding a DECIDED verdict for another head is the one outcome
    # worth its own exit code: nothing is broken, but the check stays red until
    # someone re-runs the gate, and a zero exit would let a script conclude the
    # publication finished the job.
    if record is not None and verdict.conclusion == CONCLUSION_ACTION_REQUIRED:
        record_sha = (record.get("commit_sha") or "").strip()
        if canonical_status(record) in (PASS_STATES | FAIL_STATES) and record_sha != head_sha:
            print(
                f"  LET OP: het slot draagt een {canonical_status(record)} voor "
                f"{record_sha[:12] or '<geen sha>'}, niet voor deze kop — "
                "draai de poort opnieuw."
            )
            return EXIT_STALE_VERDICT
    return EXIT_OK


def _run_review(args: argparse.Namespace, results_dir: Path, head_sha: str, repo: str) -> int:
    """``review`` — publish the one summary check for this head."""
    branch = args.branch or _resolve_head_branch(args.pr, repo)
    payload = publish_review_summary(
        args.pr,
        head_sha,
        results_dir=results_dir,
        branch=branch,
        dry_run=args.dry_run,
    )

    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}{payload['name']} -> {payload['conclusion']}")
    print(f"  kop:    {head_sha}")
    print(f"  branch: {branch}")
    print(f"  reden:  {payload['reason']}")
    if args.dry_run:
        print("  payload:")
        print(
            json.dumps(
                {
                    "name": payload["name"],
                    "head_sha": payload["head_sha"],
                    "status": "completed",
                    "conclusion": payload["conclusion"],
                    "output": {"title": payload["name"], "summary": payload["summary"]},
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    return EXIT_OK


def _run_pending_preview(args: argparse.Namespace) -> int:
    """``pending-preview`` — the PUT object promotion would send. Writes nothing.

    Printed, never applied: this is the rehearsal an operator reads before
    closing the trap, and the command that closes it
    (``apply_branch_protection.py``) stays a separate, deliberate step.
    """
    yaml_path = Path(args.yaml) if args.yaml else None
    payload = pending_promotion_put_payload(yaml_path)
    print(
        "Proefdraai: het PUT-object dat "
        "`python3 scripts/forge/apply_branch_protection.py` zou versturen ZODRA elke "
        "pending_checks-entry naar required_status_checks.checks verhuist. Er is niets "
        "geschreven en niets opgevraagd."
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return EXIT_OK


# ---------------------------------------------------------------------------
# The door permit (ADR-061, deur D3): one run per head, moved with PATCH
# ---------------------------------------------------------------------------


class ForgeDoorRefused(ForgePublishRefused):
    """The door permit is refused: bad evidence, or a head whose door runs cannot be trusted."""


DOOR_EVIDENCE_SCHEMA = 1
DOOR_EVIDENCE_SOURCE = "pr_merge._run_door"
DOOR_REVOKE_ATTEMPTS = 3
_DOOR_REVOKE_BACKOFF_SECONDS = (1.0, 3.0)
REVOKED = "revoked"
REVOKE_FAILED = "revoke_failed"


def door_evidence_gates() -> List[str]:
    """The gates the evidence must carry: ``PREFLIGHT_ORDER`` without ``door``, in order.

    ``pr_merge`` imports without side effects (it only defines and imports), so
    the names are read from the one constant the door itself runs by.
    """
    from pr_merge import PREFLIGHT_ORDER

    return [name for name in PREFLIGHT_ORDER if name != DOOR_SLUG]


def read_door_evidence(path: Path) -> Dict[str, Any]:
    """Parse the evidence file, or refuse with the reason (missing, unreadable, not an object)."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ForgeDoorRefused(f"evidence ontbreekt of is onleesbaar ({path}): {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ForgeDoorRefused(f"evidence is niet te parsen ({path}): {exc}") from exc
    if not isinstance(parsed, dict):
        raise ForgeDoorRefused(f"evidence is geen object ({path}): {type(parsed).__name__}")
    return parsed


def _door_header_reason(evidence: Dict[str, Any], sha: str, pr: int) -> Optional[str]:
    if evidence.get("schema") != DOOR_EVIDENCE_SCHEMA or isinstance(evidence.get("schema"), bool):
        return f"evidence schema {evidence.get('schema')!r} wordt niet gelezen (verwacht {DOOR_EVIDENCE_SCHEMA})"
    if evidence.get("source") != DOOR_EVIDENCE_SOURCE:
        return f"evidence source {evidence.get('source')!r} is niet {DOOR_EVIDENCE_SOURCE!r}"
    if evidence.get("head_sha") != sha:
        return f"evidence head_sha {evidence.get('head_sha')!r} is niet de gevraagde kop {sha!r}"
    number = evidence.get("pr_number")
    if isinstance(number, bool) or number != pr:
        return f"evidence pr_number {number!r} is niet PR #{pr}"
    return None


def _door_gates_reason(records: Any, expected: List[str]) -> Optional[str]:
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        return "evidence preflight_gates is geen lijst van records"
    names = [r.get("gate") for r in records]
    missing = [n for n in expected if n not in names]
    if missing:
        return f"evidence mist de poort(en) {missing}: verwacht {expected}, kreeg {names}"
    extra = [n for n in names if n not in expected or names.count(n) > 1]
    if extra:
        return (
            f"evidence heeft een extra of dubbele poort {extra}: de deur zelf hoort er niet bij "
            f"(verwacht {expected}, kreeg {names})"
        )
    if names != expected:
        return f"evidence heeft de poorten in de verkeerde volgorde: verwacht {expected}, kreeg {names}"
    for record in records:
        if record.get("verdict") != "GO":
            return f"evidence poort {record.get('gate')!r} heeft verdict {record.get('verdict')!r}, geen GO"
    return None


def validate_door_evidence(
    evidence: Dict[str, Any], *, sha: str, pr: int, expected_gates: Optional[List[str]] = None
) -> None:
    """Raise :class:`ForgeDoorRefused` unless the evidence is GO for exactly this head and PR.

    Contract 16(a). Only a record with ``verdict == "GO"`` counts; missing, ``None``
    and unknown values refuse. ``expected_gates`` defaults to :func:`door_evidence_gates`.
    """
    if not isinstance(evidence, dict):
        raise ForgeDoorRefused("evidence is geen object")
    gates = door_evidence_gates() if expected_gates is None else list(expected_gates)
    reason = _door_header_reason(evidence, sha, pr) or _door_gates_reason(
        evidence.get("preflight_gates"), gates
    )
    if reason:
        raise ForgeDoorRefused(reason)


class ForgeLedgerError(ForgeCheckRunError):
    """A door-permit receipt could not be written: the action does not happen (or is reported)."""


DOOR_RECEIPT_SOURCE = "forge_gate_publisher"
RECEIPT_WRITTEN = "appended"


def _evidence_digest(evidence: Any) -> str:
    try:
        raw = json.dumps(evidence, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = repr(evidence)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evidence_gate_names(evidence: Any) -> List[str]:
    records = evidence.get("preflight_gates") if isinstance(evidence, dict) else None
    if not isinstance(records, list):
        return []
    return [str(r.get("gate")) for r in records if isinstance(r, dict)]


def _new_operation_id() -> str:
    """A fresh identity for one permit operation: its intent and outcome receipts share it."""
    return uuid.uuid4().hex


def _write_door_receipt(
    event: str, *, status: str, receipts_file: Optional[str], op_id: str, action: str,
    phase: str, pr_number: int, head_sha: str, **fields: Any,
) -> str:
    """One receipt in the canonical ledger; returns the ``append_status``.

    A failed write raises :class:`ForgeLedgerError`. ``trigger`` is the field the
    canonical dedup key honours (``IDEMPOTENCY_FIELDS``) that is free for an event
    without a dispatch: ``task_id`` would demand a ``dispatch_id`` and ``report_path``
    names a report. It carries PR, full head, action, phase and the per-operation uuid, so two operations never
    share a key and the second-resolution timestamp no longer decides identity.
    No field called ``verdict``: ``append_receipt`` overwrites it with its own.
    """
    identity = f"door-permit:pr{pr_number}:{head_sha}:{action}:{phase}:{op_id}"
    try:
        receipt = emit_governance_receipt(
            event, receipt_kind="state_mutation", status=status, terminal="T0",
            source=DOOR_RECEIPT_SOURCE, receipts_file=receipts_file, trigger=identity,
            operation_id=op_id, action=action, phase=phase, pr_number=pr_number,
            head_sha=head_sha, **fields,
        )
    # vnx-broad-except: any way the ledger write can end is the same fail-closed answer.
    except Exception as exc:  # noqa: BLE001
        raise ForgeLedgerError(
            f"{event} ({status}) niet in het grootboek geschreven: {exc}"
        ) from exc
    return receipt.get("append_status")


def _write_intent_receipt(event: str, **kwargs: Any) -> None:
    """The intent receipt must be written as its own line: anything else refuses the action."""
    got = _write_door_receipt(event, phase="intent", **kwargs)
    if got != RECEIPT_WRITTEN:
        raise ForgeLedgerError(
            f"{event} (intent) niet als eigen regel in het grootboek gekomen "
            f"(append_status={got!r}): er gaat niets naar GitHub"
        )


def _write_outcome_receipt(event: str, **kwargs: Any) -> str:
    """The outcome receipt after the GitHub call: a non-written status is logged, never undone."""
    got = _write_door_receipt(event, phase="outcome", **kwargs)
    if got != RECEIPT_WRITTEN:
        logger.error(
            "%s (outcome) niet als eigen regel vastgelegd, ledger: %s (operatie %s)",
            event, got, kwargs.get("op_id"),
        )
    return got


class DoorRuns(NamedTuple):
    """Every ``vnx-gate/door`` run on one head, split into the own App's and foreign ones."""

    own: List[Dict[str, Any]]
    foreign: List[Dict[str, Any]]
    app_id: int

    def foreign_trail(self) -> List[Dict[str, Any]]:
        """Each foreign run as its id and the ``app.id`` GitHub showed (``None``: no ``app`` object)."""
        trail = []
        for run in self.foreign:
            app = run.get("app")
            trail.append(
                {"check_run_id": run.get("id"), "app_id": app.get("id") if isinstance(app, dict) else None}
            )
        return trail


def door_runs_on_head(sha: str, *, project_root: Path) -> DoorRuns:
    """Every door run on ``sha``, split on ``app.id`` by the reader's own rule.

    ``ci_contexts._split_by_app``: a run whose ``app`` is not an object, or whose
    ``app.id`` is not the own App, is foreign. Nothing is dropped, so a decision
    sees the foreign runs too. ``load_app_config`` refuses a missing or
    non-integer ``app_id``, so the helper's unbound branch (every run counts as
    own) is never taken from here.
    """
    app_id = load_app_config().app_id
    runs = list_check_runs(sha, DOOR_CHECK_NAME, project_root=project_root)
    own, foreign = _split_by_app(runs, app_id)
    return DoorRuns(own=list(own), foreign=list(foreign), app_id=app_id)


def _foreign_only_reason(head: DoorRuns, sha: str) -> str:
    observed = [entry["app_id"] for entry in head.foreign_trail()]
    return (
        f"{len(head.foreign)} deurrun(s) van een andere App op {sha[:12]} en geen van de eigen "
        f"App (app_id {head.app_id}): waargenomen app.id {observed}. De lezer houdt die kop "
        "blokkerend (wrong_app); de deur adopteert die run niet en zet er geen eigen run naast "
        "(ADR-061 §3, geval 4)"
    )


def _apply_door_permit(head: DoorRuns, sha: str, pr: int, project_root: Path) -> Tuple[int, str]:
    """The GitHub step of ADR-061 §3: ``(check_run_id, action)``, ``create`` or ``re_permit``.

    The case split is the reader's (``ci_contexts.classify_contexts``). Foreign
    runs and no own run is its blocking ``wrong_app`` state, so the door refuses
    (case 4) and makes no call: an own success next to it would turn that state
    into a permit. With an own run present the reader classifies the own runs
    only, so the own count decides (case 2 or 3) on a mixed head as well; the
    foreign runs go into the receipt.
    """
    if head.foreign and not head.own:
        raise ForgeDoorRefused(_foreign_only_reason(head, sha))
    title, summary = DOOR_CHECK_NAME, f"permit for PR #{pr} on {sha}"
    if len(head.own) > 1:
        raise ForgeDoorRefused(
            f"{len(head.own)} deurruns van de eigen App op {sha[:12]}: een tweede run houdt de "
            "context rood; herstel is een nieuwe commit"
        )
    if head.own:
        run_id = head.own[0].get("id")
        patch_check_run(
            run_id, CONCLUSION_SUCCESS, title, summary, name=DOOR_CHECK_NAME, head_sha=sha,
            project_root=project_root,
        )
        return run_id, "re_permit"
    created = publish_check_run(
        sha, DOOR_CHECK_NAME, CONCLUSION_SUCCESS, summary, project_root=project_root
    )
    run_id = created.get("id")
    if isinstance(run_id, bool) or not isinstance(run_id, int):
        raise ForgeDoorRefused(f"POST gaf geen check_run_id terug: {created!r}")
    return run_id, "create"


def publish_door_permit(
    evidence: Dict[str, Any], *, sha: str, pr: int, project_root: Path,
    receipts_file: Optional[str] = None,
) -> int:
    """Create or re-permit the single ``vnx-gate/door`` run on ``sha``; return its id.

    The ledger comes first (ADR-005): bad evidence writes a ``blocked`` receipt;
    otherwise an intent receipt is written BEFORE the first GitHub call, and a
    failed write stops everything. The four cases of ADR-061 §3, split as the
    reader splits (:func:`door_runs_on_head`): no door run at all, ``POST``;
    exactly one own run, ``PATCH`` it back to ``success`` (the re-permit on the
    same head); two or more own runs, refused, recovery is a new commit; only
    foreign runs, refused with no call. After the call the run is read back and
    the outcome receipt carries the id, the conclusion read and every foreign
    door run either list showed (``foreign_door_runs``).
    """
    op_id = _new_operation_id()
    base = {
        "head_sha": sha, "pr_number": pr, "receipts_file": receipts_file, "op_id": op_id,
        "evidence_digest": _evidence_digest(evidence),
        "evidence_gates": _evidence_gate_names(evidence),
    }
    try:
        validate_door_evidence(evidence, sha=sha, pr=pr)
    except ForgeDoorRefused as exc:
        _write_outcome_receipt(
            "door_permit_published", status="blocked", action="publish",
            reason=str(exc), **base,
        )
        raise
    _write_intent_receipt(
        "door_permit_published", status="info", action="publish",
        reason="permit aangevraagd uit GO-evidence", **base,
    )
    run_id: Optional[int] = None
    heads: List[DoorRuns] = []
    try:
        heads.append(door_runs_on_head(sha, project_root=project_root))
        run_id, action = _apply_door_permit(heads[0], sha, pr, project_root)
        heads.append(door_runs_on_head(sha, project_root=project_root))
        run = read_back_door(sha, run_id, project_root=project_root, listed=heads[1])
    except ForgeCheckRunError as exc:
        _write_outcome_receipt(
            "door_permit_published",
            status="blocked" if isinstance(exc, ForgeDoorRefused) else "failure",
            action="publish", reason=str(exc),
            **({"check_run_id": run_id} if run_id is not None else {}),
            **_foreign_field(heads), **base,
        )
        raise
    _write_outcome_receipt(
        "door_permit_published", status="success", action=action, check_run_id=run_id,
        conclusion=run.get("conclusion"), reason="permit gepubliceerd en teruggelezen",
        **_foreign_field(heads), **base,
    )
    return run_id


def _foreign_field(heads: List[DoorRuns]) -> Dict[str, Any]:
    """``foreign_door_runs`` over every list this operation read, each foreign run once.

    Absent, not ``[]``, when no list was read: a head that could not be listed
    is not a head without foreign runs.
    """
    if not heads:
        return {}
    seen: List[Dict[str, Any]] = []
    for head in heads:
        for entry in head.foreign_trail():
            if entry not in seen:
                seen.append(entry)
    return {"foreign_door_runs": seen}


def read_back_door(
    sha: str, check_run_id: int, *, project_root: Path, listed: Optional[DoorRuns] = None
) -> Dict[str, Any]:
    """The door run on ``sha`` with this id, own App and ``success``, or a named refusal.

    ``listed`` is the list read AFTER the call, when the caller already holds it
    (:func:`publish_door_permit` does, so its receipt names what this verdict
    saw); without it the runs are read here. Split as the reader splits: more
    than one own run refuses; the id must be one of the own runs, and an id that
    is a foreign run (another App, or no ``app`` object) refuses. A foreign run
    NEXT TO the own success run does not refuse: with an own run present the
    reader classifies the own runs only (``classify_contexts``), so the context
    reads green on that run and the permit is the run this call made or patched.
    """
    head = listed if listed is not None else door_runs_on_head(sha, project_root=project_root)
    if len(head.own) > 1:
        raise ForgeDoorRefused(f"teruglezing: {len(head.own)} deurruns van de eigen App op {sha[:12]}")
    run = next((r for r in head.own if r.get("id") == check_run_id), None)
    if run is None:
        if any(r.get("id") == check_run_id for r in head.foreign):
            raise ForgeDoorRefused(f"teruglezing: deurrun {check_run_id} is van een andere App")
        raise ForgeDoorRefused(f"teruglezing: deurrun {check_run_id} staat niet op {sha[:12]}")
    if run.get("conclusion") != CONCLUSION_SUCCESS:
        raise ForgeDoorRefused(
            f"teruglezing: deurrun {check_run_id} heeft conclusion {run.get('conclusion')!r}"
        )
    return run


def _patch_door_failure(
    check_run_id: int, title: str, sha: str, project_root: Path
) -> str:
    for attempt in range(DOOR_REVOKE_ATTEMPTS):
        try:
            answer = patch_check_run(
                check_run_id, CONCLUSION_FAILURE, title, title,
                name=DOOR_CHECK_NAME, head_sha=sha, project_root=project_root,
            )
            if answer.get("id") == check_run_id and answer.get("conclusion") == CONCLUSION_FAILURE:
                return REVOKED
        except ForgeCheckRunError as exc:
            logger.warning("deurrun %s intrekken, poging %d: %s", check_run_id, attempt + 1, exc)
        if attempt < DOOR_REVOKE_ATTEMPTS - 1:
            time.sleep(_DOOR_REVOKE_BACKOFF_SECONDS[attempt])
    return REVOKE_FAILED


def revoke_door(
    check_run_id: int, reason: str, *, sha: str, pr: int, project_root: Path,
    receipts_file: Optional[str] = None,
) -> str:
    """``PATCH`` the door run to ``failure`` (title ``permit revoked: <reason>``).

    The intent receipt is written first; if that write fails nothing is sent to
    GitHub and :class:`ForgeLedgerError` is raised. Three attempts with backoff.
    ``revoked`` only when GitHub's answer shows this id on ``failure``; anything
    else is ``revoke_failed``. There is no DELETE. The outcome receipt is
    best-effort on its own failure: the revoke result is what the caller needs,
    and the intent receipt already holds the attempt.
    """
    if not sha or not sha.strip():
        raise ForgeCheckRunError("sha is leeg: een intrekking hoort bij een kop")
    base = {
        "head_sha": sha, "pr_number": pr, "check_run_id": check_run_id, "action": "revoke",
        "receipts_file": receipts_file, "op_id": _new_operation_id(),
    }
    _write_intent_receipt("door_permit_revoked", status="info", reason=reason, **base)
    result = _patch_door_failure(check_run_id, f"permit revoked: {reason}", sha, project_root)
    try:
        _write_outcome_receipt(
            "door_permit_revoked", status="success" if result == REVOKED else "failure",
            reason=reason, revoke_result=result, **base,
        )
    except ForgeLedgerError as exc:
        logger.error("uitkomst van intrekking %s niet vastgelegd: %s", check_run_id, exc)
    return result


def _add_door_parser(sub: Any) -> None:
    door = sub.add_parser(
        "door",
        help=f"herstel- en testpad: zet {DOOR_CHECK_NAME} uit GO-evidence (geen tweede schrijver)",
        description=(
            "Herstel- en testpad voor de toestemming van de deur (ADR-061 contract 16(b)). De "
            "enige schrijver is pr_merge._run_door. Dit pad eist hetzelfde GO-evidence-bestand "
            "en kan geen GO verzinnen. De echte autoriteit is de App-sleutel in de keychain; "
            "het evidence-bestand is een tweede slot tegen vergissingen, geen beveiligingsgrens."
        ),
    )
    door.add_argument("--evidence", required=True, help="GO-evidence-bestand van de deur")
    door.add_argument("--sha", required=True, help="kop waarop de toestemming komt")
    door.add_argument("--pr", type=int, required=True, help="PR-nummer")
    door.add_argument(
        "--project-root", default=None,
        help="checkout van het doelproject (standaard afgeleid uit de resultatenmap)",
    )
    door.add_argument("--receipts-file", default=None, help="grootboekbestand (standaard het canonieke)")


def _door_project_root(args: argparse.Namespace) -> Path:
    if args.project_root:
        return Path(args.project_root)
    return resolve_forge_target(_default_results_dir()).project_root


def _run_door(args: argparse.Namespace) -> int:
    """``door`` — validate, publish or re-permit, read back. Exit 1 with the reason on refusal."""
    try:
        project_root = _door_project_root(args)
        try:
            evidence = read_door_evidence(Path(args.evidence))
        except ForgeDoorRefused as exc:
            _write_outcome_receipt(
                "door_permit_published", status="blocked", receipts_file=args.receipts_file,
                op_id=_new_operation_id(), action="publish", head_sha=args.sha,
                pr_number=args.pr, reason=str(exc),
            )
            raise
        run_id = publish_door_permit(
            evidence, sha=args.sha, pr=args.pr, project_root=project_root,
            receipts_file=args.receipts_file,
        )
    except ForgeCheckRunError as exc:
        print(f"{DOOR_CHECK_NAME}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"check_run_id={run_id}")
    return EXIT_OK


def _run_offline_command(args: argparse.Namespace) -> Optional[int]:
    """Commands that need no results dir and no PR lookup; ``None`` for the rest."""
    if args.command == "door":
        return _run_door(args)
    if args.command != "pending-preview":
        return None
    try:
        return _run_pending_preview(args)
    except ForgeCheckRunError as exc:
        # Named after the COMMAND, not after vnx-gate/review: a refusal
        # here is usually about some other pending entry, and prefixing it
        # with the review check's name would point the operator at the one
        # entry that is fine.
        print(f"pending-preview: {exc}", file=sys.stderr)
        return EXIT_ERROR


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="forge_gate_publisher.py",
        description=(
            "Publiceer het poortoordeel van een PR als GitHub check-run "
            "(vnx-gate/<poort> per poort, vnx-gate/review samenvattend), vanaf de "
            "schijf-records."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    publish = sub.add_parser("publish", help="herpubliceer vanaf het schijf-record van een PR")
    publish.add_argument("--pr", type=int, required=True, help="PR-nummer")
    publish.add_argument(
        "--gate",
        default=None,
        help="poortnaam (laat weg om elke poort met een record voor deze PR te doen)",
    )
    publish.add_argument(
        "--dry-run", action="store_true", help="toon conclusie en payload, POST niets"
    )
    publish.add_argument(
        "--results-dir",
        default=None,
        help="resultatenmap (standaard ${VNX_STATE_DIR}/review_gates/results)",
    )

    review = sub.add_parser(
        "review",
        help=f"publiceer de samenvattende check {REVIEW_SUMMARY_CHECK_NAME} voor een PR",
    )
    review.add_argument("--pr", type=int, required=True, help="PR-nummer")
    review.add_argument(
        "--dry-run", action="store_true", help="toon conclusie en payload, POST niets"
    )
    review.add_argument(
        "--results-dir",
        default=None,
        help="resultatenmap (standaard ${VNX_STATE_DIR}/review_gates/results)",
    )
    review.add_argument(
        "--branch",
        default=None,
        help="head-branch (standaard opgevraagd via gh; dezelfde scope als de merge-deur)",
    )

    preview = sub.add_parser(
        "pending-preview",
        help="toon het PUT-object dat de apply zou versturen na promotie van pending_checks",
    )
    preview.add_argument(
        "--yaml",
        default=None,
        help="pad naar branch_protection.yaml (standaard die van deze checkout)",
    )

    _add_door_parser(sub)
    args = parser.parse_args(argv)

    # Reads a file, talks to nobody, and must therefore never be gated on a
    # PR lookup or a results dir.
    offline = _run_offline_command(args)
    if offline is not None:
        return offline

    results_dir = Path(args.results_dir) if args.results_dir else _default_results_dir()

    # The repo comes from the store being published, before any gh lookup: the
    # head, the branch and the auto-merge state must all be read from the repo
    # the check-run lands on, not from whatever repo the cwd happens to be.
    try:
        repo = resolve_forge_target(results_dir).owner_repo
    except ForgeCheckRunError as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    head_sha = _resolve_head_sha(args.pr, repo)
    if not head_sha:
        print(
            f"kan de kop van PR #{args.pr} niet ophalen (gh pr view --json headRefOid gaf "
            "niets terug) — zonder kop wordt er niets gepubliceerd",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if args.command == "review":
        try:
            return _run_review(args, results_dir, head_sha, repo)
        except ForgeCheckRunError as exc:
            print(f"{REVIEW_SUMMARY_CHECK_NAME}: {exc}", file=sys.stderr)
            return EXIT_ERROR

    gates = [args.gate] if args.gate else gates_with_a_record(results_dir, args.pr)
    if not gates:
        print(
            f"geen enkel poort-record gevonden voor PR #{args.pr} in {results_dir} — "
            "draai eerst een review-poort op deze kop",
            file=sys.stderr,
        )
        return EXIT_ERROR

    worst = EXIT_OK
    for gate in gates:
        try:
            rc = _publish_one(args.pr, gate, head_sha, results_dir, dry_run=args.dry_run)
        except ForgeCheckRunError as exc:
            print(f"{check_run_name(gate)}: {exc}", file=sys.stderr)
            worst = EXIT_ERROR
            continue
        if rc != EXIT_OK and worst == EXIT_OK:
            worst = rc
    return worst


if __name__ == "__main__":
    sys.exit(main())
