"""Audit and recording helpers for gate execution.

Extracted from gate_runner.py. All functions take explicit directory paths
so they can be used without a GateRunner instance.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from governance_receipts import utc_now_iso

logger = logging.getLogger(__name__)

_GATE_ENV_FLAGS: Dict[str, str] = {
    "gemini_review": "VNX_GEMINI_REVIEW_ENABLED",
    "codex_gate": "VNX_CODEX_HEADLESS_ENABLED",
    "claude_github_optional": "VNX_CLAUDE_GITHUB_REVIEW_ENABLED",
    "ci_gate": "VNX_CI_GATE_REQUIRED",
    "wiring_gate": "VNX_WIRING_GATE_REQUIRED",
}

_GATE_BINARIES: Dict[str, str] = {
    "gemini_review": "gemini",
    "codex_gate": "codex",
    "claude_github_optional": "gh",
    "ci_gate": "gh",
    "wiring_gate": "gh",
}

# Infrastructure/execution failures — NOT semantic gate verdicts.
# gate_failed means "gate completed with blocking findings"; only emit it for reasons
# that represent a completed gate run with actual blocking findings. Anything else
# (timeouts, crashes, infra errors, validation failures) is execution-level → skip.
EXECUTION_FAILURE_REASONS: frozenset = frozenset({
    # Process lifecycle
    "exit_nonzero", "timeout", "stall", "stalled", "killed",
    # Subprocess / binary
    "subprocess_error", "subprocess_failed", "binary_not_found",
    # Isolated-checkout setup (OI-708)
    "worktree_checkout_failed",
    # Artifact and content issues
    "artifact_materialization_error", "artifact_materialization_failed",
    "empty_review_content", "validation_failed",
    # Network / auth
    "network_error", "auth_error",
    # Vertex REST path (gemini_review) API failures (OI-1178)
    "vertex_api_error",
})


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def result_file_path(
    results_dir: Path,
    gate: str,
    pr_number: Optional[int],
    pr_id: str,
) -> Optional[Path]:
    """Return the canonical result file path for a gate execution."""
    if pr_id:
        slug = pr_id.lower().replace("-", "")
        return results_dir / f"{slug}-{gate}-contract.json"
    if pr_number is not None:
        return results_dir / f"pr-{pr_number}-{gate}.json"
    return None


def persist_request(
    requests_dir: Path,
    gate: str,
    payload: Dict[str, Any],
    *,
    pr_number: Optional[int],
    pr_id: str,
) -> None:
    """Write request payload to disk."""
    if pr_id:
        slug = pr_id.lower().replace("-", "")
        path = requests_dir / f"{slug}-{gate}-contract.json"
    elif pr_number is not None:
        path = requests_dir / f"pr-{pr_number}-{gate}.json"
    else:
        return
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_skip_rationale(
    state_dir: Path,
    gate: str,
    pr_id: str,
    reason: str,
    reason_detail: str,
) -> None:
    """Append skip-rationale record to NDJSON audit trail (GATE-9)."""
    binary = _GATE_BINARIES.get(gate, gate)
    env_var = _GATE_ENV_FLAGS.get(gate, "")
    record = {
        "event_type": "gate_skip_rationale",
        "gate": gate,
        "pr_id": pr_id,
        "reason": reason,
        "reason_detail": reason_detail,
        "provider_check": {
            "binary_name": binary,
            "binary_found": shutil.which(binary) is not None,
            "env_flag": env_var,
            "env_value": os.environ.get(env_var, ""),
        },
        "compensating_action": "Manual review or operator override required.",
        "timestamp": utc_now_iso(),
    }
    audit_path = state_dir / "gate_execution_audit.ndjson"
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# GitHub PR identity
# ---------------------------------------------------------------------------


def _gh_pr_view_field(pr_number: Optional[int], field: str) -> str:
    """Fetch one ``gh pr view --json <field>`` value. Returns "" on any failure."""
    if not pr_number or shutil.which("gh") is None:
        return ""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", field],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ""
    value = data.get(field) if isinstance(data, dict) else None
    return str(value or "").strip()


def get_pr_head_sha(pr_number: Optional[int]) -> str:
    """Return the PR head commit sha via ``gh pr view --json headRefOid``.

    The PR head sha lives on GitHub, not in the local checkout. A request
    handler running under launchd resolves ``git rev-parse HEAD`` against the
    process cwd (the main checkout), which is not the PR head, so a merge check
    comparing a result's commit_sha to the PR's headRefOid would reject every
    gated merge. This is the single source of truth for the PR head identity;
    callers must not fall back to the local HEAD (OI-1307 / B6).
    """
    return _gh_pr_view_field(pr_number, "headRefOid")


def get_pr_head_branch(pr_number: Optional[int]) -> str:
    """Return the PR head branch name via ``gh pr view --json headRefName``."""
    return _gh_pr_view_field(pr_number, "headRefName")


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


def stamp_request_identity(
    result_payload: Dict[str, Any],
    request_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Stamp ``branch`` + ``commit_sha`` from the request onto a result record.

    OI-1307 (dispatch-20260816-fix1588-poort-b): the merge door's closure check
    matches a review-gate result on BOTH branch (ADR-005) and head sha. Before
    this, no writer stamped either field, so a result never carried the branch
    or commit it was produced against and a branch/sha-scoped merge check could
    only ever reject it as stale. Every writer that books a result from a
    request payload must call this so the evidence is joinable.

    An empty branch is LOUD, not silent: a result without a branch can never
    match a branch-scoped merge check, so the writer must surface that it
    produced evidence that is unjoinable rather than quietly emitting a
    branch-less record. An empty commit_sha is equally loud (B6/A3): a result
    without a head sha can never match a head-sha-scoped merge check.
    """
    branch = (request_payload.get("branch") or "").strip()
    if not branch:
        logger.warning(
            "gate_recorder.stamp_request_identity: request payload for gate=%r "
            "pr_id=%r carries no branch — the result record will not match any "
            "branch-scoped merge check (OI-1307)",
            request_payload.get("gate"),
            request_payload.get("pr_id"),
        )
    commit_sha = (request_payload.get("commit_sha") or "").strip()
    if not commit_sha:
        logger.warning(
            "gate_recorder.stamp_request_identity: request payload for gate=%r "
            "pr_id=%r carries no commit_sha — the result record will not match any "
            "head-sha-scoped merge check (OI-1307)",
            request_payload.get("gate"),
            request_payload.get("pr_id"),
        )
    result_payload["branch"] = branch
    result_payload["commit_sha"] = commit_sha
    return result_payload


