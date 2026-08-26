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



class ReviewGateTakeoverConfigError(ValueError):
    """The configured review-gate takeover chain is malformed: an unknown
    gate name, or a gate repeated (which would build a cycle in what must
    stay a simple linear chain). Raised at READ time -- never discovered
    later as an infinite walk, nor silently dropped into a partial chain.
    """


# Ordered review-gate takeover chain (BETA3-E1, 26-08 operator decision,
# dispatch 20260826-beta3-e1-overname-keten-en-uitputtingstoets): codex_gate
# -> kimi_gate -> glm_gate -> deepseek_gate. This SUPERSEDES the single-hop
# dict a prior deliverable shipped, but PRESERVES that hop's own decision
# unchanged: kimi_gate -> glm_gate is still the same pairing the 22-08
# operator decision made (see the historical rationale this replaces, below)
# -- it is now just one link inside a longer, explicitly configured chain
# rather than the whole chain.
#
# Historical rationale for kimi_gate -> glm_gate (22-08, unchanged by this
# dispatch): on 22-08 glm_gate and kimi_gate gave OPPOSITE verdicts on the
# identical diff/contract_hash -- glm FAIL with a blocking finding, kimi PASS
# with zero findings -- so there is no measured basis for which reader is the
# better fallback, only that a working reader beats an unfilled seat. The
# measured variant (ordered by defect-recall) is a follow-up track (plan open
# question 1); this mapping stays a choice under uncertainty until that
# lands.
#
# deepseek_gate is a legal END-LINK even though its runner does not exist yet
# (a separate dispatch, E2, ships it): walking onto it always resolves
# not_executable/gate_runner_missing (see `_request_deepseek`) -- a named
# skip, never a further hop, since it is deliberately absent from the chain
# it would otherwise continue into.
_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN = "codex_gate,kimi_gate,glm_gate,deepseek_gate"


def _known_takeover_gate_names() -> "frozenset[str]":
    """Gate names the takeover-chain CONFIG may legally name: the closed
    ``Gate`` enum (dispatch_spec.py) PLUS ``deepseek_gate`` -- a real future
    chain member (E2 ships its runner + its own Gate enum member) that is
    deliberately NOT added to the Gate enum here. Adding it there without a
    matching gate_request_handler dispatch branch AND
    closure_verifier._GATE_HANDLERS entry would trip
    test_closure_verifier_gate_enum_drift.py (OI-1094) -- this local addition
    is the narrower, correct scope until E2 lands the real member.
    """
    from dispatch_spec import Gate
    return frozenset(Gate._value2member_map_) | {"deepseek_gate"}


def _parse_review_gate_takeover_chain(raw: str) -> Dict[str, str]:
    """Parse a comma-separated ORDERED gate list into a {gate: next_gate}
    successor map. An empty (post-strip) ``raw`` means NO takeover chain at
    all -- returns ``{}`` -- the operator's explicit choice, distinct from an
    absent config falling back to the built-in default (see
    ``_build_review_gate_takeover_chain``).

    Never returns a partial/best-effort map: an unknown gate name, or a gate
    named more than once (the only way a strictly linear list can cycle back
    on itself), raises ``ReviewGateTakeoverConfigError`` naming the offending
    value.
    """
    names = [item.strip() for item in raw.split(",") if item.strip()]
    if not names:
        return {}
    known = _known_takeover_gate_names()
    seen: "set[str]" = set()
    for name in names:
        if name not in known:
            raise ReviewGateTakeoverConfigError(
                f"VNX_REVIEW_GATE_TAKEOVER_CHAIN names an unknown gate {name!r} "
                f"(full chain: {raw!r}); known gates: {', '.join(sorted(known))}"
            )
        if name in seen:
            raise ReviewGateTakeoverConfigError(
                f"VNX_REVIEW_GATE_TAKEOVER_CHAIN contains a cycle: {name!r} appears more than "
                f"once in {raw!r} -- a gate may take over for at most one predecessor"
            )
        seen.add(name)
    return {names[i]: names[i + 1] for i in range(len(names) - 1)}


