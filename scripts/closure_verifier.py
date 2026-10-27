#!/usr/bin/env python3
"""Governance closure verification for VNX feature branches.

Includes review-contract enforcement: closure cannot be claimed unless
the contractually required review gates have produced evidence and the
deterministic findings are clean.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt
from review_contract import ReviewContract
from codex_final_gate import enforce_codex_gate
from codex_severity_translator import translate_findings as translate_codex_findings
from gate_lane_contract import canonical_review_verdict
from gate_status import (
    FAIL_STATES as _GATE_FAIL_STATES,
    PARTIAL_REVIEW_STATES as _GATE_PARTIAL_REVIEW_STATES,
    PASS_STATES as _GATE_PASS_STATES,
    UNAVAILABLE_STATES as _GATE_UNAVAILABLE_STATES,
    review_coverage_gap as gate_review_coverage_gap,
    canonical_status as gate_canonical_status,
    decided_verdict as gate_decided_verdict,
    has_complete_evidence as gate_has_complete_evidence,
    is_pass as gate_is_pass,
    is_terminal as gate_is_terminal,
    is_test_run_record as _is_test_run_record,
)
from dispatch_spec import RETIRED_GATE_NAMES, Gate
from gate_obligations import (
    REASON_FULFILLED_BY_TAKEOVER,
    STATUS_FULFILLED,
    iter_obligations_in,
    obligation_matches_pr,
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


# Gates the closure verifier implements. A gate name in a review_stack that is
# not in this set is unverifiable: the verifier can neither attest a pass nor a
# fail for it, so it is reported as UNVERIFIED and any result file found for it
# is ignored. This closes the leak where an unknown gate passed on file
# presence alone.
#
# Derived from the canonical Gate enum (scripts/lib/dispatch_spec.py, OI-845),
# not a second hand-written list. A gate that the enum declares but the closure
# verifier does not implement lives in _GATES_NOT_IMPLEMENTED_BY_CLOSURE with a
# reason — a derived subset with a rationale, so the two collections can never
# silently drift (pinned by test_closure_verifier_gate_enum_drift).
#
# wiring_gate is intentionally excluded: it is in the Gate enum (and in
# gate_request_handler / gate_recorder), but it does not emit a terminal
# result-record carrying contract_hash + report_path, so the generic known-gate
# handling could not honestly attest it. Keeping it outside _KNOWN_GATES also
# preserves OI-1093 (#1422): records for gates NOT in _KNOWN_GATES must carry a
# producer identity (dispatch_id). Pulling wiring_gate in would drop that
# requirement. When in doubt, the side with more verification wins.
#
# RETIRED_GATE_NAMES stay interpretable so a record written before the retirement
# does not read as a config gap. Outside the Gate enum, they are never selectable
# and, absent from _REVIEW_PEER_GATES below, never a peer signer.
_GATES_NOT_IMPLEMENTED_BY_CLOSURE = frozenset({"wiring_gate"})
_KNOWN_GATES = frozenset(
    g.value for g in Gate
    if g.value not in _GATES_NOT_IMPLEMENTED_BY_CLOSURE
) | RETIRED_GATE_NAMES


# OI-1645: which gates may stand in as a PEER SIGNER when the declared gate is
# a confirmed absence (_consult_peers_for_absence) or an OI-1576 takeover
# successor (_find_takeover_successor_results). Being in _KNOWN_GATES only
# means the closure verifier can INTERPRET a gate's result record — it says
# nothing about whether that record represents a code review. Signing power
# is a strictly narrower claim.
#
# Measured live 2026-09-06 (PR #1781, main c41c7f36): with codex_gate
# unavailable and only ci_gate carrying a full, all-seven-invariant-passing
# PASS on the head, ``check_review_gate_for_merge`` returned GO with
# ``evidence_gate="ci_gate"`` — zero review gates had spoken. ci_gate renders
# a genuine, fully-evidenced pass/fail (it satisfies every invariant
# ``_merge_door_record_verdict`` checks), so nothing in the evidence-quality
# chain catches this; it verifies CI checks (tests, lint, build), never the
# code change itself. ADR-037's addendum below names the distinction: a peer
# is a review gate, CI is a second, independent merge requirement, never a
# signer.
#
# Derived from the Gate enum with a reason per exclusion — the same
# discipline _GATES_NOT_IMPLEMENTED_BY_CLOSURE already uses — so a gate added
# to the enum tomorrow is UNCLASSIFIED (caught by the drift test in
# tests/test_oi1645_peer_is_review_gate.py) instead of silently inheriting
# peer-signing rights.
#
# claude_github_optional is deliberately NOT excluded: reading
# claude_github_receipt.EVIDENCE_STATES / INTENTIONALLY_ABSENT_STATES and
# gate_request_handler's ``completed`` branch, its terminal outcome
# (``state="completed"`` + ``result_status="pass"|"fail"``) is a genuine
# Claude code review of the diff, not a CI signal — it being optional
# (may never run) only means its own absence states (not_configured,
# configured_dry_run) never coerce to a decided status, so an unrun instance
# contributes nothing to the peer route either way. Optional-but-real review
# evidence still counts when it exists; only non-review evidence is excluded.
_NON_REVIEW_SIGNER_REASONS: Dict[str, str] = {
    "wiring_gate": (
        "not implemented by the closure verifier at all — carries no "
        "interpretable verdict to sign with (see _GATES_NOT_IMPLEMENTED_BY_CLOSURE)"
    ),
    "ci_gate": (
        "verifies CI checks (tests, lint, build), not a code review of the "
        "change — ADR-037 addendum (OI-1645): CI is a second, independent "
        "merge requirement (enforced separately in pr_merge._run_ci_gate), "
        "never a review peer signer"
    ),
}


def classify_gate_signers(
    enum_values: Iterable[str],
    not_implemented: frozenset,
    non_review_reasons: Dict[str, str],
) -> frozenset:
    """Derive the review-peer gate set, refusing to let a gate the closure
    verifier cannot even read (``not_implemented``) inherit peer-signing
    rights by omission (OI-1645 follow-up, A6).

    Before this function existed, ``_REVIEW_PEER_GATES`` was a plain
    complement of ``_NON_REVIEW_SIGNER_REASONS`` with nothing comparing it to
    ``_GATES_NOT_IMPLEMENTED_BY_CLOSURE``. A gate added to the enum and to
    ``_GATES_NOT_IMPLEMENTED_BY_CLOSURE`` but forgotten in
    ``_NON_REVIEW_SIGNER_REASONS`` would silently fall out of the complement
    as a review peer — a gate the verifier cannot interpret would gain
    signing power. This function makes that omission a hard failure instead.
    """
    non_review_set = frozenset(non_review_reasons)

    unreadable_without_reason = sorted(not_implemented - non_review_set)
    if unreadable_without_reason:
        raise ValueError(
            "gate(s) excluded from closure verification (not_implemented) "
            "but missing a non-review-signer reason, so they cannot be "
            f"classified as either a peer or a non-signer: {unreadable_without_reason}"
        )

    empty_reason = sorted(g for g, reason in non_review_reasons.items() if not reason)
    if empty_reason:
        raise ValueError(f"non-review-signer reason is empty for: {empty_reason}")

    return frozenset(g for g in enum_values if g not in non_review_set)


_REVIEW_PEER_GATES = classify_gate_signers(
    (g.value for g in Gate), _GATES_NOT_IMPLEMENTED_BY_CLOSURE, _NON_REVIEW_SIGNER_REASONS
)


def _run(cmd: Sequence[str], *, cwd: Optional[Path] = None, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _find_branch_pr(branch: str) -> Optional[Dict[str, Any]]:
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,url,state,mergeStateStatus,mergeCommit,statusCheckRollup,headRefName,baseRefName",
        ]
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return payload[0] if payload else None


def _remote_branch_exists(branch: str, project_root: Path) -> bool:
    result = _run(["git", "ls-remote", "--heads", "origin", branch], cwd=project_root)
    return result.returncode == 0 and bool(result.stdout.strip())


def _rollup_all_green(rollup: List[Dict[str, Any]]) -> bool:
    """Decide whether a GitHub statusCheckRollup represents an all-green CI state.

    Handles both ``CheckRun`` (GitHub Actions) entries with ``status``/``conclusion``
    fields, and ``StatusContext`` (commit status) entries with a ``state`` field.
    A rollup containing only ``StatusContext`` entries must be evaluated against
    the ``state`` field — filtering to ``CheckRun`` only would produce an empty
    generator and silently treat failing/pending commit statuses as passing.

    A rollup with no recognised entries (or empty rollup) is NOT green: callers
    that require CI evidence should treat absent evidence as failure.
    """
    if not rollup:
        return False

    recognised = 0
    for item in rollup:
        typename = item.get("__typename") or ""
        if typename == "CheckRun":
            recognised += 1
            if item.get("status") != "COMPLETED" or item.get("conclusion") != "SUCCESS":
                return False
        elif typename == "StatusContext":
            recognised += 1
            if (item.get("state") or "").upper() != "SUCCESS":
                return False
        else:
            # Unknown entry type — be conservative and treat as not-green so
            # we never silently approve an unrecognised rollup shape.
            return False

    return recognised > 0


def _merge_commit_on_main(oid: str, project_root: Path) -> bool:
    _run(["git", "fetch", "origin", "main", "--quiet"], cwd=project_root)
    result = _run(["git", "merge-base", "--is-ancestor", oid, "origin/main"], cwd=project_root)
    return result.returncode == 0


def _load_claim_file(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(_read_text(path))
    except json.JSONDecodeError:
        return {}


def _validate_test_claims(claims: Dict[str, Any], project_root: Path) -> List[CheckResult]:
    results: List[CheckResult] = []
    test_files = claims.get("test_files") or []
    test_command = (claims.get("test_command") or "").strip()
    trusted_result = claims.get("trusted_test_result")

    if not test_files:
        results.append(CheckResult("test_files", "FAIL", "no claimed test files provided"))
    else:
        missing = []
        for rel in test_files:
            path = Path(rel)
            if not path.is_absolute():
                path = project_root / rel
            if not path.exists():
                missing.append(str(rel))
        if missing:
            results.append(CheckResult("test_files", "FAIL", f"missing claimed test files: {', '.join(missing)}"))
        else:
            results.append(CheckResult("test_files", "PASS", f"{len(test_files)} claimed test file(s) exist"))

    if test_command:
        results.append(CheckResult("test_command", "PASS", "claimed test command present"))
    elif trusted_result:
        results.append(CheckResult("test_command", "PASS", "trusted test result recorded"))
    else:
        results.append(CheckResult("test_command", "FAIL", "no test command or trusted test result provided"))

    parallel_assignments = claims.get("parallel_assignments") or []
    if parallel_assignments:
        terminals = [str(entry.get("terminal")).strip() for entry in parallel_assignments if entry.get("terminal")]
        duplicates = sorted({terminal for terminal in terminals if terminals.count(terminal) > 1})
        if duplicates:
            results.append(
                CheckResult(
                    "parallelism",
                    "FAIL",
                    f"same terminal reported for parallel work: {', '.join(duplicates)}",
                )
            )
        else:
            results.append(CheckResult("parallelism", "PASS", "parallel assignments use distinct terminals"))

    commit_map = claims.get("commit_pr_map") or {}
    if commit_map:
        bad = []
        for sha in commit_map.keys():
            result = _run(["git", "rev-parse", "--verify", f"{sha}^{{commit}}"], cwd=project_root)
            if result.returncode != 0:
                bad.append(sha)
        if bad:
            results.append(CheckResult("commit_mapping", "FAIL", f"unknown commit(s): {', '.join(bad)}"))
        else:
            results.append(CheckResult("commit_mapping", "PASS", "commit-to-PR mapping references known commits"))

    return results


def _matches_required_field(supplied: Optional[str], actual: Optional[str]) -> bool:
    """Three answers for a caller-supplied match constraint, not two (OI-1318).

    ``if head_sha:`` collapsed two different callers into one behaviour. A
    caller that does not pass a sha is saying "I am not scoping by sha". A
    caller that passes an EMPTY sha is saying something else entirely: it tried
    to resolve the PR head and could not. Under the truthy test both took the
    branch that skips the check, so a merge door that failed to determine the
    head silently dropped its own head-sha requirement and accepted any result
    for the PR — including one recorded against a commit that is no longer
    there.

    * ``None``  — not required; every record passes this constraint.
    * ``""``    — required but unresolvable; NO record passes. An unverifiable
                  state is a refusal, which is what the sibling CI gate already
                  does when ``gh`` gives it no head.
    * a value   — the record must carry exactly that value.

    The distinction lives in what the CALLER meant, so callers have to say it.
    ``ReviewContract.branch`` defaults to ``""`` and one live contract in this
    store carries that default: a contract that never had a branch is "no
    constraint", not "I tried to resolve one and failed". The closure
    verifier's internal call sites therefore pass ``contract.branch or None``,
    while the merge door passes the empty string it actually got from ``gh``
    (glm_gate advisory on 5480324a).

    An absent field on the record fails a non-``None`` constraint: a result
    without a ``commit_sha`` is stale evidence, not a wildcard.
    """
    if supplied is None:
        return True
    if supplied == "":
        return False
    return (actual or "") == supplied


def _record_matches_scope(
    data: Dict[str, Any],
    pr_id: str,
    branch: Optional[str],
    project_id: Optional[str],
    head_sha: Optional[str],
) -> bool:
    """Shared scope matcher for gate result records.

    Used by both ``_find_gate_result`` (the declared gate's own record) and
    ``_find_takeover_successor_results`` (OI-1576 provenance route) so the two
    read paths can never drift apart on what counts as in-scope evidence.
    """
    # Offline test-run records must never count as evidence for a real PR.
    # The writer (kimi_gate --diff-file) stamps them test_run: true. Reject
    # before any pr_id/branch matching so a synthetic pr_id (0, 1, 2)
    # cannot align with a real contract.
    if _is_test_run_record(data):
        return False
    # PR-scoped AND matching: if data carries pr_id it must match the queried pr_id.
    # This prevents a result from a different PR satisfying a closure check even when
    # the filename-based lookup finds a contract for the correct PR name.
    data_pr_id = data.get("pr_id")
    if data_pr_id and data_pr_id != pr_id:
        return False
    # ADR-007: project_id must be present and match when caller supplies one.
    if project_id is not None:
        data_pid = (data.get("project_id") or "").strip()
        if not data_pid or data_pid != project_id:
            return False
    # ADR-005: branch must be present and match when caller supplies one.
    # A branch-less result is stale evidence from a prior feature.
    if not _matches_required_field(branch, data.get("branch")):
        return False
    # OI-1307: head sha must be present and match when caller supplies one —
    # same strictness as branch (a result without commit_sha is stale).
    if not _matches_required_field(head_sha, data.get("commit_sha")):
        return False
    return True


def _iter_gate_result_candidates(
    gate: str,
    pr_id: str,
    results_dir: Path,
) -> Iterable[Dict[str, Any]]:
    """Every on-disk record naming this exact gate+pr_id, contract file first
    then the legacy glob — the shared path patterns and pr_id/gate matching
    both ``_find_gate_result`` (scoped) and ``_find_gate_result_ignoring_scope``
    (OI-1642, unscoped) read, so the two lookups can never grow a second,
    diverging glob.

    Branch/project_id/head_sha scope is NOT applied here — callers decide.
    Offline test-run records ARE excluded unconditionally: never valid
    evidence for a real PR, in or out of scope.

    Every file is yielded at most once: the legacy glob also matches the
    contract file, which was already read first (OI-2113: the rejection scan
    reads every candidate, so a file read twice would be judged twice).
    """
    pr_slug = pr_id.lower().replace("-", "")
    # Contract-based results: {pr_slug}-{gate}-contract.json
    contract_path = results_dir / f"{pr_slug}-{gate}-contract.json"
    if contract_path.exists():
        try:
            data = json.loads(_read_text(contract_path))
            if not _is_test_run_record(data):
                yield data
        except (json.JSONDecodeError, OSError):
            pass
    # Legacy pattern: pr-{number}-{gate}.json — require both pr_id AND gate to match
    for path in results_dir.glob(f"*-{gate}*.json"):
        if path == contract_path:
            continue
        try:
            data = json.loads(_read_text(path))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("pr_id") == pr_id and data.get("gate") == gate and not _is_test_run_record(data):
            yield data


def _find_gate_result(
    gate: str,
    pr_id: str,
    results_dir: Path,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Search for a gate result file matching the PR and gate name.

    ADR-005: if ``branch`` is provided, results without a matching ``branch``
    field are rejected as stale evidence from a prior feature.
    ADR-007: if ``project_id`` is provided, results with a missing or
    mismatched ``project_id`` field are rejected.
    OI-1307: if ``head_sha`` is provided, results whose ``commit_sha`` does not
    match are rejected with the SAME strictness as the CI gate — a result
    produced against a different commit is stale evidence for the merge.
    If a result carries a ``report_path``, that file must exist on disk or the
    result is treated as stale and skipped.

    The first of :func:`_find_gate_results`, read by the same generator.
    """
    return next(_iter_gate_results_in_scope(gate, pr_id, results_dir, branch, project_id, head_sha), None)


def _find_gate_results(
    gate: str,
    pr_id: str,
    results_dir: Path,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Every in-scope record of ``gate`` for ``pr_id``, in the order :func:`_find_gate_result` reads them.

    The seat evaluation's rejection scan reads all of them (OI-2113): the
    first one is the record that may fill a seat, and a FAIL in any of them
    rejects the head. One generator serves both lookups, so the scope rules
    (this PR, this branch/project/head, no test run) cannot drift between
    the record that fills a seat and the records that can reject.
    """
    return list(_iter_gate_results_in_scope(gate, pr_id, results_dir, branch, project_id, head_sha))


def _iter_gate_results_in_scope(
    gate: str,
    pr_id: str,
    results_dir: Path,
    branch: Optional[str],
    project_id: Optional[str],
    head_sha: Optional[str],
) -> Iterator[Dict[str, Any]]:
    for data in _iter_gate_result_candidates(gate, pr_id, results_dir):
        if _record_matches_scope(data, pr_id, branch, project_id, head_sha):
            yield data


def _find_gate_result_ignoring_scope(
    gate: str,
    pr_id: str,
    results_dir: Path,
) -> Optional[Dict[str, Any]]:
    """OI-1642: does ANY record exist for this exact gate+pr_id at all,
    regardless of branch/project_id/head_sha?

    ``_find_gate_result`` returning ``None`` conflates two different facts:
    the gate was never asked for this PR at all (case a — must stay NO-GO,
    unchanged, per ``TestUnrelatedRecordsNeverTakeOver``/ADR-037's "geen enig
    record krijgt geen peer-fallback"), versus a record exists but falls
    outside the requested scope — e.g. a ``not_executable`` record written
    before OI-1624 threaded ``branch``/``commit_sha`` through
    (``gate_request_handler._write_not_executable_result``), which the
    overwrite guard (OI-1469/OI-1470) then refuses to ever rerun once
    terminal (measured live: PR #1777's ``codex_gate`` record).

    Only ever consulted by ``check_review_gate_for_merge`` AFTER
    ``_find_gate_result`` has already returned ``None`` — never a substitute
    for the scoped lookup, and the caller still re-validates absence
    (``_is_absent_without_verdict``) before treating this as evidence of
    anything.

    Same candidate set and read order as ``_find_gate_result``
    (``_iter_gate_result_candidates``); returns the first candidate, matching
    ``_find_gate_result``'s own first-match-wins behaviour.
    """
    for data in _iter_gate_result_candidates(gate, pr_id, results_dir):
        return data
    return None


def _describe_scope_mismatch(
    data: Dict[str, Any],
    branch: Optional[str],
    project_id: Optional[str],
    head_sha: Optional[str],
) -> str:
    """Name which scope fields an out-of-scope record failed to satisfy, for
    the OI-1642 peer-route message — so the operator sees WHY the declared
    gate's own record could not stand for itself, not just that it could
    not."""
    failing: List[str] = []
    if not _matches_required_field(branch, data.get("branch")):
        failing.append(f"branch={data.get('branch')!r}")
    if project_id is not None and not _matches_required_field(project_id, data.get("project_id")):
        failing.append(f"project_id={data.get('project_id')!r}")
    if not _matches_required_field(head_sha, data.get("commit_sha")):
        failing.append(f"commit_sha={data.get('commit_sha')!r}")
    return ", ".join(failing) if failing else "onbekende scope-afwijking"


# A record whose coerced status is in this set reached its own verdict
# (pass/fail). Anything else — unavailable, not_executable, pending, ... —
# means the gate never produced a decision, which is exactly when the
# takeover-provenance route (OI-1576) may speak for it.
_DECIDED_VERDICT_STATES = _GATE_PASS_STATES | _GATE_FAIL_STATES


def _is_absent_without_verdict(result: Dict[str, Any]) -> bool:
    """OI-1624: does ``result`` represent a gate that will render no verdict
    for THIS attempt — an ABSENCE, never a rejection?

    This is deliberately built as a distinction between two independent
    axes, never as a name-list of "known outage statuses" — a status invented
    tomorrow falls into the right bucket automatically as long as it is
    classified by ``gate_status.py`` (the module that owns these categories),
    not by a second hand-picked list living here:

    - **terminal** (``gate_status.is_terminal``) answers "has this attempt
      concluded" — ``not_executable`` is terminal precisely because the gate
      was finally classified as unable to run, even though nothing executed.
    - **verdict rendered** (``_DECIDED_VERDICT_STATES``, pass ∪ fail) answers
      "did the attempt conclude WITH a decision" — a wholly separate
      question. Terminal means "this attempt is over", never "a verdict was
      reached"; only pass and a real rejection are verdicts.

    A status can be terminal without a verdict (``not_executable``) or a
    verdict without controversy (pass/fail, both terminal). What is
    deliberately excluded from "absence" is the THIRD combination: a status
    that is neither terminal nor a verdict AND is not a recognised outage
    either — the ``INCOMPLETE_STATES`` family (pending/running/queued/
    requested). Those mean "this same attempt may still mature into a
    verdict on its own", so treating them as absence and consulting a peer
    gate while they are still in flight would be premature — the merge door
    must keep refusing them on "not terminal yet", exactly as it always has
    (``_merge_door_record_verdict``'s first check, preserved unchanged).

    Absence is therefore: NOT a decided verdict, AND (the attempt concluded
    without one — ``is_terminal`` — OR it is an explicit outage status —
    ``gate_status.UNAVAILABLE_STATES``, which is itself deliberately NOT
    terminal because a rerun can still decide). Both routes read off
    gate_status.py's own category sets.
    """
    status = gate_canonical_status(result)
    if status in _DECIDED_VERDICT_STATES:
        return False
    return gate_is_terminal(result) or status in _GATE_UNAVAILABLE_STATES


# Glob metacharacters that would change the meaning of a pr_id-derived glob
# pattern (e.g. turn it into a wildcard match instead of a literal prefix).
_GLOB_UNSAFE_CHARS = frozenset("*?[]")


def _pr_scoped_json_candidates(results_dir: Path, pr_id: str) -> List[Path]:
    """Cheap filename pre-filter for ``_find_takeover_successor_results`` (OI-1599 advisory).

    Every gate-result writer (``gate_recorder.result_file_path`` and the
    legacy ``pr-{n}-{gate}.json`` fallback it replaced) stamps the SAME
    ``pr_id``/``pr_number`` value into both the filename and the record's own
    ``pr_id`` field — there is no writer that names a file for one PR and
    stamps another PR's id inside it. That means a file whose name cannot
    possibly carry this ``pr_id`` can be skipped before it is ever opened,
    without weakening ``_record_matches_scope``'s own pr_id check in any way:
    the two checks agree by construction.

    Three literal prefixes cover every writer observed in the live store:
    ``{pr_id}-...json`` (contract writer, slug == bare pr_id), ``pr-{pr_id}-...json``
    (legacy writer), and ``{slug}-...json`` where ``slug`` strips dashes the
    same way ``_find_gate_result`` does — covering a dash-bearing pr_id even
    though every current caller passes a bare digit string.

    Falls back to the unfiltered ``*.json`` scan when ``pr_id`` is empty or
    contains a glob metacharacter: a prefix filter must never risk silently
    dropping a record it cannot express as a safe literal glob.
    """
    if not pr_id or any(ch in _GLOB_UNSAFE_CHARS for ch in pr_id):
        return sorted(results_dir.glob("*.json"))
    slug = pr_id.lower().replace("-", "")
    patterns = {f"{pr_id}-*.json", f"pr-{pr_id}-*.json", f"{slug}-*.json"}
    matches: set = set()
    for pattern in patterns:
        matches.update(results_dir.glob(pattern))
    return sorted(matches)


def _find_takeover_successor_results(
    gate: str,
    pr_id: str,
    results_dir: Path,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    head_sha: Optional[str] = None,
    *,
    stated_seats: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """OI-1576: successor records that answer for seat ``gate`` themselves.

    The gate chain slides forward (codex unavailable -> kimi not executable ->
    glm takes over and writes a valid verdict under its OWN name). The runner
    walks that chain; the merge door does NOT — it reads the provenance FIELD
    the successor record carries about itself, so no reader ever has to
    reconstruct the chain from other records again.

    A record is a provenance candidate for seat ``gate`` when, and only when:

      1. its seat is ``gate`` (``review_seat_evaluation.seat_for``): the seat
         its own request on this head states (``stated_seats``, stamped since
         R5a) wins; without one the record must carry ``takeover: true`` and
         ``takeover_path[0]`` must be ``gate``. Only the FIRST hop names the
         seat (OI-2058, R5b): a later hop is a reviewer the walk passed over,
         and a record naming ``gate`` anywhere in its path used to count for
         every seat it walked past
      2. it is in scope for this PR under the SAME matcher the declared-gate
         lookup uses (``_record_matches_scope``: test_run rejection, pr_id,
         project_id, branch, and the head-sha binding from #1740)

    The record's OWN status is never inspected here — that is the caller's
    invariant chain — and a chain step's status inside someone's
    ``takeover_path`` is never consulted either: two records may legitimately
    disagree about a gate that ran twice (live #1729: deepseek's path calls
    glm_gate not_executable while glm_gate's own record says pass), and each
    record speaks only for itself.

    Returns ``(candidates, provenance_notes)``. ``provenance_notes`` names any
    record that claims ``takeover: true`` but carries an empty or absent
    ``takeover_path`` and states no seat: that is a THIRD outcome — not "no
    takeover", not "proven provenance" — and the caller must refuse it as
    provenance evidence with a message that the provenance could not be
    determined.
    """
    from review_seat_evaluation import seat_for, takeover_origin

    stated_seats = stated_seats or {}
    candidates: List[Dict[str, Any]] = []
    provenance_notes: List[str] = []
    if not results_dir.exists():
        return candidates, provenance_notes
    for path in _pr_scoped_json_candidates(results_dir, pr_id):
        try:
            data = json.loads(_read_text(path))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        record_gate = (data.get("gate") or "").strip()
        # The declared gate's own record was already evaluated by the caller;
        # this route only ever looks at OTHER gates' records.
        if not record_gate or record_gate == gate:
            continue
        # OI-1645: a successor must itself be a review gate. A ci_gate (or
        # any other _NON_REVIEW_SIGNER_REASONS gate) that annotates a
        # takeover_path naming the declared gate has still never reviewed the
        # code — the same reason it cannot sign via _find_peer_gate_results
        # applies here, so both routes share _REVIEW_PEER_GATES rather than
        # risking a second, diverging notion of "who may sign".
        if record_gate not in _REVIEW_PEER_GATES:
            continue
        stated = stated_seats.get(record_gate, "")
        if not stated and data.get("takeover") is not True:
            continue
        if not _record_matches_scope(data, pr_id, branch, project_id, head_sha):
            continue
        takeover_path = data.get("takeover_path")
        if not stated and (not isinstance(takeover_path, list) or not takeover_path):
            provenance_notes.append(
                f"{record_gate} (bestand {path.name}) claimt overname maar draagt "
                "een lege of afwezige takeover_path"
            )
            continue
        if not stated and not takeover_origin(takeover_path):
            continue
        if seat_for(record_gate, stated=stated, takeover_path=takeover_path) != gate:
            continue
        candidates.append(data)
    return candidates, provenance_notes


def _partial_review_message(result: Dict[str, Any], gate_label: str, pr_id: str) -> str:
    """OI-1851: merge-door text naming the cut size and the way out."""
    gap = gate_review_coverage_gap(result) or (result.get("reason_detail") or "")
    return (
        f"{gate_label} zag maar een deel van de diff van {pr_id} ({gap or 'afgekapte diff'}): "
        "dat is geen volledige review. Uitweg: een tweede review die de hele diff leest "
        "(bijvoorbeeld codex_gate op dezelfde head), of de PR opsplitsen tot elk deel "
        "onder het diff-plafond valt"
    )


def _merge_door_record_verdict(
    result: Dict[str, Any],
    gate_label: str,
    pr_id: str,
) -> Dict[str, Any]:
    """Run the merge door's invariant chain against one gate result record.

    The SAME five invariants for the declared gate's own record and for an
    OI-1576 takeover successor — ``gate_label`` is the record's own gate name,
    so messages always name the evidence actually being judged.

    OI-1851: a review of part of the diff is NO-GO naming the cut size,
    checked before the evidence check (whose message names another cause).
    """
    if not gate_is_terminal(result):
        status = result.get("status", "unknown")
        return {
            "verdict": "NO-GO",
            "message": f"{gate_label} resultaat is niet terminaal (status={status}): bewijs onvolledig",
            "overridden": False,
            "override_reason": None,
            "gate": gate_label,
        }
    if (
        gate_canonical_status(result) in _GATE_PARTIAL_REVIEW_STATES
        or gate_review_coverage_gap(result)
    ):
        return {
            "verdict": "NO-GO",
            "message": _partial_review_message(result, gate_label, pr_id),
            "overridden": False,
            "override_reason": None,
            "gate": gate_label,
        }
    if not gate_has_complete_evidence(result):
        return {
            "verdict": "NO-GO",
            "message": f"{gate_label} resultaat mist contract_hash en/of report_path: bewijs onvolledig",
            "overridden": False,
            "override_reason": None,
            "gate": gate_label,
        }
    report_path = Path(result["report_path"])
    if not report_path.exists():
        return {
            "verdict": "NO-GO",
            "message": f"{gate_label} report_path bestaat niet: {report_path}",
            "overridden": False,
            "override_reason": None,
            "gate": gate_label,
        }
    passed, reason = gate_is_pass(result)
    if not passed:
        return {
            "verdict": "NO-GO",
            "message": f"{gate_label} staat merge niet toe — {reason}",
            "overridden": False,
            "override_reason": None,
            "gate": gate_label,
        }
    # Invariant 7: a passing verdict whose report still carries blocking
    # indicators is a contradiction, not evidence.
    indicators = _report_blocking_indicators(report_path)
    if indicators is None:
        return {
            "verdict": "NO-GO",
            "message": f"{gate_label} report_path onleesbaar: {report_path}",
            "overridden": False,
            "override_reason": None,
            "gate": gate_label,
        }
    if indicators > 0:
        return {
            "verdict": "NO-GO",
            "message": (
                f"{gate_label} resultaat zegt pass maar het rapport bevat blocking-indicatoren "
                "— bewijs spreekt zichzelf tegen"
            ),
            "overridden": False,
            "override_reason": None,
            "gate": gate_label,
        }
    return {
        "verdict": "GO",
        "message": f"{gate_label} resultaat aanwezig en passing voor {pr_id}",
        "overridden": False,
        "override_reason": None,
        "gate": gate_label,
    }


def _find_peer_gate_results(
    gate: str,
    pr_id: str,
    results_dir: Path,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """OI-1624: other KNOWN gates' own rendered verdicts for this exact PR/sha.

    Only reached when the declared gate is an established ABSENCE
    (:func:`_is_absent_without_verdict`) with no OI-1576 takeover-successor
    naming it either. Absence is not silence: a review round commonly
    dispatches several gates for the same PR (measured live: codex_gate,
    kimi_gate and glm_gate all requested and recorded for the same head),
    and a gate that never rendered a verdict must not make the OTHER gates'
    own, independently-reached verdicts invisible to the merge door.

    Deliberately narrower than "any pass anywhere satisfies the obligation":

    - Every candidate is looked up via :func:`_find_gate_result` — the SAME
      pr_id/branch/project_id/head-sha scope matcher and offline-test-run
      rejection the declared gate's own lookup already uses, never a second,
      looser one.
    - Only records with a RENDERED verdict (``_DECIDED_VERDICT_STATES`` —
      pass or fail, never another absence status) are returned; an absent
      peer contributes nothing either way.
    - The candidate set is bounded to ``_REVIEW_PEER_GATES`` (OI-1645) —
      NOT ``_KNOWN_GATES``. ``_KNOWN_GATES`` only says the verifier can
      interpret a gate's record; ``_REVIEW_PEER_GATES`` additionally
      excludes gates whose record is not a code review at all (``ci_gate``:
      CI checks, not review — see ``_NON_REVIEW_SIGNER_REASONS``). Never an
      unbounded directory scan either way.

    This does NOT relax ``TestUnrelatedRecordsNeverTakeOver`` (OI-1576): that
    contract is about a declared gate with NO record at all (never even
    asked) being handed a stranger's unrelated pass — genuinely unknowable
    whether the seat is dead or merely not yet scheduled this round. This
    function is only ever consulted once the declared gate's OWN record
    already proves it will not render a verdict for this attempt.

    Returns ``[(gate_name, record), ...]``, unordered beyond
    ``_REVIEW_PEER_GATES`` iteration order (sorted, for determinism).
    """
    peers: List[Tuple[str, Dict[str, Any]]] = []
    for candidate_gate in sorted(_REVIEW_PEER_GATES):
        if candidate_gate == gate:
            continue
        candidate = _find_gate_result(
            candidate_gate, pr_id, results_dir,
            branch=branch, project_id=project_id, head_sha=head_sha,
        )
        if candidate is None:
            continue
        if gate_canonical_status(candidate) in _DECIDED_VERDICT_STATES:
            peers.append((candidate_gate, candidate))
    return peers


def _consult_peers_for_absence(
    gate: str,
    pr_id: str,
    results_dir: Path,
    declared_status: str,
    *,
    branch: Optional[str],
    project_id: Optional[str],
    head_sha: Optional[str],
    scope_note: str = "",
    exclude: frozenset = frozenset(),
) -> Dict[str, Any]:
    """OI-1624/OI-1642: the shared peer-consultation verdict for a CONFIRMED
    absent declared gate — reached either because the declared gate's own,
    in-scope record is an absence (OI-1624), or because its only on-disk
    record falls outside the requested branch/project_id/head_sha scope but
    is itself an absence (OI-1642, e.g. a ``not_executable`` record written
    before the scope fields were threaded through). Both call sites in
    the seat evaluation route through this ONE function so they can never
    drift apart on what counts as a valid peer signer.

    ``declared_status`` is the declared gate's own canonical status, used only
    for the message. ``scope_note``, when given, is folded into every returned
    message so the operator sees WHY the declared gate's own record could not
    stand for itself (OI-1642: which scope fields it failed to match).

    ``exclude`` (OI-2058, R5b) names the reviewers another seat already
    counts. The peer fills only this absent seat, and its signer is distinct
    from every reviewer counted for another seat: an excluded peer never
    signs. Its rejection still blocks, like any other peer's.
    """
    peers = _find_peer_gate_results(
        gate, pr_id, results_dir, branch=branch, project_id=project_id, head_sha=head_sha,
    )
    peer_fails = [
        (peer_gate, peer) for peer_gate, peer in peers
        if gate_decided_verdict(peer) == "fail"
    ]
    if peer_fails:
        # A real rejection blocks the merge regardless of what the absent
        # declared gate has to say about it, and regardless of any OTHER
        # peer's pass (harde grens: een echte afkeuring blijft blokkeren,
        # ook naast een pass van een andere poort).
        culprit_gate, culprit = sorted(peer_fails, key=lambda item: item[0])[0]
        return {
            "verdict": "NO-GO",
            "message": (
                f"{gate} is afwezig voor {pr_id} (status={declared_status!r}: "
                f"geen uitspraak gedaan{scope_note}) — {culprit_gate} keurde dezelfde head af "
                f"({gate_canonical_status(culprit)!r}): een echte afkeuring blokkeert "
                f"de merge, ook naast de afwezigheid van {gate}"
            ),
            "overridden": False,
            "override_reason": None,
            "gate": gate,
        }
    counted_elsewhere: List[str] = []
    for peer_gate, peer in sorted(peers, key=lambda item: item[0]):
        if gate_canonical_status(peer) not in _GATE_PASS_STATES:
            continue
        if peer_gate in exclude:
            counted_elsewhere.append(peer_gate)
            continue
        peer_verdict = _merge_door_record_verdict(peer, peer_gate, pr_id)
        if peer_verdict["verdict"] == "GO":
            return _accepted({
                "verdict": "GO",
                "message": (
                    f"{gate} is afwezig voor {pr_id} (status={declared_status!r}: "
                    f"geen uitspraak gedaan{scope_note}) — {peer_gate} droeg op dezelfde head "
                    "zelfstandig een geldige, volledig bewezen pass en telt als "
                    "ondertekenaar bij afwezigheid van de gedeclareerde poort (OI-1624/OI-1642)"
                ),
                "overridden": False,
                "override_reason": None,
                "gate": gate,
                "evidence_gate": peer_gate,
            }, peer)
    return _no_signer_verdict(gate, pr_id, declared_status, scope_note, counted_elsewhere)


def _no_signer_verdict(
    gate: str, pr_id: str, declared_status: str, scope_note: str, counted_elsewhere: List[str],
) -> Dict[str, Any]:
    """Zero valid signers for a confirmed absence.

    The declared gate is confirmed absent and no other known gate on this exact
    head rendered a usable verdict either. Still NO-GO (nul geldige
    ondertekenaars blijft NO-GO), but honestly reported as absence, never as
    "bewijs onvolledig" — that phrasing belongs to a record that attempted a
    verdict and fell short, not to one that never tried. A peer that passed but
    already counts for another seat is named (OI-2058, R5b).
    """
    elsewhere = (
        f" ({', '.join(counted_elsewhere)} telt al voor een andere seat: een reviewer vult één seat)"
        if counted_elsewhere else ""
    )
    return _no_go(gate, (
        f"{gate} is afwezig voor {pr_id} (status={declared_status!r}: "
        f"geen uitspraak gedaan{scope_note}, geen ondertekening) en geen andere poort op "
        "deze head leverde een geldige, volledig bewezen uitspraak — nul "
        f"geldige ondertekenaars{elsewhere}"
    ))


def _find_obligation_takeover_bookings(
    gate: str,
    pr_id: str,
    results_dir: Path,
) -> List[Dict[str, Any]]:
    """OI-1719: obligations for this exact PR+gate booked fulfilled-by-takeover.

    The obligation store (``results_dir.parent / "obligations"`` — the same
    derivation :func:`_find_gate_request_payload` uses for ``requests``) is the
    bookkeeper's own record that the declared gate's obligation was VERVULD by
    takeover evidence: the runner (D2e, ``gate_obligation_runner``) walked the
    chain, found a successor with a complete-evidence verdict, and closed the
    obligation ``fulfilled`` with ``reason=fulfilled_by_takeover_evidence`` and
    ``result_path``/``evidence_result_path`` pointing at the successor's record.
    The OI-1576 route reads the provenance FIELD the successor carries about
    itself; this route reads the BOOKING the obligation carries — the manually
    started glm gates of PRs #1830/#1832-#1838 were booked exactly this way and
    carry no ``takeover_path`` of their own (measured live: 18 of 672 result
    records have that field; theirs are not among them).

    A booking is a candidate only when ALL of these hold — miss one and the
    route does not open through this obligation at all:

      1. the obligation joins this PR (:func:`obligation_matches_pr` — the
         SAME join ``declared_gates_for_pr`` uses) and names this exact gate;
      2. ``status`` is ``fulfilled`` AND ``reason`` is
         ``fulfilled_by_takeover_evidence`` — any other fulfilment reason does
         not open this route, and a non-fulfilled (pending, or terminal
         ``failed`` under ``failed_by_takeover_evidence``) obligation never
         does either.

    Unreadable obligation files raise ValueError straight through (the same
    loud semantics as ``declared_gates_for_pr``): a store nobody can read is a
    finding, never a silent "no booking".
    """
    bookings: List[Dict[str, Any]] = []
    obligations_root = results_dir.parent / "obligations"
    for _path, record in iter_obligations_in(obligations_root):
        if (record.get("gate") or "").strip() != gate:
            continue
        if not obligation_matches_pr(record, pr_id):
            continue
        if record.get("status") != STATUS_FULFILLED:
            continue
        if record.get("reason") != REASON_FULFILLED_BY_TAKEOVER:
            continue
        bookings.append(record)
    return bookings


def _consult_obligation_takeover_booking(
    gate: str,
    pr_id: str,
    results_dir: Path,
    *,
    branch: Optional[str],
    project_id: Optional[str],
    head_sha: Optional[str],
    exclude: frozenset = frozenset(),
) -> Optional[Dict[str, Any]]:
    """OI-1719: the obligation store's takeover booking as a THIRD evidence
    source, next to the successor's self-carried ``takeover_path`` (OI-1576)
    and the independent peer signer for a confirmed absence (OI-1624/OI-1642).

    The booking fills the seat it was booked for (``gate``) and nothing else.
    ``exclude`` (OI-2058, R5b) names the reviewers another seat already counts:
    a booking that points at one of them is refused by name, because a
    reviewer fills one seat.

    Returns ``None`` when NO obligation joins this PR+gate as
    fulfilled-by-takeover — the route is simply absent and the caller falls
    through to the remaining branches unchanged. Once such a booking EXISTS,
    every failure to honour it is an explicit NO-GO from this route (de
    weigering staat gewoon), never a silent fall-through to a message that
    would claim there is no evidence at all.

    The pointed-at record must clear the ONGEWIJZIGDE invariant chain — this
    function reuses the door's own machinery, never a second, weaker copy:

      - scope/sha-binding via :func:`_record_matches_scope`, with
        ``head_sha or ""``: an unknown head makes EVERY record fail
        ``_matches_required_field``, so "onbekend of afwijkend" is a refusal
        here — this route does not suspend judgement the way OI-1571 tak 3
        does, it refuses;
      - terminal, complete evidence, report on disk, pass, no
        self-contradiction via :func:`_merge_door_record_verdict` — a
        ``not_executable`` non-verdict and a REAL rejection both fail there;
      - offline ``test_run`` records are rejected inside
        ``_record_matches_scope`` before any matching;
      - who may sign: a record under another gate's name must be a REVIEW
        gate (``_REVIEW_PEER_GATES``, OI-1645) — a booking that points a
        review obligation at e.g. a ci_gate record lets CI sign a review,
        which both other routes already forbid.

    Hard boundary, shared with both other routes: a REAL rejection on this
    head is never weakened by a takeover booking. Before any booking is
    honoured, the same peer scan :func:`_consult_peers_for_absence` uses
    (:func:`_find_peer_gate_results`) runs, and any peer fail blocks.
    """
    bookings = _find_obligation_takeover_bookings(gate, pr_id, results_dir)
    if not bookings:
        return None

    peers = _find_peer_gate_results(
        gate, pr_id, results_dir, branch=branch, project_id=project_id, head_sha=head_sha,
    )
    peer_fails = [
        (peer_gate, peer) for peer_gate, peer in peers
        if gate_decided_verdict(peer) == "fail"
    ]
    if peer_fails:
        culprit_gate, culprit = sorted(peer_fails, key=lambda item: item[0])[0]
        return {
            "verdict": "NO-GO",
            "message": (
                f"takeover-boeking voor {gate} op {pr_id} verandert niets aan de afkeuring: "
                f"{culprit_gate} keurde dezelfde head af "
                f"({gate_canonical_status(culprit)!r}) — een echte afkeuring blokkeert de "
                "merge en wordt door geen enkele boeking verzwakt"
            ),
            "overridden": False,
            "override_reason": None,
            "gate": gate,
        }

    refusal_notes: List[str] = []
    for booking in sorted(bookings, key=lambda b: str(b.get("dispatch_id") or "")):
        booking_id = str(booking.get("dispatch_id") or "<onbekende verplichting>")
        pointed, evidence_gate, refusal = _booking_evidence(
            booking, booking_id, gate, pr_id,
            branch=branch, project_id=project_id, head_sha=head_sha, exclude=exclude,
        )
        if pointed is None:
            refusal_notes.append(refusal)
            continue
        verdict = _merge_door_record_verdict(pointed, evidence_gate, pr_id)
        if verdict["verdict"] == "GO":
            return _accepted({
                "verdict": "GO",
                "message": (
                    f"{gate} bewezen via takeover-boeking: verplichting {booking_id} "
                    f"staat fulfilled_by_takeover_evidence en wijst naar {evidence_gate}, "
                    f"dat op de huidige kop een volledig bewezen pass draagt voor {pr_id}"
                ),
                "overridden": False,
                "override_reason": None,
                "gate": gate,
                "evidence_gate": evidence_gate,
                "evidence_via": "takeover_boeking",
            }, pointed)
        refusal_notes.append(verdict["message"])

    return {
        "verdict": "NO-GO",
        "message": (
            f"takeover-boeking voor {gate} op {pr_id} geweigerd: "
            + "; ".join(refusal_notes)
        ),
        "overridden": False,
        "override_reason": None,
        "gate": gate,
    }


def _booking_evidence(
    booking: Dict[str, Any],
    booking_id: str,
    gate: str,
    pr_id: str,
    *,
    branch: Optional[str],
    project_id: Optional[str],
    head_sha: Optional[str],
    exclude: frozenset,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """``(record, evidence_gate, refusal)`` for the record one booking points at.

    ``record`` is ``None`` when the booking cannot be honoured, and
    ``refusal`` then says why: no pointer, an unreadable record, a record not
    on the current head (an unknown head refuses here, it does not suspend
    judgement), a record under a gate that may not sign a review (OI-1645), or
    a reviewer that already counts for another seat (OI-2058, R5b).
    """
    evidence_ref = booking.get("result_path") or booking.get("evidence_result_path")
    evidence_ref = str(evidence_ref).strip() if evidence_ref else ""
    if not evidence_ref:
        return None, "", (
            f"verplichting {booking_id} wijst geen bewijs aan "
            "(result_path/evidence_result_path ontbreken)"
        )
    try:
        pointed = json.loads(_read_text(Path(evidence_ref)))
    except (OSError, json.JSONDecodeError):
        pointed = None
    if not isinstance(pointed, dict):
        return None, "", f"verplichting {booking_id} wijst naar onleesbaar bewijs: {evidence_ref}"
    if not _record_matches_scope(pointed, pr_id, branch, project_id, head_sha or ""):
        return None, "", (
            f"het aangewezen record van verplichting {booking_id} staat niet op de "
            f"huidige kop ({_describe_scope_mismatch(pointed, branch, project_id, head_sha or '')})"
        )
    evidence_gate = (pointed.get("gate") or "").strip() or gate
    if evidence_gate != gate and evidence_gate not in _REVIEW_PEER_GATES:
        return None, evidence_gate, (
            f"verplichting {booking_id} wijst naar {evidence_gate}, dat geen "
            "review-ondertekenaar is (OI-1645)"
        )
    if evidence_gate in exclude:
        return None, evidence_gate, (
            f"verplichting {booking_id} wijst naar {evidence_gate}, dat al voor een "
            "andere seat telt: een reviewer vult één seat"
        )
    return pointed, evidence_gate, ""


def check_review_gate_for_merge(
    pr_id: str,
    gate: str,
    results_dir: Path,
    *,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    head_sha: Optional[str] = None,
    seats: Optional[Sequence[str]] = None,
    seats_source: Optional[str] = None,
    b3_override: str = "",
    checkout: Optional[Path] = None,
    base_branch: Optional[str] = None,
) -> Dict[str, Any]:
    """GO/NO-GO merge verdict: every required review seat is filled on this head.

    The merge door's second fail-closed check (next to the CI check in
    ``pr_merge._run_ci_gate``). Since OI-2058 (reparatiegolf R5b) the door asks
    ``review_seat_evaluation.evaluate_review_seats``, the one seat evaluation
    ``vnx-gate/review``, ``vnx pr-ready`` and track closure ask as well, so the
    four can never disagree. GO only when:

      1. every seat of the project's review stack is filled by a DISTINCT
         reviewer: a completed record on this head (sha binding ``match``),
         terminal, ``contract_hash`` + ``report_path`` non-empty, the report on
         disk, a pass that the report does not contradict
         (:func:`_merge_door_record_verdict`, the same five invariants as
         before); and
      2. no review peer on this head rejected it: a FAIL verdict or a
         blocking finding in its JSON, or in its report on disk whatever its
         JSON says (invariant 7, read for every review peer, the declared gate
         and reviewers outside the seats included). A rejection blocks,
         whatever the other seats say.

    Which seats: the ``review_seats`` (source ``config``) a request on this
    head recorded, else a fresh ``review_seats.resolve_review_seats()``; a
    request with ``source=explicit`` never narrows that set. The risk class of
    this head narrows it (R3b, ``review_seat_evaluation.seats_for_risk``): the
    first seat at low and medium, every seat at high and critical, the class
    read in ``checkout`` (``None``: the store's own checkout) against
    ``base_branch``, the PR's own base (``baseRefName``; ``None``: not known,
    so the class is high). ``seats`` names
    them directly (track closure passes the merge record's list,
    ``seats_source`` says where it came from). ``gate`` is the declared gate of
    the obligation: it names the evidence in the returned dict and decides no
    seat. A dispatch declares one gate (``gate_obligations.declared_gates_for_pr``),
    and holding a merge to that one gate is how a head with a codex PASS and
    no kimi record merged.

    Each seat is filled through the evidence routes below, in this order, and
    each route fills at most that one seat. A route's GO carries the exact
    record it accepted (:func:`_accepted`), and that record is the seat's
    evidence for ``vnx-gate/review``, ``vnx pr-ready`` and track closure:

    - the seat's OWN record, when it answers for this seat itself (its request
      states the seat, or it carries no takeover). A decided verdict there
      stands on its own and is never overridden by a successor.
    - OI-1576: a SUCCESSOR that answers for the seat
      (:func:`_find_takeover_successor_results`: its own request states the
      seat, else ``takeover_path[0]``). Golf Bx/D7: a successor without a
      verdict of its own is skipped, never judged. A takeover record fills
      only the seat its walk started from, never also its own gate's seat.
    - a takeover claim without a path is a refusal (provenance undeterminable).
    - OI-1719: the obligation store's takeover booking
      (:func:`_consult_obligation_takeover_booking`), for a reviewer no other
      seat counts.
    - OI-1851: a review of part of the diff fills no seat; an independent peer
      may still sign, else the refusal names the cut.
    - OI-1624/OI-1642: for a CONFIRMED absence of the seat's own gate (an
      in-scope absence record, or an out-of-scope one), an independent peer's
      full pass on this head signs (:func:`_consult_peers_for_absence`), only
      when that peer is distinct from every reviewer counted for another seat.
      No record at all gets no peer fallback (ADR-037).

    The override escape hatch lives in the caller (``pr_merge._run_review_gate``),
    mirroring how the CI gate keeps its override in ``check_ci_run_for_head``.
    The returned dict carries ``review_seats``: the effective seat list, its
    source and who filled what, the shape the merge record stores.

    R1: every filled seat is counted against the round ledger
    (``review_seat_evaluation.counted_fill``). ``b3_override`` is the reason of
    the door's ``--override-b3``: it lets the codex seat of a PR at the B3 cap
    count, and lands in ``review_seats.round_ledger``.
    """
    from review_seat_evaluation import SOURCE_EXPLICIT, evaluate_review_seats

    evaluation = evaluate_review_seats(
        pr_id, results_dir, branch=branch, project_id=project_id, head_sha=head_sha,
        seats=seats, seats_source=seats_source or SOURCE_EXPLICIT, b3_override=b3_override,
        checkout=checkout, base_branch=base_branch,
    )
    verdict: Dict[str, Any] = {
        "verdict": "GO" if evaluation.go else "NO-GO",
        "message": evaluation.message(),
        "overridden": False,
        "override_reason": None,
        "gate": gate,
        "review_seats": evaluation.to_record(),
    }
    named = evaluation.fill_for(gate) or (evaluation.fills[0] if len(evaluation.fills) == 1 else None)
    if named is not None and named.filled and named.reviewer != named.seat:
        verdict["evidence_gate"] = named.reviewer
    if named is not None and named.filled and named.evidence_via:
        verdict["evidence_via"] = named.evidence_via
    return verdict


def _seat_claimed_verdict(
    seat: str,
    pr_id: str,
    results_dir: Path,
    *,
    own: Optional[Dict[str, Any]],
    stated_seats: Dict[str, str],
    branch: Optional[str],
    project_id: Optional[str],
    head_sha: Optional[str],
) -> Optional[Dict[str, Any]]:
    """The records that answer for ``seat`` themselves, or ``None`` when none settles it.

    ``own`` is the seat gate's own in-scope record when it answers for this
    seat (``None`` when it answers for another seat, or there is none). Its
    decided verdict stands on its own. Otherwise every successor that answers
    for the seat is judged by the unchanged invariant chain; the first GO
    fills the seat, a decided successor that does not hold is a NO-GO, and a
    takeover claim without a path is refused as undeterminable. ``None``
    hands the seat to the booking and absence routes.
    """
    if own is not None and gate_canonical_status(own) in _DECIDED_VERDICT_STATES:
        # This is also what keeps the walking-reader trap closed — a step
        # status in someone else's takeover_path can never disqualify a gate
        # that carries its own decided record (live #1729: deepseek's path
        # called glm_gate not_executable while glm_gate itself passed).
        return _accepted(_merge_door_record_verdict(own, seat, pr_id), own)
    successors, provenance_notes = _find_takeover_successor_results(
        seat, pr_id, results_dir, branch=branch, project_id=project_id, head_sha=head_sha,
        stated_seats=stated_seats,
    )
    first_failure: Optional[Dict[str, Any]] = None
    for successor in successors:
        successor_gate = (successor.get("gate") or "").strip() or seat
        if gate_canonical_status(successor) not in _DECIDED_VERDICT_STATES:
            # Golf Bx/D7: a successor that rendered no verdict of its own says
            # something about the runner, never about the code (measured on
            # #1818: kimi_gate not_executable / gate_not_subprocess_routable).
            # Judging it would latch a guaranteed NO-GO ahead of the peer route.
            continue
        verdict = _merge_door_record_verdict(successor, successor_gate, pr_id)
        if verdict["verdict"] == "GO":
            return _accepted({
                "verdict": "GO",
                "message": (
                    f"{seat} bewezen via overname: {successor_gate} leest seat {seat} "
                    f"(takeover_path[0] of de seat van zijn verzoek) en is zelf passing voor {pr_id}"
                ),
                "overridden": False,
                "override_reason": None,
                "gate": seat,
                "evidence_gate": successor_gate,
            }, successor)
        if first_failure is None:
            first_failure = verdict
    if first_failure is not None:
        return _no_go(seat, f"overname-route voor {seat}: {first_failure['message']}")
    if provenance_notes:
        return _no_go(seat, (
            f"overnamebewijs voor {seat} op {pr_id} geweigerd: herkomst niet "
            f"vast te stellen ({'; '.join(provenance_notes)})"
        ))
    return None


def _no_go(gate: str, message: str) -> Dict[str, Any]:
    return {
        "verdict": "NO-GO",
        "message": message,
        "overridden": False,
        "override_reason": None,
        "gate": gate,
    }


def _accepted(verdict: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    """A route's GO with the exact record it accepted beside it (``evidence_record``).

    The seat evaluation makes that record the seat's evidence
    (``review_seat_evaluation.SeatFill.record``), so track closure checks the
    hash and the report of the record the door accepted, never a second lookup
    of the reviewer's slot (OI-2058 R5b round 3). A NO-GO carries no record.
    """
    if verdict.get("verdict") != "GO":
        return verdict
    return dict(verdict, evidence_record=record)


def _seat_absence_verdict(
    seat: str,
    pr_id: str,
    results_dir: Path,
    *,
    own_record: Optional[Dict[str, Any]],
    read_other_seat: str,
    exclude: frozenset,
    branch: Optional[str],
    project_id: Optional[str],
    head_sha: Optional[str],
) -> Dict[str, Any]:
    """The last routes for a seat nothing claimed: partial review, absence, or nothing.

    ``read_other_seat`` names the seat the seat gate's own record answered for
    instead (it read another seat): that reviewer is taken, the seat has no
    reader of its own, and no peer fills it (it is not an absence). ``exclude``
    is every reviewer another seat counts; a peer among them never signs.
    """
    scope = {"branch": branch, "project_id": project_id, "head_sha": head_sha}
    if read_other_seat:
        return _no_go(seat, (
            f"{seat} las op deze kop seat {read_other_seat} (een reviewer vult één seat): "
            f"seat {seat} heeft geen eigen reviewer op {pr_id}"
        ))
    result = own_record
    if result is not None and gate_canonical_status(result) in _GATE_PARTIAL_REVIEW_STATES:
        # OI-1851: the partial review fills no seat; an independent peer's full
        # pass on this head is the second review that reads the rest.
        peer_verdict = _consult_peers_for_absence(
            seat, pr_id, results_dir, gate_canonical_status(result),
            scope_note=f"; zag maar een deel van de diff: {gate_review_coverage_gap(result)}",
            exclude=exclude, **scope,
        )
        if peer_verdict["verdict"] == "GO":
            return peer_verdict
        return _no_go(seat, (
            f"{_partial_review_message(result, seat, pr_id)} — "
            f"andere poorten: {peer_verdict['message']}"
        ))
    if result is not None and _is_absent_without_verdict(result):
        # OI-1624: the seat's gate spoke, and what it said is an absence.
        return _consult_peers_for_absence(
            seat, pr_id, results_dir, gate_canonical_status(result), exclude=exclude, **scope,
        )
    if result is None:
        return _seat_without_record(seat, pr_id, results_dir, exclude=exclude, **scope)
    return _accepted(_merge_door_record_verdict(result, seat, pr_id), result)


def _seat_without_record(
    seat: str,
    pr_id: str,
    results_dir: Path,
    *,
    exclude: frozenset,
    branch: Optional[str],
    project_id: Optional[str],
    head_sha: Optional[str],
) -> Dict[str, Any]:
    """OI-1642: no in-scope record for the seat's gate.

    A record for this exact gate+pr_id may still exist OUTSIDE the requested
    scope, most often because it predates OI-1624 threading branch/commit_sha
    through (the overwrite guard then refuses to rerun it). Only when that
    out-of-scope record is itself a confirmed absence does the peer route
    open; a decided verdict about another branch/sha is stale evidence, and a
    gate that was never asked gets no peer fallback (ADR-037).
    """
    out_of_scope = _find_gate_result_ignoring_scope(seat, pr_id, results_dir)
    if out_of_scope is not None and _is_absent_without_verdict(out_of_scope):
        scope_note = (
            f"; eigen record buiten scope "
            f"({_describe_scope_mismatch(out_of_scope, branch, project_id, head_sha)})"
        )
        return _consult_peers_for_absence(
            seat, pr_id, results_dir, gate_canonical_status(out_of_scope),
            branch=branch, project_id=project_id, head_sha=head_sha,
            scope_note=scope_note, exclude=exclude,
        )
    return _no_go(seat, f"geen review-gate resultaat gevonden voor {seat} op {pr_id}: merge niet toetsbaar")


# Request-side states for the optional Claude GitHub review gate that record
# an explicit, intentional absence (writer: gate_request_handler).  These are
# canonicalized in claude_github_receipt.INTENTIONALLY_ABSENT_STATES /
# EVIDENCE_STATES; duplicated here to avoid an import cycle since
# closure_verifier already pins sys.path on its own.
_INTENTIONALLY_ABSENT_REQUEST_STATES = frozenset({"not_configured", "configured_dry_run"})
_EVIDENCE_REQUEST_STATES = frozenset({"requested", "completed"})


def _find_gate_request_payload(
    gate: str,
    pr_id: str,
    results_dir: Path,
    branch: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Locate a gate **request** payload as a fallback when no result exists.

    For optional gates (notably ``claude_github_optional``), the explicit-absence
    state (``not_configured``, ``configured_dry_run``) is recorded by
    gate_request_handler in the requests directory only — no separate result
    file is written.  This helper finds that payload and normalises legacy
    ``status``-only writers into the same shape the closure verifier expects
    (with ``state``/``contributed_evidence``/``was_intentionally_absent``).
    """
    requests_dir = results_dir.parent / "requests"
    if not requests_dir.exists():
        return None

    pr_slug = pr_id.lower().replace("-", "")
    candidates: List[Path] = []
    contract_path = requests_dir / f"{pr_slug}-{gate}-contract.json"
    if contract_path.exists():
        candidates.append(contract_path)
    candidates.extend(
        path for path in requests_dir.glob(f"*-{gate}*.json")
        if path not in candidates
    )

    for path in candidates:
        try:
            data = json.loads(_read_text(path))
        except (json.JSONDecodeError, OSError):
            continue
        # Offline test-run records must never satisfy an optional-gate closure
        # check either — same rule as _find_gate_result._accept.
        if _is_test_run_record(data):
            continue
        # claude_github_optional payloads must carry an explicit pr_id so that a
        # file written for one PR cannot silently satisfy another PR's closure check.
        if gate == "claude_github_optional" and not data.get("pr_id"):
            print(
                f"WARNING: claude_github_optional request payload {path.name} is missing"
                f" pr_id — rejected to prevent cross-PR false-green (OI-1339)",
                file=sys.stderr,
            )
            continue
        if data.get("pr_id") and data["pr_id"] != pr_id:
            continue
        if data.get("gate") and data["gate"] != gate:
            continue
        # Reject requests recorded for a different branch — a cross-branch request
        # with the same pr_id must not satisfy closure for the current branch.
        if branch and data.get("branch") and data["branch"] != branch:
            continue

        # Normalise legacy `status`-only payloads (writer: _request_claude_github)
        # so downstream checks can read `state` / contributed_evidence /
        # was_intentionally_absent uniformly.
        normalised = dict(data)
        if "state" not in normalised and "status" in normalised:
            normalised["state"] = normalised["status"]
        state = normalised.get("state")
        if isinstance(state, str):
            normalised.setdefault(
                "was_intentionally_absent",
                state in _INTENTIONALLY_ABSENT_REQUEST_STATES,
            )
            normalised.setdefault(
                "contributed_evidence",
                state in _EVIDENCE_REQUEST_STATES,
            )
        normalised["__source"] = "request"
        return normalised
    return None


def _apply_codex_severity_policy(result: Dict[str, Any]) -> Dict[str, Any]:
    """Apply CFX-17 codex severity policy to a codex_gate result.

    Returns a new dict with ``blocking_findings`` reduced to those that
    were not demoted by the policy, and ``blocking_count`` reset to match.
    The original payload is left untouched. Demoted findings are preserved
    on ``advisory_findings`` so audit/contradiction detection can still see
    the rationale. A finding is considered demoted when translation
    produced an ``original_severity`` field (warning or info now).
    """
    findings = result.get("blocking_findings")
    if not isinstance(findings, list) or not findings:
        return result
    translated = translate_codex_findings(findings)
    blocking = [f for f in translated if "original_severity" not in f]
    advisory = [f for f in translated if "original_severity" in f]

    new_result = dict(result)
    new_result["blocking_findings"] = blocking
    new_result["blocking_count"] = len(blocking)
    if advisory:
        existing_advisory = result.get("advisory_findings")
        merged_advisory = list(existing_advisory) if isinstance(existing_advisory, list) else []
        merged_advisory.extend(advisory)
        new_result["advisory_findings"] = merged_advisory
        # Track demotions explicitly so operators can audit what the policy did.
        new_result["severity_demotions"] = [
            {
                "message": f.get("message", ""),
                "original_severity": f.get("original_severity", ""),
                "severity": f.get("severity", ""),
                "demotion_reason": f.get("demotion_reason", ""),
            }
            for f in advisory
            if f.get("original_severity")
        ]
    # When demotion clears the blocking set entirely, override a fail status so the
    # gate evaluates as pass. Codex sets status=fail purely from blocking_count; once
    # those findings are demoted to advisory, the status must follow or the gate-side
    # policy is a no-op against codex severity inflation.
    if not blocking and (result.get("status") or "").lower() in ("fail", "failed", "blocked"):
        new_result["status"] = "pass"
        new_result["status_overridden_by_severity_policy"] = True
    return new_result


def _check_contract_fields(contract: ReviewContract) -> List[CheckResult]:
    """Validate required contract fields. Returns a single FAIL on missing field (caller should return early), or a PASS."""
    if not contract.pr_id:
        return [CheckResult("review_contract", "FAIL", "review contract missing pr_id")]
    if not contract.review_stack:
        return [CheckResult("review_contract", "FAIL", "review contract has empty review_stack")]
    return [CheckResult(
        "review_contract",
        "PASS",
        f"review contract present for {contract.pr_id} "
        f"(stack: {', '.join(contract.review_stack)})",
    )]


def _gate_claude_github_optional(
    contract: ReviewContract,
    result: Optional[Dict[str, Any]],
    results_dir: Path,
    branch: Optional[str],
) -> CheckResult:
    gate = "claude_github_optional"
    # Fallback: explicit-absence state for the optional gate is recorded
    # in the requests directory by gate_request_handler — not as a
    # separate result file — so a missing result is not by itself
    # ambiguous.  Look there before declaring no evidence.
    if result is None:
        result = _find_gate_request_payload(gate, contract.pr_id, results_dir, branch=branch)
    if result is None:
        return CheckResult(
            f"gate_{gate}",
            "FAIL",
            f"no evidence for optional gate {gate} — state must be explicit",
        )
    if (result.get("state") or "").lower() == "completed":
        # Completed Claude review: terminal outcome lives in result_status, not status/verdict.
        # contributed_evidence=true alone must NOT mask a fail outcome or blocking findings.
        # gate_is_pass() handles state/result_status via gate_status._coerce_status (CFX-12).
        passed, reason = gate_is_pass(result)
        if passed:
            advisory_count = int(result.get("advisory_count") or 0)
            return CheckResult(
                f"gate_{gate}",
                "PASS",
                f"{gate} completed: pass (0 blocking, {advisory_count} advisory)",
            )
        return CheckResult(f"gate_{gate}", "FAIL", f"{gate} completed: {reason}")
    if result.get("was_intentionally_absent") or result.get("contributed_evidence"):
        state = result.get("state", "unknown")
        source = result.get("__source", "result")
        return CheckResult(
            f"gate_{gate}",
            "PASS",
            f"{gate} state explicit: {state} (source: {source})",
        )
    return CheckResult(
        f"gate_{gate}",
        "FAIL",
        f"{gate} state is ambiguous — neither contributed evidence nor intentionally absent",
    )


def _gate_codex_gate(
    contract: ReviewContract,
    result: Optional[Dict[str, Any]],
    results_dir: Path,
    branch: Optional[str],
) -> CheckResult:
    gate = "codex_gate"
    enforcement = enforce_codex_gate(contract)
    if enforcement.required:
        if result is None:
            return CheckResult(
                f"gate_{gate}",
                "FAIL",
                f"codex gate required ({', '.join(enforcement.reasons)}) but no result found",
            )
        if gate_is_terminal(result) and not result.get("report_path", ""):
            return CheckResult(
                f"gate_{gate}",
                "FAIL",
                "codex_gate result is missing required report_path field",
            )
        # CFX-17: demote allowlisted findings before counting blocking.
        translated = _apply_codex_severity_policy(result)
        passed, reason = gate_is_pass(translated)
        demoted = len(translated.get("severity_demotions") or [])
        if passed:
            if demoted:
                return CheckResult(
                    f"gate_{gate}",
                    "PASS",
                    f"codex gate passed after demoting {demoted} finding(s) per severity policy",
                )
            return CheckResult(f"gate_{gate}", "PASS", "codex gate passed")
        return CheckResult(f"gate_{gate}", "FAIL", f"codex gate not passing — {reason}")
    return CheckResult(f"gate_{gate}", "PASS", "codex gate not required by risk policy")


def _gate_gemini_review(
    contract: ReviewContract,
    result: Optional[Dict[str, Any]],
    results_dir: Path,
    branch: Optional[str],
) -> CheckResult:
    gate = "gemini_review"
    if result is None:
        return CheckResult(
            f"gate_{gate}",
            "FAIL",
            f"no gate result for required reviewer {gate}",
        )
    if gate_is_terminal(result) and not result.get("report_path", ""):
        return CheckResult(
            f"gate_{gate}",
            "FAIL",
            "gemini_review result is missing required report_path field",
        )
    passed, reason = gate_is_pass(result)
    advisory_count = result.get("advisory_count", 0)
    if passed:
        return CheckResult(
            f"gate_{gate}",
            "PASS",
            f"gemini review passed ({advisory_count} advisory, 0 blocking)",
        )
    return CheckResult(f"gate_{gate}", "FAIL", f"gemini review not passing — {reason}")


def _gate_ci_gate(
    contract: ReviewContract,
    result: Optional[Dict[str, Any]],
    results_dir: Path,
    branch: Optional[str],
) -> CheckResult:
    gate = "ci_gate"
    if result is None:
        return CheckResult(f"gate_{gate}", "FAIL", "no ci_gate result found")
    if not gate_is_terminal(result):
        status = result.get("status", "unknown")
        return CheckResult(
            f"gate_{gate}",
            "FAIL",
            f"ci_gate checks still {status} — incomplete evidence",
        )
    contract_hash = result.get("contract_hash", "")
    report_path = result.get("report_path", "")
    if not contract_hash:
        return CheckResult(
            f"gate_{gate}",
            "FAIL",
            "ci_gate result is missing required contract_hash field",
        )
    if not report_path:
        return CheckResult(
            f"gate_{gate}",
            "FAIL",
            "ci_gate result is missing required report_path field",
        )
    passed, reason = gate_is_pass(result)
    if passed:
        advisory_count = result.get("advisory_count", 0)
        return CheckResult(
            f"gate_{gate}",
            "PASS",
            f"ci_gate passed ({advisory_count} advisory, 0 blocking)",
        )
    return CheckResult(f"gate_{gate}", "FAIL", f"ci_gate not passing — {reason}")


def _gate_full_evidence_pass(gate: str, result: Optional[Dict[str, Any]]) -> CheckResult:
    """Shared terminal + complete-evidence + pass check (dlv45: kimi_gate/glm_gate).

    Unlike codex_gate, neither kimi_gate nor glm_gate has a risk-conditional
    "required" policy — they are only checked when present in the review
    stack, so there is no not-required PASS branch. Modeled on
    ``_gate_codex_gate``'s terminal + report_path requirements, plus
    ``gate_has_complete_evidence`` (contract_hash AND report_path, mirroring
    ``_gate_ci_gate``) so a kimi/glm result cannot pass on report_path alone —
    a kimi_gate result must carry the same evidence discipline as codex,
    never less.

    ``unavailable`` (OI-1142 provider outage) is deliberately not terminal
    (``gate_status.is_terminal``), so it is rejected here before
    ``gate_is_pass`` is ever consulted — an outage can never be booked as a
    review PASS.
    """
    if result is None:
        return CheckResult(f"gate_{gate}", "FAIL", f"no {gate} result found")
    if not gate_is_terminal(result):
        status = result.get("status", "unknown")
        return CheckResult(
            f"gate_{gate}",
            "FAIL",
            f"{gate} result is not terminal (status={status}) — incomplete evidence",
        )
    if not gate_has_complete_evidence(result):
        return CheckResult(
            f"gate_{gate}",
            "FAIL",
            f"{gate} result is missing contract_hash and/or report_path",
        )
    passed, reason = gate_is_pass(result)
    if passed:
        advisory_count = result.get("advisory_count")
        if not isinstance(advisory_count, int):
            advisory_count = len(result.get("advisory_findings") or [])
        return CheckResult(
            f"gate_{gate}",
            "PASS",
            f"{gate} passed ({advisory_count} advisory, 0 blocking)",
        )
    return CheckResult(f"gate_{gate}", "FAIL", f"{gate} not passing — {reason}")


def _gate_kimi_gate(
    contract: ReviewContract,
    result: Optional[Dict[str, Any]],
    results_dir: Path,
    branch: Optional[str],
) -> CheckResult:
    return _gate_full_evidence_pass("kimi_gate", result)


def _gate_glm_gate(
    contract: ReviewContract,
    result: Optional[Dict[str, Any]],
    results_dir: Path,
    branch: Optional[str],
) -> CheckResult:
    return _gate_full_evidence_pass("glm_gate", result)


def _gate_deepseek_gate(
    contract: ReviewContract,
    result: Optional[Dict[str, Any]],
    results_dir: Path,
    branch: Optional[str],
) -> CheckResult:
    return _gate_full_evidence_pass("deepseek_gate", result)


def _gate_claude_gate(
    contract: ReviewContract,
    result: Optional[Dict[str, Any]],
    results_dir: Path,
    branch: Optional[str],
) -> CheckResult:
    return _gate_full_evidence_pass("claude_gate", result)


# Table-driven dispatch: one entry per gate the closure verifier implements.
# Adding a gate means adding one handler here plus (if excluded) an entry in
# _GATES_NOT_IMPLEMENTED_BY_CLOSURE — not a new branch in _check_single_gate,
# which is what made the function grow past its line budget (OI-1094).
_GATE_HANDLERS: Dict[str, Any] = {
    "claude_github_optional": _gate_claude_github_optional,
    "codex_gate": _gate_codex_gate,
    "gemini_review": _gate_gemini_review,
    "ci_gate": _gate_ci_gate,
    "kimi_gate": _gate_kimi_gate,
    "glm_gate": _gate_glm_gate,
    "deepseek_gate": _gate_deepseek_gate,
    "claude_gate": _gate_claude_gate,
}


def _check_single_gate(
    gate: str,
    contract: ReviewContract,
    result: Optional[Dict[str, Any]],
    results_dir: Path,
    branch: Optional[str],
) -> CheckResult:
    """Check a single gate from the review stack, returning one CheckResult."""
    if gate not in _KNOWN_GATES:
        # Unknown gate: the verifier does not implement this gate, so it can
        # neither pass nor fail it. Presence of a result file is not evidence —
        # a record for an unrecognised gate is uninterpretable. UNVERIFIED (not
        # FAIL) keeps a governance-config gap visible instead of reading as a
        # gate that actually rejected the change; the verdict still fails
        # because closure requires every check to be PASS.
        if result is not None:
            return CheckResult(
                f"gate_{gate}",
                "UNVERIFIED",
                f"gate {gate} is not implemented by the closure verifier — result present "
                f"but uninterpretable, treated as not decided",
            )
        return CheckResult(
            f"gate_{gate}",
            "UNVERIFIED",
            f"gate {gate} is not implemented by the closure verifier — not decided, cannot pass",
        )

    handler = _GATE_HANDLERS[gate]
    return handler(contract, result, results_dir, branch)


def _check_contract_hashes(
    contract: ReviewContract, results_dir: Path, skip: frozenset = frozenset(),
) -> List[CheckResult]:
    """Check content hash consistency between contract and gate receipts.

    ADR-007: project_id-scoped filtering is enforced inside _find_gate_result; not added here
    to match the same call site as the original gate-loop (branch=contract.branch, no project_id arg).

    ``skip`` holds the seats the seat rule already judged on the merged head
    (OI-2058, R5b): their evidence is the record the door's route accepted for
    the seat, bound to that head, not the seat gate's own slot. :func:`_seat_rule_checks` runs the
    same comparison on that record, so skipping a seat here moves its check,
    it never drops it. A stack seat the merge's risk class did not require
    (R3b) is in ``skip`` too: the door asked nothing of its record.
    """
    checks: List[CheckResult] = []
    for gate in contract.review_stack:
        if gate not in _KNOWN_GATES or gate in skip:
            continue
        result = _find_gate_result(
            gate, contract.pr_id, results_dir, branch=contract.branch or None,
        )
        check = _record_hash_check(gate, gate, result, contract.content_hash)
        if check is not None:
            checks.append(check)
    return checks


def _record_hash_check(
    name: str, label: str, result: Optional[Dict[str, Any]], content_hash: str,
    *, report_match: bool = False,
) -> Optional[CheckResult]:
    """``hash_<name>`` FAIL when ``result`` carries a contract hash other than the contract's.

    One comparison for both closure rules: the old rule checks a gate's own
    record (``label`` is the gate), the seat rule the record that fills the
    seat (``label`` names the seat and its reviewer, OI-2058 R5b round 2).
    ``report_match`` (the seat rule, round 3) also reports a matching hash as a
    PASS, so the check says which record it compared; the old rule keeps
    reporting only a mismatch.
    """
    if not result or not content_hash:
        return None
    result_hash = result.get("contract_hash") or result.get("content_hash") or ""
    if not result_hash:
        return None
    if result_hash == content_hash:
        if not report_match:
            return None
        return CheckResult(
            f"hash_{name}", "PASS", f"{label} receipt hash matches the contract ({content_hash[:8]}..)",
        )
    return CheckResult(
        f"hash_{name}",
        "FAIL",
        f"{label} receipt hash mismatch — evidence is stale "
        f"(contract={content_hash[:8]}.. receipt={result_hash[:8]}..)",
    )


def _check_report_paths(
    contract: ReviewContract, results_dir: Path, skip: frozenset = frozenset(),
) -> List[CheckResult]:
    """Validate that every terminal gate result carries a report_path pointing to a real file.

    ``skip``: the seats the seat rule already judged, and the seats a merge's
    risk class did not require (see :func:`_check_contract_hashes`).
    """
    checks: List[CheckResult] = []
    for gate in contract.review_stack:
        if gate not in _KNOWN_GATES or gate in skip:
            continue
        result = _find_gate_result(
            gate, contract.pr_id, results_dir, branch=contract.branch or None,
        )
        check = _record_report_check(gate, gate, result)
        if check is not None:
            checks.append(check)
    return checks


def _record_report_check(name: str, label: str, result: Optional[Dict[str, Any]]) -> Optional[CheckResult]:
    """``report_<name>`` for a terminal ``result``: its report_path is set and the file exists.

    Shared by both closure rules, as :func:`_record_hash_check`.
    """
    if result is None or not gate_is_terminal(result):
        return None
    report_path = result.get("report_path", "")
    if not report_path:
        return CheckResult(f"report_{name}", "FAIL", f"{label} gate result is missing required report_path field")
    if not Path(report_path).exists():
        return CheckResult(f"report_{name}", "FAIL", f"{label} report_path does not exist: {report_path}")
    return CheckResult(f"report_{name}", "PASS", f"{label} normalized report exists at {report_path}")


def _check_deterministic_findings(contract: ReviewContract) -> CheckResult:
    """Return FAIL if any error-severity deterministic findings remain unresolved."""
    error_findings = [f for f in contract.deterministic_findings if f.severity == "error"]
    if error_findings:
        return CheckResult(
            "deterministic_findings",
            "FAIL",
            f"{len(error_findings)} unresolved error-severity deterministic finding(s)",
        )
    total = len(contract.deterministic_findings)
    return CheckResult("deterministic_findings", "PASS", f"{total} deterministic finding(s), 0 errors")


def _merge_record_seats(merge_record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The seat record a merge under the seat rule stored, or ``None`` for an older merge.

    The door writes ``review_seats`` (rule ``review_seat_evaluation.SEAT_RULE``)
    on every ``pr_merged`` receipt since OI-2058 R5b, and under
    ``RISK_SEAT_RULE`` since R3b. A receipt without it was merged under the
    one-seat rule; closure grandfathers it under that rule.
    """
    from review_seat_evaluation import RISK_SEAT_RULE, SEAT_RULE

    if not isinstance(merge_record, dict):
        return None
    record = merge_record.get("review_seats")
    if isinstance(record, dict) and record.get("rule") in (SEAT_RULE, RISK_SEAT_RULE):
        return record
    return None


def _seat_rule_checks(
    contract: ReviewContract,
    results_dir: Path,
    seat_record: Dict[str, Any],
) -> Tuple[List[CheckResult], frozenset]:
    """The seat rule for a PR merged under it: one check per seat, plus what blocks.

    Evaluated with the merge record's own seat list on the head it merged
    (``head_sha``), through the same evaluation the door used. A merge whose
    review was overridden recorded no seat list; its seats are resolved the way
    the door resolves them. Returns the checks and the seats they cover, so the
    per-gate handlers skip those gates (a seat may be filled by a takeover,
    which the per-gate reading of the old rule cannot see).

    No check the old rule ran disappears for a seat: the hash and report checks
    (:func:`_record_hash_check`, :func:`_record_report_check`) move to the
    record that fills the seat, named after the seat (OI-2058 R5b round 2).
    That record is ``SeatFill.record``: the exact record the door's route
    accepted (round 3), not a second lookup of the reviewer's slot. An unfilled
    seat gets them on the record that answered for it and did not hold. Seats
    resolved here read the class against the record's ``base_branch``, the base
    the door read from the PR; a record without one reads high.

    A record under ``SEAT_RULE`` (merged before R3b) is judged by that rule: a
    seat list it lacks is resolved without a risk class (``by_risk=False``), as
    before R3b. Under ``RISK_SEAT_RULE`` the record's ``seats`` are the seats
    its class required, and the stack seats its class did not require
    (:func:`_seats_not_required`) are covered too: the door asked nothing of
    them, so neither do the per-gate checks. A rejection by one of them on the
    merged head still fails closure here (``evaluation.blocking``), as it
    refuses the door.
    """
    from review_seat_evaluation import RISK_SEAT_RULE, SOURCE_MERGE_RECORD, evaluate_review_seats

    seats = [str(seat) for seat in seat_record.get("seats") or [] if str(seat).strip()]
    # round_ledger=False: the door counted the round ledger when it merged (R1);
    # closure judges the merge under the rule it merged under, as for the seats.
    evaluation = evaluate_review_seats(
        str(seat_record.get("pr_id") or contract.pr_id), results_dir,
        branch=contract.branch or None, head_sha=str(seat_record.get("head_sha") or ""),
        seats=seats or None, seats_source=SOURCE_MERGE_RECORD, round_ledger=False,
        by_risk=seat_record.get("rule") == RISK_SEAT_RULE, base_branch=seat_record.get("base_branch"),
    )
    checks: List[CheckResult] = []
    if evaluation.required.error:
        checks.append(CheckResult("review_seats", "FAIL", evaluation.message()))
    for rejection in evaluation.blocking:
        checks.append(CheckResult("review_seats_blocking", "FAIL", f"gemergde kop: {rejection.text()}"))
    for fill in evaluation.fills:
        detail = (
            f"seat {fill.seat} gevuld door {fill.reviewer} ({fill.route})" if fill.filled
            else f"seat {fill.seat} niet gevuld: {fill.message}"
        )
        checks.append(CheckResult(f"seat_{fill.seat}", "PASS" if fill.filled else "FAIL", detail))
        checks.extend(_seat_evidence_checks(fill, contract.content_hash))
    not_required = _seats_not_required(seat_record)
    if not_required:
        checks.append(CheckResult("review_seats_not_required", "PASS", _not_required_text(seat_record, not_required)))
    return checks, frozenset(evaluation.seats) | frozenset(not_required)


def _seats_not_required(seat_record: Dict[str, Any]) -> Tuple[str, ...]:
    """The stack seats a merge under ``RISK_SEAT_RULE`` did not require, read from its record alone.

    Only a record that says so positively leaves a seat out: a ``stack`` list,
    ``seats`` equal to ``required_seats``, and those seats exactly what
    ``seats_for_risk`` gives for the record's own stack and ``risk_class``. Any
    field missing, ``None``, malformed or inconsistent gives ``()``, and the
    per-gate checks demand every stack gate as before (fail-closed). The seats
    come from the merge record, never from a fresh resolution.
    """
    from review_seat_evaluation import RISK_SEAT_RULE, seats_for_risk

    if seat_record.get("rule") != RISK_SEAT_RULE:
        return ()
    stack, seats, required = (seat_record.get(key) for key in ("stack", "seats", "required_seats"))
    if not all(isinstance(value, list) for value in (stack, seats, required)):
        return ()
    if not seats or seats != required or tuple(seats) != seats_for_risk(stack, seat_record.get("risk_class")):
        return ()
    return tuple(seat for seat in seats_for_risk(stack, None) if seat not in seats)


def _not_required_text(seat_record: Dict[str, Any], not_required: Sequence[str]) -> str:
    return (
        f"risicoklasse {seat_record.get('risk_class')} ({seat_record.get('risk_reason') or 'geen reden vastgelegd'}): "
        f"de merge vereiste {', '.join(map(str, seat_record['seats']))} van stack "
        f"{', '.join(map(str, seat_record['stack']))}; "
        f"niet vereist en hier niet gevraagd: {', '.join(not_required)}. Een afkeuring op de gemergde kop "
        "blokkeert nog steeds (review_seats_blocking)"
    )


def _seat_evidence_checks(fill: Any, content_hash: str) -> List[CheckResult]:
    """The old rule's hash and report checks on the record that answers for one seat.

    A matching hash is a PASS here, so each seat names the reviewer and the hash
    of the record it checked.
    """
    reviewer = fill.reviewer or str((fill.record or {}).get("gate") or fill.seat)
    label = f"seat {fill.seat}: {reviewer}"
    found = (
        _record_hash_check(fill.seat, label, fill.record, content_hash, report_match=True),
        _record_report_check(fill.seat, label, fill.record),
    )
    return [check for check in found if check is not None]


def _validate_review_evidence(
    contract: ReviewContract,
    results_dir: Path,
    branch: Optional[str] = None,
    *,
    merge_record: Optional[Dict[str, Any]] = None,
) -> List[CheckResult]:
    """Validate review contract presence and gate results against the review stack.

    Checks:
    1. Review contract has required fields (pr_id, review_stack)
    2. Each gate in the review stack has a result
    3. Required gates (codex for high-risk) have passing verdicts
    4. Optional gates have explicit state (contributed or intentionally absent)
    5. Content hash consistency between contract and gate receipts
    6. No unresolved error-severity deterministic findings

    When ``branch`` is provided, gate results from a different branch are
    rejected as stale evidence — preventing a result from a prior branch with
    the same ``pr_id`` from satisfying current closure.

    ``merge_record`` (OI-2058, R5b) is the PR's ``pr_merged`` receipt. When it
    carries the seat rule (``review_seats``), every seat of that list must be
    filled by a distinct reviewer on the merged head and no reviewer may have
    rejected it (:func:`_seat_rule_checks`); the per-gate checks then cover
    only the gates that are no seat. Under ``RISK_SEAT_RULE`` (R3b) a stack
    seat the record's class did not require gets no per-gate check either, so
    a medium merge on its first seat alone closes as the door merged it. A
    merge record without it (merged before
    R5b), or no merge record, keeps the rule it was merged under: every gate of
    the contract's stack checked on its own record, so no closed or closable
    track reopens.
    """
    checks = _check_contract_fields(contract)
    if any(c.status == "FAIL" for c in checks):
        return checks

    seat_gates: frozenset = frozenset()
    seat_record = _merge_record_seats(merge_record)
    if seat_record is not None:
        seat_checks, seat_gates = _seat_rule_checks(contract, results_dir, seat_record)
        checks.extend(seat_checks)

    for gate in contract.review_stack:
        if gate in seat_gates:
            continue
        result = _find_gate_result(
            gate, contract.pr_id, results_dir, branch=contract.branch or None,
        )
        checks.append(_check_single_gate(gate, contract, result, results_dir, branch))

    checks.extend(_check_contract_hashes(contract, results_dir, seat_gates))
    checks.extend(_check_report_paths(contract, results_dir, seat_gates))
    checks.append(_check_deterministic_findings(contract))

    return checks


def _find_merge_record(receipts_file: Optional[Path], pr_id: str) -> Optional[Dict[str, Any]]:
    """The latest ``pr_merged`` receipt for this PR in ``receipts_file``, or ``None``.

    An internal label (``PR-HYG-1``) joins the receipt's own ``pr_id``,
    normalised on both sides. A bare GitHub number (``2073``) joins the
    receipt's ``pr_number`` and the ``pr_id`` its seat record names. The two
    namespaces are never crossed: label ``PR-3`` is not GitHub PR #3. A missing
    ledger is no merge record: closure then keeps the rule it had
    (grandfathered).
    """
    from gate_obligations import normalise_pr_id

    if receipts_file is None or not Path(receipts_file).is_file():
        return None
    found: Optional[Dict[str, Any]] = None
    with open(receipts_file, encoding="utf-8") as handle:
        for line in handle:
            if '"pr_merged"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("event_type") != "pr_merged":
                continue
            if _merge_record_names(record, pr_id, normalise_pr_id):
                found = record
    return found


def _merge_record_names(record: Dict[str, Any], pr_id: str, normalise) -> bool:
    if pr_id.isdigit():
        seats = record.get("review_seats") if isinstance(record.get("review_seats"), dict) else {}
        return pr_id in {str(record.get("pr_number") or ""), str(seats.get("pr_id") or "")}
    label = str(record.get("pr_id") or "")
    return bool(label) and normalise(label) == normalise(pr_id)


def _canonical_pr_id(value: str) -> str:
    """``#2176``, ``2176`` and `` 2176 `` are one PR; any other id stays as written."""
    text = value.strip()
    digits = text.lstrip("#").strip()
    return str(int(digits)) if digits.isdigit() else text


def _check_stale_staging(paths: Dict[str, str], active_pr_ids: Iterable[str]) -> CheckResult:
    """A staging dispatch for a PR the closing track does not name is stale.

    ``active_pr_ids`` are the track's PR identities from the tracks DB
    (ADR-047 §4). When the track could not be read the set is empty, so every
    staging dispatch that names a PR is stale: fail closed.
    """
    staging_dir = Path(paths["VNX_DISPATCH_DIR"]) / "staging"
    if not staging_dir.exists():
        return CheckResult("stale_staging", "PASS", "no staging directory")

    active_set = {_canonical_pr_id(pr_id) for pr_id in active_pr_ids}
    stale: List[str] = []
    for dispatch in staging_dir.glob("*.md"):
        content = _read_text(dispatch)
        match = re.search(r"^PR-ID:\s*(.+)$", content, re.MULTILINE)
        pr_id = match.group(1).strip() if match else ""
        if pr_id and _canonical_pr_id(pr_id) not in active_set:
            stale.append(dispatch.name)

    if stale:
        return CheckResult("stale_staging", "FAIL", f"stale staging dispatches present: {', '.join(sorted(stale))}")
    return CheckResult("stale_staging", "PASS", "no stale staging dispatches")


def _pr_dispatch_checks(pr_id: str, pr_reconciled: Any) -> List[CheckResult]:
    """``pr_has_dispatch`` PASS for a reconciled PR, then ``pr_completed`` (ADR-047 §2).

    Completed means the dispatch is in ``completed/`` AND has a terminal
    receipt. Only an exact ``receipt_confirmed is True`` counts: a missing,
    ``None`` or malformed flag is no receipt, so the PR is not completed.
    """
    provenance = pr_reconciled.provenance
    has_dispatch = CheckResult(
        "pr_has_dispatch", "PASS",
        f"dispatch {provenance.get('dispatch_id') or '?'} names {pr_id}",
    )
    if pr_reconciled.state != "completed":
        detail, status = f"{pr_id} state is {pr_reconciled.state}, not completed", "FAIL"
    elif provenance.get("receipt_confirmed") is True:
        detail, status = f"{pr_id} is completed via {provenance.get('source', '?')} with a terminal receipt", "PASS"
    else:
        detail, status = f"{pr_id} dispatch is in completed/ but has no terminal receipt, so it is not completed", "FAIL"
    return [has_dispatch, CheckResult("pr_completed", status, detail)]


def verify_pr_closure(
    *,
    pr_id: str,
    project_root: Path,
    dispatch_dir: Path,
    receipts_file: Path,
    state_dir: Path,
    review_contract: Optional[ReviewContract] = None,
    gate_results_dir: Optional[Path] = None,
    branch: Optional[str] = None,
    require_github_pr: bool = False,
) -> Dict[str, Any]:
    """Verify closure for a single PR without requiring whole-feature completion.

    This runs queue reconciliation to derive the PR's current state,
    validates gate evidence for that PR, and detects contradictions
    between structured gate results and normalized reports.

    No plan file is read (ADR-047 §2). The PR exists when a dispatch names it
    (``pr_has_dispatch``); the ``pr_queue_state.json`` projection only feeds
    drift detection and never adds or removes a PR. ``pr_completed`` needs the
    dispatch in ``completed/`` AND a terminal receipt for it: a completed
    dispatch without that receipt is not a closed PR.

    When ``branch`` is provided and ``require_github_pr`` is True, also verifies
    that a real GitHub PR exists for the branch and that required CI checks are
    green (pre-merge mode).  Local-only closure cannot pass merge readiness when
    GitHub PR / CI linkage is required.
    """
    from queue_reconciler import QueueReconciler

    checks: List[CheckResult] = []
    paths = ensure_env()

    # 1. Reconcile queue to get fresh PR state
    projection_file = state_dir / "pr_queue_state.json"
    reconciler = QueueReconciler(
        dispatch_dir=dispatch_dir,
        receipts_file=receipts_file,
        projection_file=projection_file if projection_file.is_file() else None,
    )
    result = reconciler.reconcile()

    pr_reconciled = next((p for p in result.prs if p.pr_id == pr_id), None)
    if pr_reconciled is None:
        checks.append(CheckResult(
            "pr_has_dispatch", "FAIL",
            f"no dispatch names {pr_id}, so it is not in the queue",
        ))
        return {
            "verdict": "fail",
            "mode": "per_pr",
            "pr_id": pr_id,
            "checks": [c.__dict__ for c in checks],
            "reconciled_state": None,
            "review_evidence": None,
        }
    # 2. The dispatch names the PR, and the PR is completed with a terminal receipt
    checks.extend(_pr_dispatch_checks(pr_id, pr_reconciled))

    # 3. Check for blocking drift affecting this PR
    pr_drift = [w for w in result.drift_warnings if w.pr_id == pr_id and w.severity == "blocking"]
    if pr_drift:
        checks.append(CheckResult(
            "queue_drift", "FAIL",
            f"{pr_id} has blocking queue drift: {pr_drift[0].message}",
        ))
    else:
        checks.append(CheckResult(
            "queue_drift", "PASS",
            f"no blocking drift for {pr_id}",
        ))

    # 4. Review contract and gate evidence
    review_evidence_summary: Optional[Dict[str, Any]] = None
    if review_contract is not None:
        # Per-PR closure must reject a contract for a different PR — combining
        # PR-A's queue state with PR-B's review evidence can falsely green.
        if review_contract.pr_id and review_contract.pr_id != pr_id:
            checks.append(CheckResult(
                "review_contract_pr_match",
                "FAIL",
                f"review contract pr_id {review_contract.pr_id!r} does not match closure pr_id {pr_id!r}",
            ))
        else:
            effective_results_dir = gate_results_dir
            if effective_results_dir is None:
                effective_results_dir = Path(paths["VNX_STATE_DIR"]) / "review_gates" / "results"
            merge_record = _find_merge_record(receipts_file, pr_id)
            evidence = _validate_review_evidence(review_contract, effective_results_dir, branch=branch, merge_record=merge_record)
            checks.extend(evidence)

            # 5. Gate result vs report contradiction detection
            checks.extend(
                _detect_gate_report_contradictions(
                    review_contract, effective_results_dir, branch=branch
                )
            )

            review_evidence_summary = {
                "contract_pr_id": review_contract.pr_id,
                "contract_hash": review_contract.content_hash,
                "review_stack": review_contract.review_stack,
                "risk_class": review_contract.risk_class,
                "deterministic_finding_count": len(review_contract.deterministic_findings),
                "error_finding_count": len([f for f in review_contract.deterministic_findings if f.severity == "error"]),
            }
    else:
        checks.append(CheckResult(
            "review_contract", "FAIL",
            "no review contract provided — per-PR closure requires contract-backed evidence",
        ))

    # 5. GitHub PR and CI checks (when branch is provided and explicitly required)
    # Local-only closure cannot pass merge readiness without a real GitHub PR + green checks.
    # Per-PR mode must mirror the full-feature pre_merge guarantees: state == OPEN and
    # mergeStateStatus == CLEAN, otherwise a draft, blocked, or merged-but-stale PR with
    # green CI could be reported as merge-ready.
    github_pr: Optional[Dict[str, Any]] = None
    if branch and require_github_pr:
        github_pr = _find_branch_pr(branch)
        checks.append(CheckResult(
            "github_pr_exists",
            "PASS" if github_pr else "FAIL",
            f"GitHub PR {'found' if github_pr else 'missing'} for branch {branch}",
        ))
        if github_pr:
            state = str(github_pr.get("state") or "").upper()
            merge_state = str(github_pr.get("mergeStateStatus") or "").upper()
            mergeable = state == "OPEN" and merge_state == "CLEAN"
            checks.append(CheckResult(
                "github_pr_mergeable",
                "PASS" if mergeable else "FAIL",
                f"PR state={state or 'unknown'} mergeStateStatus={merge_state or 'unknown'}"
                + ("" if mergeable else " — PR must be OPEN and CLEAN for merge readiness"),
            ))
            rollup = github_pr.get("statusCheckRollup") or []
            all_green = _rollup_all_green(rollup)
            checks.append(CheckResult(
                "github_checks",
                "PASS" if all_green else "FAIL",
                "all required GitHub checks green" if all_green
                else "GitHub checks incomplete or failing — merge readiness blocked",
            ))

    verdict = "pass" if all(c.status == "PASS" for c in checks) else "fail"
    return {
        "verdict": verdict,
        "mode": "per_pr",
        "pr_id": pr_id,
        "branch": branch,
        "reconciled_state": {
            "pr_id": pr_reconciled.pr_id,
            "state": pr_reconciled.state,
            "provenance": pr_reconciled.provenance,
        },
        "checks": [c.__dict__ for c in checks],
        "review_evidence": review_evidence_summary,
        "github_pr": github_pr,
    }


# The producer line a gate report writes about itself. Accepts the bold-field
# form the report contract requires (``**Model**: x``) and the bare/frontmatter
# forms that also occur, because the point is to READ what the report claims,
# not to police how it is written.
_REPORT_MODEL_RE = re.compile(
    r"(?im)^\s*[-*]?\s*\**\s*model\s*\**\s*[:=]\s*\**\s*([A-Za-z0-9._\-/]+)"
)


def _report_declared_model(report_content: str) -> str:
    """The model the report says produced it, or "" when it does not say."""
    match = _REPORT_MODEL_RE.search(report_content)
    return match.group(1).strip() if match else ""


def _detect_producer_identity_contradiction(
    gate: str,
    result: Dict[str, Any],
    report_content: str,
) -> Optional[CheckResult]:
    """The record and its own report must name the same model (OI-1450).

    Measured across the central store on 2026-08-29: of 48 gate result records
    carrying a ``model``, **7 name a different model than the report they point
    at** — every one of them ``kimi-k2-7-code`` in the record against
    ``kimi-default`` in the report. Both are producer-identity claims about the
    same run, and they cannot both be true.

    That matters beyond tidiness, because producer identity is what
    ``record_terminal_result`` refuses a terminal write without: a record whose
    identity disagrees with its own evidence is not authenticated by it, it is
    merely accompanied by it. Cost attribution, model-quality comparison and
    "which model missed this" all read one of the two numbers and neither
    knows the other exists.

    Returns ``None`` when there is nothing to compare — no model on the record,
    or no model line in the report. Silence about an unmeasurable pair is the
    honest answer; asserting agreement would be inventing it.
    """
    record_model = str(result.get("model") or "").strip()
    if not record_model:
        return None
    report_model = _report_declared_model(report_content)
    if not report_model:
        return None
    if record_model == report_model:
        return CheckResult(
            f"producer_identity_{gate}",
            "PASS",
            f"{gate}: record and report agree on model={record_model}",
        )
    return CheckResult(
        f"producer_identity_{gate}",
        "FAIL",
        f"{gate}: result record says model={record_model!r} but its own report "
        f"says model={report_model!r} — two producer-identity claims about one "
        f"run, and the record is not authenticated by evidence that disagrees "
        f"with it",
    )


def _detect_gate_report_contradictions(
    contract: ReviewContract,
    results_dir: Path,
    branch: Optional[str] = None,
) -> List[CheckResult]:
    """Detect contradictions between structured gate result JSON and normalized report content.

    A contradiction exists when:
    - Gate result JSON says pass but report contains blocking findings
    - Gate result JSON says fail but report claims all clear
    - Gate result blocking_count disagrees with actual blocking findings in report

    When ``branch`` is provided, gate results from a different branch are
    rejected as stale evidence so contradiction detection always runs against
    the current branch's payloads.
    """
    checks: List[CheckResult] = []

    for gate in contract.review_stack:
        if gate not in _KNOWN_GATES:
            continue
        result = _find_gate_result(
            gate, contract.pr_id, results_dir, branch=contract.branch or None,
        )
        if result is None:
            continue

        # CFX-17: contradiction detection must see the same demoted view of codex
        # findings the gate decision used, otherwise a successful demotion would
        # be flagged as a gate/report mismatch.
        if gate == "codex_gate":
            result = _apply_codex_severity_policy(result)

        report_path_str = result.get("report_path", "")
        if not report_path_str:
            # A record that carries producer identity but points at no report
            # is the shape that hid OI-1450: all seven contradicting records
            # have report_path=None, so a detector keyed on report_path never
            # looked at them. Silently skipping reads as "consistent" in a
            # list of PASS lines. Say what is actually true instead.
            if str(result.get("model") or "").strip():
                checks.append(CheckResult(
                    f"producer_identity_{gate}",
                    "WARN",
                    f"{gate}: result record claims model="
                    f"{str(result.get('model')).strip()!r} but carries no "
                    f"report_path — the claim cannot be checked against any "
                    f"evidence",
                ))
            continue

        report_path = Path(report_path_str)
        if not report_path.exists():
            continue

        try:
            report_content = report_path.read_text(encoding="utf-8")
        except OSError:
            continue

        # Use gate_is_pass so all PASS_STATES (completed, approve, passed, pass)
        # are treated as a positive gate outcome for contradiction purposes.
        # A narrower check that only recognises literal "pass"/"fail" would
        # miss status="completed" and status="approve", which are valid pass
        # states too.
        passed, _ = gate_is_pass(result)
        gate_blocking = result.get("blocking_count", 0)
        if not isinstance(gate_blocking, int):
            gate_blocking = len(result.get("blocking_findings") or [])

        # Count blocking-severity indicators in report
        report_blocking_indicators = _count_report_blocking_indicators(report_content)

        # Contradiction 1: gate says pass but report has blocking indicators
        if passed and report_blocking_indicators > 0:
            checks.append(CheckResult(
                f"contradiction_{gate}",
                "FAIL",
                f"{gate}: gate result says pass but report contains "
                f"{report_blocking_indicators} blocking indicator(s) — evidence mismatch",
            ))
        # Contradiction 2: gate says fail/blocking but report has none
        elif (not passed) and gate_blocking > 0 and report_blocking_indicators == 0:
            checks.append(CheckResult(
                f"contradiction_{gate}",
                "FAIL",
                f"{gate}: gate result says fail with {gate_blocking} blocking finding(s) "
                f"but report contains no blocking indicators — evidence mismatch",
            ))
        else:
            checks.append(CheckResult(
                f"contradiction_{gate}",
                "PASS",
                f"{gate}: gate result and report content are consistent",
            ))

        identity_check = _detect_producer_identity_contradiction(
            gate, result, report_content,
        )
        if identity_check is not None:
            checks.append(identity_check)

    return checks


_CLOSURE_OUTPUT_RE = re.compile(r"^\s*-?\s*\[(FAIL|PASS|WARN)\]\s")

# Matches the normalized findings block gate_artifacts.format_report writes
# (``_format_findings_section``) ahead of the raw ``## Gate Output`` tool
# transcript. Captures everything from just past the "## Findings" heading
# up to the next "## "-level heading (or end of string), so the raw
# transcript that follows is never part of the captured text.
_FINDINGS_SECTION_RE = re.compile(r"^## Findings[ \t]*$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def _extract_normalized_findings_section(content: str) -> Optional[str]:
    """Return the report's normalized ``## Findings`` block, or None if absent.

    Headless gate reports (codex_gate) pair a normalized findings section —
    written from the reviewer's actual parsed verdict — with a raw
    ``## Gate Output`` dump of its full tool transcript. That transcript is
    read material (tool calls, contents of files the reviewer inspected)
    and can contain the same substrings this scan looks for — a `blocking:`
    parameter name, a `severity: blocking` string literal — in code the
    reviewer merely read, never in its verdict (OI-1394). Reports without a
    normalized section (e.g. ci_gate's compact non-toolstream output) return
    None here, and the caller falls back to a full-content scan — unchanged
    from prior behavior for those gates.
    """
    match = _FINDINGS_SECTION_RE.search(content)
    return match.group(1) if match else None


# Matches a gate report's own trailing verdict fence — the SAME shape
# glm_gate._extract_verdict / kimi_gate._extract_verdict parse (OI-1473).
# Kept as its own copy for the same reason those two don't share one: each
# reader stays self-contained, but the RULE must never diverge.
_VERDICT_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_FENCE_EXTRA_VERDICTS = frozenset({"block"})


def _fence_verdict_word(value: Any) -> str:
    """The fence's verdict as a lowercase word, ``""`` when it is not a real one.

    The shared reading :func:`gate_lane_contract.canonical_review_verdict`
    (``revise`` is a ``fail``, OI-1947) plus the ``block`` word this reader has
    always accepted. ``approve``, the echoed ``pass|fail|blocked`` placeholder and
    anything else stay refused.
    """
    word = canonical_review_verdict(value)
    if word:
        return word
    raw = str(value if value is not None else "").strip().lower()
    return raw if raw in _FENCE_EXTRA_VERDICTS else ""
_FENCE_BLOCKING_SEVERITIES = frozenset({"blocking", "error"})


def _extract_report_verdict_fence(content: str) -> Optional[Dict[str, Any]]:
    """Return the report's own trailing ```json``` verdict fence as a dict.

    Scans fenced JSON blocks from the END of the document and returns the
    first one (i.e. the LAST fence in the report) whose ``verdict`` field is
    a real verdict value — mirroring glm_gate/kimi_gate's own
    ``_extract_verdict``, so a review-gate report and the gate script that
    produced it can never read two different "actual answers" out of the
    same text. This also means an echoed instruction template earlier in the
    report (whose ``verdict`` field is the literal placeholder
    "pass|fail|blocked") is skipped exactly like it is at the gate script
    itself, never mistaken for a real verdict here.

    Returns None when no such fence exists — the caller falls back to the
    normalized ``## Findings`` section, then the prose scan.
    """
    for block in reversed(_VERDICT_FENCE_RE.findall(content or "")):
        try:
            obj = json.loads(block)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and _fence_verdict_word(obj.get("verdict")):
            return obj
    return None


def _count_fence_blocking_indicators(fence: Dict[str, Any]) -> int:
    """Count blocking-severity indicators in an already-parsed verdict fence.

    Reads two STRUCTURED fields, never prose: the fence's own ``verdict``
    (non-pass counts once) and each ``findings[]`` entry's ``severity``
    field (``blocking``/``error`` counts once each). A finding's free-text
    ``message`` is never scanned — that is exactly the loose-word matching
    this invariant must not do (OI-1473).
    """
    count = 0
    verdict = _fence_verdict_word(fence.get("verdict"))
    if verdict and verdict != "pass":
        count += 1
    findings = fence.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            severity = str(finding.get("severity", "")).strip().lower()
            if severity in _FENCE_BLOCKING_SEVERITIES:
                count += 1
    return count


# Negation markers that flip a blocking-shaped word into its own denial — a
# reviewer writing "Not blocking: ..." or "niet-blocking" is asserting the
# ABSENCE of a blocking finding, and a scan that flags the denial punishes
# the more precise report (OI-1473, live on PR #1691). Checked against the
# few characters immediately preceding a match, case-insensitively.
_NEGATION_MARKERS = ("niet-", "non-", "no ", "not ", "geen ")
_NEGATION_LOOKBACK = 15

# Structural severity-field markers: each requires the literal field prefix
# the report writers emit (gate_artifacts._format_findings_section's
# "[BLOCKING]"/"(severity: ...)" shapes), never a bare word in prose. Safe to
# scan within a normalized ``## Findings`` section on its own.
_FINDINGS_SECTION_PATTERNS = (
    r"\[BLOCKING\]",
    r"\*\*Severity\*\*:\s*blocking",
    r"severity:\s*blocking",
)

# Vangnet patterns for reports with neither a verdict fence nor a normalized
# findings section — the same field markers above, plus two looser prose
# markers that only apply as a last resort.
_PROSE_FALLBACK_PATTERNS = _FINDINGS_SECTION_PATTERNS + (
    r"BLOCK(?:ER|ING)\s*:",
    r"Status:\s*FAIL",
)


def _count_matches_excluding_negations(patterns: Sequence[str], text: str) -> int:
    """Count regex matches across *patterns* in *text*, skipping any match
    immediately preceded by a negation marker (see ``_NEGATION_MARKERS``)."""
    count = 0
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            window_start = max(0, match.start() - _NEGATION_LOOKBACK)
            preceding = text[window_start:match.start()].lower()
            if any(marker in preceding for marker in _NEGATION_MARKERS):
                continue
            count += 1
    return count


def _count_report_blocking_indicators(content: str) -> int:
    """Count blocking-severity indicators in a gate report, on STRUCTURED
    evidence wherever it exists — never a loose word in prose (OI-1473).

    Excludes lines written by the closure verifier itself (``- [FAIL] ...``)
    to prevent a self-reference loop where the verifier's own output is
    counted as a blocking indicator on re-read. Three tiers, each used only
    when the previous one found nothing to read a verdict from:

    1. Primary — the report's own trailing ```json``` verdict fence (see
       ``_extract_report_verdict_fence``): a contradiction is the fence's
       ``verdict`` being non-pass, or any ``findings[]`` entry carrying
       severity ``blocking``/``error``. When a fence exists, it is
       authoritative — the count it produces is returned as-is, even 0.
    2. Secondary — the normalized ``## Findings`` section (see
       ``_extract_normalized_findings_section``), scanned for structural
       severity-field markers only (``_FINDINGS_SECTION_PATTERNS``). Also
       authoritative when present, even 0.
    3. Vangnet — neither of the above exists: a full-content prose scan
       (``_PROSE_FALLBACK_PATTERNS``) that skips any match preceded by a
       negation marker, so "Not blocking: ..." / "niet-blocking" cannot
       trip a contradiction a reviewer explicitly ruled out.

    A report with no fence, no findings section, and no vangnet match is
    genuinely silent on blocking evidence — that is an absence of evidence,
    not evidence of contradiction, so it returns 0 (fail-open by design).
    """
    filtered_lines = [
        line for line in content.splitlines()
        if not _CLOSURE_OUTPUT_RE.match(line)
    ]
    filtered = "\n".join(filtered_lines)

    fence = _extract_report_verdict_fence(filtered)
    if fence is not None:
        return _count_fence_blocking_indicators(fence)

    findings_section = _extract_normalized_findings_section(filtered)
    if findings_section is not None:
        return _count_matches_excluding_negations(_FINDINGS_SECTION_PATTERNS, findings_section)

    return _count_matches_excluding_negations(_PROSE_FALLBACK_PATTERNS, filtered)


def _report_blocking_indicators(report_path: Path) -> Optional[int]:
    """Invariant 7's reading of one report on disk: its blocking indicators, ``None`` when unreadable.

    The one report reader for a contradiction between a record and its report.
    The merge door judges a seat's record with it
    (:func:`_merge_door_record_verdict`), and the seat evaluation uses it to
    find a rejection in the report of any review peer on the head
    (``review_seat_evaluation.report_rejects``, OI-2058 R5b round 2).
    """
    try:
        content = Path(report_path).read_text(encoding="utf-8")
    except OSError:
        return None
    return _count_report_blocking_indicators(content)


_DISPATCH_BRANCH_PREFIX = "dispatch/"


@dataclass(frozen=True)
class TrackEvidence:
    """What the tracks DB says about the track a whole-feature closure closes.

    A feature is a track (ADR-047 §3). ``derived_status`` is the stored value
    the track reconciler wrote; ``recomputed_status`` is ``_compute_derived_status``
    on the store as it is now, so a stale stored ``done`` cannot close a track
    that a later blocker or an unmerged PR holds. ``error`` names why the track
    could not be read; when it is set the other fields are empty and the track
    checks fail.
    """

    track_id: str = ""
    title: str = ""
    derived_status: Optional[str] = None
    recomputed_status: Optional[str] = None
    pr_numbers: frozenset = frozenset()
    merged: frozenset = frozenset()
    pr_ids: frozenset = frozenset()
    hold: Optional[Dict[str, Any]] = None
    error: str = ""


def _track_evidence_from_conn(
    conn: Any, project_id: str, branch: str, track_id: Optional[str], state_dir: Path, project_root: Path
) -> TrackEvidence:
    """Resolve the track (explicit, else the dispatch behind ``dispatch/<id>``) and read it."""
    from track_reconciler import (
        _compute_derived_status,
        _delivery_hold,
        _has_col,
        _load_merged_pr_numbers,
        _parse_pr_numbers,
    )

    if not track_id:
        if not branch.startswith(_DISPATCH_BRANCH_PREFIX):
            return TrackEvidence(error=f"no track given and branch {branch!r} is not dispatch/<dispatch_id>")
        dispatch_id = branch[len(_DISPATCH_BRANCH_PREFIX):]
        row = conn.execute(
            "SELECT track FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
            (dispatch_id, project_id),
        ).fetchone()
        track_id = str((row["track"] if row else None) or "").strip()
        if not track_id:
            return TrackEvidence(error=f"dispatch {dispatch_id!r} names no track in project {project_id!r}")
    if not _has_col(conn, "tracks", "derived_status"):
        return TrackEvidence(track_id=track_id, error="tracks.derived_status absent: migration 0028 not applied")
    track = conn.execute(
        "SELECT title, derived_status, pr_ref FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    if track is None:
        return TrackEvidence(track_id=track_id, error=f"track {track_id!r} not found in project {project_id!r}")
    dispatch_refs = [
        row["pr_ref"] for row in conn.execute(
            "SELECT pr_ref FROM dispatches WHERE track = ? AND project_id = ? AND pr_ref IS NOT NULL",
            (track_id, project_id),
        )
    ]
    pr_numbers = _parse_pr_numbers(track["pr_ref"])
    pr_ids = {str(n) for ref in dispatch_refs for n in _parse_pr_numbers(ref)} | {str(n) for n in pr_numbers}
    merged = _load_merged_pr_numbers(state_dir, project_root) if pr_numbers else frozenset()
    return TrackEvidence(
        track_id=track_id,
        title=track["title"] or "",
        derived_status=track["derived_status"],
        recomputed_status=_compute_derived_status(conn, track_id, project_id, merged),
        pr_numbers=pr_numbers,
        merged=frozenset(merged),
        pr_ids=frozenset(pr_ids),
        hold=_delivery_hold(conn, track_id, project_id, track["pr_ref"]),
    )


def _read_track_evidence(state_dir: Path, project_root: Path, branch: str, track_id: Optional[str]) -> TrackEvidence:
    """Read the closing track from the tracks DB. Anything not derivable is an error, never a pass."""
    import sqlite3

    import tracks as tracks_lib
    from project_root import resolve_project_id

    db_path = tracks_lib._db_path(state_dir)
    if not db_path.is_file():
        return TrackEvidence(error=f"tracks DB not found: {db_path}")
    try:
        project_id = resolve_project_id(project_root)
    except RuntimeError as exc:
        return TrackEvidence(error=f"project_id not resolvable (ADR-007): {exc}")
    try:
        conn = tracks_lib._get_conn(state_dir)
    except (sqlite3.Error, tracks_lib.EmptySchemaDecoyError) as exc:
        return TrackEvidence(error=f"tracks DB unreadable: {exc}")
    try:
        return _track_evidence_from_conn(conn, project_id, branch, track_id, state_dir, project_root)
    except sqlite3.Error as exc:
        return TrackEvidence(error=f"tracks DB query failed: {exc}")
    finally:
        conn.close()


def _track_closure_checks(track: TrackEvidence) -> List[CheckResult]:
    """ADR-047 §3: ``track_derived_status`` replaces feature_plan_status, ``track_prs_delivered``
    replaces pr_queue_complete; metadata_sync is gone without a replacement."""
    if track.error:
        return [
            CheckResult("track_derived_status", "FAIL", track.error),
            CheckResult("track_prs_delivered", "FAIL", track.error),
        ]
    status = f"{track.derived_status or 'missing'} (recomputed: {track.recomputed_status or 'missing'})"
    done = track.derived_status == "done" and track.recomputed_status == "done"
    problems: List[str] = [] if done else [f"derived_status is {status}, not done"]
    if not track.pr_numbers:
        problems.append("tracks.pr_ref names no PR")
    unmerged = sorted(track.pr_numbers - track.merged)
    if unmerged:
        problems.append("not merged: " + ", ".join(f"#{n}" for n in unmerged))
    if track.hold is not None:
        held = ", ".join(f"#{n}" for n in track.hold["held_prs"])
        problems.append(f"delivery hold on {held}: {track.hold['reason']}")
    prs = ", ".join(f"#{n}" for n in sorted(track.pr_numbers))
    return [
        CheckResult("track_derived_status", "PASS" if done else "FAIL", f"track {track.track_id} derived_status is {status}"),
        CheckResult(
            "track_prs_delivered",
            "FAIL" if problems else "PASS",
            "; ".join(problems) if problems else f"track {track.track_id}: {prs} merged, no delivery hold",
        ),
    ]


def verify_closure(
    *,
    project_root: Path,
    branch: str,
    mode: str,
    track_id: Optional[str] = None,
    claim_file: Optional[Path] = None,
    review_contract: Optional[ReviewContract] = None,
    gate_results_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Whole-feature closure. The feature is a track, read from the tracks DB
    (``track_id``, else the dispatch behind a ``dispatch/<id>`` branch); no plan file is read."""
    claims = _load_claim_file(claim_file)
    paths = ensure_env()
    state_dir = Path(paths["VNX_STATE_DIR"])

    track = _read_track_evidence(state_dir, project_root, branch, track_id)
    checks: List[CheckResult] = _track_closure_checks(track)

    branch_exists = _remote_branch_exists(branch, project_root)
    checks.append(
        CheckResult(
            "branch_pushed",
            "PASS" if branch_exists else "FAIL",
            f"remote branch {'found' if branch_exists else 'missing'}: {branch}",
        )
    )

    pr = _find_branch_pr(branch)
    checks.append(
        CheckResult(
            "pr_exists",
            "PASS" if pr else "FAIL",
            f"PR {'found' if pr else 'missing'} for branch {branch}",
        )
    )

    if pr:
        if mode == "pre_merge":
            clean = str(pr.get("mergeStateStatus") or "").upper() == "CLEAN" and str(pr.get("state") or "").upper() == "OPEN"
            checks.append(
                CheckResult(
                    "merge_state",
                    "PASS" if clean else "FAIL",
                    f"PR state={pr.get('state')} mergeStateStatus={pr.get('mergeStateStatus')}",
                )
            )
            rollup = pr.get("statusCheckRollup") or []
            all_green = _rollup_all_green(rollup)
            checks.append(
                CheckResult(
                    "github_checks",
                    "PASS" if all_green else "FAIL",
                    "all required GitHub checks green" if all_green else "GitHub checks incomplete or failing",
                )
            )
        else:
            merged = str(pr.get("state") or "").upper() == "MERGED"
            checks.append(
                CheckResult(
                    "pr_merged",
                    "PASS" if merged else "FAIL",
                    f"PR state={pr.get('state')}",
                )
            )
            merge_commit = ((pr.get("mergeCommit") or {}) if isinstance(pr.get("mergeCommit"), dict) else {}) or {}
            oid = merge_commit.get("oid")
            on_main = bool(oid) and _merge_commit_on_main(oid, project_root)
            checks.append(
                CheckResult(
                    "merge_commit_on_main",
                    "PASS" if on_main else "FAIL",
                    f"merge commit {'present on origin/main' if on_main else 'missing from origin/main'}",
                )
            )

    checks.extend(_validate_test_claims(claims, project_root))
    checks.append(_check_stale_staging(paths, track.pr_ids))

    # Review contract and gate evidence enforcement
    review_evidence_summary: Optional[Dict[str, Any]] = None
    if review_contract is not None:
        effective_results_dir = gate_results_dir
        if effective_results_dir is None:
            effective_results_dir = Path(paths["VNX_STATE_DIR"]) / "review_gates" / "results"
        checks.extend(_validate_review_evidence(
            review_contract, effective_results_dir, branch=branch, merge_record=_feature_merge_record(pr, paths),
        ))
        # Gate result vs report contradiction detection — feature-level closure
        # must catch a [BLOCKING] report shipped alongside a passing gate JSON.
        checks.extend(
            _detect_gate_report_contradictions(
                review_contract, effective_results_dir, branch=branch
            )
        )
        review_evidence_summary = {
            "contract_pr_id": review_contract.pr_id,
            "contract_hash": review_contract.content_hash,
            "review_stack": review_contract.review_stack,
            "risk_class": review_contract.risk_class,
            "deterministic_finding_count": len(review_contract.deterministic_findings),
            "error_finding_count": len([f for f in review_contract.deterministic_findings if f.severity == "error"]),
        }
    else:
        checks.append(CheckResult(
            "review_contract",
            "FAIL",
            "no review contract provided — closure requires contract-backed evidence",
        ))

    verdict = "pass" if all(check.status == "PASS" for check in checks) else "fail"
    payload = {
        "verdict": verdict,
        "mode": mode,
        "branch": branch,
        "track_id": track.track_id or None,
        "feature_title": track.title,
        "checks": [check.__dict__ for check in checks],
        "pr": pr,
        "claim_file": str(claim_file) if claim_file else None,
        "review_evidence": review_evidence_summary,
    }
    return payload


def _feature_merge_record(pr: Optional[Dict[str, Any]], paths: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The ``pr_merged`` receipt of the feature branch's PR in this project's ledger, or ``None``."""
    if not pr or pr.get("number") is None:
        return None
    return _find_merge_record(Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson", str(pr.get("number")))


def _default_claim_file(paths: Dict[str, str]) -> Path:
    return Path(paths["VNX_STATE_DIR"]) / "closure_claim.json"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VNX closure verifier")
    parser.add_argument("--branch", default=None)
    parser.add_argument(
        "--track-id",
        default=None,
        help="Whole-feature closure: the track to close (default: the track of the dispatch behind a dispatch/<id> branch)",
    )
    parser.add_argument("--mode", choices=("pre_merge", "post_merge"), default="pre_merge")
    parser.add_argument("--claim-file", default=None)
    parser.add_argument("--review-contract", default=None, help="Path to review contract JSON")
    parser.add_argument("--gate-results-dir", default=None, help="Directory containing gate result JSONs")
    parser.add_argument("--pr-id", default=None, help="Per-PR closure mode: verify a single PR without requiring whole-feature completion")
    parser.add_argument(
        "--require-github-pr",
        action="store_true",
        help="In --pr-id mode, require a real GitHub PR + green checks for the branch (auto-enabled when --mode pre_merge).",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--emit-receipt", action="store_true")
    args = parser.parse_args(argv)

    paths = ensure_env()
    project_root = Path(paths["PROJECT_ROOT"]).resolve()

    contract: Optional[ReviewContract] = None
    if args.review_contract:
        contract_path = Path(args.review_contract)
        if contract_path.exists():
            contract = ReviewContract.from_json(contract_path.read_text(encoding="utf-8"))

    gate_results_dir: Optional[Path] = None
    if args.gate_results_dir:
        gate_results_dir = Path(args.gate_results_dir)

    # Per-PR closure mode
    if args.pr_id:
        # Infer the current branch when --branch is omitted so the caller does
        # not have to repeat themselves and so require_github_pr is reachable
        # with just --pr-id + --mode pre_merge.
        branch = args.branch or _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root
        ).stdout.strip() or None
        # pre_merge mode auto-enables GitHub PR enforcement; --require-github-pr
        # forces it explicitly in any mode (CFX-2).
        require_github_pr = bool(args.require_github_pr or args.mode == "pre_merge")
        result = verify_pr_closure(
            pr_id=args.pr_id,
            project_root=project_root,
            dispatch_dir=Path(paths["VNX_DISPATCH_DIR"]),
            receipts_file=Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson",
            state_dir=Path(paths["VNX_STATE_DIR"]),
            review_contract=contract,
            gate_results_dir=gate_results_dir,
            branch=branch,
            require_github_pr=require_github_pr,
        )
    else:
        branch = args.branch or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root).stdout.strip()
        claim_file = Path(args.claim_file) if args.claim_file else _default_claim_file(paths)

        result = verify_closure(
            project_root=project_root,
            branch=branch,
            mode=args.mode,
            track_id=args.track_id,
            claim_file=claim_file if claim_file.exists() else None,
            review_contract=contract,
            gate_results_dir=gate_results_dir,
        )

        if args.emit_receipt:
            emit_governance_receipt(
                "closure_verification_result",
                receipt_kind="review_gate",
                status="success" if result["verdict"] == "pass" else "blocked",
                branch=branch,
                verification_mode=args.mode,
                feature_title=result.get("feature_title"),
                verifier="closure_verifier.py",
                checks=result["checks"],
                review_evidence=result.get("review_evidence"),
            )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        mode_label = f"Per-PR ({args.pr_id})" if args.pr_id else "Closure"
        print(f"{mode_label} verifier: {result['verdict'].upper()}")
        for check in result["checks"]:
            print(f"- [{check['status']}] {check['name']}: {check['detail']}")

    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
