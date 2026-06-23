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
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class EnvelopeSpec:
    """Normalized dispatch parameters passed through PREPARE -> ROUTE -> EXECUTE -> GOVERN."""

    dispatch_id: str
    terminal_id: str
    provider: str
    model: str
    instruction: str
    role: Optional[str]
    pr_id: Optional[str]
    state_dir: Path
    data_dir: Path


@dataclass
class EnvelopeResult:
    """Outcome from a complete envelope run."""

    status: str           # "success" | "failure" | "timeout"
    returncode: int
    report_path: Optional[Path]
    receipt_path: Optional[Path]
    completion_text: str = ""
    error: Optional[str] = None


class EnvelopeGovernError(RuntimeError):
    """Raised when GOVERN cannot emit or confirm a receipt (fail-closed contract)."""


# ---------------------------------------------------------------------------
# Internal adapter result
# ---------------------------------------------------------------------------


@dataclass
class _AdapterResult:
    returncode: int
    completion_text: str
    status: str           # "success" | "failure" | "timeout"
    token_usage: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None
    timed_out: bool = False
    event_writer_failures: int = 0


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class CodexAdapter:
    """Wraps spawn_codex — reuses existing spawn without reimplementation.

    event_writer is not wired in PR-1 (no EventStore setup here); the audit
    stream gap is documented in Open Items and closed in a later PR.
    """

    def run(
        self,
        spec: EnvelopeSpec,
        event_writer: Optional[Callable] = None,
        cwd: Optional[Path] = None,
    ) -> _AdapterResult:
        from provider_spawns.codex_spawn import spawn_codex  # noqa: PLC0415

        try:
            result = spawn_codex(
                prompt=spec.instruction,
                model=spec.model,
                dispatch_id=spec.dispatch_id,
                terminal_id=spec.terminal_id,
                event_writer=event_writer,
                cwd=cwd,
            )
        except BrokenPipeError as exc:
            return _AdapterResult(
                returncode=1,
                completion_text="",
                status="failure",
                error=f"codex spawn BrokenPipeError: {exc}",
            )

        token_usage: Dict[str, int] = {}
        raw_usage = getattr(result, "token_usage", None)
        if isinstance(raw_usage, dict):
            token_usage = {
                "input": int(
                    raw_usage.get("input_tokens", raw_usage.get("input", 0)) or 0
                ),
                "output": int(
                    raw_usage.get("output_tokens", raw_usage.get("output", 0)) or 0
                ),
                "cache_hit": int(
                    raw_usage.get(
                        "cache_read_tokens", raw_usage.get("cache_hit", 0)
                    ) or 0
                ),
            }

        if result.error:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="failure",
                token_usage=token_usage,
                error=result.error,
                event_writer_failures=result.event_writer_failures,
            )
        if result.timed_out:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="timeout",
                token_usage=token_usage,
                timed_out=True,
                event_writer_failures=result.event_writer_failures,
            )
        status = "success" if result.returncode == 0 else "failure"
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status=status,
            token_usage=token_usage,
            event_writer_failures=result.event_writer_failures,
        )


