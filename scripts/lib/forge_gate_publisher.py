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
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    load_app_config,
    publish_check_run,
)
from gate_status import (  # noqa: E402
    ALL_KNOWN_STATES,
    FAIL_STATES,
    INCOMPLETE_STATES,
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
    "publish_for_record",
    "publish_review_summary",
    "read_result_record",
    "read_result_record_at",
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


class ForgePublishRefused(ForgeCheckRunError):
    """This publication is refused on policy, not on transport.

    Distinct from :class:`ForgeAPIError` (GitHub said no) and
    :class:`ForgeKeychainError` (we could not authenticate): here nothing was
    ever sent, because sending it would have been wrong.
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
) -> ForgeVerdict:
    """Is there a valid review accord on THIS head? One verdict for all gates.

    Where :func:`classify_record` answers "what did gate X say", this answers
    the question branch protection actually needs answered, and it is the only
    one of the two that can be made required: a per-gate check would force
    whichever gate happened to be named, on every PR, while the fleet's real
    rule has always been "at least one review peer signed".

    **The rule is read, never restated.** ``closure_verifier``'s
    ``_REVIEW_PEER_GATES`` (who may sign — OI-1645 keeps ``ci_gate`` out: it
    verifies tests, lint and build, never the change) and
    ``check_review_gate_for_merge`` (whether a signature holds, including the
    OI-1576 takeover chain and the OI-1624 peer route) do the deciding. A
    second copy here would be a check that says GO where the merge door says
    NO-GO, and the whole point of publishing this to GitHub is that the two
    agree. ``TestTheRuleComesFromTheMergeDoor`` pins the reading: widen the
    door's peer set and this conclusion moves with it.

    On top of the door, :func:`_proven_pass_on_head` — the SAME predicate the
    per-gate publisher consults before a green check leaves the machine. The
    door does not read ``evidence_source``, so without this the summary would
    be MORE permissive than the per-gate check it summarizes, and a
    ``reanchored`` verdict could sign a head no gate ever ran against.

    The three conclusions, and why the split falls where it does:

    ``success``       a review peer signed this exact head, and that signature
                      survives the merge door's chain and the head/test-run/
                      provenance conditions.
    ``failure``       nobody signed, and the head is not waiting on anything:
                      some gate DID render a verdict here. Either a review
                      peer said no (or said yes and its own report contradicts
                      it), or the only evidence is non-review evidence — a
                      ``ci_gate`` pass and nothing else is the OI-1645 state,
                      fully judged and entirely unreviewed.
    ``action_required``  nothing decided has been said about this head at all:
                      no records, provider outage, a gate still running, or a
                      verdict that belongs to another commit. Absence of
                      evidence, which on a required check must block — never
                      ``neutral`` or ``skipped``, both of which GitHub reads
                      as SATISFIED.

    ``branch``/``project_id`` are passed straight through to the door. Handing
    it less scope than the merge door gets would make this check the more
    lenient of the two, which is the one direction that costs something.
    """
    resolved_head = _require_head(head_sha)
    directory = results_dir if results_dir is not None else _default_results_dir()
    pr_id = str(pr_number)

    # Imported at call time for the same reason _proven_pass_on_head does it:
    # closure_verifier's import tail has no business loading on every gate run
    # that merely records a result.
    from closure_verifier import (  # noqa: PLC0415
        _KNOWN_GATES,
        _REVIEW_PEER_GATES,
        _find_gate_result,
        check_review_gate_for_merge,
    )

    peers = sorted(_REVIEW_PEER_GATES)
    if not directory.exists():
        return ForgeVerdict(
            CONCLUSION_ACTION_REQUIRED,
            f"geen review-akkoord op deze kop: de resultatenmap {directory} bestaat niet, "
            "dus geen enkele review-poort heeft hier een record achtergelaten",
        )

    for gate in peers:
        door = check_review_gate_for_merge(
            pr_id, gate, directory, branch=branch, project_id=project_id,
            head_sha=resolved_head,
        )
        if door.get("verdict") != "GO":
            continue
        # WHICH record carried the GO: the declared gate's own, or the peer /
        # takeover successor the door accepted in its place.
        evidence_gate = str(door.get("evidence_gate") or gate)
        record = _find_gate_result(
            evidence_gate, pr_id, directory, branch=branch, project_id=project_id,
            head_sha=resolved_head,
        )
        proven, why = _proven_pass_on_head(record, resolved_head)
        if not proven:
            # The door accepts it, this layer does not. Never silent: the two
            # disagreeing is exactly the state an operator has to see, because
            # the merge would go through while the check stays red.
            logger.warning(
                "forge_gate_publisher: %s telt %s als ondertekenaar voor %s op %s, maar het "
                "record is hier geen bewezen pass (%s) — de samenvattende check blijft rood",
                "de merge-deur", evidence_gate, gate, resolved_head[:12], why,
            )
            continue
        via = (
            f"{evidence_gate} (via overname/ondertekening voor {gate})"
            if evidence_gate != gate
            else evidence_gate
        )
        return ForgeVerdict(
            CONCLUSION_SUCCESS,
            f"geldig review-akkoord op deze kop: {via} — {why}",
        )

    # Nobody signed. Is this head still waiting, or has it been judged and
    # found unreviewed? Everything the closure verifier can interpret counts
    # towards "something was decided here", INCLUDING the gates that may not
    # sign — that is precisely what separates the two red conclusions.
    spoken: List[str] = []
    for gate in sorted(_KNOWN_GATES):
        record = _find_gate_result(
            gate, pr_id, directory, branch=branch, project_id=project_id,
            head_sha=resolved_head,
        )
        if record is None:
            continue
        status = canonical_status(record)
        if status in (PASS_STATES | FAIL_STATES):
            spoken.append(f"{gate}={status}")

    if not spoken:
        return ForgeVerdict(
            CONCLUSION_ACTION_REQUIRED,
            "geen review-akkoord op deze kop: geen enkele poort heeft hier een beslissende "
            f"uitspraak achtergelaten (afwezig, uitgevallen, nog bezig, of een oordeel over "
            f"een andere commit). Draai een review-poort op {resolved_head[:12]}. "
            f"Ondertekenaars die tellen: {', '.join(peers)}.",
        )

    signers_spoke = [entry for entry in spoken if entry.split("=")[0] in _REVIEW_PEER_GATES]
    if signers_spoke:
        return ForgeVerdict(
            CONCLUSION_FAILURE,
            "geen geldig review-akkoord op deze kop: de review-poorten die hier spraken "
            f"({', '.join(signers_spoke)}) leverden geen bewezen pass. Volledige uitspraak "
            f"op deze kop: {', '.join(spoken)}.",
        )
    return ForgeVerdict(
        CONCLUSION_FAILURE,
        f"geen review-akkoord op deze kop: alleen niet-review-bewijs ({', '.join(spoken)}). "
        "CI is een tweede, zelfstandige merge-eis en nooit een ondertekenaar (OI-1645), "
        f"dus deze kop is beoordeeld maar niet gereviewd. Ondertekenaars die tellen: "
        f"{', '.join(peers)}.",
    )


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


def _gh_pr_json(pr_number: int, field: str) -> Dict[str, Any]:
    """One ``gh pr view --json <field>`` object, or refuse.

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
            ["gh", "pr", "view", str(pr_number), "--json", field],
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


def auto_merge_is_armed(pr_number: int) -> bool:
    """Whether this PR will merge itself once its checks go green.

    Raises :class:`ForgePublishRefused` when the answer cannot be established —
    see :func:`_gh_pr_json`. "I could not look" is not "no".
    """
    data = _gh_pr_json(pr_number, "autoMergeRequest")
    return bool(data.get("autoMergeRequest"))


def refuse_if_auto_merge_is_armed(pr_number: int) -> None:
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
    if auto_merge_is_armed(pr_number):
        raise ForgePublishRefused(
            f"PR #{pr_number} heeft een actieve auto-merge (autoMergeRequest): een "
            "success-publicatie is geweigerd, want een groene check zou hier de merge "
            "zelf uitvoeren. Zet auto-merge uit (`gh pr merge --disable-auto`) en "
            "publiceer daarna opnieuw."
        )


def _note_red_over_armed_auto_merge(pr_number: int, check_name: str, conclusion: str) -> None:
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
        armed = auto_merge_is_armed(pr_number)
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


def publish_for_record(
    pr_number: int,
    gate: str,
    head_sha: str,
    *,
    results_dir: Optional[Path] = None,
    record_path: Optional[Path] = None,
    dry_run: bool = False,
    project_root: Optional[Path] = None,
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

        if verdict.conclusion == CONCLUSION_SUCCESS:
            refuse_if_auto_merge_is_armed(pr_number)
            proven, why = _proven_pass_on_head(record, resolved_head)
            if not proven:
                raise ForgePublishRefused(
                    f"weiger success voor {check_run_name(gate)} op {resolved_head[:12]}: "
                    f"het record is geen bewezen pass op deze kop — {why}"
                )
        elif not dry_run:
            # Red goes out whatever the auto-merge state is; the operator still
            # gets told. A rehearsal posts nothing, so it has nothing to note.
            _note_red_over_armed_auto_merge(pr_number, check_run_name(gate), verdict.conclusion)

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
            resolved_head, name, verdict.conclusion, payload["summary"], project_root=project_root
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
    project_root: Optional[Path] = None,
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
    """
    resolved_head = _require_head(head_sha)

    # Before any network call: is the App registered at all? Same reason as in
    # publish_for_record — and a --dry-run skips it, because rehearsing the
    # conclusion is exactly what an operator wants BEFORE registering one.
    if not dry_run:
        load_app_config()

    try:
        verdict = review_verdict(
            pr_number, resolved_head, results_dir=results_dir, branch=branch,
            project_id=project_id,
        )
        if verdict.conclusion == CONCLUSION_SUCCESS:
            refuse_if_auto_merge_is_armed(pr_number)
        elif not dry_run:
            _note_red_over_armed_auto_merge(
                pr_number, REVIEW_SUMMARY_CHECK_NAME, verdict.conclusion
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
            project_root=project_root,
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
    from forge_protection_drift import load_protection_config  # noqa: PLC0415

    path = Path(yaml_path) if yaml_path is not None else DEFAULT_YAML_PATH
    config = load_protection_config(path)
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


def _resolve_head_sha(pr_number: int) -> str:
    """The PR head from GitHub, via the fleet's single source of truth.

    ``gate_recorder.get_pr_head_sha`` — never ``git rev-parse HEAD``, which
    resolves against the process cwd and is not the PR head (OI-1307).
    """
    from gate_recorder import get_pr_head_sha  # noqa: PLC0415

    return get_pr_head_sha(pr_number)


def _resolve_head_branch(pr_number: int) -> str:
    """The PR's head branch, or refuse.

    Uses this module's strict :func:`_gh_pr_json`, not
    ``gate_recorder._gh_pr_view_field`` (which returns "" on every failure).
    The leniency is right when stamping identity onto a record; here an empty
    branch would silently widen the scope handed to the merge door, and this
    check would then accept evidence the door itself rejects as stale — the
    one direction a summary check may never fall.
    """
    branch = str(_gh_pr_json(pr_number, "headRefName").get("headRefName") or "").strip()
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


def _run_review(args: argparse.Namespace, results_dir: Path, head_sha: str) -> int:
    """``review`` — publish the one summary check for this head."""
    branch = args.branch or _resolve_head_branch(args.pr)
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

    args = parser.parse_args(argv)

    # Reads a file, talks to nobody, and must therefore never be gated on a
    # PR lookup or a results dir.
    if args.command == "pending-preview":
        try:
            return _run_pending_preview(args)
        except ForgeCheckRunError as exc:
            print(f"{REVIEW_SUMMARY_CHECK_NAME}: {exc}", file=sys.stderr)
            return EXIT_ERROR

    results_dir = Path(args.results_dir) if args.results_dir else _default_results_dir()

    head_sha = _resolve_head_sha(args.pr)
    if not head_sha:
        print(
            f"kan de kop van PR #{args.pr} niet ophalen (gh pr view --json headRefOid gaf "
            "niets terug) — zonder kop wordt er niets gepubliceerd",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if args.command == "review":
        try:
            return _run_review(args, results_dir, head_sha)
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