class ResultOverwriteRefused(ValueError):
    """A write would replace a terminal, evidenced gate result with a less
    decided one (OI-1469/OI-1470)."""


class _CorruptResult:
    """Sentinel: the result file exists but could not be parsed as a dict.

    Distinct from ``None`` (absent). Absent, empty, and corrupt are three
    different states (OI-1469/OI-1470 advisory) -- an existing file that
    fails to parse might be a torn write left behind by one of the
    non-atomic writers this guard exists to police (blocking-1 is exactly
    that: a raw ``write_text`` with no tmp+replace), and it might be hiding
    a real terminal, evidenced verdict underneath the truncation. Collapsing
    it into "nothing there" would fail the guard OPEN on the one case it
    most needs to fail closed.
    """


_CORRUPT_RESULT = _CorruptResult()


def _read_existing_result(result_path: Path) -> Union[Dict[str, Any], _CorruptResult, None]:
    """Best-effort read of a prior result record.

    Returns ``None`` when the file is genuinely absent, the module-level
    :data:`_CORRUPT_RESULT` sentinel when it exists but could not be parsed
    (OSError, invalid JSON, or JSON that isn't an object), or the parsed
    dict on success. Callers must check for the sentinel explicitly --
    treating it as ``None`` was the OI-1469/OI-1470 advisory gap.
    """
    if not result_path.exists():
        return None
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _CORRUPT_RESULT
    return data if isinstance(data, dict) else _CORRUPT_RESULT


