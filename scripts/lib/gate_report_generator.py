"""Gate report generation and audit trail (GateReportGeneratorMixin).

Extracted from review_gate_manager.py as part of F27 batch refactor.
Methods handle writing result records and NDJSON audit entries.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


class GateReportGeneratorMixin:
    """Mixin providing report writing and audit trail methods for ReviewGateManager."""

    def _write_not_executable_result(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        reason: str,
        reason_detail: str,
        contract_hash: str = "",
        dispatch_id: str = "",
    ) -> Tuple[Dict[str, Any], bool]:
        """Write a not_executable result record (GATE-4).

        Routes through ``gate_recorder.write_result_guarded`` (OI-1469/
        OI-1470/OI-1471) instead of a bare ``write_text``. This is a
        REQUEST-time writer -- ``_mark_gate_unavailable`` calls it when a
        gate is (re-)requested and its binary is unavailable, which fires
        BEFORE any run, unlike the RESULT-time recorders in
        ``gate_recorder.py`` that already carried the guard. Without it, a
        request-time refusal on a PR that already carries a real, decided
        verdict (e.g. a launchd worker missing the gate's CLI on PATH) would
        silently erase that verdict -- measured live against PR #1691's
        codex-gate pass (contract_hash ``466cd2ca75d7a7fb``).

        Returns ``(payload_on_disk, written)``. ``payload_on_disk`` is the
        not_executable payload just built when the write landed, or the
        untouched pre-existing terminal record when the guard refused it.
        ``written`` is ``False`` on refusal -- the caller must not assume
        the constructed payload reached disk without checking it.
        """
        from gate_recorder import write_result_guarded
        from review_gate_manager import _utc_now

        now = _utc_now()
        payload: Dict[str, Any] = {
            "gate": gate,
            "pr_id": pr_id or (str(pr_number) if pr_number else ""),
            "pr_number": pr_number,
            "status": "not_executable",
            "reason": reason,
            "reason_detail": reason_detail,
            # OI-1415: same text as reason_detail above, in the canonical
            # field a generic failure-reason reader looks for (#1666).
            "failure_reason": reason_detail,
            "summary": f"{gate} not executable: {reason_detail}",
            "contract_hash": contract_hash,
            "report_path": "",
            "blocking_findings": [],
            "advisory_findings": [],
            "required_reruns": [],
            "residual_risk": "Gate evidence not available. Compensating evidence required.",
            "recorded_at": now,
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if pr_id:
            result_file = self._contract_result_path(gate, pr_id)
        elif pr_number is not None:
            result_file = self._result_path(gate, pr_number)
        else:
            return payload, True
        return write_result_guarded(
            result_file, payload, gate=gate, pr_ref=pr_id or str(pr_number or ""),
        )

    def _write_skip_rationale(
        self,
        *,
        gate: str,
        pr_id: str,
        reason: str,
        reason_detail: str,
    ) -> None:
        """Append a skip-rationale record to the NDJSON audit trail (GATE-9).

        Delegates to :func:`gate_recorder.write_skip_rationale` — the ONE
        writer of this record shape (OI-1490). This method used to build its
        own ``record`` dict, with its own copy of the gate->env-flag map and
        its own raw ``shutil.which(binary_name)`` on a caller-supplied name,
        appending to the SAME ``gate_execution_audit.ndjson`` as
        gate_recorder. Two writers of one event_type in one file is how the
        shapes drift: this copy's env map had already lost ``wiring_gate``,
        and once gate_recorder learned ``provider_kind`` the same file would
        have carried two shapes of ``provider_check`` — with the records
        still doing the invented-binary lookup being exactly the ones a
        reader filtering on ``provider_kind`` would not see.

        ``binary_name`` is gone from the signature rather than accepted and
        ignored: the name now comes from the single registry
        (``gate_recorder.GATE_PROVIDERS``), so a caller can no longer pass a
        name nobody ever shipped.
        """
        from gate_recorder import write_skip_rationale  # noqa: PLC0415

        write_skip_rationale(
            self.state_dir, gate, pr_id=pr_id, reason=reason, reason_detail=reason_detail,
        )

    def _write_failure_result(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        reason: str,
        reason_detail: str,
        duration_seconds: float,
        partial_output_lines: int,
        runner_pid: int,
        contract_hash: str = "",
    ) -> Tuple[Dict[str, Any], bool]:
        """Write a failed result record for timeout/stall (GATE-6/7).

        Routes through ``gate_recorder.write_result_guarded`` (OI-1472),
        exactly like its sibling ``_write_not_executable_result`` directly
        above. It was left on a bare ``write_text`` when that one was
        converted, and its payload is precisely the shape the OI-1470 guard
        exists for: TERMINAL (``status="failed"``) with an EMPTY
        ``report_path`` — a decided outcome carrying no evidence. Landing
        that over a real, evidenced pass is OI-1470's ``pass`` ->
        ``unavailable`` loss with a different status word. The method has no
        call site today; wiring one up must not be the moment the missing
        guard is discovered.

        Returns ``(payload_on_disk, written)`` — the same contract as
        ``_write_not_executable_result``, so the two siblings report a
        refusal identically. ``written`` is ``False`` when the guard refused,
        and ``payload_on_disk`` is then the untouched pre-existing record,
        NOT the failure payload built here.
        """
        from gate_recorder import write_result_guarded
        from review_gate_manager import _utc_now

        now = _utc_now()
        payload: Dict[str, Any] = {
            "gate": gate,
            "pr_id": pr_id or (str(pr_number) if pr_number else ""),
            "pr_number": pr_number,
            "status": "failed",
            "reason": reason,
            "reason_detail": reason_detail,
            "duration_seconds": duration_seconds,
            "partial_output_lines": partial_output_lines,
            "runner_pid": runner_pid,
            "killed_at": now,
            "summary": f"Gate execution {reason}: {reason_detail}",
            "contract_hash": contract_hash,
            "report_path": "",
            "blocking_findings": [],
            "advisory_findings": [],
            "required_reruns": [gate],
            "residual_risk": f"Gate {reason}. Re-run required.",
            "recorded_at": now,
        }
        if pr_id:
            result_file = self._contract_result_path(gate, pr_id)
        elif pr_number is not None:
            result_file = self._result_path(gate, pr_number)
        else:
            return payload, True
        return write_result_guarded(
            result_file, payload, gate=gate, pr_ref=pr_id or str(pr_number or ""),
        )
