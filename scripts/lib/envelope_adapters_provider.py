"""envelope_adapters_provider.py — ProviderAdapter (codex/kimi/gemini/litellm/
deepseek-harness/glm-harness/local-gemma) for the dispatch_envelope module
family.

Leaf module: no imports from sibling envelope_* modules or from
dispatch_envelope itself (the facade imports FROM here, never the reverse).
Moved unchanged from dispatch_envelope.py as PR-6 of the dispatch-monolith-split
(dispatch-monolith-split, PR-6 of 6) — see dispatch_envelope.py's module
docstring for the split's seam order.

ExecutionPlan and _AdapterResult are RUNTIME imports (not TYPE_CHECKING):
typing.get_type_hints() resolves an annotation against the __globals__ of the
module a function is DEFINED in, not wherever a facade re-exports it from.
ProviderAdapter.run / ProviderAdapter._run_kimi annotate their `plan` param
as ExecutionPlan and return _AdapterResult — hiding either import behind
TYPE_CHECKING makes the module import fine and the code run fine, but
get_type_hints() on those two methods raises NameError (OI-288's exact
failure mode; see tests/test_dispatch_envelope_annotations_resolve.py).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from dispatch_plan import ExecutionPlan
from envelope_types import _AdapterResult


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
            _worker_role_env,
        )

        pv: "Provider" = plan.provider  # type: ignore[assignment]

        # OI-1215: the provider-lane envelope calls the spawn_* functions DIRECTLY
        # (it never goes through provider_dispatch._dispatch_* wrappers), so the
        # VNX_WORKER_ROLE overlay must be built HERE from plan.role and threaded into
        # each spawn's extra_env. Without it, every provider-lane worker resolves to
        # the restrictive code-worker fallback in pretooluse_worker_scope_enforce.py
        # even when the spec carried a genuine role.
        role_env = _worker_role_env(plan.role)

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
                    extra_env=role_env,
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
                    extra_env=role_env,
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
                    extra_env=role_env,
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
                    extra_env=role_env,
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
                    extra_env=role_env,
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
                role=plan.role,
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
            _worker_role_env,
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
                extra_env=_worker_role_env(plan.role),
                cwd=cwd,
                # worker-provider-kimi-flip (20260723): honor the spec's staged deadline
                # instead of spawn_kimi's own hardcoded 900s default — a caller staging a
                # longer deadline (e.g. stage_spec_bundle's 3600s default) was previously
                # silently capped short (memory: provider-lane-900s-deadline-kills-builds).
                total_deadline=float(plan.deadline_seconds),
                # OI-1087: hand the plan's task_class to the spawn so the fabrication
                # guard can exempt positively-known read-only classes (review/analysis):
                # for those, an unchanged worktree is the intended outcome, not proof
                # of fabrication. Four review dispatches false-failed on 2026-08-07
                # because this signal never left the adapter.
                task_class=plan.task_class,
            )
        except BrokenPipeError as exc:
            return _AdapterResult(
                returncode=1, completion_text="", status="failure",
                error=f"kimi spawn BrokenPipeError: {exc}",
            )
        return _map_generic_spawn_result(result, Provider.KIMI.value, model=model)