def _check_overwrite_guard(
    result_path: Path,
    new_payload: Dict[str, Any],
    *,
    gate: str,
    pr_ref: str,
) -> None:
    """Refuse a write that would downgrade an existing terminal result.

    OI-1469/OI-1470: three independent writers (glm_gate's live run, its
    ``--reprocess`` recovery path, and gate_obligation_runner) land in the
    same ``results/pr-<N>-<gate>.json`` slot with no ordering guarantee —
    whoever writes last wins, even when that write is a provider outage. A
    real, decided verdict (``gate_status.is_terminal`` AND
    ``gate_status.has_complete_evidence`` — contract_hash + report_path both
    present) may only be replaced by another decided, evidenced verdict:
    never by an in-flight/outage status (OI-1470's second glm_gate run:
    ``pass`` -> ``unavailable`` on an OpenRouter 402), and never by an
    evidence-less terminal placeholder such as ``not_executable`` (a
    ``--reprocess`` refusal, or a fresh not_executable booking). A terminal
    record with NO complete evidence (e.g. an existing not_executable) is
    not "decided" and stays freely overwritable — it must not permanently
    freeze the slot.
    """
    from gate_status import is_terminal, has_complete_evidence, canonical_status  # noqa: PLC0415

    existing = _read_existing_result(result_path)
    if existing is _CORRUPT_RESULT:
        # Advisory (OI-1469/OI-1470): absent, empty, and corrupt are three
        # distinct states. An unreadable-but-present file must never read as
        # "nothing there" -- that fails the guard OPEN on exactly the shape a
        # non-atomic writer (blocking-1's raw ``write_text`` before this fix,
        # and the two writers OI-1472 later routed through the guard) can
        # produce: a torn write over what may have been a decided, evidenced
        # verdict. Refuse rather than guess. Since OI-1472 no production
        # writer left in this tree can create that shape itself -- they all
        # write tmp+replace via write_result_guarded -- but a crash, a full
        # disk, or a foreign writer still can.
        logger.warning(
            "gate_recorder: REFUSING to overwrite unreadable existing result "
            "gate=%s pr=%s path=%s -- the file exists but could not be "
            "parsed; it may be a truncated terminal, evidenced verdict "
            "(OI-1469/OI-1470 advisory)",
            gate, pr_ref, result_path,
        )
        raise ResultOverwriteRefused(
            f"{gate} result for pr={pr_ref!r} at {result_path} exists but is "
            f"unreadable/corrupt -- refusing to overwrite an unverifiable record"
        )
    if existing is None or not is_terminal(existing):
        return
    existing_status = canonical_status(existing)
    new_status = canonical_status(new_payload)
    if not is_terminal(new_payload):
        logger.warning(
            "gate_recorder: REFUSING to overwrite terminal result gate=%s pr=%s "
            "existing_status=%r with non-terminal status=%r — an outage or "
            "in-flight status must never erase a decided verdict (OI-1469/OI-1470)",
            gate, pr_ref, existing_status, new_status,
        )
        raise ResultOverwriteRefused(
            f"{gate} result for pr={pr_ref!r} is terminal (status={existing_status!r}) "
            f"— refusing to overwrite with non-terminal status={new_status!r}"
        )
    if has_complete_evidence(existing) and not has_complete_evidence(new_payload):
        logger.warning(
            "gate_recorder: REFUSING to overwrite decided result gate=%s pr=%s "
            "existing_status=%r (complete evidence) with evidence-less terminal "
            "status=%r",
            gate, pr_ref, existing_status, new_status,
        )
        raise ResultOverwriteRefused(
            f"{gate} result for pr={pr_ref!r} carries complete evidence "
            f"(status={existing_status!r}) — refusing to overwrite with "
            f"evidence-less status={new_status!r}"
        )


def _write_result_atomic(result_path: Path, payload: Dict[str, Any]) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = result_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(result_path)


def write_result_guarded(
    result_path: Path,
    payload: Dict[str, Any],
    *,
    gate: str,
    pr_ref: str,
) -> Tuple[Dict[str, Any], bool]:
    """Write a gate result unless it would downgrade an existing terminal one.

    Shared low-level write primitive (OI-1469/OI-1470). Every writer of a
    ``review_gates/results/`` record that is not required to raise on
    refusal (see :func:`record_terminal_result` for the raising variant)
    routes through this, so the overwrite guard applies by construction.

    That sentence was a recommendation until OI-1472 and is now a fact
    about this tree — a claim worth stating only because it is checkable.
    The complete set of production writers, all guarded:

    ==========================================  ==========================
    writer                                      variant
    ==========================================  ==========================
    ``record_terminal_result``                  raises on refusal
    ``record_not_executable``                   via this function
    ``record_failure`` (and its thin wrapper
    ``record_failure_simple``)                  via this function
    ``gate_report_generator``
    ``._write_not_executable_result``           via this function
    ``gate_report_generator``
    ``._write_failure_result``                  via this function
    ``gate_artifacts._write_result_record``     via this function
    ==========================================  ==========================

    A new writer that calls ``write_text`` on a results path instead is
    outside that table and outside the guard; ``tests/
    test_oi1472_residual_guard_writers.py`` fails when one appears.
    Returns ``(payload_on_disk, written)`` — the new payload and ``True`` on
    success, or the unchanged existing payload and ``False`` when refused.
    On a refusal caused by an unreadable/corrupt existing file (the
    OI-1469/OI-1470 advisory case), there is no parseable existing payload to
    hand back — the caller gets the attempted ``payload`` instead (never the
    internal :data:`_CORRUPT_RESULT` sentinel), which is NOT what is on disk;
    ``written is False`` is what tells the caller the write did not land.
    """
    try:
        _check_overwrite_guard(result_path, payload, gate=gate, pr_ref=pr_ref)
    except ResultOverwriteRefused:
        existing = _read_existing_result(result_path)
        on_disk = existing if isinstance(existing, dict) else {}
        return on_disk or payload, False
    _write_result_atomic(result_path, payload)
    return payload, True


