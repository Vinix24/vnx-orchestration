#!/usr/bin/env python3
"""provider_dispatch.py — Provider-agnostic dispatch entry-point (Wave 4.6).

Routes dispatch execution to the appropriate provider spawn handler based on
``--provider``. PR-4.6.1: claude wired. PR-4.6.3: codex wired. PR-4.6.4: gemini wired.
PR-4.6.5: litellm wired (litellm:<sub_provider> format).
All other providers raise SystemExit(64) until their handlers land.

See: claudedocs/wave4.6-provider-dispatch-generalization-design-2026-05-13.md

BILLING SAFETY: this module does NOT import the Anthropic SDK.  Claude dispatch
delegates entirely to ``subprocess_dispatch.py`` which invokes ``claude -p`` via
subprocess only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger(__name__)

_EX_USAGE = 64  # sysexits.h EX_USAGE

# Providers whose spawn handlers exist.
_IMPLEMENTED_PROVIDERS = {"claude", "codex", "gemini", "kimi", "litellm", "deepseek-harness"}

# Mapping: provider literal -> which future PR delivers its handler.
_FUTURE_PR_MAP: dict = {}

# LiteLLM sub-provider defaults when VNX_LITELLM_MODEL is not set.
_LITELLM_SUB_PROVIDER_DEFAULTS: dict = {
    "bedrock": "bedrock/claude-sonnet-4-6",
    "deepseek": "deepseek/deepseek-v4-pro",
    "moonshot": "moonshot/kimi-k2-0905-preview",
    "zai": "openrouter/z-ai/glm-5",
    "ollama": "ollama/llama3",
    "anthropic": "anthropic/claude-sonnet-4-6",
}

# Env vars required per sub-provider (fast-fail before subprocess spawn)
_SUB_PROVIDER_KEY_REQS: dict = {
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "zai": "OPENROUTER_API_KEY",
}

# GLM model names that are LEGACY — rejected on zai dispatch (PR-7.3)
_DEPRECATED_ZAI_MODELS = frozenset({"glm-4.5", "glm-4.6"})

# Default model alias per sub-provider — used to build lane key for contract lookup
_SUB_PROVIDER_DEFAULT_ALIAS: dict = {
    "deepseek": "deepseek-v4-pro",
    "moonshot": "kimi-k2-0905-default",
    "zai": "glm-5.1-default",
}


def _resolve_state_dir() -> Path:
    """Resolve VNX state directory from environment."""
    env = os.environ.get("VNX_STATE_DIR", "")
    if env:
        return Path(env)
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    if data_dir:
        return Path(data_dir) / "state"
    return Path(".vnx-data") / "state"


def _resolve_dispatch_paths(raw: str) -> "list[str] | None":
    """Parse comma-separated dispatch-paths arg into a list, or None when empty."""
    if not (raw or "").strip():
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


def _enrich_instruction(args: argparse.Namespace) -> str:
    """Prepend intelligence context and repo map to instruction for non-Claude provider paths.

    Claude dispatches are enriched inside subprocess_dispatch.deliver_with_recovery
    via skill_injection._build_intelligence_section; this function handles the
    remaining providers (codex, gemini, litellm, kimi).

    Applies two layers (best-effort, each layer falls back silently on failure):
    1. Intelligence injection (existing — ADR context, prior findings, etc.)
    2. Repo-map layer (new — mirrors headless_dispatch_daemon's DispatchEnricher step)

    Returns the original instruction unchanged on any failure.
    """
    # Layer: intelligence injection (existing)
    try:
        from intelligence_injection import build_intelligence_section  # noqa: PLC0415
        enriched = build_intelligence_section(
            instruction=args.instruction,
            dispatch_id=args.dispatch_id,
            role=getattr(args, "role", None),
            state_dir=_resolve_state_dir(),
            pr_id=getattr(args, "pr_id", None),
            dispatch_paths=_resolve_dispatch_paths(getattr(args, "dispatch_paths", "") or ""),
        )
    except ImportError as exc:
        logger.warning("_enrich_instruction: intelligence_injection unavailable (%s)", exc)
        enriched = args.instruction

    # Layer: repo map (new — extends coverage to all providers)
    try:
        from dispatch_enricher import apply_repo_map_layer  # noqa: PLC0415
        enriched = apply_repo_map_layer(
            enriched,
            {"role": getattr(args, "role", None)},
        )
    except Exception as exc:
        logger.warning("_enrich_instruction: repo map layer failed (%s) — skipping", exc)

    return enriched


def _extract_response_text(result: Any) -> str:
    """Return completion_text from any spawn result, or empty string."""
    return (getattr(result, "completion_text", None) or "")


def _extract_token_usage(result: Any, provider: str) -> Dict[str, int]:
    """Normalize token_usage from any spawn result to {input, output, cache_hit}.

    Each provider emits usage under different field names:
    - litellm:*  — prompt_tokens / completion_tokens (OpenAI format from _litellm_runner.py)
    - codex      — input_tokens / output_tokens / cache_read_tokens
    - gemini     — input_tokens / output_tokens / cache_read_tokens
    - claude     — input_tokens / output_tokens / cache_read_input_tokens (from result event)
    - kimi       — input_tokens / output_tokens (same as codex/gemini)
    """
    usage = {"input": 0, "output": 0, "cache_hit": 0}
    raw = getattr(result, "token_usage", None)
    if not isinstance(raw, dict):
        logger.warning(
            "token_usage extraction returned 0 for provider=%s; check spawn_result shape", provider
        )
        return usage

    if provider.startswith("litellm:"):
        usage["input"] = int(raw.get("prompt_tokens", 0) or 0)
        usage["output"] = int(raw.get("completion_tokens", 0) or 0)
        details = raw.get("prompt_tokens_details") or {}
        cache = int(details.get("cached_tokens", 0) or 0) or int(raw.get("prompt_cache_hit_tokens", 0) or 0)
        usage["cache_hit"] = cache
    elif provider in ("codex", "gemini", "kimi"):
        # Accept both the raw spawn shape (input_tokens/output_tokens/cache_read_tokens)
        # AND an already-normalized shape (input/output/cache_read or cache_hit). A
        # normalized dict reaching here previously yielded 0 — which then zeroed the
        # receipt token_usage while the cost-event (computed from the same dict
        # elsewhere) carried real numbers. Unify the extraction so both agree.
        usage["input"] = int(raw.get("input_tokens", raw.get("input", 0)) or 0)
        usage["output"] = int(raw.get("output_tokens", raw.get("output", 0)) or 0)
        usage["cache_hit"] = int(
            raw.get("cache_read_tokens", raw.get("cache_read", raw.get("cache_hit", 0))) or 0
        )
    elif provider in ("claude", "deepseek-harness"):
        usage["input"] = int(raw.get("input_tokens", raw.get("input", 0)) or 0)
        usage["output"] = int(raw.get("output_tokens", raw.get("output", 0)) or 0)
        usage["cache_hit"] = int(raw.get("cache_read_input_tokens", raw.get("cache_hit", 0)) or 0)
    else:
        usage["input"] = int(raw.get("input_tokens", raw.get("prompt_tokens", raw.get("input", 0))) or 0)
        usage["output"] = int(raw.get("output_tokens", raw.get("completion_tokens", raw.get("output", 0))) or 0)
        usage["cache_hit"] = int(raw.get("cache_read_tokens", raw.get("cache_hit", 0)) or 0)

    if usage["input"] == 0 and usage["output"] == 0:
        logger.warning(
            "token_usage extraction returned 0 for provider=%s; check spawn_result shape", provider
        )
    return usage


# Maps VNX provider literals to their wave7_models.yaml registry keys.
_PROVIDER_TO_REGISTRY_KEY: Dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
    "kimi": "kimi",
    "deepseek-harness": "deepseek",
}


def _load_pricing_from_registry(provider: str, model: str) -> Optional[Dict[str, float]]:
    """Load {input, output} pricing per MTok from wave7_models.yaml. Returns None on miss.

    Handles direct providers (claude, codex, gemini, kimi) via _PROVIDER_TO_REGISTRY_KEY,
    and litellm sub-providers (litellm:deepseek, litellm:moonshot, litellm:zai) by
    extracting the sub-provider from the colon-delimited string.
    """
    registry_key = _PROVIDER_TO_REGISTRY_KEY.get(provider)
    if registry_key is None and provider.startswith("litellm:") and ":" in provider:
        registry_key = provider.split(":", 1)[1].split(":", 1)[0]
    if not registry_key:
        logger.warning(
            "_load_pricing_from_registry: unknown provider=%s — no pricing available",
            provider,
        )
        return None
    try:
        from providers import provider_registry as _reg
        registry = _reg.load()
        cfg = registry.get(registry_key)
        if cfg is None or not cfg.models:
            logger.warning(
                "_load_pricing_from_registry: no models for provider=%s registry_key=%s",
                provider, registry_key,
            )
            return None
        model_key = model.split("/")[-1] if "/" in model else model
        entry = (
            cfg.models.get(model_key)
            or next((v for k, v in cfg.models.items() if k in model_key or model_key in k), None)
            or next(iter(cfg.models.values()), None)
        )
        if entry is None:
            return None
        return {
            "input": float(entry.cost_input_per_mtok),
            "output": float(entry.cost_output_per_mtok),
        }
    except Exception as exc:
        logger.debug("_load_pricing_from_registry: failed for provider=%s model=%s: %s", provider, model, exc)
        return None


def _compute_kimi_cost(model: Optional[str], token_usage: Dict[str, int]) -> Optional[float]:
    """Compute cost via wave7_models.yaml kimi_cli section for kimi provider."""
    if not token_usage or (token_usage.get("input", 0) == 0 and token_usage.get("output", 0) == 0):
        return None
    try:
        from providers import provider_registry as _reg
        registry = _reg.load()
        cfg = registry.get("kimi_cli")
        if cfg is None or not cfg.models:
            return None
        target_key = (model or "").strip() or "kimi-default"
        entry = cfg.models.get(target_key) or next(iter(cfg.models.values()), None)
        if entry is None:
            return None
        cost_in = (token_usage.get("input", 0) / 1_000_000) * entry.cost_input_per_mtok
        cost_out = (token_usage.get("output", 0) / 1_000_000) * entry.cost_output_per_mtok
        return round(cost_in + cost_out, 8)
    except Exception as exc:
        logger.debug("_compute_kimi_cost failed: %s", exc)
        return None


def _compute_cost(provider: str, model: str, token_usage: Dict[str, int]) -> Optional[float]:
    """Compute cost_usd from wave7_models.yaml pricing. Returns None on lookup miss."""
    if not token_usage or (token_usage.get("input", 0) == 0 and token_usage.get("output", 0) == 0):
        return None
    if provider == "kimi":
        return _compute_kimi_cost(model, token_usage)
    pricing = _load_pricing_from_registry(provider, model)
    if pricing:
        cost_in = (token_usage.get("input", 0) / 1_000_000) * pricing["input"]
        cost_out = (token_usage.get("output", 0) / 1_000_000) * pricing["output"]
        return round(cost_in + cost_out, 8)
    # Registry miss — fall back to the provider_costs rate table so API lanes
    # (litellm:deepseek/zai, codex-API, gemini) still resolve to real dollars
    # rather than silently landing at 0. The rate table returns None for
    # subscription/OAuth flat lanes, which is the correct documented-$0 result.
    try:
        from provider_costs import resolve_cost_usd  # noqa: PLC0415
        return resolve_cost_usd(
            provider, model, token_usage.get("input", 0), token_usage.get("output", 0)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("_compute_cost: rate-table fallback failed for %s/%s: %s", provider, model, exc)
        return None


def _build_frontmatter(
    args: argparse.Namespace,
    provider: str,
    model_used: str,
    result: Any,
    duration: float,
    token_usage: Dict[str, int],
    cost_usd: Optional[float],
) -> Dict[str, Any]:
    """Build unified_report_v1 frontmatter from dispatch context + spawn result."""
    from unified_report_schema import SCHEMA_VERSION

    spawn_fm = result.frontmatter_fields() if hasattr(result, "frontmatter_fields") else {}

    frontmatter: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "dispatch_id": args.dispatch_id,
        "provider": spawn_fm.get("provider", provider.split(":")[0]),
        "sub_provider": spawn_fm.get("sub_provider", "none"),
        "model": model_used,
        "terminal_id": args.terminal_id,
        "pool_id": os.environ.get("VNX_POOL_ID", "headless"),
        "role": getattr(args, "role", None) or "backend-developer",
        "task_class": os.environ.get("VNX_TASK_CLASS", "implementation"),
        "pr_id": getattr(args, "pr_id", None) or "none",
        "duration_seconds": round(duration, 3),
        "exit_code": spawn_fm.get("exit_code", getattr(result, "returncode", 1)),
        "token_usage": spawn_fm.get("token_usage", {
            "input": token_usage.get("input", 0),
            "output": token_usage.get("output", 0),
            "cache_read": token_usage.get("cache_hit", 0),
        }),
        "cost_usd": cost_usd if cost_usd is not None else 0.0,
        "route_decision": {
            "strategy": os.environ.get("VNX_ROUTE_STRATEGY", "default"),
            "selected_provider": provider,
            "selected_model": model_used,
        },
    }

    # Surface explicit token-accounting availability when the adapter reports it
    # (e.g. kimi-cli 1.44.0 stream-json carries no usage). Read from the result
    # object directly — not from frontmatter_fields() — to keep the cross-provider
    # frontmatter contract stable.
    tum = getattr(result, "token_usage_measured", None)
    if tum is not None:
        frontmatter["token_usage_measured"] = bool(tum)

    return frontmatter


_EMIT_MAX_RETRIES = 3
_EMIT_RETRY_DELAY = 0.5  # seconds; multiplied by attempt number for backoff

# Terminal → track mapping for headless provider dispatches (T0 plans, T1/T2/T3
# execute Track A/B/C). Unknown terminals fall back to "headless".
_TERMINAL_TRACK = {"T1": "A", "T2": "B", "T3": "C"}


def _record_provider_metadata(
    args: argparse.Namespace,
    provider: str,
    status: str,
    report_path: Path,
    state_dir: Path,
) -> None:
    """Best-effort: upsert a provider-stamped dispatch_metadata row.

    This is what makes the self-learning/intelligence layer provider-aware: the
    headless multi-provider path previously wrote NO dispatch_metadata row, so
    non-Claude work created zero intelligence rows and the receipt processor's
    outcome UPDATE was a silent no-op. Failures here never abort the dispatch.
    """
    try:
        from dispatch_metadata_db import upsert_dispatch_provider_row  # noqa: PLC0415
        db_path = Path(state_dir) / "quality_intelligence.db"
        terminal = getattr(args, "terminal_id", "") or ""
        upsert_dispatch_provider_row(
            db_path,
            dispatch_id=args.dispatch_id,
            terminal=terminal,
            provider=provider,
            track=_TERMINAL_TRACK.get(terminal, "headless"),
            role=getattr(args, "role", None),
            gate=getattr(args, "gate", None) or None,
            pr_id=getattr(args, "pr_id", None),
            outcome_status=status,
            report_path=str(report_path),
            project_id=os.environ.get("VNX_PROJECT_ID", "vnx-dev"),
        )
    except Exception as exc:  # noqa: BLE001 — metadata logging is non-fatal
        logger.warning(
            "_record_provider_metadata: failed for dispatch=%s provider=%s (non-fatal): %s",
            getattr(args, "dispatch_id", "?"), provider, exc,
        )


def _emit_governance(
    args: argparse.Namespace,
    provider: str,
    model_used: str,
    result: Any,
    start_time: datetime,
    end_time: datetime,
    status: str,
) -> None:
    """Emit dispatch receipt + unified report after every spawn handler call.

    Transient OSError/RuntimeError (rename collision, brief lock) retries up to
    _EMIT_MAX_RETRIES times with exponential backoff.  After exhausting retries,
    raises to the caller — it is the caller's responsibility to decide whether to
    kill the worker.  ValueError (invalid provider) is not transient and re-raises
    immediately without retry.
    """
    from governance_emit import emit_dispatch_receipt, emit_unified_report

    state_dir = Path(os.environ.get("VNX_STATE_DIR", ".vnx-data/state"))
    data_dir = Path(os.environ.get("VNX_DATA_DIR", ".vnx-data"))
    duration = (end_time - start_time).total_seconds()
    token_usage = _extract_token_usage(result, provider)
    cost_usd = _compute_cost(provider, model_used, token_usage)

    # Deterministic report path — emitted below by emit_unified_report. Computed
    # up front so the receipt can carry the linkage even though the report write
    # happens afterward (the path string is stable regardless of write order).
    report_path = data_dir / "unified_reports" / f"{args.dispatch_id}.md"

    # ADR-005: emit cost event BEFORE receipt/report writes. Raises on failure — fail-loud.
    from provider_costs import emit_provider_cost  # noqa: PLC0415
    emit_provider_cost(
        provider=provider,
        model=model_used,
        input_tokens=token_usage.get("input") if token_usage else None,
        output_tokens=token_usage.get("output") if token_usage else None,
        cost_usd_estimate=cost_usd,
        dispatch_id=args.dispatch_id,
        project_id=os.environ.get("VNX_PROJECT_ID", "vnx-dev"),
    )

    # Provider-aware self-learning: stamp a dispatch_metadata row so EVERY governed
    # dispatch (incl. non-Claude) feeds the intelligence layer tagged by provider.
    # Best-effort — metadata logging is non-fatal to the dispatch.
    _record_provider_metadata(args, provider, status, report_path, state_dir)

    for attempt in range(_EMIT_MAX_RETRIES):
        try:
            receipt_path = emit_dispatch_receipt(
                dispatch_id=args.dispatch_id,
                terminal_id=args.terminal_id,
                provider=provider,
                model=model_used,
                pr_id=getattr(args, "pr_id", None),
                status=status,
                completion_pct=100 if status == "success" else 0,
                risk=0.0,
                findings=[],
                duration_seconds=duration,
                token_usage=token_usage,
                cost_usd=cost_usd,
                state_dir=state_dir,
                report_path=str(report_path),
            )
            print(f"Receipt: {receipt_path}", file=sys.stderr)
            break
        except ValueError as exc:
            logger.error(
                "_emit_governance: receipt failed dispatch=%s (invalid provider): %s",
                args.dispatch_id, exc,
            )
            raise
        except RuntimeError as exc:
            if attempt < _EMIT_MAX_RETRIES - 1:
                logger.warning(
                    "_emit_governance: transient receipt write failure (attempt %d/%d): %s — retrying",
                    attempt + 1, _EMIT_MAX_RETRIES, exc,
                )
                time.sleep(_EMIT_RETRY_DELAY * (attempt + 1))
                continue
            logger.error(
                "_emit_governance: persistent receipt write failure after %d retries: %s — receipt may be lost",
                _EMIT_MAX_RETRIES, exc,
            )
            raise

    frontmatter = _build_frontmatter(
        args, provider, model_used, result, duration, token_usage, cost_usd,
    )

    for attempt in range(_EMIT_MAX_RETRIES):
        try:
            report_path = emit_unified_report(
                dispatch_id=args.dispatch_id,
                terminal_id=args.terminal_id,
                provider=provider,
                instruction=args.instruction,
                response_text=_extract_response_text(result),
                findings=[],
                duration_seconds=duration,
                data_dir=data_dir,
                frontmatter=frontmatter,
            )
            print(f"Report: {report_path}", file=sys.stderr)
            break
        except RuntimeError as exc:
            if attempt < _EMIT_MAX_RETRIES - 1:
                logger.warning(
                    "_emit_governance: transient report write failure (attempt %d/%d): %s — retrying",
                    attempt + 1, _EMIT_MAX_RETRIES, exc,
                )
                time.sleep(_EMIT_RETRY_DELAY * (attempt + 1))
                continue
            logger.error(
                "_emit_governance: persistent report write failure after %d retries: %s — report may be lost",
                _EMIT_MAX_RETRIES, exc,
            )
            raise


def _build_lane_key(base_sub: str, model_alias: "str | None") -> str:
    """Build a behavior_contracts lane key from sub-provider parts.

    e.g. ("deepseek", None) -> "litellm:deepseek:deepseek-v4-pro"
         ("moonshot", "kimi-k2-6") -> "litellm:moonshot:kimi-k2-6"
    """
    alias = model_alias or _SUB_PROVIDER_DEFAULT_ALIAS.get(base_sub, "default")
    return f"litellm:{base_sub}:{alias}"


def _litellm_parts(provider: str) -> tuple[str, "str | None"]:
    sub_provider = provider.split(":", 1)[1] if provider.startswith("litellm:") else ""
    sub_parts = sub_provider.split(":", 1)
    base_sub = sub_parts[0] if sub_parts else ""
    model_alias = sub_parts[1] if len(sub_parts) > 1 else None
    return base_sub, model_alias


def _constraint_model_for_provider(args: argparse.Namespace, provider: str) -> str:
    """Resolve the model label used by dispatch pre-flight checks."""
    if provider == "codex":
        return os.environ.get("VNX_CODEX_MODEL", "") or _resolve_codex_model()
    if provider == "gemini":
        return os.environ.get("VNX_GEMINI_MODEL", "gemini-2.5-pro")
    if provider == "kimi":
        return os.environ.get("VNX_KIMI_MODEL", "") or _resolve_kimi_model_label()
    if provider == "deepseek-harness":
        from provider_spawns.deepseek_harness_spawn import resolve_harness_model  # noqa: PLC0415

        return resolve_harness_model(args.model if args.model != "sonnet" else None)
    if provider.startswith("litellm:") or provider == "litellm":
        base_sub, model_alias = _litellm_parts(provider)
        env_model = os.environ.get("VNX_LITELLM_MODEL", "")
        if env_model:
            return env_model
        if model_alias:
            return model_alias
        if getattr(args, "model", None) and args.model != "sonnet":
            return args.model
        if base_sub == "deepseek":
            return _resolve_deepseek_model()
        if base_sub == "moonshot":
            return _resolve_moonshot_model(None)
        if base_sub == "zai":
            return _resolve_zai_model(None)
        if base_sub and base_sub in _LITELLM_SUB_PROVIDER_DEFAULTS:
            return _LITELLM_SUB_PROVIDER_DEFAULTS[base_sub]
        if base_sub:
            return args.model
    return args.model


def _constraint_registry_check_enabled(args: argparse.Namespace, provider: str) -> bool:
    if provider == "deepseek-harness":
        # The harness lane governs its model via resolve_harness_model (default
        # deepseek-v4-pro) and the endpoint's authoritative model field — it is
        # not a litellm-registry-driven route. The forbid_route subscription
        # block is the real safety gate, not registry membership.
        return False
    if not (provider.startswith("litellm:") or provider == "litellm"):
        return True
    base_sub, model_alias = _litellm_parts(provider)
    if os.environ.get("VNX_LITELLM_MODEL", "") or model_alias:
        return True
    if getattr(args, "model", None) and args.model != "sonnet":
        return True
    return base_sub in {"deepseek", "moonshot", "zai"}


def _constraint_via_for_provider(provider: str, sub_provider: "str | None") -> "str | None":
    via_per_sub = {
        "deepseek": "litellm",
        "moonshot": "moonshot",
        "openrouter": "openrouter",
        "zai": "openrouter",
    }
    if provider.startswith("litellm:") or provider == "litellm":
        return via_per_sub.get(sub_provider or "", "litellm")
    if provider == "deepseek-harness":
        # Own-key KEY-AUTH harness lane. The deepseek-harness-subscription-blocked
        # constraint forbids via=claude_harness_subscription (the OAuth-subscription
        # redirect). This measured-safe lane runs the own DeepSeek key in key-auth
        # mode, so it routes via=claude_harness_keyed and clears pre-flight.
        return "claude_harness_keyed"
    if provider in ("claude", "codex", "gemini", "kimi"):
        return "cli"
    return None


def _emit_constraint_failure_receipt(
    args: argparse.Namespace,
    provider: str,
    model: str,
    failure_reason: str,
) -> None:
    """Record fail-closed pre-flight failures before any provider spawn."""
    state_dir = Path(os.environ.get("VNX_STATE_DIR", ".vnx-data/state"))
    receipt_path = state_dir / "t0_receipts.ndjson"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    receipt = {
        "dispatch_id": args.dispatch_id,
        "terminal_id": args.terminal_id,
        "provider": provider,
        "model": model,
        "status": "failure",
        "completion_pct": 0,
        "risk": 0.0,
        "duration_seconds": 0.0,
        "token_usage": {"input": 0, "output": 0, "cache_hit": 0},
        "cost_usd": 0.0,
        "findings": [],
        "pr_id": getattr(args, "pr_id", None),
        "failure_reason": failure_reason,
        "timestamp": now_ts,
        "recorded_at": now_ts,
    }
    line = json.dumps(receipt, separators=(",", ":")) + "\n"
    with receipt_path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _check_constraints(args: argparse.Namespace, provider: str):
    """Run provider_constraints.yaml pre-flight and raise on blocking violations."""
    from providers.constraint_enforcer import ConstraintViolationError, check_constraints  # noqa: PLC0415

    sub_provider = None
    if provider.startswith("litellm:"):
        sub_provider, _model_alias = _litellm_parts(provider)
    elif provider == "deepseek-harness":
        # sub_provider=deepseek lets the deepseek-harness-subscription-blocked
        # constraint match on forbidden_route.provider=deepseek; the keyed via
        # (set below) is what keeps this own-key lane clear of the block.
        sub_provider = "deepseek"
    model = _constraint_model_for_provider(args, provider)
    via = _constraint_via_for_provider(provider, sub_provider)
    violations = check_constraints(
        provider=provider,
        sub_provider=sub_provider,
        model=model,
        terminal_id=args.terminal_id,
        role=args.role,
        via=via,
        instruction_text=args.instruction,
        env=os.environ,
        check_registry=_constraint_registry_check_enabled(args, provider),
    )
    for violation in violations:
        if violation.severity == "blocking":
            raise ConstraintViolationError(violation)
        if violation.override_applied:
            logger.warning("[%s] warning overridden by env flag: %s", violation.code, violation.message)
        else:
            logger.warning("[%s] %s", violation.code, violation.message)
    return violations


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VNX provider-agnostic dispatch entry (Wave 4.6)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--provider",
        required=True,
        help=(
            "Provider to use for dispatch. "
            "Accepted values: claude, codex, gemini, kimi, litellm:<model>. "
            "Example: --provider claude, --provider kimi, --provider litellm:deepseek-v4-pro"
        ),
    )
    # Forward all existing subprocess_dispatch.py flags verbatim.
    parser.add_argument("--terminal-id", required=True)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--role", default=None)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--no-auto-commit", action="store_true")
    parser.add_argument("--gate", default="")
    parser.add_argument("--dispatch-paths", default="")
    parser.add_argument("--pr-id", default=None)
    parser.add_argument(
        "--auto-route", action="store_true",
        help="Use smart_router to auto-select provider+model (opt-in, default off).",
    )
    parser.add_argument(
        "--no-repo-map", action="store_true", dest="no_repo_map",
        help="Skip repo map injection (mirrors --no-repo-map in subprocess_dispatch).",
    )
    return parser


def _dispatch_claude(args: argparse.Namespace) -> int:
    """Delegate to subprocess_dispatch.deliver_with_recovery (claude path).

    Produces byte-identical NDJSON + receipt as direct subprocess_dispatch
    invocation — the delegation preserves all argument semantics unchanged.
    """
    import subprocess_dispatch as sd

    # OI-1107: fall back to Role: header in instruction, then to documented default.
    role = args.role
    if role is None:
        role = sd._extract_role_from_instruction(args.instruction) or sd._ROLE_FALLBACK

    dispatch_paths: list[str] | None = None
    if args.dispatch_paths.strip():
        dispatch_paths = [p.strip() for p in args.dispatch_paths.split(",") if p.strip()]

    start_time = datetime.now(timezone.utc)
    ok = sd.deliver_with_recovery(
        terminal_id=args.terminal_id,
        instruction=args.instruction,
        model=args.model,
        dispatch_id=args.dispatch_id,
        role=role,
        max_retries=args.max_retries,
        auto_commit=not args.no_auto_commit,
        gate=args.gate,
        dispatch_paths=dispatch_paths,
        pr_id=args.pr_id,
    )
    end_time = datetime.now(timezone.utc)

    # Read token_usage from delivery side-channel (populated by spawn_claude).
    # recovery._resolve_token_usage_and_cost uses .get() to leave the entry
    # available for this governance-receipt path; we .pop() here to clean up.
    _claude_token_usage = None
    try:
        from subprocess_dispatch_internals.delivery import _dispatch_token_usage as _tu_cache
        _claude_token_usage = _tu_cache.pop(args.dispatch_id, None)
    except Exception as _tu_exc:
        logger.debug("_dispatch_claude: token_usage side-channel read failed: %s", _tu_exc)

    class _ClaudeResult:
        completion_text = ""
        token_usage = _claude_token_usage

    status = "success" if ok else "failure"
    _emit_governance(args, "claude", args.model, _ClaudeResult(), start_time, end_time, status)
    return 0 if ok else 1


def _create_provider_worktree(dispatch_id: str) -> Optional[Path]:
    """Create an isolated worktree for a provider dispatch.

    Only called when VNX_ISOLATED_WORKTREE=1.  Returns the worktree Path on
    success, None on failure (logs the error and falls back to shared path).
    """
    try:
        from dispatch_worktree_isolation import create_dispatch_worktree  # noqa: PLC0415
        wt_path = create_dispatch_worktree(dispatch_id)
        logger.info("provider isolation: worktree created at %s (dispatch=%s)", wt_path, dispatch_id)
        return wt_path
    except RuntimeError as exc:
        logger.error(
            "provider isolation: create_dispatch_worktree failed for %s: %s — running in shared worktree",
            dispatch_id, exc,
        )
        return None


def _remove_provider_worktree(dispatch_id: str) -> None:
    """Remove the isolated worktree for a provider dispatch.  Best-effort; idempotent."""
    try:
        from dispatch_worktree_isolation import remove_dispatch_worktree  # noqa: PLC0415
        remove_dispatch_worktree(dispatch_id)
        logger.info("provider isolation: worktree removed (dispatch=%s)", dispatch_id)
    except Exception as exc:
        logger.warning(
            "provider isolation: remove_dispatch_worktree failed for %s: %s",
            dispatch_id, exc,
        )


def _dispatch_codex(args: argparse.Namespace) -> int:
    """Route to spawn_codex for codex-provider dispatches (PR-4.6.3).

    Prompt is the raw instruction; file-content injection is caller's responsibility.
    Wires EventStore as event_writer so codex dispatches produce a NDJSON audit trail
    identical to the claude path (provider-agnostic audit completeness, ADR-005).
    """
    from provider_spawns.codex_spawn import spawn_codex

    event_store = None
    try:
        from event_store import EventStore
        event_store = EventStore()
    except Exception as _es_exc:
        logger.error(
            "_dispatch_codex: EventStore init failed; cannot proceed without audit sink (ADR-005): %s",
            _es_exc,
        )
        return 1

    model = os.environ.get("VNX_CODEX_MODEL", "") or _resolve_codex_model()
    enriched_instruction = _enrich_instruction(args)
    isolation_cwd: Optional[Path] = None
    if os.environ.get("VNX_ISOLATED_WORKTREE") == "1":
        isolation_cwd = _create_provider_worktree(args.dispatch_id)
    start_time = datetime.now(timezone.utc)
    try:
        result = spawn_codex(
            prompt=enriched_instruction,
            model=model,
            dispatch_id=args.dispatch_id,
            terminal_id=args.terminal_id,
            event_writer=event_store.append if event_store is not None else None,
            cwd=isolation_cwd,
        )
        end_time = datetime.now(timezone.utc)

        if result.error:
            _emit_governance(args, "codex", model, result, start_time, end_time, "failure")
            print(f"spawn_codex failed: {result.error}", file=sys.stderr)
            return 1
        if result.timed_out:
            _emit_governance(args, "codex", model, result, start_time, end_time, "timeout")
            print("spawn_codex timed out", file=sys.stderr)
            return 1
        if result.returncode != 0:
            _emit_governance(args, "codex", model, result, start_time, end_time, "failure")
            return 1
        if result.event_writer_failures > 0:
            logger.error(
                "codex dispatch completed but %d event_writer failures occurred — audit gap",
                result.event_writer_failures,
            )
            _emit_governance(args, "codex", model, result, start_time, end_time, "success")
            return 2
        _emit_governance(args, "codex", model, result, start_time, end_time, "success")
        return 0
    finally:
        try:
            event_store.clear(args.terminal_id, archive_dispatch_id=args.dispatch_id)
        except Exception as _exc:
            logger.debug("_dispatch_codex: event archive+clear failed: %s", _exc)
        if isolation_cwd is not None:
            _remove_provider_worktree(args.dispatch_id)


def _resolve_codex_model() -> str:
    """Load codex model key from registry (openai section), fallback to hardcoded default.

    Returns the registry model key (e.g. 'gpt-5.2-codex') so the receipt model field
    is never empty when VNX_CODEX_MODEL is unset.
    """
    _CODEX_FALLBACK = "gpt-5.2-codex"
    try:
        from providers import provider_registry as _reg
        registry = _reg.load()
    except Exception as e:
        logger.error("provider_dispatch: registry load failed for codex: %s", e)
        return _CODEX_FALLBACK
    cfg = registry.get("openai")
    if cfg is None or not cfg.models:
        return _CODEX_FALLBACK
    return next(iter(cfg.models.keys()))


def _resolve_kimi_model_label() -> str:
    """Load kimi CLI default model key from registry (kimi_cli section), fallback to 'kimi-default'.

    Returns the registry model key (e.g. 'kimi-default') so the receipt model field
    is never the generic 'default' string when VNX_KIMI_MODEL is unset.
    """
    _KIMI_FALLBACK = "kimi-default"
    try:
        from providers import provider_registry as _reg
        registry = _reg.load()
    except Exception as e:
        logger.error("provider_dispatch: registry load failed for kimi_cli: %s", e)
        return _KIMI_FALLBACK
    cfg = registry.get("kimi_cli")
    if cfg is None or not cfg.models:
        return _KIMI_FALLBACK
    return next(iter(cfg.models.keys()))


def _resolve_deepseek_model() -> str:
    """Load DeepSeek model litellm_name from registry, fallback to hardcoded default."""
    from providers import provider_registry as _reg
    try:
        rec = _reg.get_default_model("deepseek")
    except (FileNotFoundError, ValueError) as e:
        logger.error("provider_dispatch: registry resolve failed for deepseek: %s", e)
        raise RuntimeError(f"provider registry resolution failed: {e}") from e
    if rec is not None:
        return rec.litellm_name
    return _LITELLM_SUB_PROVIDER_DEFAULTS["deepseek"]


def _resolve_moonshot_model(model_alias: "str | None" = None) -> str:
    """Load Moonshot model litellm_name from registry.

    When model_alias is given (e.g. 'kimi-k2-6'), looks up that specific model key.
    Defaults to 'kimi-k2-0905-default' (cost-effective lane) when alias is absent.
    Falls back to hardcoded default when registry is unavailable.
    """
    from providers import provider_registry as _reg
    try:
        registry = _reg.load()
    except (FileNotFoundError, ValueError) as e:
        logger.error("provider_dispatch: registry resolve failed for moonshot: %s", e)
        raise RuntimeError(f"provider registry resolution failed: {e}") from e
    cfg = registry.get("moonshot")
    if cfg is None or not cfg.enabled or not cfg.models:
        return _LITELLM_SUB_PROVIDER_DEFAULTS["moonshot"]
    target_key = model_alias or "kimi-k2-0905-default"
    if target_key in cfg.models:
        return cfg.models[target_key].litellm_name
    # alias not found — fall back to first available model
    return next(iter(cfg.models.values())).litellm_name


def _validate_zai_model_not_legacy(model: str) -> None:
    """Raise ValueError when model names a deprecated GLM version."""
    model_lower = (model or "").lower().strip()
    if model_lower in _DEPRECATED_ZAI_MODELS:
        raise ValueError(f"GLM-4.5/4.6 are LEGACY, use GLM-5.1 (got: {model!r})")


def _resolve_zai_model(model_alias: "str | None" = None) -> str:
    """Load GLM-5.1 litellm_name from registry via OpenRouter.

    Defaults to 'glm-5.1-default' (openrouter/z-ai/glm-5) when alias is absent.
    Falls back to hardcoded default when registry is unavailable.
    """
    from providers import provider_registry as _reg
    try:
        registry = _reg.load()
    except (FileNotFoundError, ValueError) as e:
        logger.error("provider_dispatch: registry resolve failed for zai: %s", e)
        raise RuntimeError(f"provider registry resolution failed: {e}") from e
    cfg = registry.get("zai")
    if cfg is None or not cfg.enabled or not cfg.models:
        return _LITELLM_SUB_PROVIDER_DEFAULTS["zai"]
    target_key = model_alias or "glm-5.1-default"
    if target_key in cfg.models:
        return cfg.models[target_key].litellm_name
    return next(iter(cfg.models.values())).litellm_name


def _dispatch_litellm(args: argparse.Namespace) -> int:
    """Route to spawn_litellm for litellm-provider dispatches (PR-4.6.5).

    Accepts --provider litellm:<sub_provider>, e.g. litellm:deepseek.
    Model resolved via VNX_LITELLM_MODEL env var, registry lookup, sub_provider default,
    or "anthropic/claude-sonnet-4-6" fallback. Wires EventStore for NDJSON audit.
    DeepSeek requires DEEPSEEK_API_KEY env var (fast-fail before subprocess spawn).
    """
    from provider_spawns.litellm_spawn import spawn_litellm

    event_store = None
    try:
        from event_store import EventStore
        event_store = EventStore()
    except Exception as _es_exc:
        logger.error(
            "_dispatch_litellm: EventStore init failed; cannot proceed without audit sink (ADR-005): %s",
            _es_exc,
        )
        return 1

    parts = args.provider.split(":", 1)
    sub_provider = parts[1] if len(parts) > 1 else ""

    # Normalize sub-sub-routing: litellm:moonshot:kimi-k2-6 -> base=moonshot, alias=kimi-k2-6
    sub_parts = sub_provider.split(":", 1)
    base_sub = sub_parts[0]
    model_alias = sub_parts[1] if len(sub_parts) > 1 else None

    # Fast-fail for providers that require an explicit API key
    required_key = _SUB_PROVIDER_KEY_REQS.get(base_sub)
    if required_key and not os.environ.get(required_key):
        print(
            f"litellm:{base_sub} requires {required_key} env var",
            file=sys.stderr,
        )
        return _EX_USAGE

    env_model = os.environ.get("VNX_LITELLM_MODEL", "")
    if base_sub == "deepseek":
        model = env_model or _resolve_deepseek_model()
    elif base_sub == "moonshot":
        model = env_model or _resolve_moonshot_model(model_alias)
    elif base_sub == "zai":
        _validate_zai_model_not_legacy(args.model)
        if model_alias:
            _validate_zai_model_not_legacy(model_alias)
        model = env_model or _resolve_zai_model(model_alias)
    elif env_model:
        model = env_model
    elif base_sub and base_sub in _LITELLM_SUB_PROVIDER_DEFAULTS:
        model = _LITELLM_SUB_PROVIDER_DEFAULTS[base_sub]
    elif base_sub:
        model = f"{base_sub}/default"
    else:
        model = "anthropic/claude-sonnet-4-6"

    lane_key = _build_lane_key(base_sub, model_alias)
    _contract = None
    _tool_call_shape = None
    try:
        from providers.behavior_contracts import get_contract as _get_contract
        _contract = _get_contract(lane_key)
        _tool_call_shape = _contract.tool_call_shape
        logger.debug(
            "_dispatch_litellm: lane=%s cache_control=%s tool_shape=%s",
            lane_key,
            _contract.cache_control_supported,
            _tool_call_shape,
        )
    except KeyError:
        logger.warning(
            "_dispatch_litellm: no behavior contract for lane %r — proceeding without contract enforcement",
            lane_key,
        )

    enriched_instruction = _enrich_instruction(args)
    isolation_cwd_litellm: Optional[Path] = None
    if os.environ.get("VNX_ISOLATED_WORKTREE") == "1":
        isolation_cwd_litellm = _create_provider_worktree(args.dispatch_id)
    start_time = datetime.now(timezone.utc)
    try:
        result = spawn_litellm(
            prompt=enriched_instruction,
            model=model,
            dispatch_id=args.dispatch_id,
            terminal_id=args.terminal_id,
            sub_provider=base_sub or None,
            lane=lane_key,
            tool_call_shape=_tool_call_shape,
            event_writer=event_store.append if event_store is not None else None,
            cwd=isolation_cwd_litellm,
        )
        end_time = datetime.now(timezone.utc)

        if result.error:
            _emit_governance(args, args.provider, model, result, start_time, end_time, "failure")
            print(f"spawn_litellm failed: {result.error}", file=sys.stderr)
            return 1
        if result.timed_out:
            _emit_governance(args, args.provider, model, result, start_time, end_time, "timeout")
            print("spawn_litellm timed out", file=sys.stderr)
            return 1
        if result.returncode != 0:
            _emit_governance(args, args.provider, model, result, start_time, end_time, "failure")
            return 1
        if result.event_writer_failures > 0:
            logger.error(
                "litellm dispatch completed but %d event_writer failures occurred — audit gap",
                result.event_writer_failures,
            )
            _emit_governance(args, args.provider, model, result, start_time, end_time, "success")
            return 2
        _emit_governance(args, args.provider, model, result, start_time, end_time, "success")
        return 0
    finally:
        try:
            event_store.clear(args.terminal_id, archive_dispatch_id=args.dispatch_id)
        except Exception as _exc:
            logger.debug("_dispatch_litellm: event archive+clear failed: %s", _exc)
        if isolation_cwd_litellm is not None:
            _remove_provider_worktree(args.dispatch_id)


def _dispatch_kimi(args: argparse.Namespace) -> int:
    """Route to spawn_kimi for kimi-provider dispatches (Wave 7.7).

    Auth via ``kimi login`` (OAuth). No API key env var required.
    Model resolved via VNX_KIMI_MODEL env var or kimi config default.
    Wires EventStore as event_writer so kimi dispatches produce a NDJSON audit trail.
    """
    from provider_spawns.kimi_spawn import spawn_kimi

    event_store = None
    try:
        from event_store import EventStore
        event_store = EventStore()
    except Exception as _es_exc:
        logger.error(
            "_dispatch_kimi: EventStore init failed; cannot proceed without audit sink (ADR-005): %s",
            _es_exc,
        )
        return 1

    model = os.environ.get("VNX_KIMI_MODEL", "") or None
    model_label = model or _resolve_kimi_model_label()
    enriched_instruction = _enrich_instruction(args)
    isolation_cwd_kimi: Optional[Path] = None
    if os.environ.get("VNX_ISOLATED_WORKTREE") == "1":
        isolation_cwd_kimi = _create_provider_worktree(args.dispatch_id)
    start_time = datetime.now(timezone.utc)
    try:
        result = spawn_kimi(
            prompt=enriched_instruction,
            model=model,
            dispatch_id=args.dispatch_id,
            terminal_id=args.terminal_id,
            event_writer=event_store.append if event_store is not None else None,
            cwd=isolation_cwd_kimi,
        )
        end_time = datetime.now(timezone.utc)

        if result.error:
            _emit_governance(args, "kimi", model_label, result, start_time, end_time, "failure")
            print(f"spawn_kimi failed: {result.error}", file=sys.stderr)
            return 1
        if result.timed_out:
            _emit_governance(args, "kimi", model_label, result, start_time, end_time, "timeout")
            print("spawn_kimi timed out", file=sys.stderr)
            return 1
        if result.returncode != 0:
            _emit_governance(args, "kimi", model_label, result, start_time, end_time, "failure")
            return 1
        if result.event_writer_failures > 0:
            logger.error(
                "kimi dispatch completed but %d event_writer failures occurred — audit gap",
                result.event_writer_failures,
            )
            _emit_governance(args, "kimi", model_label, result, start_time, end_time, "success")
            return 2
        _emit_governance(args, "kimi", model_label, result, start_time, end_time, "success")
        return 0
    finally:
        try:
            event_store.clear(args.terminal_id, archive_dispatch_id=args.dispatch_id)
        except Exception as _exc:
            logger.debug("_dispatch_kimi: event archive+clear failed: %s", _exc)
        if isolation_cwd_kimi is not None:
            _remove_provider_worktree(args.dispatch_id)


def _dispatch_deepseek_harness(args: argparse.Namespace) -> int:
    """Route to spawn_deepseek_harness for the governed DeepSeek-harness lane.

    Execution lane (not a review lane): the ``claude`` CLI drives DeepSeek's
    Anthropic-compatible endpoint with full tool-use, authenticated with the
    OWN DeepSeek key in KEY-AUTH mode (never the OAuth subscription).  Reuses
    the governed claude spawn path (SubprocessAdapter) so it emits a receipt and
    is not the raw ``claude -p`` receipt-bypass the GOV-1 guard targets.

    Fast-fails before any subprocess spawn when DEEPSEEK_API_KEY is absent —
    the lane must never ride the production OAuth account.
    """
    from provider_spawns.deepseek_harness_spawn import (
        DEFAULT_DEEPSEEK_HARNESS_MODEL,
        DEEPSEEK_API_KEY_ENV,
        DeepSeekHarnessSpawnResult,
        resolve_harness_model,
        spawn_deepseek_harness,
    )

    start_time = datetime.now(timezone.utc)

    if not os.environ.get(DEEPSEEK_API_KEY_ENV):
        print(
            f"deepseek-harness requires {DEEPSEEK_API_KEY_ENV} env var "
            "(own-key key-auth; OAuth subscription is forbidden for this lane)",
            file=sys.stderr,
        )
        end_time = datetime.now(timezone.utc)
        _blocked_result = DeepSeekHarnessSpawnResult(
            returncode=_EX_USAGE,
            completion={},
            events_written=0,
            session_id=None,
            timed_out=False,
            model=DEFAULT_DEEPSEEK_HARNESS_MODEL,
            error=f"missing {DEEPSEEK_API_KEY_ENV} — blocked before spawn",
            token_usage=None,
        )
        try:
            _emit_governance(
                args, "deepseek-harness", DEFAULT_DEEPSEEK_HARNESS_MODEL,
                _blocked_result, start_time, end_time, "blocked",
            )
        except Exception as _eg_exc:
            logger.error(
                "_dispatch_deepseek_harness: emit_governance for blocked dispatch failed: %s",
                _eg_exc,
            )
        return _EX_USAGE

    event_store = None
    try:
        from event_store import EventStore
        event_store = EventStore()
    except Exception as _es_exc:
        logger.error(
            "_dispatch_deepseek_harness: EventStore init failed; cannot proceed "
            "without audit sink (ADR-005): %s",
            _es_exc,
        )
        return 1

    model = resolve_harness_model(args.model if args.model != "sonnet" else None)
    enriched_instruction = _enrich_instruction(args)
    # start_time already set at top of function (before the missing-key guard).
    try:
        result = spawn_deepseek_harness(
            prompt=enriched_instruction,
            model=model,
            dispatch_id=args.dispatch_id,
            terminal_id=args.terminal_id,
        )
        end_time = datetime.now(timezone.utc)
        model_used = result.model or model

        if result.error:
            _emit_governance(args, "deepseek-harness", model_used, result, start_time, end_time, "failure")
            print(f"spawn_deepseek_harness failed: {result.error}", file=sys.stderr)
            return 1
        if result.timed_out:
            _emit_governance(args, "deepseek-harness", model_used, result, start_time, end_time, "timeout")
            print("spawn_deepseek_harness timed out", file=sys.stderr)
            return 1
        if result.returncode != 0:
            _emit_governance(args, "deepseek-harness", model_used, result, start_time, end_time, "failure")
            return 1
        _emit_governance(args, "deepseek-harness", model_used, result, start_time, end_time, "success")
        return 0
    finally:
        try:
            event_store.clear(args.terminal_id, archive_dispatch_id=args.dispatch_id)
        except Exception as _exc:
            logger.debug("_dispatch_deepseek_harness: event archive+clear failed: %s", _exc)


def _dispatch_gemini(args: argparse.Namespace) -> int:
    """Route to spawn_gemini for gemini-provider dispatches (PR-4.6.4).

    Prompt is the raw instruction; file-content injection is caller's responsibility.
    """
    from event_store import EventStore
    from provider_spawns.gemini_spawn import spawn_gemini

    event_store = None
    try:
        event_store = EventStore()
    except Exception as _es_exc:
        logger.error(
            "_dispatch_gemini: EventStore init failed; cannot proceed without audit sink (ADR-005): %s",
            _es_exc,
        )
        return 1

    model = os.environ.get("VNX_GEMINI_MODEL", "gemini-2.5-pro")
    enriched_instruction = _enrich_instruction(args)
    isolation_cwd_gemini: Optional[Path] = None
    if os.environ.get("VNX_ISOLATED_WORKTREE") == "1":
        isolation_cwd_gemini = _create_provider_worktree(args.dispatch_id)
    start_time = datetime.now(timezone.utc)
    try:
        result = spawn_gemini(
            prompt=enriched_instruction,
            model=model,
            dispatch_id=args.dispatch_id,
            terminal_id=args.terminal_id,
            event_writer=event_store.append,
            cwd=isolation_cwd_gemini,
        )
        end_time = datetime.now(timezone.utc)

        if result.error:
            _emit_governance(args, "gemini", model, result, start_time, end_time, "failure")
            print(f"spawn_gemini failed: {result.error}", file=sys.stderr)
            return 1
        if result.timed_out:
            _emit_governance(args, "gemini", model, result, start_time, end_time, "timeout")
            print("spawn_gemini timed out", file=sys.stderr)
            return 1
        if result.returncode != 0:
            _emit_governance(args, "gemini", model, result, start_time, end_time, "failure")
            return 1
        if result.event_writer_failures > 0:
            logger.error(
                "gemini dispatch completed but %d event_writer failures occurred — audit gap",
                result.event_writer_failures,
            )
            _emit_governance(args, "gemini", model, result, start_time, end_time, "success")
            return 2
        _emit_governance(args, "gemini", model, result, start_time, end_time, "success")
        return 0
    finally:
        try:
            event_store.clear(args.terminal_id, archive_dispatch_id=args.dispatch_id)
        except Exception as _exc:
            logger.debug("_dispatch_gemini: event archive+clear failed: %s", _exc)
        if isolation_cwd_gemini is not None:
            _remove_provider_worktree(args.dispatch_id)


def main(argv: list[str] | None = None) -> int:
    """Parse args, route to the correct provider handler, return exit code."""
    from env_loader import load_env
    load_env()
    parser = _build_parser()

    # argparse exits with code 2 on unrecognised provider values — but provider
    # is a free-form string (litellm:<model>), not a fixed choices= set, so we
    # validate manually after parsing.
    args = parser.parse_args(argv)

    # Propagate --no-repo-map flag to env var so apply_repo_map_layer picks it up.
    if getattr(args, "no_repo_map", False):
        os.environ["VNX_NO_REPO_MAP"] = "1"

    provider = args.provider

    # PR-SR-3/4: smart_router end-to-end pipeline (opt-in via --auto-route).
    # Uses explicit decide() + parse + write_route_decision() to ensure NDJSON
    # persistence is never silently swallowed by a bundled route() failure.
    if getattr(args, "auto_route", False):
        try:
            from smart_router import decide as _smart_decide, parse_route_model_id, write_route_decision  # noqa: PLC0415

            _dp = _resolve_dispatch_paths(args.dispatch_paths)
            _route_decision = _smart_decide(
                instruction=args.instruction,
                role=args.role,
                dispatch_paths=_dp,
            )

            if _route_decision.primary:
                _r_provider, _r_model = parse_route_model_id(
                    _route_decision.primary.model_id,
                )
                provider = _r_provider
                args.provider = _r_provider
                args.model = _r_model
                os.environ["VNX_ROUTE_STRATEGY"] = "smart_router"
                os.environ["VNX_TASK_CLASS"] = _route_decision.task_class

            _state_dir = _resolve_state_dir()
            write_route_decision(args.dispatch_id, _route_decision, state_dir=_state_dir)

            logger.info(
                "smart_router: auto-route provider=%s model=%s (task_class=%s)",
                provider, args.model, _route_decision.task_class,
            )
        except Exception as _route_exc:
            logger.warning(
                "smart_router: auto-route failed (%s); falling back to --provider=%s --model=%s",
                _route_exc, args.provider, args.model,
            )

    if provider not in _IMPLEMENTED_PROVIDERS and not provider.startswith("litellm:"):
        parser.error(
            f"Unknown provider '{provider}'. "
            "Accepted values: claude, codex, gemini, kimi, deepseek-harness, litellm:<model>."
        )

    # PR-ROUTE-1: enforce provider constraints before any handler runs.
    try:
        _check_constraints(args, provider)
    except FileNotFoundError:
        if os.environ.get("VNX_CONSTRAINTS_STRICT") == "1":
            print("provider_dispatch: provider_constraints.yaml not found and VNX_CONSTRAINTS_STRICT=1", file=sys.stderr)
            return 1
        logger.debug("provider_dispatch: provider_constraints.yaml not found - skipping enforcement")
    except Exception as exc:
        from providers.constraint_enforcer import ConstraintViolationError  # noqa: PLC0415

        if not isinstance(exc, ConstraintViolationError):
            raise
        model = _constraint_model_for_provider(args, provider)
        failure_reason = f"constraint violation: {exc}"
        _emit_constraint_failure_receipt(args, provider, model, failure_reason)
        print(f"provider_dispatch: constraint violation - {exc}", file=sys.stderr)
        return 1

    if provider == "claude":
        return _dispatch_claude(args)

    if provider == "codex":
        return _dispatch_codex(args)

    if provider == "gemini":
        return _dispatch_gemini(args)

    if provider == "kimi":
        return _dispatch_kimi(args)

    if provider == "deepseek-harness":
        return _dispatch_deepseek_harness(args)

    if provider.startswith("litellm:") or provider == "litellm":
        return _dispatch_litellm(args)

    # Unknown literal — argparse-style error (exit code 2).
    parser.error(
        f"Unknown provider '{provider}'. "
        "Accepted values: claude, codex, gemini, kimi, litellm:<model>."
    )
    return 2  # unreachable; parser.error() exits


if __name__ == "__main__":
    sys.exit(main())
