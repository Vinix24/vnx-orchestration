"""Audit and recording helpers for gate execution.

Extracted from gate_runner.py. All functions take explicit directory paths
so they can be used without a GateRunner instance.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

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
    branch-less record.
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
    result_payload["branch"] = branch
    result_payload["commit_sha"] = (request_payload.get("commit_sha") or "").strip()
    return result_payload


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
    """
    from gate_status import is_terminal, has_producer_identity  # noqa: PLC0415

    if is_terminal(payload) and not has_producer_identity(payload):
        raise ValueError(
            f"{gate} result for pr_id={pr_id!r} has a terminal status "
            f"({payload.get('status')!r}) but no producer identity "
            f"(dispatch_id) — refusing to write unauthenticated gate evidence"
        )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = result_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(result_path)
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
        rf.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

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
    if rf:
        rf.write_text(json.dumps(failure_payload, indent=2), encoding="utf-8")

    # Emit gate_failed for codex_gate only when the gate itself reported a verdict
    # failure (not for infrastructure/execution errors like timeout or stall).
    if gate == "codex_gate" and reason not in EXECUTION_FAILURE_REASONS:
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