def record_terminal_result(
    *,
    gate: str,
    pr_id: str,
    result_path: Path,
    payload: Dict[str, Any],
) -> Path:
    """Atomically persist a terminal (pass/fail) gate result at an explicit path.

    Single write path for gates outside the built-in review_gate_manager
    pipeline (codex_gate/gemini_review/ci_gate/claude_github_optional
    already enforce contract_hash + report_path at write time via
    gate_result_parser._validate_and_persist_result). A free-form gate like
    kimi_gate had no such enforcement, so nothing stopped a hand-authored
    JSON from landing in review_gates/results/ indistinguishable from a real
    run (OI-1093: three such records surfaced in mission-control's store).
    Refusing to write a terminal result without producer identity closes
    that gap for every future writer that routes through this function.

    Non-terminal payloads (pending/running/queued) are not gate evidence
    yet, so they are not held to this requirement. The caller
    resolves ``result_path`` itself — gate writers use different filename
    conventions (legacy ``pr-N-gate.json`` vs. ``{slug}-gate-contract.json``)
    and this function does not need to know which.

    Also refuses (:class:`ResultOverwriteRefused`, a ``ValueError`` subclass)
    a write that would downgrade an existing decided, evidenced result to a
    less-decided one — see :func:`_check_overwrite_guard`. glm_gate.py and
    kimi_gate.py already catch ``(OSError, ValueError)`` around this call and
    fail loudly rather than silently overwrite (OI-1469/OI-1470).
    """
    from gate_status import is_terminal, has_producer_identity  # noqa: PLC0415

    if is_terminal(payload) and not has_producer_identity(payload):
        raise ValueError(
            f"{gate} result for pr_id={pr_id!r} has a terminal status "
            f"({payload.get('status')!r}) but no producer identity "
            f"(dispatch_id) — refusing to write unauthenticated gate evidence"
        )
    _check_overwrite_guard(result_path, payload, gate=gate, pr_ref=pr_id)
    _write_result_atomic(result_path, payload)
    return result_path


def record_not_executable(
    *,
    gate: str,
    pr_number: Optional[int],
    pr_id: str,
    reason: str,
    reason_detail: str,
    request_payload: Dict[str, Any],
    requests_dir: Path,
    results_dir: Path,
    state_dir: Path,
) -> Dict[str, Any]:
    """Record not_executable and write skip-rationale (GATE-4/9)."""
    now = utc_now_iso()
    request_payload["status"] = "not_executable"
    request_payload["reason"] = reason
    request_payload["reason_detail"] = reason_detail
    request_payload["resolved_at"] = now
    persist_request(requests_dir, gate, request_payload, pr_number=pr_number, pr_id=pr_id)

    result_payload: Dict[str, Any] = {
        "gate": gate,
        "pr_id": pr_id or (str(pr_number) if pr_number else ""),
        "pr_number": pr_number,
        "status": "not_executable",
        "reason": reason,
        "reason_detail": reason_detail,
        "summary": f"{gate} not executable: {reason_detail}",
        "contract_hash": request_payload.get("contract_hash", ""),
        "report_path": "",
        "blocking_findings": [],
        "advisory_findings": [],
        "required_reruns": [],
        "residual_risk": "Gate evidence not available. Compensating evidence required.",
        "recorded_at": now,
    }
    stamp_request_identity(result_payload, request_payload)

    rf = result_file_path(results_dir, gate, pr_number=pr_number, pr_id=pr_id)
    if rf:
        result_payload, _written = write_result_guarded(
            rf, result_payload, gate=gate, pr_ref=pr_id or str(pr_number or ""),
        )

    write_skip_rationale(
        state_dir, gate,
        pr_id=pr_id or str(pr_number or ""),
        reason=reason,
        reason_detail=reason_detail,
    )
    return result_payload


