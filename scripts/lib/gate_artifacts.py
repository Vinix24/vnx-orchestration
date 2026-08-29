"""Artifact materialization for gate execution.

Extracted from gate_runner.py. Depends on gate_recorder for failure recording.
Codex output parsing is delegated to codex_parser.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from governance_receipts import utc_now_iso
import gate_depth
import gate_recorder
from codex_parser import parse_codex_findings

logger = logging.getLogger(__name__)


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


def _format_findings_section(
    blocking: List[Dict[str, Any]], advisory: List[Dict[str, Any]]
) -> str:
    """Render the normalized findings block closure_verifier's indicator scan reads.

    Kept separate from the raw ``## Gate Output`` tool transcript that follows
    it: that transcript is the reviewer's read material (tool calls, contents
    of files it inspected while working) and can contain the literal
    substrings the scan looks for — a ``blocking:`` parameter name, a
    ``severity: blocking`` string literal — in code the reviewer merely read,
    never in its verdict (OI-1394). Only this section is ever counted.
    """
    lines = ["## Findings", "", f"Blocking count: {len(blocking)}", f"Advisory count: {len(advisory)}"]
    if blocking:
        lines.append("")
        lines.append("### Blocking")
        lines.append("")
        for finding in blocking:
            message = finding.get("message", "") if isinstance(finding, dict) else str(finding)
            severity = finding.get("severity", "") if isinstance(finding, dict) else ""
            # Omit the parenthetical when severity is literally "blocking" —
            # it would otherwise also match the `severity:\s*blocking`
            # pattern, double-counting a single finding as two indicators.
            suffix = f" (original severity: {severity})" if severity and severity != "blocking" else ""
            lines.append(f"- [BLOCKING] {message}{suffix}")
    if advisory:
        lines.append("")
        lines.append("### Advisory")
        lines.append("")
        for finding in advisory:
            message = finding.get("message", "") if isinstance(finding, dict) else str(finding)
            severity = finding.get("severity", "warning") if isinstance(finding, dict) else "warning"
            lines.append(f"- {message} (severity: {severity})")
    return "\n".join(lines)


def format_report(
    gate: str,
    stdout: str,
    request_payload: Dict[str, Any],
    blocking: Optional[List[Dict[str, Any]]] = None,
    advisory: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Format gate output as a normalized headless report.

    When ``blocking``/``advisory`` are not None (i.e. this gate parses
    structured findings — currently codex_gate only), a normalized
    ``## Findings`` section is written ahead of the raw tool-output dump. See
    ``_format_findings_section`` for why the scan must never see the raw
    transcript. Gates that do not parse structured findings get no such
    section, so closure_verifier falls back to its prior full-content scan —
    unchanged behavior for those gates.
    """
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
    ]
    if blocking is not None or advisory is not None:
        lines.append(_format_findings_section(blocking or [], advisory or []))
        lines.append("")
    lines.extend([
        "## Gate Output",
        "",
        stdout.strip() if stdout.strip() else "(no output)",
        "",
    ])
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


def _cleanup_orphan_report(report_file: Path) -> None:
    """Best-effort removal of a report whose result record never landed."""
    try:
        report_file.unlink(missing_ok=True)
    except OSError:
        pass


