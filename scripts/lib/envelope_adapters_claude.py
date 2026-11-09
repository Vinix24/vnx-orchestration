"""envelope_adapters_claude.py — codex + claude-subprocess adapters for the
dispatch_envelope module family.

Leaf module: no imports from sibling envelope_* modules or from
dispatch_envelope itself (the facade imports FROM here, never the reverse).
Moved unchanged from dispatch_envelope.py as PR-5 of the dispatch-monolith-split
(dispatch-monolith-split, PR-5 of 6) — see dispatch_envelope.py's module
docstring for the split's seam order.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from envelope_types import EnvelopeSpec, _AdapterResult

logger = logging.getLogger(__name__)


def _switch_stamps(spec: EnvelopeSpec) -> Dict[str, str]:
    """``VNX_PROJECT_ID`` + ``VNX_WORKER_DISPATCH_ID`` for the worker of ``spec``.

    The project is the one of the store the dispatch belongs to (``spec.state_dir``), so a
    provider-switch decision inside the worker is for the door's project, and the dispatch
    marker makes the ``vnx providers`` write verbs refuse there (provider-schakelaar B).
    """
    import provider_switch

    return provider_switch.worker_env_stamps(spec.dispatch_id, state_dir=spec.state_dir)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _codex_skill_inputs(spec: EnvelopeSpec) -> Dict[str, Any]:
    """The role, state dir and project that ``spawn_codex`` needs to materialise the skill set (ADR-059 decision 13)."""
    try:
        from project_scope import resolve_stamp_project_id

        project_id = resolve_stamp_project_id()
    except Exception:  # vnx-silent-except: an unresolved tenant is reported as no_project_id
        project_id = None
    return {"role": spec.role, "state_dir": spec.state_dir, "project_id": project_id}


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
                extra_env=_switch_stamps(spec),
                cwd=cwd,
                **_codex_skill_inputs(spec),
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
                model=spec.model,
            )
        if result.timed_out:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="timeout",
                token_usage=token_usage,
                timed_out=True,
                event_writer_failures=result.event_writer_failures,
                model=spec.model,
            )
        status = "success" if result.returncode == 0 else "failure"
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status=status,
            token_usage=token_usage,
            event_writer_failures=result.event_writer_failures,
            model=spec.model,
        )


def _worker_extra_env(spec: EnvelopeSpec) -> Dict[str, str]:
    """The env the claude worker gets on top of the scrubbed ambient one."""
    # A-bis-2: the headless lane never exported VNX_DATA_DIR into the
    # worker's own shell, so a worker guessing "$VNX_DATA_DIR/unified_reports/
    # <id>.md" (base_worker.md) resolved it wherever its cwd happened to
    # land — often not the central store GOVERN reads back from (measured
    # 2026-09-06: reports orphaned in a repo-local .vnx-data instead of the
    # central one). VNX_REPORT_PATH gives the worker the exact absolute
    # file to write, no guessing required; VNX_DATA_DIR/_EXPLICIT mirror
    # the two-key contract other lanes already export (plan_gate_panel.py).
    _report_path = Path(spec.data_dir) / "unified_reports" / f"{spec.dispatch_id}.md"
    extra_env = {
        **_switch_stamps(spec),
        "VNX_DATA_DIR": str(spec.data_dir),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_REPORT_PATH": str(_report_path),
    }

    # OI-1886: the tmux lane's VNX_WORKER_ROLE / VNX_DISPATCH_PATHS export
    # (TmuxInteractiveDispatch._spawn_session) was removed with the tmux lane
    # itself (#1868) and never replaced here, so a headless dispatch declaring
    # dispatch_paths was never actually narrowed by the PreToolUse worker-scope
    # hook — only the role's coarse file_write_scope applied. Mirrors
    # provider_dispatch._worker_role_env via the same shared setters.
    from dispatch_identity import normalize_role  # noqa: PLC0415
    from worker_permissions import dispatch_paths_env_value  # noqa: PLC0415

    _resolved_role = normalize_role(spec.role)
    if _resolved_role:
        extra_env["VNX_WORKER_ROLE"] = _resolved_role
    _dp_env = dispatch_paths_env_value(spec.dispatch_paths)
    if _dp_env is not None:
        extra_env["VNX_DISPATCH_PATHS"] = _dp_env
    return extra_env


def _materialize_shadow_skills(spec: EnvelopeSpec, cwd: Optional[Path]) -> None:
    """Agent-layout step before the spawn: the shadow copy or the native claude copy.

    The mode comes from ``agent_layout.layout_mode()`` (``off`` does nothing). Fail-open:
    any miss or exception logs one WARNING and the spawn starts as it would without it.
    """
    from dispatch_identity import normalize_role

    try:
        import agent_layout

        mode = agent_layout.layout_mode()
        if mode == "off":
            return
        project_id = None
        try:
            from project_scope import resolve_stamp_project_id

            project_id = resolve_stamp_project_id()
        except Exception:  # vnx-silent-except: an unresolved tenant is reported as no_project_id
            project_id = None
        from agent_skill_materialize import materialize_skill_set

        materialize_skill_set(
            normalize_role(spec.role), worktree=cwd, provider="claude", mode=mode,
            dispatch_id=spec.dispatch_id, state_dir=spec.state_dir, project_id=project_id,
        )
    except Exception as exc:  # fail-open: the materialisation never stops a dispatch
        logger.warning("agent skill materialisation for role %r: %s", spec.role, type(exc).__name__)


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
        cwd: Optional[Path] = None, on_spawn: Optional[Callable[[int], None]] = None,
    ) -> _AdapterResult:
        from env_scrub_patterns import DEFAULT_SCRUB_KEY_PATTERNS  # noqa: PLC0415
        from provider_spawns.claude_spawn import spawn_claude  # noqa: PLC0415

        extra_env = _worker_extra_env(spec)
        _materialize_shadow_skills(spec, cwd)

        try:
            result = spawn_claude(
                prompt=spec.instruction,
                model=spec.model,
                dispatch_id=spec.dispatch_id,
                terminal_id=spec.terminal_id,
                event_writer=event_writer,
                cwd=cwd,
                # OI-865: the plan's explicit per-dispatch opt-in reaches the
                # scope args — True keeps the ambient MCP config, False (the
                # default) is today's force-empty scoped posture.
                role=spec.role, requires_mcp=spec.requires_mcp,
                total_deadline=float(spec.deadline_seconds),
                scrub_env_keys=DEFAULT_SCRUB_KEY_PATTERNS,
                # K-4c (ADR-045 §5): hands the caller the child's pid the moment
                # claude actually spawned, so it can record start-time evidence.
                extra_env=extra_env, on_spawn=on_spawn,
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
        refused = getattr(result, "spawn_refused", None)
        common = {
            "returncode": result.returncode,
            "completion_text": result.completion_text or "",
            "token_usage": token_usage,
            "session_id": result.session_id,
            "model_resolved": getattr(result, "model_resolved", None),
            "model": getattr(result, "model", None) or spec.model,
            # ADR-043: no claude process started; GOVERN books a zero cost-ledger line.
            "spawn_refused": refused if isinstance(refused, str) else None,
        }

        if result.error:
            return _AdapterResult(status="failure", error=result.error, **common)
        if result.timed_out:
            return _AdapterResult(status="timeout", timed_out=True, **common)
        if result.stopped_early:
            return _AdapterResult(status="success", **common)
        return _AdapterResult(status="success" if result.returncode == 0 else "failure", **common)