def record_failure(
    *,
    gate: str,
    pr_number: Optional[int],
    pr_id: str,
    result: Dict[str, Any],
    request_payload: Dict[str, Any],
    requests_dir: Path,
    results_dir: Path,
) -> Dict[str, Any]:
    """Record a failed gate execution (timeout/stall/error).

    Execution-level failures — reasons in :data:`EXECUTION_FAILURE_REASONS`
    — mean the gate never produced a verdict (crash, timeout, infra error).
    Those are absence of evidence and book ``unavailable`` (OI-1178), never
    ``failed``: a non-execution must not read as a rejected PR. A real
    verdict failure (the gate ran and found a blockade) still books
    ``failed``, but that path flows through
    :func:`gate_artifacts.materialize_artifacts`, not here.
    """
    now = utc_now_iso()
    reason = result["reason"]
    reason_detail = result["reason_detail"]
    is_execution_failure = reason in EXECUTION_FAILURE_REASONS
    status = "unavailable" if is_execution_failure else "failed"

    request_payload["status"] = status
    request_payload["failed_at"] = now
    persist_request(requests_dir, gate, request_payload, pr_number=pr_number, pr_id=pr_id)

    failure_payload: Dict[str, Any] = {
        "gate": gate,
        "pr_id": pr_id or (str(pr_number) if pr_number else ""),
        "pr_number": pr_number,
        "status": status,
        "reason": reason,
        "reason_detail": reason_detail,
        "duration_seconds": result["duration_seconds"],
        "partial_output_lines": result["partial_output_lines"],
        "runner_pid": result["runner_pid"],
        "killed_at": now,
        "summary": (
            f"{gate} UNAVAILABLE (gate did not run — {reason}: {reason_detail}) — NOT a review fail"
            if is_execution_failure
            else f"Gate execution {reason}: {reason_detail}"
        ),
        "contract_hash": request_payload.get("contract_hash", ""),
        "report_path": "",
        "blocking_findings": [],
        "advisory_findings": [],
        "required_reruns": [gate],
        "residual_risk": f"Gate {reason}. Re-run required.",
        "recorded_at": now,
    }
    stamp_request_identity(failure_payload, request_payload)

    rf = result_file_path(results_dir, gate, pr_number=pr_number, pr_id=pr_id)
    written = True
    if rf:
        failure_payload, written = write_result_guarded(
            rf, failure_payload, gate=gate, pr_ref=pr_id or str(pr_number or ""),
        )

    # Emit gate_failed for codex_gate only when the gate itself reported a verdict
    # failure (not for infrastructure/execution errors like timeout or stall) AND
    # the write actually landed — a refused write left the prior (real) result
    # standing, so telling the register "gate_failed" would describe a write
    # that never happened (OI-1469/OI-1470).
    if written and gate == "codex_gate" and reason not in EXECUTION_FAILURE_REASONS:
        try:
            from gate_register_emit import emit_codex_gate_to_register
            emit_codex_gate_to_register(
                "gate_failed",
                dispatch_id=request_payload.get("dispatch_id", ""),
                pr_number=pr_number,
                pr_id=pr_id,
                gate=gate,
            )
        except (ImportError, OSError) as e:
            logger.debug("Failed to emit gate register event: %s", e)

    return failure_payload


def record_failure_simple(
    *,
    gate: str,
    pr_number: Optional[int],
    pr_id: str,
    reason: str,
    reason_detail: str,
    request_payload: Dict[str, Any],
    requests_dir: Path,
    results_dir: Path,
) -> Dict[str, Any]:
    """Record a simple failure (artifact materialization errors)."""
    return record_failure(
        gate=gate, pr_number=pr_number, pr_id=pr_id,
        result={
            "reason": reason,
            "reason_detail": reason_detail,
            "duration_seconds": 0.0,
            "partial_output_lines": 0,
            "runner_pid": os.getpid(),
        },
        request_payload=request_payload,
        requests_dir=requests_dir,
        results_dir=results_dir,
    )