def _write_result_record(
    results_dir: Path,
    gate: str,
    pr_number: Optional[int],
    pr_id: str,
    result_payload: Dict[str, Any],
    report_file: Path,
) -> Optional[str]:
    """Write result JSON through the overwrite guard; error detail or None.

    OI-1472: this was the last PRODUCTION writer of a
    ``review_gates/results/`` record still calling ``write_text`` directly,
    so the OI-1469/OI-1470 overwrite guard did not cover it. That matters
    even though the payload this writer builds — a decided verdict WITH
    ``contract_hash`` and ``report_path`` — is one the guard normally admits:

    1. A bare ``write_text`` is not atomic. A crash or a full disk mid-write
       leaves a torn file, which is exactly the unreadable-but-present shape
       ``gate_recorder._check_overwrite_guard`` refuses on. This writer could
       manufacture the corruption the guard exists to detect.
       :func:`gate_recorder.write_result_guarded` writes tmp + ``os.replace``.
    2. It also bypassed the read side, so it would overwrite a torn record
       left by something else instead of refusing to guess what it had been.

    A guard refusal is NOT an I/O failure and is reported as its own detail
    string. It deliberately does NOT orphan-clean the report: the
    pre-existing record stays on disk still pointing at its own report, and
    the fresh report is kept as evidence of what the refused run said.
    Deleting it would repeat the evidence loss this guard was built to stop.
    """
    result_file = gate_recorder.result_file_path(results_dir, gate, pr_number, pr_id)
    try:
        if result_file is None:
            raise ValueError("pr_number or pr_id required")
        json.loads(json.dumps(result_payload, indent=2))  # verify valid JSON (GATE-12 step 5)
        _payload_on_disk, written = gate_recorder.write_result_guarded(
            result_file, result_payload, gate=gate, pr_ref=pr_id or str(pr_number or ""),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _cleanup_orphan_report(report_file)
        return f"Failed to write result record: {exc}"

    if not written:
        logger.warning(
            "gate_artifacts: result write REFUSED by the overwrite guard "
            "gate=%s pr=%s path=%s -- keeping the fresh report at %s as "
            "evidence of the refused run (OI-1472)",
            gate, pr_id or pr_number, result_file, report_file,
        )
        return (
            "Result write refused by the overwrite guard: an existing decided, "
            "evidenced verdict for this gate would have been downgraded"
        )

    if not result_file.exists():
        _cleanup_orphan_report(report_file)
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

    # Parsed ahead of the report write (not after, as previously) so the
    # normalized findings section can be embedded in the report itself,
    # separate from the raw tool-output dump (OI-1394). Only codex_gate
    # parses structured findings today; other gates pass blocking=None/
    # advisory=None so format_report omits the section and closure_verifier
    # falls back to scanning the full report, unchanged from prior behavior.
    findings: List[Dict[str, Any]] = []
    residual_risk = ""
    findings_parsed = False
    if gate == "codex_gate":
        parsed = parse_codex_findings(stdout)
        findings = parsed["findings"]
        residual_risk = parsed.get("residual_risk", "") or ""
        findings_parsed = True
    blocking, advisory = _classify_findings(findings)

    report_file = Path(report_path)
    try:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(
            format_report(
                gate, stdout, request_payload,
                blocking=blocking if findings_parsed else None,
                advisory=advisory if findings_parsed else None,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return gate_recorder.record_failure_simple(
            **_fail, reason="artifact_materialization_failed",
            reason_detail=f"Failed to write report: {exc}",
        )

    # GATE-11: validate atomically — cleanup orphan report on any validation failure
    file_err = _validate_report_file(report_file)
    content_err = None if file_err else _validate_content(stdout)
    if file_err or content_err:
        try:
            report_file.unlink(missing_ok=True)
        except OSError:
            pass
        if file_err:
            return gate_recorder.record_failure_simple(
                **_fail, reason="artifact_materialization_failed", reason_detail=file_err,
            )
        return gate_recorder.record_failure_simple(
            **_fail, reason=content_err[0], reason_detail=content_err[1],
        )

    # OI-1485: what the run DID, beside what it concluded. Measured from the
    # same stream the report already embeds, so this adds no new capture path
    # — the numbers were on disk all along and simply never reached the record
    # anyone merges on.
    depth = gate_depth.measure_execution_depth(stdout)

    # A run that reached a verdict without a single investigative action is an
    # absence of evidence, not a clean review. It books `unavailable` and lands
    # in required_reruns so it is bought again, rather than passing every
    # invariant and being read as a PASS. The report stays on disk: it is the
    # evidence of what the refused run said, and deleting it would repeat the
    # evidence loss the overwrite guard exists to prevent.
    if gate_depth.is_degenerate(depth):
        logger.warning(
            "gate_artifacts: REFUSING a degenerate %s run pr=%s — %s",
            gate, pr_id or pr_number, gate_depth.degenerate_detail(depth),
        )
        return gate_recorder.record_failure(
            gate=gate, pr_number=pr_number, pr_id=pr_id,
            result={
                "reason": "gate_execution_degenerate",
                "reason_detail": gate_depth.degenerate_detail(depth),
                "duration_seconds": duration_seconds,
                "partial_output_lines": len(stdout.splitlines()),
                "runner_pid": os.getpid(),
            },
            request_payload=request_payload,
            requests_dir=requests_dir,
            results_dir=results_dir,
            extra={
                "execution_depth": depth.to_dict(),
                "degenerate_report_path": str(report_file),
            },
        )

    contract_hash = _compute_contract_hash(request_payload, gate)
    now = utc_now_iso()

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
        "execution_depth": depth.to_dict(),
        "recorded_at": now,
    }
    gate_recorder.stamp_request_identity(result_payload, request_payload)
    if real_dispatch_id:
        result_payload["dispatch_id"] = real_dispatch_id

    if (write_err := _write_result_record(results_dir, gate, pr_number, pr_id, result_payload, report_file)):
        return gate_recorder.record_failure_simple(
            **_fail, reason="artifact_materialization_failed", reason_detail=write_err,
        )

    request_payload["status"] = "completed"
    request_payload["completed_at"] = now
    gate_recorder.persist_request(requests_dir, gate, request_payload, pr_number=pr_number, pr_id=pr_id)

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

    # Emit to dispatch register (codex_gate only — best-effort).
    # Prefer explicit JSON verdict from codex output; fall back to severity-derivation.
    if gate == "codex_gate":
        try:
            from gate_register_emit import emit_codex_gate_to_register
            verdict_obj = parsed.get("verdict", {})
            verdict_str = verdict_obj.get("verdict", "").lower() if isinstance(verdict_obj, dict) else ""
            if verdict_str in ("pass", "passed"):
                register_event = "gate_passed"
            elif verdict_str in ("fail", "failed", "blocked"):
                register_event = "gate_failed"
            else:
                register_event = "gate_passed" if not blocking else "gate_failed"
            emit_codex_gate_to_register(
                register_event,
                dispatch_id=real_dispatch_id,
                pr_number=pr_number,
                pr_id=pr_id,
                gate=gate,
            )
        except (ImportError, OSError) as e:
            logger.debug("Failed to emit gate register event: %s", e)
    elif gate in ("gemini_review", "claude_github_optional"):
        logger.info("materialize_artifacts: register classification deferred for gate=%s", gate)

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

