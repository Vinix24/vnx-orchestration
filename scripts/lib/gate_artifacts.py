"""Artifact materialization for gate execution.

Extracted from gate_runner.py. Depends on gate_recorder for failure recording.
Codex output parsing is delegated to codex_parser.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from governance_receipts import utc_now_iso
import gate_recorder
from codex_parser import parse_codex_findings


# ---------------------------------------------------------------------------
# Contract hash
# ---------------------------------------------------------------------------


def _compute_contract_hash(request_payload: Dict[str, Any], gate: str) -> str:
    """Derive a 16-hex contract hash from prompt or gate metadata."""
    if request_payload.get("contract_hash"):
        return request_payload["contract_hash"]
    if "prompt" in request_payload:
        return hashlib.sha256(
            request_payload["prompt"].encode("utf-8")
        ).hexdigest()[:16]
    fallback = json.dumps({
        "gate": gate,
        "branch": request_payload.get("branch", ""),
        "changed_files": sorted(request_payload.get("changed_files", [])),
    }, sort_keys=True)
    return hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_report(gate: str, stdout: str, request_payload: Dict[str, Any]) -> str:
    """Format gate output as a normalized headless report."""
    pr_ref = request_payload.get("pr_id") or str(request_payload.get("pr_number", ""))
    branch = request_payload.get("branch", "")
    lines = [
        f"# {gate} — Headless Gate Report",
        "",
        f"**PR**: {pr_ref}",
        f"**Branch**: {branch}",
        f"**Gate**: {gate}",
        f"**Generated**: {utc_now_iso()}",
        "",
        "---",
        "",
        "## Gate Output",
        "",
        stdout.strip() if stdout.strip() else "(no output)",
        "",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Artifact materialization (GATE-11/12)
# ---------------------------------------------------------------------------


def _validate_report_file(report_file: Path) -> Optional[str]:
    """Return error detail string if report is missing/empty, else None."""
    if not report_file.exists() or report_file.stat().st_size == 0:
        return "Report file is empty or missing after write"
    return None


def _validate_content(stdout: str) -> Optional[Tuple[str, str]]:
    """Return (reason, reason_detail) if content is too sparse, else None."""
    stripped = stdout.strip()
    content_lines = [ln for ln in stripped.splitlines() if ln.strip()] if stripped else []
    if len(content_lines) < 3 and stripped != "(no output)":
        return (
            "empty_review_content",
            f"Gate output has only {len(content_lines)} substantive line(s); expected review content",
        )
    return None


def _classify_findings(
    findings: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split findings into (blocking, advisory) by severity."""
    blocking: List[Dict[str, Any]] = []
    advisory: List[Dict[str, Any]] = []
    for finding in findings:
        severity = str(finding.get("severity", "")).lower()
        if severity in {"error", "blocking", "critical", "high"}:
            blocking.append(finding)
        else:
            advisory.append(finding)
    return blocking, advisory


