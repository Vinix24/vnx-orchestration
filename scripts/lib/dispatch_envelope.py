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

import logging
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
    phase.  completion_text is left empty (matching legacy _dispatch_claude
    behavior) because the worker writes its own report via _ensure_unified_report;
    the envelope report is a governance wrapper, not the worker's full output.

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
                completion_text="",
                status="failure",
                token_usage=token_usage,
                error=result.error,
            )
        if result.timed_out:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text="",
                status="timeout",
                token_usage=token_usage,
                timed_out=True,
            )
        if result.stopped_early:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text="",
                status="success",
                token_usage=token_usage,
            )
        status = "success" if result.returncode == 0 else "failure"
        return _AdapterResult(
            returncode=result.returncode,
            completion_text="",
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
        pass
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
    except OSError:
        pass
    return False


def _govern(
    spec: EnvelopeSpec,
    adapter_result: _AdapterResult,
    start_time: datetime,
    end_time: datetime,
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
