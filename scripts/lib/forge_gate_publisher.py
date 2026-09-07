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
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
_SCRIPTS_DIR = _LIB_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

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
    "RECOVERY_COMMAND_TEMPLATE",
    "ForgeAPIError",
    "ForgeAppConfigError",
    "ForgeCheckRunError",
    "ForgeKeychainError",
    "ForgePublishRefused",
    "ForgeStatusUnmapped",
    "ForgeVerdict",
    "check_run_name",
    "classify_record",
    "conclusion_for",
    "gates_with_a_record",
    "main",
    "publish_for_record",
    "read_result_record",
    "refuse_if_auto_merge_is_armed",
    "result_record_path",
]

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

#: The command that republishes a check-run from the record already on disk.
#: Carried in every failure log, so a publication the recorder swallowed is
#: always one copy-paste away from repair.
RECOVERY_COMMAND_TEMPLATE = (
    "python3 scripts/lib/forge_gate_publisher.py publish --pr {pr} --gate {gate}"
)

_GH_TIMEOUT_SECONDS = 15


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
    """``vnx-gate/<gate>`` — the name branch protection matches on."""
    if not gate or not gate.strip():
        raise ForgeCheckRunError("gate is leeg: een check-run zonder poortnaam matcht niets")
    return f"{CHECK_RUN_NAME_PREFIX}/{gate.strip()}"


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
    """
    path = record_path or result_record_path(
        results_dir if results_dir is not None else _default_results_dir(), pr_number, gate
    )
    if path is None or not path.exists():
        return None
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


def refuse_if_auto_merge_is_armed(pr_number: int) -> None:
    """Refuse any publication on a PR that has auto-merge queued.

    ``pr_merge.py:430`` adds ``--auto`` as soon as the repo allows it, and the
    repo does not (``allow_auto_merge: false``, held there by B1's drift
    check). If that ever flips, an armed PR turns this publisher into the thing
    that performs the merge: GitHub merges the moment the last required check
    goes green, with no human between the check-run and ``main``.

    So the refusal covers EVERY conclusion, not just ``success``. A queued
    auto-merge means a human already handed the decision to a robot, and this
    robot declines to be the one that pulls it — whatever it was about to say.
    """
    data = _gh_pr_json(pr_number, "autoMergeRequest")
    if data.get("autoMergeRequest"):
        raise ForgePublishRefused(
            f"PR #{pr_number} heeft een actieve auto-merge (autoMergeRequest): elke "
            "publicatie is geweigerd, want een groene check zou hier de merge zelf "
            "uitvoeren. Zet auto-merge uit (`gh pr merge --disable-auto`) en publiceer "
            "daarna opnieuw."
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

    Two refusals, both before anything is sent: an armed auto-merge
    (:func:`refuse_if_auto_merge_is_armed`), and a ``success`` for a record
    that is not a proven pass on this head. The second one re-asks
    :func:`_proven_pass_on_head` rather than trusting the conclusion it was
    just handed. That is not distrust of :func:`classify_record` — it is the
    one place where a mistake anywhere upstream turns into a green light on
    ``main``, so the predicate that DEFINES "proven" is consulted at the exact
    moment the green light would be given.
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

    refuse_if_auto_merge_is_armed(pr_number)

    record = read_result_record(
        pr_number, gate, results_dir=results_dir, record_path=record_path
    )
    verdict = classify_record(record, resolved_head)

    if verdict.conclusion == CONCLUSION_SUCCESS:
        proven, why = _proven_pass_on_head(record, resolved_head)
        if not proven:
            raise ForgePublishRefused(
                f"weiger success voor {check_run_name(gate)} op {resolved_head[:12]}: het "
                f"record is geen bewezen pass op deze kop — {why}"
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
    if dry_run:
        return payload

    response = publish_check_run(
        resolved_head, name, verdict.conclusion, payload["summary"], project_root=project_root
    )
    payload["check_run_id"] = response.get("id")
    return payload


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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="forge_gate_publisher.py",
        description=(
            "Publiceer het poortoordeel van een PR als GitHub check-run "
            "(vnx-gate/<poort>), vanaf het schijf-record."
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

    args = parser.parse_args(argv)

    results_dir = Path(args.results_dir) if args.results_dir else _default_results_dir()

    head_sha = _resolve_head_sha(args.pr)
    if not head_sha:
        print(
            f"kan de kop van PR #{args.pr} niet ophalen (gh pr view --json headRefOid gaf "
            "niets terug) — zonder kop wordt er niets gepubliceerd",
            file=sys.stderr,
        )
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