def _build_review_gate_takeover_chain() -> Dict[str, str]:
    """Resolve the operator's review-gate takeover chain from config
    (BETA3-E1, 26-08), never a hardcoded dict. Reads through
    ``config_runtime.get`` -- the SAME canonical-resolver + DB-override
    precedence ``review_gate_manager._build_default_review_stack`` already
    uses for ``VNX_DEFAULT_REVIEW_STACK``, and the one the OI-1462
    cross-process contract test (PR #1694) pins -- never a raw
    ``os.environ`` read that could silently disagree with what this same
    module's ``_ci_gate_available`` resolves two lines away.

    Resolved FRESH on every call (unlike ``DEFAULT_REVIEW_STACK``, which
    caches at module-import time): a takeover decision is made per gate per
    review request, so a config edit takes effect on the very next request
    instead of needing a process restart.

    Logs, at each resolution, whether the active chain came from an operator
    override or the built-in default -- comparing the resolved string
    against ``_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN`` verbatim, the same
    provenance technique ``config_registry.all_effective`` already uses
    (``is_default = value == entry.default``).
    """
    import config_runtime
    raw = config_runtime.get("VNX_REVIEW_GATE_TAKEOVER_CHAIN") or ""
    chain = _parse_review_gate_takeover_chain(raw)
    source = "standaard (registry default)" if raw == _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN else "operator-configuratie"
    logger.info(
        "gate_request_handler: review-gate takeover chain actief -- bron=%s waarde=%r keten=%r",
        source, raw, chain,
    )
    return chain


def _resolve_report_path(report_path: str, data_dir: Path) -> Optional[Path]:
    """Path-safe resolve of a gate result record's ``report_path`` field.

    Mirrors ``governance_emit._resolve_lane_log_path``'s defense-in-depth
    shape (that function's docstring calls it out by name): ``report_path``
    comes from a result record on disk -- state, not trusted input -- so a
    ``relative_to`` escape check against the reports dir runs before any
    read. An unconstrained ``report_path`` could point anywhere the process
    can read; the sibling lane-log reader two functions above already gets
    this right, this one previously did not. Returns None (refuse, no read)
    on anything unsafe or unresolvable -- a bad lookup must never block
    classification, only skip the report-fallback source.
    """
    if not report_path:
        return None
    reports_dir = (Path(data_dir) / "unified_reports").resolve()
    try:
        candidate = Path(report_path).resolve()
    except (OSError, RuntimeError):
        return None
    try:
        candidate.relative_to(reports_dir)
    except ValueError:
        logger.warning(
            "gate_request_handler: report-path read refused -- path %s escaped %s",
            candidate, reports_dir,
        )
        return None
    return candidate