def _write_result_record(
    results_dir: Path,
    gate: str,
    pr_number: Optional[int],
    pr_id: str,
    result_payload: Dict[str, Any],
    report_file: Path,
) -> Optional[str]:
    """Write result JSON; return error detail string on failure, else None."""
    try:
        if pr_id:
            result_file = results_dir / f"{pr_id.lower().replace('-', '')}-{gate}-contract.json"
        elif pr_number is not None:
            result_file = results_dir / f"pr-{pr_number}-{gate}.json"
        else:
            raise ValueError("pr_number or pr_id required")

        result_json = json.dumps(result_payload, indent=2)
        json.loads(result_json)  # verify valid JSON (GATE-12 step 5)
        result_file.write_text(result_json, encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        try:
            report_file.unlink(missing_ok=True)
        except OSError:
            pass
        return f"Failed to write result record: {exc}"

    if not result_file.exists():
        try:
            report_file.unlink(missing_ok=True)
        except OSError:
            pass
        return "Result file missing after write"
    return None


def materialize_artifacts(
    *,
    gate: str,
    pr_number: Optional[int],
    pr_id: str,
    stdout: str,
    request_payload: Dict[str, Any],
    duration_seconds: float,
    requests_dir: Path,
    results_dir: Path,
    reports_dir: Path,
) -> Dict[str, Any]:
    """Atomic artifact materialization (GATE-11/12).

    Sequence: write report → verify → compute hash → write result → verify.
    On any failure, transitions to failed via gate_recorder.
    """
    _fail = dict(
        gate=gate, pr_number=pr_number, pr_id=pr_id,
        request_payload=request_payload,
        requests_dir=requests_dir, results_dir=results_dir,
    )

    report_path = request_payload.get("report_path", "")
    if not report_path:
        return gate_recorder.record_failure_simple(
            **_fail, reason="artifact_materialization_failed",
            reason_detail="No report_path in request payload",
        )

    report_file = Path(report_path)
    try:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(format_report(gate, stdout, request_payload), encoding="utf-8")
    except OSError as exc:
        return gate_recorder.record_failure_simple(
            **_fail, reason="artifact_materialization_failed",
            reason_detail=f"Failed to write report: {exc}",
        )

    if (err := _validate_report_file(report_file)):
        return gate_recorder.record_failure_simple(
            **_fail, reason="artifact_materialization_failed", reason_detail=err,
        )

    if (content_err := _validate_content(stdout)):
        return gate_recorder.record_failure_simple(
            **_fail, reason=content_err[0], reason_detail=content_err[1],
        )

    contract_hash = _compute_contract_hash(request_payload, gate)
    now = utc_now_iso()
    findings: List[Dict[str, Any]] = []
    residual_risk = ""
    if gate == "codex_gate":
        parsed = parse_codex_findings(stdout)
        findings = parsed["findings"]
        residual_risk = parsed.get("residual_risk", "") or ""
    blocking, advisory = _classify_findings(findings)

    real_dispatch_id = request_payload.get("dispatch_id", "")
    result_payload: Dict[str, Any] = {
        "gate": gate,
        "pr_id": pr_id or (str(pr_number) if pr_number else ""),
        "pr_number": pr_number,
        "status": "completed",
        "summary": f"{gate} execution completed successfully",
        "contract_hash": contract_hash,
        "report_path": str(report_file),
        "findings": findings,
        "blocking_findings": blocking,
        "advisory_findings": advisory,
        "required_reruns": [],
        "residual_risk": residual_risk,
        "duration_seconds": duration_seconds,
        "recorded_at": now,
    }
    if real_dispatch_id:
        result_payload["dispatch_id"] = real_dispatch_id

    if (write_err := _write_result_record(results_dir, gate, pr_number, pr_id, result_payload, report_file)):
        return gate_recorder.record_failure_simple(
            **_fail, reason="artifact_materialization_failed", reason_detail=write_err,
        )

    request_payload["status"] = "completed"
    request_payload["completed_at"] = now
    gate_recorder.persist_request(requests_dir, gate, request_payload, pr_number=pr_number, pr_id=pr_id)

    try:
        import logging as _logging
        from dispatch_register import append_event as _append_reg
        from state_rebuild_trigger import maybe_trigger_state_rebuild as _trigger_rebuild

        _pr_num = pr_number
        _feature_id = ""
        if _pr_num is None and pr_id:
            try:
                _pr_num = int(pr_id)
            except (ValueError, TypeError):
                # Non-numeric pr_id (contract path "PR-6" style) — use as feature_id fallback
                _feature_id = str(pr_id)

        _gate_event = "gate_passed" if not blocking else "gate_failed"
        _reg_result = _append_reg(
            _gate_event,
            dispatch_id=real_dispatch_id or "",
            pr_number=_pr_num,
            feature_id=_feature_id,
            gate=gate,
        )
        if not _reg_result:
            _logging.warning(
                "dispatch_register: gate event %s dropped — no identifying field "
                "(dispatch_id=%r, pr_number=%r, pr_id=%r)",
                _gate_event, real_dispatch_id, _pr_num, pr_id,
            )
        _trigger_rebuild()
    except Exception:
        pass

    # Write JSON sidecar to report_pipeline/ for intelligence DB ingestion (OI-1066)
    try:
        sidecar = {
            "dispatch_id": real_dispatch_id or f"gate-{gate}-pr-{pr_number if pr_number is not None else pr_id}",
            "gate": gate,
            "pr_id": pr_id,
            "pr_number": pr_number,
            "status": result_payload["status"],
            "findings": findings,
            "blocking_findings": blocking,
            "advisory_findings": advisory,
            "contract_hash": contract_hash,
            "report_path": str(report_file),
            "duration_seconds": duration_seconds,
            "source": "gate_runner",
            "recorded_at": now,
        }
        sidecar_dir = reports_dir.parent / "state" / "report_pipeline"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path = sidecar_dir / f"gate-{gate}-pr-{pr_number or pr_id}.json"
        sidecar_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    except Exception as _exc:
        logger.warning("materialize_artifacts: sidecar write failed: %s", _exc)

    return result_payload


# ---------------------------------------------------------------------------
# Consistency verification (GATE-12/13)
# ---------------------------------------------------------------------------


def verify_artifact_consistency(
    result_path: Path,
    contract_content: str = "",
) -> bool:
    """Verify artifact consistency (GATE-12/13). Returns True if all checks pass."""
    if not result_path.exists():
        return False
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    report_path = result.get("report_path", "")
    if report_path:
        rp = Path(report_path)
        if not rp.exists() or rp.stat().st_size == 0:
            return False

    if contract_content and result.get("contract_hash"):
        expected = hashlib.sha256(contract_content.encode("utf-8")).hexdigest()[:16]
        if result["contract_hash"] != expected:
            return False

    for field in ("gate", "status", "recorded_at"):
        if field not in result:
            return False
    return True