class ClaudeSubprocessAdapter:
    """Wraps spawn_claude — reuses existing spawn without reimplementation.

    Maps ClaudeSpawnResult fields to _AdapterResult for the envelope GOVERN
    phase.  Worker reports remain the primary audit artifact; completion_text
    is now also captured from spawn_claude for benchmark and utility callers
    that need the model's raw text output.

    event_writer is not wired in PR-2 (SubprocessAdapter handles EventStore
    internally); the audit stream gap is documented in Open Items.
    """

    def run(
        self,
        spec: EnvelopeSpec,
        event_writer: Optional[Callable] = None,
        cwd: Optional[Path] = None,
    ) -> _AdapterResult:
        from provider_spawns.claude_spawn import spawn_claude  # noqa: PLC0415

        try:
            result = spawn_claude(
                prompt=spec.instruction,
                model=spec.model,
                dispatch_id=spec.dispatch_id,
                terminal_id=spec.terminal_id,
                event_writer=event_writer,
                cwd=cwd,
                role=spec.role,
            )
        except BrokenPipeError as exc:
            return _AdapterResult(
                returncode=1,
                completion_text="",
                status="failure",
                error=f"claude spawn BrokenPipeError: {exc}",
            )

        token_usage: Dict[str, int] = {}
        raw_usage = result.token_usage
        if isinstance(raw_usage, dict) and raw_usage:
            token_usage = {
                "input": int(raw_usage.get("input_tokens", 0) or 0),
                "output": int(raw_usage.get("output_tokens", 0) or 0),
                "cache_hit": int(
                    raw_usage.get("cache_read_input_tokens", 0) or 0
                ),
            }

        if result.error:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="failure",
                token_usage=token_usage,
                error=result.error,
            )
        if result.timed_out:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="timeout",
                token_usage=token_usage,
                timed_out=True,
            )
        if result.stopped_early:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="success",
                token_usage=token_usage,
            )
        status = "success" if result.returncode == 0 else "failure"
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status=status,
            token_usage=token_usage,
        )


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
# PREPARE
# ---------------------------------------------------------------------------


def _prepare(spec: EnvelopeSpec) -> str:
    """Enrich instruction with intelligence context and repo map (best-effort).

    Mirrors _enrich_instruction in provider_dispatch.py but operates on EnvelopeSpec.
    Both layers fall back silently to the original instruction on any failure.
    """
    instruction = spec.instruction

    try:
        from intelligence_injection import build_intelligence_section  # noqa: PLC0415

        instruction = build_intelligence_section(
            instruction=instruction,
            dispatch_id=spec.dispatch_id,
            role=spec.role,
            state_dir=spec.state_dir,
            pr_id=spec.pr_id,
            dispatch_paths=None,
        )
    except ImportError:
        logger.debug(
            "envelope._prepare: intelligence_injection not available — skipping"
        )
    except Exception as exc:
        logger.warning(
            "envelope._prepare: intelligence injection failed (%s) — skipping", exc
        )

    try:
        from dispatch_enricher import apply_repo_map_layer  # noqa: PLC0415

        instruction = apply_repo_map_layer(instruction, {"role": spec.role})
    except Exception as exc:
        logger.warning(
            "envelope._prepare: repo map layer failed (%s) — skipping", exc
        )

    return instruction


# ---------------------------------------------------------------------------
# GOVERN
# ---------------------------------------------------------------------------