def _scan_seat_failure_text(result: Dict[str, Any], manager: "Optional[GateRequestHandlerMixin]") -> "tuple[str, str]":
    """Scan every available raw-text source for the provider's own
    exhaustion marker via ``governance_emit._classify_lane_log_text`` --
    NEVER a second, hand-rolled marker list. Tries, in order: the JSON
    result record's own ``residual_risk``/``reason_detail``/``summary``
    fields (populated when the #1683 lane-log lift already ran), then --
    only when ``manager`` is not None, since the file lookup needs
    ``manager.paths`` to resolve a data dir -- ``manager._read_lane_log_or_report_text``
    (lane log, then the gate's own report; BETA3-E1 fix-forward).

    A MODULE-LEVEL function, not a method: ``_classify_review_seat_failure``
    is called unbound (``self=None``) by
    ``tests/test_oi1452_lane_log_lift_provider_failed_detail.py``, so the
    file-fallback half must be a plain, optional argument rather than
    something that blows up on a None receiver.

    Returns ``(state, detail)`` where ``state`` is ``'lane_exhausted'`` or
    ``'no_response'``, and ``detail`` is a bounded, marker-anchored snippet
    when ``state == 'lane_exhausted'`` (never the full multi-KB source) --
    the SAME text ``_dispatch_review_seat`` embeds into its takeover
    annotation, so the annotation PRESERVES the actual failure text rather
    than only referencing whichever record it came from (a takeover
    overwriting the source record must never erase the reason it was
    granted on). Never raises: a missing/unreadable file source is silently
    skipped, not a classification failure.
    """
    field_text = " ".join(
        str(result.get(key, "")) for key in ("residual_risk", "reason_detail", "summary")
    ).strip()
    if field_text:
        state, snippet = _classify_lane_log_text(field_text)
        if state == "lane_exhausted":
            return "lane_exhausted", snippet or field_text

    if manager is not None:
        file_text = manager._read_lane_log_or_report_text(result)
        if file_text:
            state, snippet = _classify_lane_log_text(file_text)
            if state == "lane_exhausted":
                return "lane_exhausted", snippet or file_text

    return "no_response", (field_text or "no detail recorded")


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

    def _deepseek_gate_runner_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "deepseek_gate.py"

    def _deepseek_gate_available(self) -> bool:
        return self._deepseek_gate_runner_path().exists()

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
        if gate == "deepseek_gate":
            return self._request_deepseek(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
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
        # reason == "dispatch_error" (or an unrecognised reason): scan for
        # the provider's own exhaustion marker via _scan_seat_failure_text --
        # the SAME classifier deliverable 0 built for the lane-log lift,
        # extended (BETA3-E1) to also try the gate's own report when no lane
        # log carries it. Never a second hand-rolled marker scan, and never
        # keyed on a parser-side msg field. Measured: 29 kimi cases carry
        # msg='Expecting value: line 1' beside raw='Error code: 403'; a
        # msg-keyed check reads toestand 2 while the raw body says toestand 1.
        state, _detail = _scan_seat_failure_text(result, self)
        return state

    def _read_lane_log_or_report_text(self, result: Dict[str, Any]) -> str:
        """Best-effort raw-text read backing the exhaustion-marker fallback
        scan (BETA3-E1 fix-forward, T0 live finding glm-gate-pr1691-1787754901,
        26-08): the per-dispatch lane log first (the SAME source the #1683
        lift already reads via ``governance_emit._read_lane_log_text``), then
        the gate's own REPORT file at ``result['report_path']`` when no lane
        log exists for this dispatch_id at all.

        Root cause this covers: the #1683 lift only ever reads the lane log.
        When a provider error lands in the gate's own report instead (no
        lane log for that dispatch), the marker never reaches
        residual_risk/reason_detail/summary -- even though the SAME
        classifier on the report's raw text finds it correctly. Measured:
        glm-gate-pr1691-1787754901 had NO lane log at all, but its 9345-byte
        report carried the real 402 ``openrouter_credits`` body.

        Returns "" on any missing dispatch_id/report_path, missing/empty
        file, or an unresolvable data dir -- a failed lookup must never
        raise out of a classification decision, and never fabricates text
        from nothing. An UNREADABLE report (exists, but ``OSError`` on read)
        is logged with its path (BETA3-E1b fix-forward) rather than
        swallowed indistinguishably from "no report exists" -- the classifier
        two frames up falls back to ``no_response`` either way, but an
        operator reading logs must be able to tell "nothing to read" apart
        from "a read failed", or a broken report-fallback source looks
        identical to a real absence of exhaustion evidence.

        ``report_path`` is on-disk STATE (a result record), not trusted
        input, so it is resolved via ``_resolve_report_path`` and refused if
        it would read outside the reports dir -- the SAME defense-in-depth
        shape ``governance_emit._resolve_lane_log_path`` already applies to
        the lane-log source two lines above.
        """
        try:
            data_dir = Path(self.paths["VNX_DATA_DIR"])
        except (AttributeError, KeyError, TypeError):
            data_dir = None

        dispatch_id = str(result.get("dispatch_id") or "")
        if dispatch_id and data_dir is not None:
            from governance_emit import _read_lane_log_text
            lane_text = _read_lane_log_text(dispatch_id, data_dir)
            if lane_text:
                return lane_text

        report_path = result.get("report_path") or ""
        if report_path and data_dir is not None:
            report_file = _resolve_report_path(report_path, data_dir)
            if report_file is None:
                return ""
            try:
                if not report_file.is_file():
                    return ""
                text = report_file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning(
                    "gate_request_handler: report read failed dispatch=%s path=%s: %s",
                    dispatch_id or "<unknown>", report_file, exc,
                )
                return ""
            if text.strip():
                return text
        return ""

    def _stamp_takeover_annotations(
        self,
        payload: Dict[str, Any],
        *,
        pr_number: Optional[int],
        takeover_from: str,
        failure_reason: str,
        takeover_reason: str,
        takeover_source_status: str,
        takeover_path: Optional[List[Dict[str, Any]]] = None,
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

        ``takeover_path`` (BETA3-E1): the FULL sequence of hops the seat took
        to reach this gate -- one entry per exhausted predecessor, in order
        -- so a reader sees "codex_gate exhausted, kimi_gate exhausted, glm_gate
        decided" instead of only the last jump. Defaults to a single-entry
        list built from the other (single-hop) arguments when the caller has
        no multi-hop path to report, so the field is NEVER absent on a
        takeover record.

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
        if takeover_path is None:
            takeover_path = [{
                "gate": takeover_from,
                "reason": takeover_reason,
                "detail": failure_reason,
                "status": takeover_source_status,
            }]
        annotations = {
            "failure_reason": failure_reason,
            "takeover": True,
            "takeover_from": takeover_from,
            "takeover_reason": takeover_reason,
            "takeover_source_status": takeover_source_status,
            "takeover_path": takeover_path,
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

    def _chain_exhausted_path(self, gate: str, pr_number: int) -> Path:
        return self.results_dir / f"pr-{pr_number}-{gate}-chain-exhausted.json"

    def _chain_exhausted_result(
        self,
        *,
        gate: str,
        pr_number: int,
        branch: str,
        dispatch_id: str,
        path: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Named terminal end-state (BETA3-E1): the takeover chain starting
        from ``gate`` walked every configured hop and every one of them was
        ``lane_exhausted``, with no further successor configured. Written to
        its OWN file (``*-chain-exhausted.json``) -- it never overwrites any
        individual gate's own request/result record, and it is NEVER a
        silent re-dispatch of ``gate`` itself, which ``path`` already proves
        dead.
        """
        from review_gate_manager import _utc_now

        failure_reason = " -- ".join(
            f"{hop['gate']} unavailable ({hop['reason']}): {hop['detail']}" for hop in path
        ) + " -- takeover chain exhausted, no live reader remains"
        now = _utc_now()
        payload: Dict[str, Any] = {
            "gate": gate,
            "pr_number": pr_number,
            "branch": branch,
            "status": "chain_exhausted",
            "reason": "takeover_chain_exhausted",
            "reason_detail": failure_reason,
            "failure_reason": failure_reason,
            "takeover": True,
            "takeover_from": gate,
            "takeover_path": path,
            "summary": f"review-gate takeover chain exhausted starting from {gate}: {failure_reason}",
            "requested_at": now,
            "resolved_at": now,
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        atomic_write_json(self._chain_exhausted_path(gate, pr_number), payload)
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
        chain: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Dispatch one review-stack seat, walking the configured takeover
        CHAIN (BETA3-E1) forward while every candidate's own last-recorded
        result is ``lane_exhausted``, then dispatching the first candidate
        that is not. Overname happens on REQUEST-time -- decided here,
        before issuing a new request -- never on attest-time (rewriting an
        already-attested final verdict), since that would let evidence
        change owners after the fact.

        CHAIN WALK: starting at ``gate``, each candidate's own last result is
        read via ``_read_existing_gate_result``. A candidate with no prior
        result yet, or a non-``lane_exhausted`` one (``unreadable_verdict`` /
        ``no_response`` abstain -- never take over), STOPS the walk there and
        that candidate is (re-)dispatched live. A ``lane_exhausted``
        candidate is recorded into ``path`` and the walk continues to
        ``chain.get(current)``. This means a hop through an
        ALREADY-PROVEN-dead gate (one whose own exhaustion was recorded in
        an earlier round) costs no extra round, but a NEWLY-discovered dead
        gate still needs its own round before the NEXT hop can act on it --
        the two-round latency documented below is per NEW gate along the
        path, not per round of ``request_reviews``. This is a deliberate
        choice (dispatch 20260826-beta3-e1, plan open question "per ronde
        een stap of binnen een ronde door?"): it lets an already-known-dead
        multi-hop chain resolve in ONE round instead of needing N rounds to
        walk N already-recorded hops, while still respecting the two-round
        rule for any hop that has not yet been proven dead by an actual
        dispatch attempt.

        This is DELIBERATELY (at minimum) two rounds per NEW hop, not one:
        the decision reads the LAST SAVED result, so a seat only gets filled
        on the request AFTER the one that recorded its failure. A
        synchronous refusal (binary missing) and an asynchronous one (quota
        403 surfacing mid-execution) would otherwise need two different
        in-round semantics to take over immediately -- one round of latency
        is the price of a single, uniform rule instead of two. Do not "fix"
        this into an in-round takeover for a brand-new hop: the first round
        after a quota-death yields an undecided/refused seat, the second
        round fills it, and that is the intended behaviour, not a bug.

        If the chain runs out (no ``chain.get(current)``) while ``current``
        is still ``lane_exhausted``, that is a NAMED terminal end-state
        (``_chain_exhausted_result``) -- never a silent fallback to
        re-dispatching the originally-requested ``gate``.
        """
        if chain is None:
            chain = _build_review_gate_takeover_chain()

        if gate not in chain:
            # This gate has no configured successor at all -- either the
            # chain is empty (operator explicitly disabled takeover) or this
            # particular gate was never wired into it. Dispatch normally,
            # exactly as a gate with no takeover mechanism always has --
            # NEVER a chain_exhausted terminal state for a gate that was
            # never part of a configured chain to begin with (that state is
            # reserved for a chain that WAS entered and then ran out).
            return self._dispatch_one_review(gate, pr_number, branch, risk_class, changed_files, mode, dispatch_id)

        path: List[Dict[str, Any]] = []
        current = gate
        while True:
            existing_result = self._read_existing_gate_result(current, pr_number)
            if existing_result is None:
                break
            seat_state = self._classify_review_seat_failure(existing_result)
            if seat_state != "lane_exhausted":
                break
            # B3: the takeover annotation is a MANDATORY, never-empty field --
            # an empty reason here would make this a silent refusal, not a
            # documented overname. The detail is the ACTUAL marker-anchored
            # snippet _scan_seat_failure_text found (lane log or report),
            # embedded here as a value -- never a pointer back to
            # existing_result -- so a later overwrite of that gate's own
            # result record (a separate, known issue T0 tracks independently)
            # cannot erase the reason this hop was recorded on.
            hop_reason = existing_result.get("reason", "unknown_reason")
            _hop_state, hop_detail = _scan_seat_failure_text(existing_result, self)
            path.append({
                "gate": current,
                "reason": hop_reason,
                "detail": hop_detail,
                "status": existing_result.get("status", ""),
            })
            next_gate = chain.get(current)
            if next_gate is None:
                return self._chain_exhausted_result(
                    gate=gate, pr_number=pr_number, branch=branch, dispatch_id=dispatch_id, path=path,
                )
            current = next_gate

        payload = self._dispatch_one_review(current, pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if not path:
            return payload

        failure_reason = " -- ".join(
            f"{hop['gate']} unavailable ({hop['reason']}): {hop['detail']}" for hop in path
        ) + f" -- {current} substituted as reader"
        last_hop = path[-1]
        return self._stamp_takeover_annotations(
            payload,
            pr_number=pr_number,
            takeover_from=last_hop["gate"],
            failure_reason=failure_reason,
            takeover_reason=last_hop["reason"],
            takeover_source_status=last_hop["status"],
            takeover_path=path,
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

        # Resolved ONCE per request_reviews() call (not per gate in the
        # stack): every seat in this stack takes over against the SAME
        # operator-configured chain, and a single log line per PR-review
        # cycle is enough operator visibility without repeating it per gate.
        chain = _build_review_gate_takeover_chain()

        for gate in review_stack_list:
            payload = self._dispatch_review_seat(
                gate, pr_number, branch, risk_class, changed_files, mode, dispatch_id, chain=chain,
            )
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
        atomic_write_json(self._request_path("gemini_review", pr_number), payload)
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
        atomic_write_json(self._request_path("codex_gate", pr_number), payload)
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
        atomic_write_json(self._request_path("kimi_gate", pr_number), payload)
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
        atomic_write_json(self._request_path("glm_gate", pr_number), payload)
        return payload

    def _request_deepseek(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        """deepseek_gate is a legal review-gate-takeover-CHAIN link (BETA3-E1,
        26-08 operator decision) whose runner ships in a separate dispatch
        (E2) — it is deliberately NOT a ``dispatch_spec.Gate`` enum member
        yet (see ``_known_takeover_gate_names``). Until E2 lands this always
        refuses, mirroring ``_request_glm``'s own pre-runner refusal branch:
        a reason distinct from ``unknown_review_gate`` so an operator can
        tell "not a real gate" apart from "real gate, runner not implemented
        yet" — the chain's own named-skip requirement.
        """
        from review_gate_manager import _utc_now

        available = self._deepseek_gate_available()
        requested_at = _utc_now()
        payload: Dict[str, Any] = {
            "gate": "deepseek_gate",
            "status": "requested" if available else "not_executable",
            "provider": "deepseek",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="deepseek_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ) if available else "",
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            reason = "gate_runner_missing"
            reason_detail = "scripts/deepseek_gate.py does not exist yet — ships in dispatch E2"
            payload["reason"] = reason
            payload["reason_detail"] = reason_detail
            payload["resolved_at"] = payload["requested_at"]
            self._write_not_executable_result(
                gate="deepseek_gate", pr_number=pr_number, pr_id="",
                reason=reason, reason_detail=reason_detail,
                dispatch_id=dispatch_id,
            )
            self._write_skip_rationale(
                gate="deepseek_gate", pr_id=str(pr_number),
                reason=reason, reason_detail=reason_detail,
                binary_name="deepseek_gate.py",
            )
        atomic_write_json(self._request_path("deepseek_gate", pr_number), payload)
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
        atomic_write_json(self._request_path("claude_github_optional", pr_number), payload)
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
        atomic_write_json(self._request_path("ci_gate", pr_number), payload)
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
