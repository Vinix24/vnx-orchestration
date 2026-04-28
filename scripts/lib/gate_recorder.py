"""Audit and recording helpers for gate execution.

Extracted from gate_runner.py. All functions take explicit directory paths
so they can be used without a GateRunner instance.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from governance_receipts import utc_now_iso

_GATE_ENV_FLAGS: Dict[str, str] = {
    "gemini_review": "VNX_GEMINI_REVIEW_ENABLED",
    "codex_gate": "VNX_CODEX_HEADLESS_ENABLED",
    "claude_github_optional": "VNX_CLAUDE_GITHUB_REVIEW_ENABLED",
}

_GATE_BINARIES: Dict[str, str] = {
    "gemini_review": "gemini",
    "codex_gate": "codex",
    "claude_github_optional": "gh",
}

# Infrastructure/execution failures — NOT semantic gate verdicts.
# gate_failed means "gate completed with blocking findings"; never emit it for these.
EXECUTION_FAILURE_REASONS: frozenset = frozenset({
    "exit_nonzero", "timeout", "stall", "stalled",
    "artifact_materialization_error", "artifact_materialization_failed",
    "subprocess_failed", "killed", "binary_not_found",
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
    """Record a failed gate execution (timeout/stall/error)."""
    now = utc_now_iso()
    request_payload["status"] = "failed"
    request_payload["failed_at"] = now
    persist_request(requests_dir, gate, request_payload, pr_number=pr_number, pr_id=pr_id)

    failure_payload: Dict[str, Any] = {
        "gate": gate,
        "pr_id": pr_id or (str(pr_number) if pr_number else ""),
        "pr_number": pr_number,
        "status": "failed",
        "reason": result["reason"],
        "reason_detail": result["reason_detail"],
        "duration_seconds": result["duration_seconds"],
        "partial_output_lines": result["partial_output_lines"],
        "runner_pid": result["runner_pid"],
        "killed_at": now,
        "summary": f"Gate execution {result['reason']}: {result['reason_detail']}",
        "contract_hash": request_payload.get("contract_hash", ""),
        "report_path": "",
        "blocking_findings": [],
        "advisory_findings": [],
        "required_reruns": [gate],
        "residual_risk": f"Gate {result['reason']}. Re-run required.",
        "recorded_at": now,
    }

    rf = result_file_path(results_dir, gate, pr_number=pr_number, pr_id=pr_id)
    if rf:
        rf.write_text(json.dumps(failure_payload, indent=2), encoding="utf-8")

    # Emit gate_failed for codex_gate only when the gate itself reported a verdict
    # failure (not for infrastructure/execution errors like timeout or stall).
    if gate == "codex_gate" and result["reason"] not in EXECUTION_FAILURE_REASONS:
        try:
            from gate_register_emit import emit_codex_gate_to_register
            emit_codex_gate_to_register(
                "gate_failed",
                dispatch_id=request_payload.get("dispatch_id", ""),
                pr_number=pr_number,
                pr_id=pr_id,
                gate=gate,
            )
        except Exception:
            pass

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