def _receipt_exists_for_dispatch(receipt_path: Path, dispatch_id: str) -> bool:
    """Check whether the NDJSON receipt file already contains a line for dispatch_id.

    Used for idempotent dedup: when the legacy path (deliver_with_recovery) already
    wrote a receipt for this dispatch, the envelope GOVERN skips its own receipt
    write to avoid double-emit.
    """
    if not receipt_path.exists():
        return False
    target = f'"dispatch_id":"{dispatch_id}"'
    try:
        with open(receipt_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if target in line:
                    return True
    except OSError as exc:
        logger.warning(
            "envelope._receipt_exists_for_dispatch: cannot read receipt ledger %s: %s — "
            "treating as unreadable (fail-closed: will skip emit to avoid double-receipt)",
            receipt_path,
            exc,
        )
        return True
    return False


def _govern(
    spec: EnvelopeSpec,
    adapter_result: _AdapterResult,
    start_time: datetime,
    end_time: datetime,
    phantom_diff: Optional[str] = None,
) -> tuple:
    """Emit unified_report then dispatch receipt. Returns (report_path, receipt_path).

    Fail-closed contract: raises EnvelopeGovernError when receipt_path is None or
    absent on disk after emit — never silently loses a receipt.
    Report is emitted first so the receipt can carry the linkage (ADR-005 ordering).

    Idempotent dedup: when the receipt NDJSON already contains a line for this
    dispatch_id (written by deliver_with_recovery's internal close-out as a safety
    net), the GOVERN receipt write is skipped.  This avoids double-emit during the
    migration period where both legacy and envelope paths may run.
    """
    from governance_emit import emit_dispatch_receipt, emit_unified_report  # noqa: PLC0415

    duration = (end_time - start_time).total_seconds()

    # REPORT first — idempotent: worker-written file is preserved, not overwritten
    report_path: Optional[Path] = None
    try:
        report_path = emit_unified_report(
            dispatch_id=spec.dispatch_id,
            terminal_id=spec.terminal_id,
            provider=spec.provider,
            instruction=spec.instruction,
            response_text=adapter_result.completion_text,
            findings=[],
            duration_seconds=duration,
            data_dir=spec.data_dir,
        )
    except Exception as exc:
        logger.error(
            "envelope._govern: report emit failed dispatch=%s: %s — proceeding to receipt",
            spec.dispatch_id,
            exc,
        )

    # RECEIPT second — fail-closed, with idempotent dedup
    receipt_path: Optional[Path] = None
    ndjson_path = spec.state_dir / "t0_receipts.ndjson"
    if _receipt_exists_for_dispatch(ndjson_path, spec.dispatch_id):
        logger.info(
            "envelope._govern: receipt already exists for dispatch=%s — skipping (idempotent dedup)",
            spec.dispatch_id,
        )
        receipt_path = ndjson_path
    else:
        try:
            receipt_path = emit_dispatch_receipt(
                dispatch_id=spec.dispatch_id,
                terminal_id=spec.terminal_id,
                provider=spec.provider,
                model=spec.model,
                pr_id=spec.pr_id,
                status=adapter_result.status,
                completion_pct=100 if adapter_result.status == "success" else 0,
                risk=0.0,
                findings=[],
                duration_seconds=duration,
                token_usage=adapter_result.token_usage,
                cost_usd=None,
                state_dir=spec.state_dir,
                report_path=str(report_path) if report_path else None,
            )
        except Exception as exc:
            raise EnvelopeGovernError(
                f"envelope._govern: receipt emit raised for dispatch={spec.dispatch_id}: {exc}"
            ) from exc

        if receipt_path is None:
            raise EnvelopeGovernError(
                f"envelope._govern: receipt_path is None after emit "
                f"(fail-closed) dispatch={spec.dispatch_id}"
            )
        if not receipt_path.exists():
            raise EnvelopeGovernError(
                f"envelope._govern: receipt file absent on disk after emit "
                f"path={receipt_path} dispatch={spec.dispatch_id} (fail-closed)"
            )

    logger.info(
        "envelope._govern: dispatch=%s status=%s report=%s receipt=%s",
        spec.dispatch_id,
        adapter_result.status,
        report_path,
        receipt_path,
    )
    # P0.2: inline phantom-guard (provider lanes — the kimi/glm/deepseek text-only fabrication
    # vector). A delivery worker that reports success with no worktree/branch diff is rejected via
    # a corrective failed receipt. worktree_path is unavailable on EnvelopeSpec, so the guard derives
    # the dispatch/<id> branch (isolated dispatches) or abstains (never false-rejects). Non-fatal.
    try:
        from phantom_guard import record_phantom_if_any  # noqa: PLC0415
        _tok = adapter_result.token_usage or {}
        record_phantom_if_any(
            dispatch_id=spec.dispatch_id,
            role=spec.role,
            status=adapter_result.status,
            token_usage=(int(_tok.get("input", 0) or 0) + int(_tok.get("output", 0) or 0)) or None,
            worktree_path=None,
            base_sha=None,
            worktree_diff=phantom_diff,  # F1: pre-captured before the worktree teardown
            receipts_file=str(spec.state_dir / "t0_receipts.ndjson"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "envelope._govern: phantom-guard check failed (non-fatal) dispatch=%s: %s",
            spec.dispatch_id, exc,
        )
    return report_path, receipt_path


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
    )

    # EXECUTE
    start_time = datetime.now(timezone.utc)
    adapter_result = adapter.run(enriched_spec)
    end_time = datetime.now(timezone.utc)

    # GOVERN (fail-closed on receipt)
    report_path, receipt_path = _govern(enriched_spec, adapter_result, start_time, end_time)

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


def _map_generic_spawn_result(result: Any, provider_str: str) -> _AdapterResult:
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
        )
    if result.timed_out:
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status="timeout",
            token_usage=token_usage,
            timed_out=True,
            event_writer_failures=ewf,
        )
    status = "success" if result.returncode == 0 else "failure"
    return _AdapterResult(
        returncode=result.returncode,
        completion_text=(result.completion_text or ""),
        status=status,
        token_usage=token_usage,
        event_writer_failures=ewf,
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
            _resolve_kimi_model_label,
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
            return _map_generic_spawn_result(result, pv.value)

        # ---- kimi ----
        if pv == Provider.KIMI:
            from provider_spawns.kimi_spawn import spawn_kimi  # noqa: PLC0415

            # None signals kimi CLI to use its own default; label used only for logging
            model = plan.model if plan.model not in ("default", "") else None
            try:
                result = spawn_kimi(
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
                    error=f"kimi spawn BrokenPipeError: {exc}",
                )
            return _map_generic_spawn_result(result, pv.value)

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
            return _map_generic_spawn_result(result, pv.value)

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
            return _map_generic_spawn_result(result, pv.value)

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
                )
            if result.timed_out:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="timeout",
                    token_usage=token_usage,
                    timed_out=True,
                )
            if getattr(result, "stopped_early", False):
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="success",
                    token_usage=token_usage,
                )
            status = "success" if result.returncode == 0 else "failure"
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status=status,
                token_usage=token_usage,
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
                )
            if result.timed_out:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="timeout",
                    token_usage=token_usage,
                    timed_out=True,
                )
            status = "success" if result.returncode == 0 else "failure"
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status=status,
                token_usage=token_usage,
            )

        # Provider.CLAUDE, Provider.AUTO, or any unexpected value — programming error
        raise ValueError(
            f"ProviderAdapter: unsupported provider {pv!r} — "
            f"claude and auto do not route through the provider envelope "
            f"(claude_tmux_subscription is executed by the tmux lane, wired in PR-4)"
        )


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
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
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
    )

    from dispatch_worktree_isolation import (  # noqa: PLC0415
        create_dispatch_worktree,
        remove_dispatch_worktree,
    )

    wt_path: Optional[Path] = None
    try:
        wt_path = create_dispatch_worktree(plan.dispatch_id)
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
        report_path, receipt_path = _govern(enriched_spec, _fail_result, _fail_start, _fail_end)
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
        # F1 (codex): capture the worker's diff from the LIVE worktree BEFORE the teardown below —
        # remove_dispatch_worktree deletes both the worktree and the local dispatch/<id> branch, so
        # the phantom-guard inside _govern could not otherwise resolve the provider lane's diff and
        # would abstain, letting the exact kimi/glm/deepseek phantom slip through.
        try:
            from phantom_guard import compute_worktree_diff  # noqa: PLC0415
            # F3 (codex): use the plan's actual base_ref, not a hardcoded origin/main — a seeded /
            # non-main base would make an empty worker run look non-empty and let a phantom pass.
            _phantom_diff = compute_worktree_diff(wt_path, base_ref=plan.base_ref or "origin/main")
        except Exception:  # noqa: BLE001 — best-effort; None -> guard abstains, never false-rejects
            _phantom_diff = None
    finally:
        remove_dispatch_worktree(plan.dispatch_id)

    report_path, receipt_path = _govern(enriched_spec, result, start, end, phantom_diff=_phantom_diff)

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
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
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
    )

    start = datetime.now(timezone.utc)
    result = ClaudeSubprocessAdapter().run(enriched_spec)
    end = datetime.now(timezone.utc)

    report_path, receipt_path = _govern(enriched_spec, result, start, end)

    return EnvelopeResult(
        status=result.status,
        returncode=0 if result.status == "success" else 1,
        report_path=report_path,
        receipt_path=receipt_path,
        completion_text=result.completion_text,
        error=result.error,
    )
