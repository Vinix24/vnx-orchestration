"""Gate result recording and finding classification (GateResultParserMixin).

Extracted from review_gate_manager.py as part of F27 batch refactor.
Methods handle finding classification (advisory/blocking) and result persistence.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from gemini_prompt_renderer import GeminiReviewReceipt
from claude_github_receipt import ClaudeGitHubReviewReceipt
from gate_outcome import GateOutcome, GateRequest, commit_gate_outcome
from gate_recorder import (
    ResultOverwriteRefused,
    get_pr_head_sha,
    stamp_request_identity,
    write_result_guarded,
)
from gate_status import INCOMPLETE_STATES, canonical_status

# The fields record_result's receipt carries besides the generic ones
# (gate_recorder._emit_terminal_result_receipt), all read off its record.
_RECORD_RESULT_RECEIPT_FIELDS = (
    "branch", "summary", "advisory_findings", "blocking_findings", "advisory_count",
    "residual_risk", "required_reruns",
)


def evidence_error(payload: Dict[str, Any]) -> str:
    """Why ``payload`` lacks the evidence a pass/fail gate result must carry, or ``""``.

    The result writer's own bar (``_validate_and_persist_result``): a
    ``contract_hash``, a ``report_path``, and a report file on disk at that
    path. A standing verdict a switched-off seat reports clears the same bar
    (``gate_request_handler._standing_verdict_gap``), so the two can never
    disagree about what a decided result needs.
    """
    if not payload.get("contract_hash"):
        return "contract_hash is required for pass/fail gate results"
    if not payload.get("report_path"):
        return "report_path is required for pass/fail gate results"
    if not Path(payload["report_path"]).exists():
        return f"report_path file does not exist: {payload['report_path']}"
    return ""


class GateResultParserMixin:
    """Mixin providing result recording and finding classification for ReviewGateManager."""

    def record_claude_github_result(
        self,
        *,
        pr_id: str,
        branch: str,
        status: str,
        summary: str,
        findings: Optional[List[Dict[str, Any]]] = None,
        contract_hash: str = "",
        completed_at: str = "",
        pr_number: Optional[int] = None,
        report_path: str = "",
        required_reruns: Optional[List[str]] = None,
    ) -> ClaudeGitHubReviewReceipt:
        """Record a Claude GitHub review result linked to a ReviewContract.

        Classifies findings into advisory/blocking and persists the result
        with the contract_hash so T0 can correlate with the original contract.

        Per the headless review evidence contract, ``report_path`` and
        ``required_reruns`` are persisted so the closure verifier can validate
        the full evidence chain.

        GE-4c-1: the record, its one ``review_gate_result`` receipt and the
        contract request's end are one ``commit_gate_outcome``. The receipt
        carries what ``_emit_claude_github_receipt`` booked before, the head
        read off the contract request (it is not on this record) included. A
        refusal raises the commit's :class:`ResultOverwriteRefused`, with its
        cause, and changes nothing.
        """
        from review_gate_manager import _utc_now

        raw_findings = findings or []
        request_payload = self._load_contract_request_payload("claude_github_optional", pr_id)
        effective_contract_hash = contract_hash or str(request_payload.get("contract_hash", ""))
        effective_report_path = report_path or str(request_payload.get("report_path", ""))
        result_payload: Dict[str, Any] = {
            "gate": "claude_github_optional",
            "pr_id": pr_id,
            "pr_number": pr_number,
            "branch": branch,
            "status": status,
            "summary": summary,
            "findings": raw_findings,
            "contract_hash": effective_contract_hash,
            "requested_at": "",
            "completed_at": completed_at or _utc_now(),
        }
        receipt = ClaudeGitHubReviewReceipt.from_result_payload(result_payload)
        canonical_report_path = self._canonical_report_path(effective_report_path)
        if status in {"pass", "fail"}:
            if not effective_contract_hash:
                raise ValueError("contract_hash is required for pass/fail gate results")
            if not canonical_report_path:
                raise ValueError("report_path is required for pass/fail gate results")

        full_payload = receipt.to_dict()
        full_payload["findings"] = raw_findings
        full_payload["report_path"] = canonical_report_path
        full_payload["required_reruns"] = list(required_reruns or [])
        full_payload["residual_risk"] = full_payload.get("residual_risk", "")

        result_file = self._contract_result_path("claude_github_optional", pr_id)
        # The head named on the contract request, under both names: the
        # idempotency key reads ``commit_sha``, the round ledger its alias. A
        # request that names no head stamps neither.
        commit_sha = str(request_payload.get("commit_sha") or "").strip()
        outcome = GateOutcome.from_record("claude_github_optional", full_payload, pr_id=pr_id, receipt_fields={
            "branch": branch, "summary": summary,
            "advisory_findings": full_payload["advisory_findings"],
            "blocking_findings": full_payload["blocking_findings"],
            "advisory_count": receipt.advisory_count,
            "contributed_evidence": receipt.contributed_evidence(),
            **({"commit_sha": commit_sha, "head_sha": commit_sha} if commit_sha else {}),
        })
        commit_gate_outcome(
            outcome, results_dir=self.results_dir, result_path=result_file, raise_on_refusal=True,
            request=self._answered_request(request_payload, full_payload["completed_at"]),
        )
        return receipt

    def _answered_request(self, request_payload: Dict[str, Any], completed_at: str) -> Optional[GateRequest]:
        """The request on disk this result answers, or ``None`` when there is none to end.

        An absent request stays absent: ending one would write a request no
        one made.
        """
        if not request_payload:
            return None
        return GateRequest(self.requests_dir, request_payload, {"completed_at": completed_at})

    def record_result(
        self,
        *,
        gate: str,
        pr_number: int,
        branch: str,
        status: str,
        summary: str,
        findings: Optional[List[Dict[str, Any]]] = None,
        residual_risk: Optional[str] = None,
        contract_hash: str = "",
        pr_id: str = "",
        report_path: str = "",
        required_reruns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Record a review gate result with explicit advisory/blocking finding classification.

        Findings are classified by their ``severity`` field:
        - ``"blocking"`` or ``"error"`` severity -> blocking_findings
        - all other values -> advisory_findings

        Both lists are always present in the payload so downstream consumers can
        act on the classification without re-parsing the raw findings list.

        Per the headless review evidence contract, ``report_path`` and
        ``required_reruns`` are persisted in every gate result so the closure
        verifier can validate the full evidence chain.
        """
        from review_gate_manager import _utc_now

        raw_findings = findings or []
        request_payload = self._load_request_payload(gate, pr_number)
        effective_contract_hash = contract_hash or str(request_payload.get("contract_hash", ""))
        effective_report_path = report_path or str(request_payload.get("report_path", ""))
        receipt = GeminiReviewReceipt.from_raw_findings(
            pr_id=pr_id or str(pr_number),
            raw_findings=raw_findings,
            contract_hash=effective_contract_hash,
            reviewed_at=_utc_now(),
        )

        payload = self._build_result_payload(
            gate=gate, pr_number=pr_number, pr_id=pr_id, branch=branch,
            status=status, summary=summary, raw_findings=raw_findings,
            receipt=receipt, residual_risk=residual_risk or "",
            effective_contract_hash=effective_contract_hash,
            effective_report_path=effective_report_path,
            required_reruns=required_reruns,
        )
        # A4: stamp branch + commit_sha through the shared writer so a
        # CLI-recorded result carries the same PR-head identity every other
        # writer does — from GitHub, never the local checkout HEAD (B6).
        stamp_request_identity(
            payload,
            {
                "gate": gate,
                "pr_id": pr_id or str(pr_number),
                "branch": branch,
                "commit_sha": get_pr_head_sha(pr_number),
            },
        )
        self._validate_and_persist_result(payload, status, gate, pr_number, request_payload)
        return payload

    def _build_result_payload(
        self, *, gate: str, pr_number: int, pr_id: str, branch: str,
        status: str, summary: str, raw_findings: List[Dict[str, Any]],
        receipt: "GeminiReviewReceipt", residual_risk: str,
        effective_contract_hash: str, effective_report_path: str,
        required_reruns: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Build the result payload dict from receipt and parameters."""
        payload: Dict[str, Any] = {
            "gate": gate, "pr_number": pr_number,
            "pr_id": pr_id or str(pr_number), "branch": branch,
            "status": status, "summary": summary, "findings": raw_findings,
            "advisory_findings": [f.to_dict() for f in receipt.advisory_findings],
            "blocking_findings": [f.to_dict() for f in receipt.blocking_findings],
            "advisory_count": receipt.advisory_count,
            "blocking_count": receipt.blocking_count,
            "residual_risk": residual_risk,
            "contract_hash": effective_contract_hash,
            "report_path": effective_report_path,
            "required_reruns": list(required_reruns or []),
            "recorded_at": receipt.reviewed_at,
        }
        payload["report_path"] = self._canonical_report_path(payload["report_path"])
        return payload

    def _validate_and_persist_result(
        self, payload: Dict[str, Any], status: str, gate: str, pr_number: int,
        request_payload: Dict[str, Any],
    ) -> None:
        """Validate pass/fail constraints, then commit the record, its receipt and its request's end.

        GE-3 put this writer behind the overwrite guard: a ``record-result``
        must never erase a terminal record the runner already wrote for the
        same PR, gate and head. GE-4c-1 makes it one ``commit_gate_outcome``:
        the receipt is the commit's, with the residual risk, findings and
        required reruns ``_emit_result_receipt`` booked before, and the
        ``pr-<N>-<gate>`` request ends with it. A refusal raises
        :class:`ResultOverwriteRefused` (a ``ValueError``), the commit's own
        with its cause, and changes nothing.
        """
        if status in {"pass", "fail"}:
            error = evidence_error(payload)
            if error:
                raise ValueError(error)
        result_path = self._result_path(gate, pr_number)
        if canonical_status(payload) in INCOMPLETE_STATES:
            # A queued or requested record is no outcome (ADR-046 §1): it keeps
            # the overwrite guard alone, like a ci_gate running snapshot, and
            # books no receipt and ends no request.
            _payload_on_disk, written = write_result_guarded(
                result_path, payload, gate=gate, pr_ref=str(payload.get("pr_id") or pr_number),
            )
        else:
            # pr_id "": this record's slot and its request are both named by
            # the number (``pr-<N>-<gate>.json``), whatever pr_id it carries.
            outcome = GateOutcome.from_record(
                gate, payload, pr_id="", receipt_fields={key: payload[key] for key in _RECORD_RESULT_RECEIPT_FIELDS},
            )
            _payload_on_disk, written = commit_gate_outcome(
                outcome, results_dir=self.results_dir, result_path=result_path, raise_on_refusal=True,
                request=self._answered_request(request_payload, payload["recorded_at"]),
            )
        if not written:
            raise ResultOverwriteRefused(
                f"{gate} result for pr={pr_number} at {result_path} was refused by "
                f"the overwrite guard: a decided, evidenced verdict already holds "
                f"this slot for this head; the record on disk is unchanged"
            )

    def _classify_unavailable(self, gate: str) -> tuple:
        """Return (reason_code, reason_detail) for an unavailable gate provider (GATE-4).

        The provider name comes from ``gate_recorder.GATE_PROVIDERS``, never
        from the caller (OI-1490). This used to take a ``binary_name``
        argument and run a raw ``shutil.which`` on it, and the request-time
        kimi path passed ``"kimi_gate.py"`` — a name that is not a binary and
        never was, so the lookup could only fail and the gate could only be
        booked ``provider_not_installed``. Same fabrication as
        ``gate_runner``'s, on the other of the two paths; fixing one and
        leaving the other is a promise the code does not keep.

        Five answers, matching gate_runner's:

        * unregistered gate -> ``unsupported_gate_type``; a routing bug, not an
          environment complaint.
        * a gate whose provider family the operator switched off ->
          ``provider_switched_off``, with the switch decision as the detail.
          Named before every kind-specific branch below, so a switched-off
          harness-lane gate is never booked ``gate_runner_missing`` and never
          looks like a missing binary (provider-schakelaar C).
        * script-runner gate -> ``gate_runner_missing``. Reaching here means
          the caller's own availability check (``_kimi_gate_available`` and
          friends, which test for the runner FILE) already said no, so the
          runner is absent — never a PATH question.
        * harness-lane gate -> ``gate_runner_missing``, defensively: since
          dispatch 20260911-c6 step 2 the request-time availability check is
          lane-aware (``gate_is_available`` returns True for a registered
          harness-lane gate), so this branch is only reachable through a
          caller that skipped that check. The answer stays "the runner is
          absent", never a PATH lookup on the provider string.
        * PATH-binary gate -> the env-flag / which-lookup logic below,
          unchanged, on the registry's name.
        """
        from gate_recorder import (  # noqa: PLC0415
            GATE_PROVIDER_HARNESS_LANE,
            GATE_PROVIDER_SCRIPT_RUNNER,
            PROVIDER_SWITCHED_OFF_REASON,
            gate_switch_refusal,
            resolve_gate_provider,
        )

        provider = resolve_gate_provider(gate)
        if provider is None:
            return (
                "unsupported_gate_type",
                f"{gate} is not in gate_recorder.GATE_PROVIDERS — register it as a PATH "
                f"binary, a script runner, or a harness lane; no binary name is guessed "
                f"from the gate name",
            )
        refusal = gate_switch_refusal(
            gate, consumer="gate_result_parser._classify_unavailable",
            state_dir=getattr(self, "state_dir", None),
        )
        if refusal is not None:
            return (PROVIDER_SWITCHED_OFF_REASON, refusal.detail())
        provider_kind, binary_name = provider
        if provider_kind == GATE_PROVIDER_SCRIPT_RUNNER:
            return (
                "gate_runner_missing",
                f"{binary_name} does not exist — {gate} is a script runner and its "
                f"runner is not on disk; this is not a PATH lookup",
            )
        if provider_kind == GATE_PROVIDER_HARNESS_LANE:
            return (
                "gate_runner_missing",
                f"{gate} is a harness-lane gate (provider {binary_name}); its "
                f"request-time availability check said no and its runner file is "
                f"not on disk — this is not a PATH lookup",
            )

        # NOTE: this per-gate (env_var, default) map is NOT the same data as
        # gate_recorder._GATE_ENV_FLAGS — it carries a DEFAULT per gate, which
        # that one does not. Left in place deliberately rather than half-merged
        # into a shape that loses the defaults.
        env_flags = {
            "gemini_review": ("VNX_GEMINI_REVIEW_ENABLED", "1"),
            "codex_gate": ("VNX_CODEX_HEADLESS_ENABLED", "1"),
            "claude_github_optional": ("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0"),
            "ci_gate": ("VNX_CI_GATE_REQUIRED", "0"),
        }
        env_var, default = env_flags.get(gate, ("", "0"))
        disabled = env_var and os.environ.get(env_var, default) != "1" if gate == "ci_gate" else env_var and os.environ.get(env_var, default) == "0"
        binary_found = shutil.which(binary_name) is not None

        if disabled and not binary_found:
            return ("provider_disabled", f"{binary_name} binary not found in PATH and {env_var}=0")
        if disabled:
            return ("provider_disabled", f"{env_var} is set to 0")
        if not binary_found:
            return ("provider_not_installed", f"{binary_name} binary not found in PATH")
        return ("provider_not_configured", f"{gate} provider is not configured")
