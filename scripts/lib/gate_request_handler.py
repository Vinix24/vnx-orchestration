"""Gate request creation and orchestration (GateRequestHandlerMixin).

Extracted from review_gate_manager.py as part of F27 batch refactor.
Methods handle creating gate request payloads for Gemini, Codex, and Claude GitHub.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from atomic_io import atomic_write_json
from auto_merge_policy import codex_final_gate_required
from review_contract import ReviewContract
from gemini_prompt_renderer import render_gemini_prompt
from gate_recorder import get_pr_head_sha
from governance_emit import _classify_lane_log_text
from claude_github_receipt import (
    ClaudeGitHubReviewReceipt,
    STATE_NOT_CONFIGURED,
    STATE_CONFIGURED_DRY_RUN,
    STATE_REQUESTED,
    STATE_BLOCKED,
    STATE_COMPLETED,
)

logger = logging.getLogger(__name__)


# Fixed fallback order for review-gate takeover (review-gate-provider-agnostic
# plan, deliverable 5). This is an OPERATOR DECISION recorded 22-08, not a
# measured defect-recall ranking: on 22-08 glm_gate and kimi_gate gave
# OPPOSITE verdicts on the identical diff/contract_hash -- glm FAIL with a
# blocking finding, kimi PASS with zero findings -- so there is no measured
# basis for which reader is the better fallback, only that a working reader
# beats an unfilled seat. The measured variant (ordered by defect-recall) is
# a follow-up track (plan open question 1); this mapping stays a choice under
# uncertainty until that lands.
_REVIEW_GATE_TAKEOVER_ORDER: Dict[str, str] = {
    "kimi_gate": "glm_gate",
}


class GateRequestHandlerMixin:
    """Mixin providing gate request creation methods for ReviewGateManager."""

    def _gemini_available(self) -> bool:
        return os.environ.get("VNX_GEMINI_REVIEW_ENABLED", "1") != "0" and shutil.which("gemini") is not None

    def _codex_headless_available(self) -> bool:
        return os.environ.get("VNX_CODEX_HEADLESS_ENABLED", "1") != "0" and shutil.which("codex") is not None

    def _claude_github_configured(self) -> bool:
        return os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0") == "1" and shutil.which("gh") is not None

    def _ci_gate_available(self) -> bool:
        import config_runtime
        return config_runtime.get_bool("VNX_CI_GATE_REQUIRED") and shutil.which("gh") is not None

    def _check_ci_gate_requirement_mismatch(self, dispatch_id: str) -> None:
        """OI-1462: compare THIS process's (the vervuller's) own resolution of
        ``VNX_CI_GATE_REQUIRED`` against what the obligation's writer (the
        eiser, running earlier in a possibly different environment) stamped
        at registration time. A finding is a benoemde condition — logged
        loudly, ledgered (ADR-005: an NDJSON event before the obligation
        record mutation, never only the mutation), and persisted onto the
        obligation record — never a silent skip, since the whole defect
        class is two processes disagreeing on the same flag with nothing
        noticing. Two distinct finding kinds
        (``gate_obligations.check_gate_requirement_mismatch``):
        ``value_mismatch`` (both sides captured a value, and they differ) and
        ``writer_capture_failed`` (the eiser's OWN flag-read broke — a fault,
        not a value to compare against).

        Best-effort: dispatch_id may be blank (non-obligation call sites),
        the obligation may not exist yet, or bookkeeping may fail — none of
        that may ever break an actual gate request.
        """
        if not dispatch_id:
            return
        try:
            import config_runtime
            from gate_obligations import (
                check_gate_requirement_mismatch,
                obligation_path,
                update_obligation,
            )
            from review_gate_manager import emit_governance_receipt

            path = obligation_path(self.state_dir, dispatch_id)
            if not path.exists():
                return
            record = json.loads(path.read_text(encoding="utf-8"))
            mismatch = check_gate_requirement_mismatch(
                record, flag="VNX_CI_GATE_REQUIRED",
                reader_value=config_runtime.get_bool("VNX_CI_GATE_REQUIRED"),
            )
            if mismatch is None:
                return
            if mismatch["kind"] == "writer_capture_failed":
                logger.warning(
                    "gate_request_handler: dispatch=%s flag=%s -- the eiser's "
                    "OWN gate-requirement capture FAILED at registration time "
                    "(%s); this obligation's requirement was never reliably "
                    "established and cannot be cross-checked",
                    dispatch_id, mismatch["flag"], mismatch["writer_error"],
                )
            else:
                logger.warning(
                    "gate_request_handler: cross-process gate-requirement mismatch "
                    "for dispatch=%s flag=%s writer=%s reader=%s -- the eiser and "
                    "the vervuller resolved VNX_CI_GATE_REQUIRED differently",
                    dispatch_id, mismatch["flag"], mismatch["writer_value"], mismatch["reader_value"],
                )
            # ADR-005: the ledger is canonical, before any other durable
            # mutation -- emit the NDJSON event first, then mutate the
            # obligation record, so an operator tailing t0_receipts.ndjson
            # sees this finding without having to know to go read obligation
            # JSON files.
            emit_governance_receipt(
                "gate_requirement_mismatch",
                receipt_kind="review_gate",
                status="mismatch",
                dispatch_id=dispatch_id,
                gate="ci_gate",
                **mismatch,
            )
            update_obligation(path, gate_requirement_mismatch=mismatch)
        except Exception as exc:  # vnx-silent-except: mismatch detection is diagnostic tooling on the read path -- it must never block or fail an actual gate request; failures here are logged at debug with dispatch_id + reason so they stay visible without risking the gate itself
            logger.debug(
                "gate_request_handler: ci_gate requirement-mismatch check skipped for dispatch=%s: %s",
                dispatch_id, exc,
            )

    def _kimi_gate_runner_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "kimi_gate.py"

    def _kimi_gate_available(self) -> bool:
        return self._kimi_gate_runner_path().exists()

    def _glm_gate_runner_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "glm_gate.py"

    def _glm_gate_available(self) -> bool:
        return self._glm_gate_runner_path().exists()

    def _dispatch_one_review(
        self,
        gate: str,
        pr_number: int,
        branch: str,
        risk_class: str,
        changed_files: List[str],
        mode: str,
        dispatch_id: str,
    ) -> Dict[str, Any]:
        if gate == "gemini_review":
            return self._request_gemini(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "codex_gate":
            return self._request_codex(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "claude_github_optional":
            from review_contract import ReviewContract
            contract = ReviewContract(
                pr_id=str(pr_number),
                branch=branch,
                risk_class=risk_class,
                changed_files=list(changed_files),
                dispatch_id=dispatch_id,
            )
            receipt = self.request_claude_github_with_contract(
                contract=contract,
                mode=mode,
                dispatch_id=dispatch_id,
                pr_number=pr_number,
            )
            payload = receipt.to_dict()
            payload["status"] = receipt.state  # backwards compat for emit_governance_receipt
            return payload
        if gate == "ci_gate":
            return self._request_ci_gate(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "wiring_gate":
            return self._request_wiring_gate(pr_number, branch, dispatch_id)
        if gate == "kimi_gate":
            return self._request_kimi(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "glm_gate":
            return self._request_glm(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        return {"gate": gate, "status": "blocked", "reason": "unknown_review_gate"}

    def _read_existing_gate_result(self, gate: str, pr_number: Optional[int]) -> Optional[Dict[str, Any]]:
        """Read a previously-recorded terminal result for (gate, pr_number) from
        disk, if one exists.

        Backs the REQUEST-time takeover decision (deliverable 5): before
        (re-)requesting a gate, this checks whether it already produced a
        terminal not_executable/unavailable result for this PR on an earlier
        call, so a known-broken seat can be routed around instead of
        re-requested. Never invents a live outcome by executing anything
        itself. Returns None on a missing or unreadable file so a corrupt or
        absent result reads as "no signal" -- not as a failure to take over.
        """
        if pr_number is None:
            return None
        result_file = self._result_path(gate, pr_number)
        if not result_file.exists():
            return None
        try:
            return json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _classify_review_seat_failure(self, result: Dict[str, Any]) -> str:
        """Classify a previously-recorded gate result into one of the plan's
        three failure states, or 'ok' when it is not a takeover-eligible
        failure at all.

        Returns:
            'lane_exhausted'     -- toestand 1: exhaustion with body (a
                                     payment/quota code, or a permanent
                                     not_executable refusal with a
                                     provider-owned reason). Take over.
            'unreadable_verdict' -- toestand 2: a response existed, the
                                     verdict block did not parse. Abstain --
                                     never take over.
            'no_response'        -- toestand 3: a bare non-zero exit with no
                                     body, or no signal at all. Never take
                                     over.
            'ok'                 -- a real terminal verdict (pass/fail), or a
                                     not_executable refusal that is not a
                                     provider reason (e.g. glm's pre-dlv1
                                     "runner not shipped yet" refusal).
                                     Dispatch normally.
        """
        status = result.get("status", "")
        if status == "not_executable":
            # A code-not-shipped refusal is not a provider outage -- taking
            # over FROM a seat that was never real would misattribute cause.
            if result.get("reason") == "gate_runner_missing":
                return "ok"
            return "lane_exhausted"
        if status != "unavailable":
            return "ok"
        reason = result.get("reason", "")
        if reason == "parse_error":
            return "unreadable_verdict"
        if reason == "no_verdict":
            return "no_response"
        # reason == "dispatch_error" (or an unrecognised reason): scan the
        # RAW detail text for the provider's own exhaustion marker via the
        # SAME classifier deliverable 0 built for the lane-log lift -- never
        # a second hand-rolled marker scan, and never keyed on a parser-side
        # msg field. Measured: 29 kimi cases carry
        # msg='Expecting value: line 1' beside raw='Error code: 403'; a
        # msg-keyed check reads toestand 2 while the raw body says toestand 1.
        detail_text = " ".join(
            str(result.get(key, "")) for key in ("residual_risk", "reason_detail", "summary")
        ).strip()
        if not detail_text:
            return "no_response"
        state, _snippet = _classify_lane_log_text(detail_text)
        return "lane_exhausted" if state == "lane_exhausted" else "no_response"

    def _stamp_takeover_annotations(
        self,
        payload: Dict[str, Any],
        *,
        pr_number: Optional[int],
        takeover_from: str,
        failure_reason: str,
        takeover_reason: str,
        takeover_source_status: str,
    ) -> Dict[str, Any]:
        """Annotate a fallback gate's payload with takeover provenance and
        persist it into whichever on-disk record(s) ``_dispatch_one_review``
        already wrote.

        An annotation that lives only in the returned dict and never reaches
        the file an operator or ``closure_verifier`` actually reads back is
        evidence that exists and guards nothing (the OI-1178 lesson,
        reapplied here) -- so both the request record and, when present, the
        terminal result record are rewritten with the same annotation
        fields. ``failure_reason`` is the CANONICAL reason field (#1666 /
        OI-1415) -- never a one-off field name a generic receipt reader would
        not recognise.

        Both writes go through ``atomic_write_json`` (temp file in the same
        directory + ``os.replace``), never a bare ``write_text``. These are
        EVIDENCE files: a torn write here passes the existence check a reader
        does first and only fails on the content it trusts -- worse than no
        record at all, because it surfaces exactly when the record is
        needed. Not suppressed with a `# vnx-atomic-write` marker: that
        marker is for writes where a torn write is harmless, which a gate
        evidence record is not.
        """
        gate = payload.get("gate", "")
        annotations = {
            "failure_reason": failure_reason,
            "takeover": True,
            "takeover_from": takeover_from,
            "takeover_reason": takeover_reason,
            "takeover_source_status": takeover_source_status,
        }
        payload.update(annotations)

        if pr_number is not None:
            request_file = self._request_path(gate, pr_number)
            if request_file.exists():
                atomic_write_json(request_file, payload)

            result_file = self._result_path(gate, pr_number)
            if result_file.exists():
                try:
                    result_payload = json.loads(result_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    result_payload = None
                if result_payload is not None:
                    result_payload.update(annotations)
                    atomic_write_json(result_file, result_payload)
        return payload

    def _dispatch_review_seat(
        self,
        gate: str,
        pr_number: int,
        branch: str,
        risk_class: str,
        changed_files: List[str],
        mode: str,
        dispatch_id: str,
    ) -> Dict[str, Any]:
        """Dispatch one review-stack seat, taking over to the configured
        fallback gate when the seat's last recorded result was toestand 1
        (exhaustion with body). Overname happens on REQUEST-time -- decided
        here, before issuing a new request -- never on attest-time (rewriting
        an already-attested final verdict), since that would let evidence
        change owners after the fact.

        This is DELIBERATELY two rounds, not one: the decision reads the
        LAST SAVED result, so a seat only gets filled on the request AFTER
        the one that recorded its failure. A synchronous refusal (binary
        missing) and an asynchronous one (quota 403 surfacing mid-execution)
        would otherwise need two different in-round semantics to take over
        immediately -- one round of latency is the price of a single,
        uniform rule instead of two. Do not "fix" this into an in-round
        takeover: the first round after a quota-death yields an
        undecided/refused seat, the second round fills it, and that is the
        intended behaviour, not a bug.
        """
        existing_result = self._read_existing_gate_result(gate, pr_number)
        fallback_gate = _REVIEW_GATE_TAKEOVER_ORDER.get(gate)
        if existing_result is None or fallback_gate is None:
            return self._dispatch_one_review(gate, pr_number, branch, risk_class, changed_files, mode, dispatch_id)

        seat_state = self._classify_review_seat_failure(existing_result)
        if seat_state != "lane_exhausted":
            return self._dispatch_one_review(gate, pr_number, branch, risk_class, changed_files, mode, dispatch_id)

        payload = self._dispatch_one_review(
            fallback_gate, pr_number, branch, risk_class, changed_files, mode, dispatch_id,
        )
        failed_reason = existing_result.get("reason", "unknown_reason")
        failed_detail = (
            existing_result.get("reason_detail")
            or existing_result.get("residual_risk")
            or existing_result.get("summary")
            or "no detail recorded"
        )
        # B3: the takeover annotation is a MANDATORY, never-empty field -- an
        # empty reason here would make this a silent refusal, not a
        # documented overname.
        failure_reason = (
            f"{gate} unavailable ({failed_reason}): {failed_detail} -- "
            f"{fallback_gate} substituted as reader"
        )
        return self._stamp_takeover_annotations(
            payload,
            pr_number=pr_number,
            takeover_from=gate,
            failure_reason=failure_reason,
            takeover_reason=failed_reason,
            takeover_source_status=existing_result.get("status", ""),
        )

    def request_reviews(
        self,
        *,
        pr_number: int,
        branch: str,
        review_stack: Optional[Iterable[str]] = None,
        risk_class: str,
        changed_files: Iterable[str],
        mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import DEFAULT_REVIEW_STACK, _utc_now, emit_governance_receipt

        changed_files = [str(path).strip() for path in changed_files if str(path).strip()]

        if review_stack is not None:
            review_stack_list = [item.strip() for item in review_stack if str(item).strip()]
        else:
            from profile_gate_resolver import resolve_gate_stack as _resolve_gate_stack
            _profile_stack = _resolve_gate_stack(changed_files)
            review_stack_list = _profile_stack if _profile_stack is not None else list(DEFAULT_REVIEW_STACK)
        requested: List[Dict[str, Any]] = []

        for gate in review_stack_list:
            payload = self._dispatch_review_seat(gate, pr_number, branch, risk_class, changed_files, mode, dispatch_id)
            requested.append(payload)
            receipt_fields: Dict[str, Any] = {}
            if payload.get("takeover"):
                # Top-level, in addition to the nested `request` payload, so
                # a generic ledger reader finds the canonical reason field
                # without knowing to look inside `request` (OI-1415).
                receipt_fields["failure_reason"] = payload["failure_reason"]
                receipt_fields["takeover_from"] = payload["takeover_from"]
            emit_governance_receipt(
                "review_gate_request",
                receipt_kind="review_gate",
                status=payload["status"],
                terminal="T0",
                pr_id=str(pr_number),
                pr_number=pr_number,
                branch=branch,
                gate=payload["gate"],
                review_mode=mode,
                risk_class=risk_class,
                changed_files=changed_files,
                request=payload,
                dispatch_id=dispatch_id,
                **receipt_fields,
            )

        return {
            "pr_number": pr_number,
            "branch": branch,
            "requested": requested,
        }

    def _mark_gate_unavailable(
        self,
        payload: Dict[str, Any],
        *,
        gate: str,
        binary_name: str,
        pr_number: Optional[int],
        pr_id: str,
        contract_hash: str = "",
        dispatch_id: str = "",
    ) -> None:
        """Record unavailability in payload and write skip/result records."""
        reason, detail = self._classify_unavailable(gate, binary_name)
        payload["reason"] = reason
        payload["reason_detail"] = detail
        # OI-1415 (same fix as #1666 for phantom_guard/pr_enforcement): stamp
        # the canonical failure_reason field with the SAME text as the
        # lane-own reason_detail above -- a seat that refuses on its very
        # first round (no prior recorded result, so `_dispatch_review_seat`
        # never reaches the takeover branch) must still carry a reason a
        # generic failure-reason reader can find, not only a lane-specific
        # field name.
        payload["failure_reason"] = detail
        payload["resolved_at"] = payload["requested_at"]
        self._write_not_executable_result(
            gate=gate, pr_number=pr_number, pr_id=pr_id,
            reason=reason, reason_detail=detail,
            contract_hash=contract_hash,
            dispatch_id=dispatch_id,
        )
        self._write_skip_rationale(
            gate=gate, pr_id=pr_id or str(pr_number),
            reason=reason, reason_detail=detail,
            binary_name=binary_name,
        )

    def _request_gemini(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        available = self._gemini_available()
        requested_at = _utc_now()
        payload = {
            "gate": "gemini_review",
            "status": "requested" if available else "not_executable",
            "provider": "gemini_cli",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="gemini_review",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="gemini_review", binary_name="gemini",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._request_path("gemini_review", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _build_gemini_contract_payload(
        self,
        contract: ReviewContract,
        mode: str,
        dispatch_id: str,
        available: bool,
        requested_at: str,
        prompt: str,
        pr_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "gate": "gemini_review",
            "status": "requested" if available else "not_executable",
            "provider": "gemini_cli",
            "branch": contract.branch,
            "pr_id": contract.pr_id,
            "pr_number": None,
            "review_mode": mode,
            "risk_class": contract.risk_class,
            "changed_files": contract.changed_files,
            "contract_hash": contract.content_hash,
            "prompt": prompt,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "dispatch_id": dispatch_id,
            "report_path": self._build_report_path(
                gate="gemini_review",
                requested_at=requested_at,
                pr_id=contract.pr_id,
            ),
        }
        if not available:
            self._mark_gate_unavailable(
                payload, gate="gemini_review", binary_name="gemini",
                pr_number=None, pr_id=contract.pr_id,
                contract_hash=contract.content_hash,
                dispatch_id=dispatch_id,
            )
        return payload

    def request_gemini_with_contract(
        self,
        *,
        contract: ReviewContract,
        mode: str = "per_pr",
        dispatch_id: str = "",
        pr_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Request a Gemini review driven by a canonical ReviewContract.

        Renders a deliverable-aware prompt from the contract and persists the
        request payload including the rendered prompt text and contract hash.

        Raises:
            gemini_prompt_renderer.MissingContractFieldError: when the contract is missing required fields.
        """
        from review_gate_manager import _utc_now, emit_governance_receipt

        prompt = render_gemini_prompt(contract)
        available = self._gemini_available()
        requested_at = _utc_now()
        payload = self._build_gemini_contract_payload(
            contract, mode, dispatch_id, available, requested_at, prompt, pr_number=pr_number,
        )

        request_file = self._contract_request_path("gemini_review", contract.pr_id)
        request_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        emit_governance_receipt(
            "review_gate_request",
            receipt_kind="review_gate",
            status=payload["status"],
            terminal="T0",
            pr_id=contract.pr_id,
            branch=contract.branch,
            gate="gemini_review",
            review_mode=mode,
            risk_class=contract.risk_class,
            contract_hash=contract.content_hash,
            changed_files=contract.changed_files,
            dispatch_id=dispatch_id,
        )
        return payload

    def _validate_pr_number_for_github(
        self,
        pr_number: Optional[int],
        contract_pr_id: str,
    ) -> Optional[tuple]:
        if pr_number is None:
            return (
                STATE_BLOCKED,
                "missing_github_pr_number",
                "gh pr comment requires a real GitHub PR number; "
                f"governance pr_id {contract_pr_id!r} is not a valid PR ref",
            )
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
            return (
                STATE_BLOCKED,
                "invalid_github_pr_number",
                f"pr_number must be a positive int (got {pr_number!r}); "
                f"the governance pr_id {contract_pr_id!r} is not a valid PR ref",
            )
        return None

    def _trigger_github_comment(self, pr_number: int, comment_body: str) -> tuple:
        proc = subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--body", comment_body],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return (STATE_REQUESTED, None, None)
        return (STATE_BLOCKED, "claude_github_trigger_failed", proc.stderr.strip())

    def _determine_claude_github_state(
        self,
        configured: bool,
        contract_pr_id: str,
        comment_body: str,
        pr_number: Optional[int] = None,
    ) -> tuple:
        """Determine Claude GitHub review state from environment configuration.

        ``pr_number`` is the real GitHub PR number used to target ``gh pr comment``.
        ``contract_pr_id`` is the governance ID (e.g. "PR-4") and is *not* a valid
        GitHub PR reference. If we attempted to trigger a comment without a real
        ``pr_number``, ``gh`` would either fail outright or — worse, with a numeric
        contract id — target the wrong PR. Treat that as BLOCKED rather than
        silently issuing a bogus call.

        ``pr_number`` must be a positive ``int``. Any other value (including
        strings that look numeric, ``0``, or negative values) is rejected as a
        misuse — passing ``contract.pr_id`` here would silently target the
        wrong PR.

        Returns (state, reason, stderr_detail) tuple.
        """
        if not configured:
            return (STATE_NOT_CONFIGURED, "claude_github_not_configured", None)
        if os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "0") != "1":
            return (STATE_CONFIGURED_DRY_RUN, None, None)
        invalid = self._validate_pr_number_for_github(pr_number, contract_pr_id)
        if invalid is not None:
            return invalid
        return self._trigger_github_comment(pr_number, comment_body)

    def _build_claude_github_payload(
        self,
        receipt: ClaudeGitHubReviewReceipt,
        contract: ReviewContract,
        mode: str,
        dispatch_id: str,
        requested_at: str,
        stderr_detail: Optional[str],
    ) -> Dict[str, Any]:
        payload = receipt.to_dict()
        if stderr_detail:
            payload["stderr"] = stderr_detail
        payload["review_mode"] = mode
        payload["risk_class"] = contract.risk_class
        payload["changed_files"] = contract.changed_files
        payload["commit_sha"] = get_pr_head_sha(receipt.pr_number)
        payload["dispatch_id"] = dispatch_id
        payload["report_path"] = self._build_report_path(
            gate="claude_github_optional",
            requested_at=requested_at,
            pr_id=contract.pr_id,
        )
        return payload

    def _persist_claude_github_files(
        self,
        payload: Dict[str, Any],
        contract: ReviewContract,
        requested_at: str,
    ) -> None:
        request_file = self._contract_request_path("claude_github_optional", contract.pr_id)
        request_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        # Persist the explicit state as a result record so closure_verifier can
        # observe optional-gate state via review_gates/results/ — without this
        # mirror, no_op / dry_run / requested / blocked configurations are
        # invisible to the verifier and break closure for normal optional-gate
        # paths.
        result_file = self._contract_result_path("claude_github_optional", contract.pr_id)
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_payload = dict(payload)
        result_payload["gate"] = "claude_github_optional"
        result_payload["recorded_at"] = requested_at
        result_file.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

    def _build_claude_github_receipt(
        self,
        contract: ReviewContract,
        state: str,
        reason: Optional[str],
        requested_at: str,
        pr_number: Optional[int],
        comment_body: str,
    ) -> ClaudeGitHubReviewReceipt:
        return ClaudeGitHubReviewReceipt(
            pr_id=contract.pr_id,
            state=state,
            contract_hash=contract.content_hash,
            branch=contract.branch,
            pr_number=pr_number,
            gh_comment_body=comment_body if state == STATE_REQUESTED else "",
            reason=reason,
            requested_at=requested_at,
        )

    def _emit_claude_github_request_receipt(
        self,
        contract: ReviewContract,
        mode: str,
        dispatch_id: str,
        state: str,
        receipt: ClaudeGitHubReviewReceipt,
    ) -> None:
        from review_gate_manager import emit_governance_receipt

        emit_governance_receipt(
            "review_gate_request",
            receipt_kind="review_gate",
            status=state,
            terminal="T0",
            pr_id=contract.pr_id,
            branch=contract.branch,
            gate="claude_github_optional",
            review_mode=mode,
            risk_class=contract.risk_class,
            contract_hash=contract.content_hash,
            changed_files=contract.changed_files,
            contributed_evidence=receipt.contributed_evidence(),
            was_intentionally_absent=receipt.was_intentionally_absent(),
            dispatch_id=dispatch_id,
        )

    def request_claude_github_with_contract(
        self,
        *,
        contract: ReviewContract,
        mode: str = "per_pr",
        dispatch_id: str = "",
        pr_number: Optional[int] = None,
    ) -> ClaudeGitHubReviewReceipt:
        """Request a Claude GitHub review driven by a canonical ReviewContract.

        Determines the explicit review state from environment configuration and
        persists the request payload linked to the contract hash.

        ``pr_number`` is the real GitHub PR number; required only when the
        environment opts in to actually triggering ``gh pr comment``. The
        closure verifier requires the resulting state to be visible in the
        review_gates ``results/`` directory regardless of the state value, so
        the state is always materialised as a result record (not just a
        request) to keep the optional-gate evidence loop closed.
        """
        from review_gate_manager import _utc_now

        configured = self._claude_github_configured()
        requested_at = _utc_now()
        comment_body = os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_COMMENT", "@claude review")

        state, reason, stderr_detail = self._determine_claude_github_state(
            configured, contract.pr_id, comment_body, pr_number=pr_number,
        )

        receipt = self._build_claude_github_receipt(contract, state, reason, requested_at, pr_number, comment_body)
        payload = self._build_claude_github_payload(receipt, contract, mode, dispatch_id, requested_at, stderr_detail)
        self._persist_claude_github_files(payload, contract, requested_at)
        self._emit_claude_github_request_receipt(contract, mode, dispatch_id, state, receipt)
        return receipt

    def _request_codex(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        required = mode == "final" or codex_final_gate_required(changed_files)
        available = self._codex_headless_available()
        # Model from env only; empty string means "use codex config.toml default".
        # See gate_runner._build_gate_cmd and ~/.codex/config.toml for defaults.
        model = os.environ.get("VNX_CODEX_HEADLESS_MODEL") or os.environ.get("VNX_CODEX_MODEL") or ""
        requested_at = _utc_now()
        payload = {
            "gate": "codex_gate",
            "status": "requested" if available else "not_executable",
            "provider": "codex_headless",
            "model": model,
            "required": required,
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="codex_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="codex_gate", binary_name="codex",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._request_path("codex_gate", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _request_kimi(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        available = self._kimi_gate_available()
        requested_at = _utc_now()
        payload = {
            "gate": "kimi_gate",
            "status": "requested" if available else "not_executable",
            "provider": "kimi",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="kimi_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="kimi_gate", binary_name="kimi_gate.py",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._request_path("kimi_gate", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _request_glm(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        """glm_gate is a recognised Gate.GLM_GATE member whose runner
        (scripts/glm_gate.py) has not shipped yet — a separate deliverable adds
        it. Until then this must refuse with a reason distinct from
        ``unknown_review_gate`` (dispatch_spec Gate rejects that request before
        it ever reaches here) so an operator can tell "not a real gate" apart
        from "real gate, runner not implemented yet".
        """
        from review_gate_manager import _utc_now

        available = self._glm_gate_available()
        requested_at = _utc_now()
        payload: Dict[str, Any] = {
            "gate": "glm_gate",
            "status": "requested" if available else "not_executable",
            "provider": "glm",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="glm_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ) if available else "",
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            reason = "gate_runner_missing"
            reason_detail = "scripts/glm_gate.py does not exist yet — ships in a separate deliverable"
            payload["reason"] = reason
            payload["reason_detail"] = reason_detail
            payload["resolved_at"] = payload["requested_at"]
            self._write_not_executable_result(
                gate="glm_gate", pr_number=pr_number, pr_id="",
                reason=reason, reason_detail=reason_detail,
                dispatch_id=dispatch_id,
            )
            self._write_skip_rationale(
                gate="glm_gate", pr_id=str(pr_number),
                reason=reason, reason_detail=reason_detail,
                binary_name="glm_gate.py",
            )
        self._request_path("glm_gate", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _apply_claude_github_configured_state(
        self,
        payload: Dict[str, Any],
        pr_number: int,
    ) -> None:
        payload["status"] = "queued"
        if os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "0") == "1":
            comment = os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_COMMENT", "@claude review")
            proc = subprocess.run(
                ["gh", "pr", "comment", str(pr_number), "--body", comment],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                payload["status"] = "requested"
            else:
                payload["status"] = "blocked"
                payload["reason"] = "claude_github_trigger_failed"
                payload["stderr"] = proc.stderr.strip()
        else:
            payload["status"] = "configured_dry_run"

    def _request_claude_github(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        configured = self._claude_github_configured()
        requested_at = _utc_now()
        payload = {
            "gate": "claude_github_optional",
            "status": "not_configured",
            "provider": "claude_github",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="claude_github_optional",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if configured:
            self._apply_claude_github_configured_state(payload, pr_number)
        self._request_path("claude_github_optional", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _request_ci_gate(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        available = self._ci_gate_available()
        self._check_ci_gate_requirement_mismatch(dispatch_id)
        requested_at = _utc_now()
        payload: Dict[str, Any] = {
            "gate": "ci_gate",
            "status": "requested" if available else "not_executable",
            "provider": "gh_cli",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="ci_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="ci_gate", binary_name="gh",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._request_path("ci_gate", pr_number).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _build_ci_gate_contract_payload(
        self,
        contract: "ReviewContract",
        pr_number: int,
        mode: str,
        dispatch_id: str,
        available: bool,
        requested_at: str,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "gate": "ci_gate",
            "status": "requested" if available else "not_executable",
            "provider": "gh_cli",
            "branch": contract.branch,
            "pr_id": contract.pr_id,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": contract.risk_class,
            "changed_files": contract.changed_files,
            "contract_hash": contract.content_hash,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "dispatch_id": dispatch_id,
            "report_path": self._build_report_path(
                gate="ci_gate",
                requested_at=requested_at,
                pr_id=contract.pr_id,
            ),
        }
        if not available:
            self._mark_gate_unavailable(
                payload, gate="ci_gate", binary_name="gh",
                pr_number=pr_number, pr_id=contract.pr_id,
                contract_hash=contract.content_hash,
                dispatch_id=dispatch_id,
            )
        return payload

    def _emit_ci_gate_contract_receipt(
        self,
        contract: "ReviewContract",
        mode: str,
        dispatch_id: str,
        status: str,
    ) -> None:
        from review_gate_manager import emit_governance_receipt

        emit_governance_receipt(
            "review_gate_request",
            receipt_kind="review_gate",
            status=status,
            terminal="T0",
            pr_id=contract.pr_id,
            branch=contract.branch,
            gate="ci_gate",
            review_mode=mode,
            risk_class=contract.risk_class,
            contract_hash=contract.content_hash,
            changed_files=contract.changed_files,
            dispatch_id=dispatch_id,
        )

    def request_ci_gate_with_contract(
        self,
        *,
        contract: "ReviewContract",
        pr_number: int,
        mode: str = "per_pr",
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        """Request a ci_gate execution driven by a canonical ReviewContract.

        Writes a contract-scoped request file ({pr_slug}-ci_gate-contract.json)
        with the canonical pr_id and the contract's content_hash.  This enables
        closure_verifier._find_gate_result to locate the result via the contract
        path and ensures the result's contract_hash matches ReviewContract.content_hash.

        ``pr_number`` is the real GitHub PR number used by ``gh pr checks``.
        """
        from review_gate_manager import _utc_now

        if not contract.pr_id:
            raise ValueError("contract.pr_id is required for ci_gate contract request")

        available = self._ci_gate_available()
        requested_at = _utc_now()
        payload = self._build_ci_gate_contract_payload(contract, pr_number, mode, dispatch_id, available, requested_at)

        request_file = self._contract_request_path("ci_gate", contract.pr_id)
        request_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        self._emit_ci_gate_contract_receipt(contract, mode, dispatch_id, payload["status"])
        return payload

    def _wiring_gate_available(self) -> bool:
        return shutil.which("gh") is not None

    def _request_wiring_gate(
        self, pr_number: int, branch: str, dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now
        from wiring_gate import WiringGateError, check_pr_wiring
        import config_runtime

        available = self._wiring_gate_available()
        requested_at = _utc_now()
        # The gate's blocking posture is the same toggle that decides its status
        # (check_pr_wiring: status="fail" if required else "advisory"). Carrying it
        # explicitly lets the required-failure count in gate_executor honor the
        # advisory contract: a shadow-mode gate (VNX_WIRING_GATE_REQUIRED=0) is
        # required=False and must never gate the merge, while required=True blocks.
        required = config_runtime.get_bool("VNX_WIRING_GATE_REQUIRED")

        if not available:
            payload: Dict[str, Any] = {
                "gate": "wiring_gate",
                "status": "not_executable",
                "provider": "gh_cli",
                "required": required,
                "branch": branch,
                "pr_number": pr_number,
                "requested_at": requested_at,
                "reason": "provider_not_installed",
                "reason_detail": "gh binary not found in PATH",
            }
            if dispatch_id:
                payload["dispatch_id"] = dispatch_id
            _path = self._request_path("wiring_gate", pr_number)
            _tmp = _path.with_suffix(".tmp")
            _tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(_tmp, _path)
            return payload

        try:
            result = check_pr_wiring(pr_number)
        except WiringGateError as exc:
            payload = {
                "gate": "wiring_gate",
                "status": "fail",
                "provider": "ast_grep",
                "required": required,
                "branch": branch,
                "pr_number": pr_number,
                "requested_at": requested_at,
                "completed_at": _utc_now(),
                "summary": f"Wiring gate error: {exc}",
                "blocking_findings": [{
                    "severity": "blocking",
                    "title": "Wiring gate subprocess failure",
                    "description": str(exc),
                }],
                "blocking_count": 1,
                "advisory_count": 0,
                "total_checked": 0,
                "skipped_symbols": [],
            }
            if dispatch_id:
                payload["dispatch_id"] = dispatch_id
            _path = self._request_path("wiring_gate", pr_number)
            _tmp = _path.with_suffix(".tmp")
            _tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(_tmp, _path)
            return payload

        payload = {
            "gate": "wiring_gate",
            "status": result.status,
            "provider": "ast_grep",
            "required": required,
            "branch": branch,
            "pr_number": pr_number,
            "requested_at": requested_at,
            "completed_at": _utc_now(),
            "summary": result.summary,
            "blocking_findings": [
                {
                    "severity": "blocking" if result.status == "fail" else "advisory",
                    "title": f"Unwired {s.kind}: {s.name}",
                    "description": f"{s.file}:{s.line} — zero callers outside definition file",
                }
                for s in result.unwired
            ],
            "blocking_count": len(result.unwired) if result.status == "fail" else 0,
            "advisory_count": len(result.unwired) if result.status == "advisory" else 0,
            "total_checked": result.total_checked,
            "skipped_symbols": result.skipped,
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        _path = self._request_path("wiring_gate", pr_number)
        _tmp = _path.with_suffix(".tmp")
        _tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(_tmp, _path)
        return payload
