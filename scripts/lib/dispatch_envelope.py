"""dispatch_envelope.py — Flag-gated unified dispatch envelope (PR-1 codex, PR-2 claude-subprocess).

Strangler-fig approach: legacy default OFF, each lane activated per VNX_UNIFIED_ENVELOPE_LANES.
Activate with VNX_UNIFIED_ENVELOPE=1 and VNX_UNIFIED_ENVELOPE_LANES containing "codex"
and/or "claude-subprocess".

Seams: PREPARE -> ROUTE -> EXECUTE -> GOVERN

GOVERN is fail-closed: a missing receipt_path raises EnvelopeGovernError — never
silently loses a receipt. Report is emitted before the receipt so the receipt carries
the linkage even when the report file is new (ADR-005).

Per-lane dual-receipt safety: GOVERN emits both report AND receipt. When the
receipt NDJSON already contains a line for this dispatch_id (e.g. written by
deliver_with_recovery's internal close-out), the GOVERN receipt write is skipped
(idempotent dedup). No double-emit.

Reuses:
  - spawn_codex from provider_spawns.codex_spawn (no reimplementation)
  - spawn_claude from provider_spawns.claude_spawn (no reimplementation)
  - emit_dispatch_receipt + emit_unified_report from governance_emit

No new hooks. Idempotent receipts (governance_emit uses fcntl.flock).
EventStore wiring and cost emission are open items for later PRs (PR-1 scope: flag-gate only).
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))

# ExecutionPlan / ExecutionPermit: the plan + permit types routed by the
# adapters and run_envelope_plan. Runtime import (not TYPE_CHECKING) so
# typing.get_type_hints() on these signatures resolves them — the F821
# unresolved-name finding (OI-288) was hiding a get_type_hints NameError.
# No import cycle: dispatch_plan -> dispatch_spec and dispatch_internal
# are stdlib-only leaf modules.
from dispatch_internal import ExecutionPermit  # noqa: E402
from dispatch_plan import ExecutionPlan  # noqa: E402

# Runtime import (not TYPE_CHECKING): typing.get_type_hints() on functions
# defined here (e.g. run_envelope_plan) resolves against THIS module's
# __globals__, so these names must be bound at runtime, not just for
# static type checkers.
from envelope_types import (  # noqa: E402
    EnvelopeGovernError,
    EnvelopeResult,
    EnvelopeSpec,
    _AdapterResult,
)
from envelope_prepare import (  # noqa: E402
    _prepare,
    _record_integrity,
    _verify_role_application,
)
from envelope_govern_support import (  # noqa: E402
    _archive_dispatch_events,
    _clear_dispatch_events,
    _receipt_exists_for_dispatch,
    _resolve_fix_forward_diff,
    _resolve_phantom_diff,
    _unknown_verification,
    _verification_from_report,
)
from envelope_govern import _govern  # noqa: E402
from envelope_adapters_claude import (  # noqa: E402
    ClaudeSubprocessAdapter,
    CodexAdapter,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adapters -- CodexAdapter, ClaudeSubprocessAdapter moved to
# envelope_adapters_claude.py (dispatch-monolith-split, PR-5 of 6); imported
# above at bare name so every existing dispatch_envelope.CodexAdapter /
# dispatch_envelope.ClaudeSubprocessAdapter-shaped coupling keeps binding.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Lane router
# ---------------------------------------------------------------------------

_LANE_REGISTRY: Dict[str, type] = {
    "codex": CodexAdapter,
    "claude-subprocess": ClaudeSubprocessAdapter,
}


class LaneRouter:
    """Maps a lane name to an adapter instance."""

    def get(self, lane: str) -> object:
        cls = _LANE_REGISTRY.get(lane)
        if cls is None:
            raise ValueError(f"LaneRouter: no adapter registered for lane={lane!r}")
        return cls()


# ---------------------------------------------------------------------------
# PREPARE — _prepare, _record_integrity, _verify_role_application moved to
# envelope_prepare.py (dispatch-monolith-split, PR-2 of 6); imported above
# at bare name so every existing dispatch_envelope._prepare-shaped coupling
# keeps binding.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GOVERN support -- _receipt_exists_for_dispatch, _resolve_fix_forward_diff,
# _resolve_phantom_diff, _unknown_verification, _verification_from_report,
# _archive_dispatch_events, _clear_dispatch_events moved to
# envelope_govern_support.py (dispatch-monolith-split, PR-3 of 6); imported
# above at bare name so every existing dispatch_envelope._name-shaped
# coupling keeps binding.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GOVERN -- _govern moved to envelope_govern.py (dispatch-monolith-split,
# PR-4 of 6); imported above at bare name so every existing
# dispatch_envelope._govern-shaped coupling keeps binding. Every call site
# below (run_envelope, run_envelope_plan, run_envelope_headless_plan) stays
# in this facade, so patch("dispatch_envelope._govern") keeps binding
# against THIS module's globals unchanged by the move.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Envelope entry point
# ---------------------------------------------------------------------------


def run_envelope(spec: EnvelopeSpec, lane: str = "codex") -> EnvelopeResult:
    """Run PREPARE -> ROUTE -> EXECUTE -> GOVERN for the given lane.

    Returns EnvelopeResult on success / failure / timeout.
    Raises EnvelopeGovernError when GOVERN cannot confirm a receipt (fail-closed).
    """
    # ROUTE
    router = LaneRouter()
    adapter = router.get(lane)

    # PREPARE — enrich instruction (best-effort; failure falls back to original)
    enriched_instruction = _prepare(spec)
    enriched_spec = EnvelopeSpec(
        dispatch_id=spec.dispatch_id,
        terminal_id=spec.terminal_id,
        provider=spec.provider,
        model=spec.model,
        instruction=enriched_instruction,
        role=spec.role,
        pr_id=spec.pr_id,
        state_dir=spec.state_dir,
        data_dir=spec.data_dir,
        deadline_seconds=spec.deadline_seconds,
        # Chain-link — carried through PREPARE unchanged.
        parent_dispatch=spec.parent_dispatch,
        task_class=spec.task_class,
        tier_from=spec.tier_from,
        tier_to=spec.tier_to,
    )

    # INTEGRITY — persist the enriched final prompt + verify raw+injections reconstruct it
    integrity = _record_integrity(enriched_spec, enriched_instruction, spec.instruction)

    # EXECUTE
    start_time = datetime.now(timezone.utc)
    adapter_result = adapter.run(enriched_spec)
    end_time = datetime.now(timezone.utc)

    # GOVERN (fail-closed on receipt)
    report_path, receipt_path = _govern(
        enriched_spec, adapter_result, start_time, end_time, integrity=integrity
    )

    returncode = 0 if adapter_result.status == "success" else 1
    return EnvelopeResult(
        status=adapter_result.status,
        returncode=returncode,
        report_path=report_path,
        receipt_path=receipt_path,
        completion_text=adapter_result.completion_text,
        error=adapter_result.error,
    )


# ---------------------------------------------------------------------------
# PR-3: ProviderAdapter + plan-based provider execution
# ---------------------------------------------------------------------------


def _fail_loud_on_empty_success(
    status: str,
    returncode: int,
    completion_text: str,
    provider_str: str,
    raw_result: Any,
) -> tuple:
    """Refuse to report a "success" with a blank completion — surface it instead.

    A provider spawn can return returncode=0 with an empty completion_text when
    its own CLI/output-format changed underneath it (the exact silent-empty-
    completion vector this guard exists to catch — see dispatch instruction
    20260710-211102-envelope-provider-lane-empty-completion). Not every spawn_*
    does its own empty-extraction check (kimi_spawn does; the harness/litellm/
    codex/gemini spawns do not), so this backstop lives once, centrally, here —
    the ONE place every provider-lane result passes through before GOVERN.

    Returns (status, returncode, error) — unchanged unless status=="success"
    and completion_text is blank, in which case status is downgraded to
    "failure", returncode is forced non-zero, and error carries the raw spawn
    result (repr'd) so the failure is diagnosable instead of a silent blank
    report + receipt.
    """
    if status == "success" and not (completion_text or "").strip():
        return (
            "failure",
            returncode or 1,
            (
                f"provider={provider_str} returned an empty completion with "
                f"returncode={returncode} — refusing to report a silent empty "
                f"success (raw spawn result: {raw_result!r})"
            ),
        )
    if status != "success" and not (completion_text or "").strip():
        # OI-925 (restant van OI-866): a NON-success spawn with a blank
        # completion and no captured error previously surfaced in the
        # dispatch_cli log line as "(no error captured)" even though the
        # receipt classified it as empty_completion. Give the operator a
        # diagnosable message instead of a silent blank. (Deepseek-harness:
        # a returncode!=0 spawn with empty text and error=None lands here —
        # _coerce_empty_completion_to_retryable only rewrites the rc=0 case.)
        return (
            status,
            returncode or 1,
            (
                f"provider={provider_str} returned an empty completion with "
                f"returncode={returncode} — no error captured by spawn"
            ),
        )
    return status, returncode, None


def _map_generic_spawn_result(
    result: Any, provider_str: str, model: Optional[str] = None
) -> _AdapterResult:
    """Map codex/kimi/gemini/litellm:* spawn result to _AdapterResult.

    Normalises token fields via _extract_token_usage from provider_dispatch.
    Handles error, timeout, and success/failure by returncode. Does NOT handle
    the deepseek-harness stopped_early or local-gemma error-condition variant —
    those are handled inline in ProviderAdapter.run().
    """
    from provider_dispatch import _extract_token_usage  # noqa: PLC0415

    token_usage: Dict[str, int] = _extract_token_usage(result, provider_str)
    ewf = getattr(result, "event_writer_failures", 0)

    if result.error:
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status="failure",
            token_usage=token_usage,
            error=result.error,
            event_writer_failures=ewf,
            model=model,
        )
    if result.timed_out:
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status="timeout",
            token_usage=token_usage,
            timed_out=True,
            event_writer_failures=ewf,
            model=model,
        )
    completion_text = result.completion_text or ""
    status = "success" if result.returncode == 0 else "failure"
    status, returncode, guard_error = _fail_loud_on_empty_success(
        status, result.returncode, completion_text, provider_str, result
    )
    return _AdapterResult(
        returncode=returncode,
        completion_text=completion_text,
        status=status,
        token_usage=token_usage,
        error=guard_error,
        event_writer_failures=ewf,
        model=model,
    )


class ProviderAdapter:
    """Routes provider-lane ExecutionPlan to the correct spawn function.

    Mirrors CodexAdapter's shape but routes by plan.provider to the correct
    spawn function. Reuses existing resolution helpers and raw spawn functions
    from provider_dispatch and provider_spawns.*. Does NOT call the governing
    _dispatch_* wrappers in provider_dispatch — those govern; the envelope
    governs once via _govern.

    Supported plan.provider values: codex, kimi, gemini, litellm:deepseek,
    litellm:zai, litellm:moonshot, deepseek-harness, local-gemma.
    Provider.CLAUDE and Provider.AUTO are programming errors → ValueError.

    event_writer is not wired in PR-3 (EventStore setup is an open item for a
    later PR); the audit gap is documented in Open Items.
    """

    def run(
        self,
        plan: "ExecutionPlan",
        instruction: str,
        *,
        event_writer: Optional[Callable] = None,
        cwd: Optional[Path] = None,
    ) -> _AdapterResult:
        from dispatch_spec import Provider  # noqa: PLC0415
        from provider_dispatch import (  # noqa: PLC0415
            _MLX_MODEL_MAP,
            _build_lane_key,
            _extract_token_usage,
            _resolve_codex_model,
            _resolve_deepseek_model,
            _resolve_moonshot_model,
            _resolve_zai_model,
        )

        pv: "Provider" = plan.provider  # type: ignore[assignment]

        # ---- codex ----
        if pv == Provider.CODEX:
            from provider_spawns.codex_spawn import spawn_codex  # noqa: PLC0415

            model = (
                plan.model if plan.model not in ("default", "") else _resolve_codex_model()
            )
            try:
                result = spawn_codex(
                    prompt=instruction,
                    model=model,
                    dispatch_id=plan.dispatch_id,
                    terminal_id=plan.target_id,
                    event_writer=event_writer,
                    cwd=cwd,
                )
            except BrokenPipeError as exc:
                return _AdapterResult(
                    returncode=1, completion_text="", status="failure",
                    error=f"codex spawn BrokenPipeError: {exc}",
                )
            return _map_generic_spawn_result(result, pv.value, model=model)

        # ---- kimi ----
        if pv == Provider.KIMI:
            return self._run_kimi(plan, instruction, event_writer=event_writer, cwd=cwd)

        # ---- gemini ----
        if pv == Provider.GEMINI:
            from provider_spawns.gemini_spawn import spawn_gemini  # noqa: PLC0415

            model = (
                plan.model
                if plan.model not in ("default", "sonnet", "")
                else os.environ.get("VNX_GEMINI_MODEL", "gemini-2.5-pro")
            )
            try:
                result = spawn_gemini(
                    prompt=instruction,
                    model=model,
                    dispatch_id=plan.dispatch_id,
                    terminal_id=plan.target_id,
                    event_writer=event_writer,
                    cwd=cwd,
                )
            except BrokenPipeError as exc:
                return _AdapterResult(
                    returncode=1, completion_text="", status="failure",
                    error=f"gemini spawn BrokenPipeError: {exc}",
                )
            return _map_generic_spawn_result(result, pv.value, model=model)

        # ---- litellm:deepseek | litellm:zai | litellm:moonshot ----
        if pv in (Provider.LITELLM_DEEPSEEK, Provider.LITELLM_ZAI, Provider.LITELLM_MOONSHOT):
            from provider_spawns.litellm_spawn import spawn_litellm  # noqa: PLC0415

            # Extract sub-provider from the enum value: "litellm:deepseek" -> "deepseek"
            base_sub = pv.value.split(":", 1)[1]
            if plan.model not in ("default", ""):
                model = plan.model
            elif pv == Provider.LITELLM_DEEPSEEK:
                model = _resolve_deepseek_model()
            elif pv == Provider.LITELLM_ZAI:
                model = _resolve_zai_model()
            else:
                model = _resolve_moonshot_model()
            lane_key = _build_lane_key(base_sub, None)
            try:
                result = spawn_litellm(
                    prompt=instruction,
                    model=model,
                    dispatch_id=plan.dispatch_id,
                    terminal_id=plan.target_id,
                    event_writer=event_writer,
                    sub_provider=base_sub,
                    lane=lane_key,
                    cwd=cwd,
                )
            except BrokenPipeError as exc:
                return _AdapterResult(
                    returncode=1, completion_text="", status="failure",
                    error=f"litellm spawn BrokenPipeError: {exc}",
                )
            return _map_generic_spawn_result(result, pv.value, model=model)

        # ---- deepseek-harness ----
        if pv == Provider.DEEPSEEK_HARNESS:
            from provider_spawns.deepseek_harness_spawn import (  # noqa: PLC0415
                resolve_harness_model,
                spawn_deepseek_harness,
            )

            raw_model = (
                plan.model if plan.model not in ("default", "sonnet", "") else None
            )
            model = resolve_harness_model(raw_model)
            try:
                result = spawn_deepseek_harness(
                    prompt=instruction,
                    model=model,
                    dispatch_id=plan.dispatch_id,
                    terminal_id=plan.target_id,
                    event_writer=event_writer,
                    cwd=cwd,
                    total_deadline=float(plan.deadline_seconds),
                )
            except BrokenPipeError as exc:
                return _AdapterResult(
                    returncode=1, completion_text="", status="failure",
                    error=f"deepseek-harness spawn BrokenPipeError: {exc}",
                )
            token_usage: Dict[str, int] = _extract_token_usage(result, pv.value)
            if result.error:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="failure",
                    token_usage=token_usage,
                    error=result.error,
                    model=model,
                )
            if result.timed_out:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="timeout",
                    token_usage=token_usage,
                    timed_out=True,
                    model=model,
                )
            if getattr(result, "stopped_early", False):
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="success",
                    token_usage=token_usage,
                    model=model,
                )
            _dh_text = result.completion_text or ""
            status = "success" if result.returncode == 0 else "failure"
            status, _dh_rc, _dh_err = _fail_loud_on_empty_success(
                status, result.returncode, _dh_text, pv.value, result
            )
            return _AdapterResult(
                returncode=_dh_rc,
                completion_text=_dh_text,
                status=status,
                token_usage=token_usage,
                error=_dh_err,
                model=model,
            )

        # ---- glm-harness ---- (codex flip-PR F2: the door normalizes GLM -> glm-harness, so the
        # provider envelope MUST be able to execute it, not raise unsupported.)
        if pv == Provider.GLM_HARNESS:
            from provider_spawns.glm_harness_spawn import (  # noqa: PLC0415
                resolve_harness_model,
                spawn_glm_harness,
            )

            raw_model = (
                plan.model if plan.model not in ("default", "sonnet", "") else None
            )
            model = resolve_harness_model(raw_model)
            try:
                result = spawn_glm_harness(
                    prompt=instruction,
                    model=model,
                    dispatch_id=plan.dispatch_id,
                    terminal_id=plan.target_id,
                    event_writer=event_writer,
                    cwd=cwd,
                    total_deadline=float(plan.deadline_seconds),
                )
            except BrokenPipeError as exc:
                return _AdapterResult(
                    returncode=1, completion_text="", status="failure",
                    error=f"glm-harness spawn BrokenPipeError: {exc}",
                )
            token_usage = _extract_token_usage(result, pv.value)
            if result.error:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="failure",
                    token_usage=token_usage,
                    error=result.error,
                    model=model,
                )
            if result.timed_out:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="timeout",
                    token_usage=token_usage,
                    timed_out=True,
                    model=model,
                )
            if getattr(result, "stopped_early", False):
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="success",
                    token_usage=token_usage,
                    model=model,
                )
            _gh_text = result.completion_text or ""
            status = "success" if result.returncode == 0 else "failure"
            status, _gh_rc, _gh_err = _fail_loud_on_empty_success(
                status, result.returncode, _gh_text, pv.value, result
            )
            return _AdapterResult(
                returncode=_gh_rc,
                completion_text=_gh_text,
                status=status,
                token_usage=token_usage,
                error=_gh_err,
                model=model,
            )

        # ---- local-gemma ----
        if pv == Provider.LOCAL_GEMMA:
            from provider_spawns.local_gemma_spawn import spawn_local_gemma  # noqa: PLC0415

            raw_model = (
                plan.model if plan.model not in ("default", "sonnet", "") else "gemma-4b-local"
            )
            canonical_model = _MLX_MODEL_MAP.get(raw_model, raw_model)
            result = spawn_local_gemma(
                instruction=instruction,
                model=canonical_model,
                role=None,
                deadline_seconds=300,
                dispatch_id=plan.dispatch_id,
                project_id="vnx-dev",
            )
            token_usage = _extract_token_usage(result, pv.value)
            if result.error and result.returncode != 0:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="failure",
                    token_usage=token_usage,
                    error=result.error,
                    model=canonical_model,
                )
            if result.timed_out:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="timeout",
                    token_usage=token_usage,
                    timed_out=True,
                    model=canonical_model,
                )
            _lg_text = result.completion_text or ""
            status = "success" if result.returncode == 0 else "failure"
            status, _lg_rc, _lg_err = _fail_loud_on_empty_success(
                status, result.returncode, _lg_text, pv.value, result
            )
            return _AdapterResult(
                returncode=_lg_rc,
                completion_text=_lg_text,
                status=status,
                token_usage=token_usage,
                error=_lg_err,
                model=canonical_model,
            )

        # Provider.CLAUDE, Provider.AUTO, or any unexpected value — programming error
        raise ValueError(
            f"ProviderAdapter: unsupported provider {pv!r} — "
            f"claude and auto do not route through the provider envelope "
            f"(claude_tmux_subscription is executed by the tmux lane, wired in PR-4)"
        )

    def _run_kimi(
        self,
        plan: "ExecutionPlan",
        instruction: str,
        *,
        event_writer: Optional[Callable] = None,
        cwd: Optional[Path] = None,
    ) -> _AdapterResult:
        """Kimi spawn branch, extracted from run() (OI-709 function-size gate).

        Pure extraction — behavior identical to the former inline kimi branch.
        """
        from dispatch_spec import Provider  # noqa: PLC0415
        from provider_spawns.kimi_spawn import spawn_kimi  # noqa: PLC0415
        from provider_dispatch import (  # noqa: PLC0415
            KimiModelResolutionError,
            _kimi_resolve_cli_model_arg,
            _kimi_resolve_requested_key,
        )

        # Shared resolver (20260721-kimi-lane-hardening): args.model/plan.model >
        # VNX_KIMI_MODEL > registry K3 default; bare provider/alias tokens ("kimi",
        # "kimi-default") normalize to the default; an unmapped/stale model_key RAISES
        # instead of ever being passed raw to `-m` or silently substituted with K3.
        try:
            model = _kimi_resolve_cli_model_arg(_kimi_resolve_requested_key(plan.model))
        except KimiModelResolutionError as exc:
            return _AdapterResult(
                returncode=1, completion_text="", status="failure",
                error=f"kimi model resolution failed: {exc}",
            )
        try:
            result = spawn_kimi(
                prompt=instruction,
                model=model,
                dispatch_id=plan.dispatch_id,
                terminal_id=plan.target_id,
                event_writer=event_writer,
                cwd=cwd,
                # worker-provider-kimi-flip (20260723): honor the spec's staged deadline
                # instead of spawn_kimi's own hardcoded 900s default — a caller staging a
                # longer deadline (e.g. stage_spec_bundle's 3600s default) was previously
                # silently capped short (memory: provider-lane-900s-deadline-kills-builds).
                total_deadline=float(plan.deadline_seconds),
            )
        except BrokenPipeError as exc:
            return _AdapterResult(
                returncode=1, completion_text="", status="failure",
                error=f"kimi spawn BrokenPipeError: {exc}",
            )
        return _map_generic_spawn_result(result, Provider.KIMI.value, model=model)


def run_envelope_plan(
    plan: "ExecutionPlan",
    permit: "ExecutionPermit",
    *,
    state_dir: Path,
    data_dir: Path,
) -> EnvelopeResult:
    """Execute a validated ExecutionPlan for the provider lane.

    Provider lane covers codex, kimi, gemini, litellm:*, deepseek-harness,
    local-gemma. The claude_tmux_subscription lane is wired separately in PR-4.

    require_permit is the first action — un-evadable and cannot be moved.

    Raises:
        PermissionError: permit was not issued by issue_permit for this plan.
        ValueError: plan.lane is not "provider".
        EnvelopeGovernError: GOVERN cannot confirm receipt (fail-closed).
    """
    from dispatch_internal import is_valid_instruction_hash, require_permit  # noqa: PLC0415

    require_permit(plan, permit)  # un-evadable backstop — FIRST action

    if plan.lane != "provider":
        raise ValueError(
            f"run_envelope_plan handles the provider lane only; got lane={plan.lane!r} "
            f"(claude_tmux_subscription is executed by the tmux lane, wired in PR-4)"
        )

    # P0-3 (PR-4c): REQUIRE a valid 64-hex plan hash before delivery — fail-CLOSED.
    # The old `if plan.instruction_sha256:` guard fell OPEN on an empty hash, letting
    # an empty-hash plan + valid permit spawn mutated content. No hash → no spawn.
    if not is_valid_instruction_hash(plan.instruction_sha256):
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=None,
            receipt_path=None,
            completion_text="",
            error=(
                f"plan.instruction_sha256 is not a valid 64-hex digest "
                f"(got {plan.instruction_sha256!r}); refusing to deliver (fail-closed)"
            ),
        )

    # TOCTOU verification — re-read and verify sha256 before delivering
    instruction = Path(plan.instruction_file).read_text(encoding="utf-8")
    actual = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    if actual != plan.instruction_sha256:
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=None,
            receipt_path=None,
            completion_text="",
            error=(
                f"instruction file mutated after permit: sha256 mismatch "
                f"(expected {plan.instruction_sha256[:12]}…, got {actual[:12]}…)"
            ),
        )

    spec = EnvelopeSpec(
        dispatch_id=plan.dispatch_id,
        terminal_id=plan.target_id,
        provider=plan.provider.value,
        model=plan.model,
        instruction=instruction,
        role=plan.role,  # F2 (codex): carry the role so the phantom-guard review-exemption applies
        pr_id=plan.pr_id,
        state_dir=state_dir,
        data_dir=data_dir,
        deadline_seconds=plan.deadline_seconds,
        # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): from the plan,
        # which the door computed from the spec.
        parent_dispatch=plan.parent_dispatch,
        task_class=plan.task_class,
        tier_from=plan.tier_from,
        tier_to=plan.tier_to,
    )

    enriched_instruction = _prepare(spec)
    enriched_spec = EnvelopeSpec(
        dispatch_id=spec.dispatch_id,
        terminal_id=spec.terminal_id,
        provider=spec.provider,
        model=spec.model,
        instruction=enriched_instruction,
        role=spec.role,
        pr_id=spec.pr_id,
        state_dir=spec.state_dir,
        data_dir=spec.data_dir,
        deadline_seconds=spec.deadline_seconds,
        parent_dispatch=spec.parent_dispatch,
        task_class=spec.task_class,
        tier_from=spec.tier_from,
        tier_to=spec.tier_to,
    )

    # INTEGRITY — the bundle dir is the pending/<id>/ that hosts instruction.md.
    integrity = _record_integrity(
        enriched_spec,
        enriched_instruction,
        instruction,
        bundle_dir=Path(plan.instruction_file).parent,
    )

    from dispatch_worktree_isolation import (  # noqa: PLC0415
        create_dispatch_worktree,
        remove_dispatch_worktree,
        resolve_consumer_project_root,
    )

    wt_path: Optional[Path] = None
    try:
        # Resolve the CONSUMER project root explicitly (VNX_PROJECT_ROOT / CWD-git,
        # never __file__) so a central-install consumer (SC/MC/SEO/...) gets its
        # worktree under ITS OWN project — not the shared ~/.vnx-system checkout
        # this lane code lives under in a central install (P0 provider-worktree-
        # root-fix). Resolved once and reused for both create and remove below so
        # a fluctuating ambient CWD can't split the two onto different roots.
        # Any resolution failure is handled by the same fail-loud path below as
        # a worktree-creation failure — never falls back to a shared checkout.
        _consumer_project_root = resolve_consumer_project_root()
        wt_path = create_dispatch_worktree(plan.dispatch_id, project_root=_consumer_project_root)
    except Exception as _wt_exc:
        _isolation_error = (
            f"isolation required (require_worktree) but worktree creation failed "
            f"for {plan.dispatch_id}: {_wt_exc} — aborting; no shared-checkout fallback"
        )
        logger.error("run_envelope_plan: %s", _isolation_error)
        _fail_result = _AdapterResult(
            returncode=1,
            completion_text="",
            status="failure",
            error=_isolation_error,
        )
        _fail_start = _fail_end = datetime.now(timezone.utc)
        report_path, receipt_path = _govern(
            enriched_spec, _fail_result, _fail_start, _fail_end, integrity=integrity
        )
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=report_path,
            receipt_path=receipt_path,
            completion_text="",
            error=_isolation_error,
        )

    _phantom_diff: Optional[str] = None
    try:
        start = datetime.now(timezone.utc)
        result = ProviderAdapter().run(plan, enriched_spec.instruction, cwd=wt_path)
        end = datetime.now(timezone.utc)
        # F1 (codex): capture the worker's diff BEFORE the teardown below —
        # remove_dispatch_worktree deletes both the worktree and the local dispatch/<id>
        # branch, so the phantom-guard inside _govern could not otherwise resolve the
        # provider lane's diff and would abstain, letting the exact kimi/glm/deepseek
        # phantom slip through.
        # Prefer the pushed branch (survives teardown → more durable evidence) over the
        # live worktree diff (ephemeral, torn down in the finally block). When the worker
        # pushed its branch, the branch diff is the more reliable evidence.
        try:
            _phantom_diff = _resolve_phantom_diff(
                plan.dispatch_id,
                base_ref=plan.base_ref or "origin/main",
                wt_path=wt_path,
                repo=_consumer_project_root,
            )
        except Exception:  # noqa: BLE001 — best-effort; None -> guard abstains, never false-rejects
            _phantom_diff = None
    finally:
        remove_dispatch_worktree(plan.dispatch_id, project_root=_consumer_project_root)

    report_path, receipt_path = _govern(
        enriched_spec, result, start, end, phantom_diff=_phantom_diff, integrity=integrity,
        base_ref=plan.base_ref or "origin/main",
    )

    return EnvelopeResult(
        status=result.status,
        returncode=0 if result.status == "success" else 1,
        report_path=report_path,
        receipt_path=receipt_path,
        completion_text=result.completion_text,
        error=result.error,
    )


def run_envelope_headless_plan(
    plan: "ExecutionPlan",
    permit: "ExecutionPermit",
    *,
    state_dir: Path,
    data_dir: Path,
    role: Optional[str] = None,
) -> EnvelopeResult:
    """Execute a validated ExecutionPlan for the claude_headless lane (api_metered billing).

    Headless lane routes to ClaudeSubprocessAdapter (spawn_claude, claude -p) with the
    same require_permit + instruction-sha256 TOCTOU verify + fail-closed GOVERN as the
    provider lane. ClaudeSubprocessAdapter is reused — not reimplemented.

    Raises:
        PermissionError: permit was not issued by issue_permit for this plan.
        ValueError: plan.lane is not "claude_headless".
        EnvelopeGovernError: GOVERN cannot confirm receipt (fail-closed).
    """
    from dispatch_internal import is_valid_instruction_hash, require_permit  # noqa: PLC0415

    require_permit(plan, permit)  # un-evadable backstop — FIRST action

    if plan.lane != "claude_headless":
        raise ValueError(
            f"run_envelope_headless_plan handles lane='claude_headless' only; "
            f"got lane={plan.lane!r}"
        )

    # P0-3: REQUIRE a valid 64-hex plan hash before delivery — fail-CLOSED.
    if not is_valid_instruction_hash(plan.instruction_sha256):
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=None,
            receipt_path=None,
            completion_text="",
            error=(
                f"plan.instruction_sha256 is not a valid 64-hex digest "
                f"(got {plan.instruction_sha256!r}); refusing to deliver (fail-closed)"
            ),
        )

    # TOCTOU verification — re-read and verify sha256 before delivering
    instruction = Path(plan.instruction_file).read_text(encoding="utf-8")
    actual = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    if actual != plan.instruction_sha256:
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=None,
            receipt_path=None,
            completion_text="",
            error=(
                f"instruction file mutated after permit: sha256 mismatch "
                f"(expected {plan.instruction_sha256[:12]}…, got {actual[:12]}…)"
            ),
        )

    spec = EnvelopeSpec(
        dispatch_id=plan.dispatch_id,
        terminal_id=plan.target_id,
        provider="claude",
        model=plan.model,
        instruction=instruction,
        role=role,
        pr_id=plan.pr_id,
        state_dir=state_dir,
        data_dir=data_dir,
        deadline_seconds=plan.deadline_seconds,
        # Chain-link (OI-985): same four fields as run_envelope_plan —
        # without these, every claude_headless dispatch drops the chain.
        parent_dispatch=plan.parent_dispatch,
        task_class=plan.task_class,
        tier_from=plan.tier_from,
        tier_to=plan.tier_to,
    )

    enriched_instruction = _prepare(spec)
    enriched_spec = EnvelopeSpec(
        dispatch_id=spec.dispatch_id,
        terminal_id=spec.terminal_id,
        provider=spec.provider,
        model=spec.model,
        instruction=enriched_instruction,
        role=spec.role,
        pr_id=spec.pr_id,
        state_dir=spec.state_dir,
        data_dir=spec.data_dir,
        deadline_seconds=spec.deadline_seconds,
        parent_dispatch=spec.parent_dispatch,
        task_class=spec.task_class,
        tier_from=spec.tier_from,
        tier_to=spec.tier_to,
    )

    # INTEGRITY — persist the enriched final prompt + verify reconstruction
    integrity = _record_integrity(
        enriched_spec,
        enriched_instruction,
        instruction,
        bundle_dir=Path(plan.instruction_file).parent,
    )

    start = datetime.now(timezone.utc)
    result = ClaudeSubprocessAdapter().run(enriched_spec)
    end = datetime.now(timezone.utc)

    report_path, receipt_path = _govern(enriched_spec, result, start, end, integrity=integrity)

    return EnvelopeResult(
        status=result.status,
        returncode=0 if result.status == "success" else 1,
        report_path=report_path,
        receipt_path=receipt_path,
        completion_text=result.completion_text,
        error=result.error,
    )
