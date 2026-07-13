#!/usr/bin/env python3
"""vnx dispatch-agent — dispatch a task to a named agent."""

import shutil
import sys
import uuid
from pathlib import Path

from vnx_cli import _engine


def _resolve_agent_path(project_dir: Path, agent: str) -> Path | None:
    """Resolve an agent CLAUDE.md.

    Order: project ``agents/`` and ``examples/`` (project-local wins), then the
    engine's ``agents/`` (the FLEET-WIDE shared library — backend-developer,
    frontend-developer, system-architect, quality-engineer, security-engineer,
    code-reviewer, and the content agents), then the engine's ``examples/``
    (packaged demos). The engine ``agents/`` fallback is what lets any project
    dispatch a generic dev-worker without keeping its own per-project copy.
    """
    candidates = [
        project_dir / "agents" / agent / "CLAUDE.md",
        project_dir / "examples" / agent / "CLAUDE.md",
        _engine.engine_root() / "agents" / agent / "CLAUDE.md",
        _engine.engine_root() / "examples" / agent / "CLAUDE.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _read_default_instruction(config_path: Path) -> str | None:
    """Read default_instruction value from config.yaml using line-by-line parse.

    Avoids a PyYAML dependency in the pip console-script package.
    Only handles top-level scalar values (not multi-line or anchored YAML).
    """
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("default_instruction:"):
                value = stripped.split(":", 1)[1].strip()
                return value.strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _infer_provider_for_model(model: str) -> str:
    """Derive the honoring provider for a requested ``--model`` string.

    Reuses dispatch_bridge._canonical_provider for exact provider-name matches
    (covers the reported case, ``--model kimi`` -> Provider.KIMI) and falls
    back to the same provider-signature substring convention the
    kimi-via-cli-only guard (constraint_enforcer.py) already applies to model
    strings, for provider-specific model ids that aren't themselves a bare
    provider name (kimi-k2-6, glm-5.1, deepseek-v4-pro, gemini-2.5-pro, ...).

    Raises ValueError when no provider can be honored, so the caller can
    hard-error before dispatch instead of letting the provider silently
    default to "claude" (the dispatch-agent-lane-coercion bug).
    """
    from dispatch_bridge import _canonical_provider  # type: ignore[import]  # noqa: PLC0415
    from dispatch_spec import Provider  # type: ignore[import]  # noqa: PLC0415

    normalized = (model or "").strip().lower()
    if not normalized:
        return Provider.CLAUDE.value

    # Exact provider-name match (kimi, codex, gemini, claude, glm, zai,
    # deepseek-harness, glm-harness, local-gemma, auto, ...).
    try:
        return _canonical_provider(normalized).value
    except ValueError:
        pass

    # Bare claude model tiers/aliases are not provider names themselves.
    if normalized in {"sonnet", "opus", "haiku"} or normalized.startswith(
        ("claude-", "opus-", "sonnet-", "haiku-")
    ):
        return Provider.CLAUDE.value

    # Provider-specific model-id substrings — the same convention the
    # kimi-via-cli-only guard already uses ("kimi" in model_norm).
    for needle, provider in (
        ("kimi", Provider.KIMI),
        ("glm", Provider.GLM_HARNESS),
        ("zai", Provider.GLM_HARNESS),
        ("deepseek", Provider.DEEPSEEK_HARNESS),
        ("gemini", Provider.GEMINI),
        ("gemma", Provider.LOCAL_GEMMA),
        ("gpt", Provider.CODEX),
        ("codex", Provider.CODEX),
    ):
        if needle in normalized:
            return provider.value

    raise ValueError(
        f"--model {model!r} does not map to any honorable provider lane. "
        "Use a claude model (sonnet/opus/haiku), a kimi model (kimi*), a glm "
        "model (glm*/zai*), a deepseek model (deepseek*), a gemini model "
        "(gemini*), or pass a provider name directly "
        "(claude/codex/gemini/kimi/glm-harness/deepseek-harness/local-gemma)."
    )


def _resolve_agent_config(
    agent: str,
    agent_claude_md: Path,
    project_dir: Path,
) -> tuple[dict[str, object] | None, bool]:
    """Load extended agent config when VNX_AGENT_FOLDERS is enabled.

    Returns (agent_config, enabled). On import or parse errors, falls back to
    legacy behavior (config=None) so a broken resolver never blocks dispatch.
    """
    try:
        from agent_resolver import agent_folders_enabled, resolve_agent
    except ImportError:
        return None, False

    if not agent_folders_enabled():
        return None, False

    agent_config = resolve_agent(
        agent,
        project_dir=project_dir,
        engine_root=_engine.engine_root(),
    )
    if agent_config is None or agent_config.claude_md != agent_claude_md:
        return None, True
    return {
        "provider": agent_config.provider,
        "model": agent_config.model,
        "default_instruction": agent_config.default_instruction,
    }, True


def vnx_dispatch_agent(args) -> int:
    agent = args.agent
    instruction = getattr(args, "instruction", None)
    model = getattr(args, "model", None)
    explicit_model = model  # user's raw --model override, if any (captured before defaulting)
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # Validate agent CLAUDE.md exists
    agent_claude_md = _resolve_agent_path(project_dir, agent)
    if agent_claude_md is None:
        print(
            f"Error: agent '{agent}' not found. "
            f"Expected: agents/{agent}/CLAUDE.md or examples/{agent}/CLAUDE.md",
            file=sys.stderr,
        )
        return 1

    # Add the packaged engine to sys.path so subprocess_dispatch is importable
    # for both editable checkouts and pip-installed wheels. Also required so
    # scripts/lib/agent_resolver.py can be imported in the pip package layout.
    _engine.ensure_engine_on_path()

    agent_config, agent_folders_on = _resolve_agent_config(agent, agent_claude_md, project_dir)

    # Resolve instruction: explicit arg > config default_instruction > legacy scan
    if not instruction:
        if agent_config is not None:
            instruction = agent_config.get("default_instruction")  # type: ignore[assignment]
        if not instruction:
            config_path = agent_claude_md.parent / "config.yaml"
            instruction = _read_default_instruction(config_path)

    if not instruction:
        print(
            f"Error: --instruction is required for agent '{agent}' "
            "(no default_instruction found in config.yaml).",
            file=sys.stderr,
        )
        return 1

    # Resolve model: explicit arg > config model > legacy default sonnet
    if not model:
        if agent_config is not None and agent_config.get("model"):
            model = agent_config["model"]  # type: ignore[assignment]
        else:
            model = "sonnet"

    # Resolve provider (root cause of dispatch-agent-lane-coercion, 20260713-LANECOERCE):
    # --model kimi previously discarded agent_config["provider"] entirely and deliver_via_door
    # silently defaulted provider="claude", spawning a claude-subscription worker for a kimi
    # request with no error and no honored kimi-via-cli-only routing.
    #   1. An explicit --model override always wins — the user's choice must be honored via the
    #      matching provider, not silently re-routed to whatever the agent's config declares.
    #   2. Otherwise trust the agent's own agent_config["provider"] when set (it may legitimately
    #      declare a provider without also pinning a specific model).
    #   3. Otherwise infer from the resolved (possibly default "sonnet") model.
    # A model that maps to no honorable provider hard-errors BEFORE dispatch instead of silently
    # coercing to claude.
    if explicit_model:
        try:
            provider = _infer_provider_for_model(explicit_model)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    elif agent_config is not None and agent_config.get("provider"):
        provider = str(agent_config["provider"])
    else:
        try:
            provider = _infer_provider_for_model(model)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    # Generate a dispatch ID
    dispatch_id = f"D-{uuid.uuid4().hex[:8]}"

    try:
        from subprocess_dispatch import deliver_with_recovery  # type: ignore[import]
        from dispatch_bridge import deliver_via_door  # type: ignore[import]
        from dispatch_flags import single_entry_enabled  # type: ignore[import]
    except ImportError as exc:
        print(
            f"Error: could not import subprocess_dispatch: {exc}\n"
            "Ensure scripts/lib/ exists in the project directory.",
            file=sys.stderr,
        )
        return 1

    # The legacy fallback lane (deliver_with_recovery / subprocess_dispatch) only ever drives the
    # `claude` CLI — it has no concept of provider. When the single-entry door is disabled
    # (VNX_DISPATCH_LEGACY=1 / VNX_SINGLE_ENTRY_DISPATCH=0) it would silently spawn a claude
    # worker for a non-claude provider instead of honoring it, reintroducing the exact
    # dispatch-agent-lane-coercion bug this fix closes on the door path. Hard-error instead.
    if provider != "claude" and not single_entry_enabled():
        print(
            f"Error: --model {model!r} resolves to provider {provider!r}, but the single-entry "
            "dispatch door is disabled (VNX_DISPATCH_LEGACY=1 / VNX_SINGLE_ENTRY_DISPATCH=0). "
            "The legacy dispatch lane only drives the claude CLI and cannot honor a non-claude "
            "provider. Unset VNX_DISPATCH_LEGACY / VNX_SINGLE_ENTRY_DISPATCH to use the door, "
            "or request a claude model.",
            file=sys.stderr,
        )
        return 1

    # Preflight (audit high #6): the default lane drives an installed, authenticated `claude` CLI as
    # a subprocess. A missing binary otherwise surfaces only as a bare "status: failed". Scoped to
    # the claude lane — a kimi/codex/gemini/glm dispatch has no dependency on the `claude` binary.
    if provider == "claude" and shutil.which("claude") is None:
        print(
            "Warning: 'claude' CLI not found on PATH. The default dispatch lane drives an installed, "
            "authenticated `claude` CLI as a subprocess.\n"
            "Install + authenticate it (or select a different lane via the model/provider), then "
            "re-run. Run `vnx doctor` to check worker CLIs.",
            file=sys.stderr,
        )

    # Derive the project_id from the TARGET project (--project-dir), not the
    # CLI/engine cwd. Without this the door falls back to _resolve_project_id()
    # which reads the engine location (vnx-dev), so a consumer dispatch lands its
    # entire governance state — receipt, report, spec, events, log — in the WRONG
    # store (cross-project audit contamination; sales-copilot -> vnx-dev). The
    # door accepts project_id and prefers it when the caller knows it.
    project_id = _engine.derive_project_id(project_dir)

    print(f"Dispatching to agent '{agent}' (dispatch_id={dispatch_id}, project_id={project_id}) ...")

    # Route through the single-entry door (gated by VNX_SINGLE_ENTRY_DISPATCH / VNX_DISPATCH_LEGACY);
    # the legacy subprocess lane runs only when the door is off. codex flip-PR F3: the shipped
    # `vnx dispatch-agent` must honor the flags like scripts/commands/dispatch-agent.sh, not bypass them.
    success = deliver_via_door(
        lambda: deliver_with_recovery(
            terminal_id=agent,
            instruction=instruction,
            model=model,
            dispatch_id=dispatch_id,
            role=agent,
        ),
        instruction_text=instruction,
        dispatch_id=dispatch_id,
        target_slot="T1",
        role=agent,
        provider=provider,
        model=model,
        project_id=project_id,
    )

    status = "done" if success else "failed"
    print(f"dispatch_id : {dispatch_id}")
    print(f"status      : {status}")

    if not success:
        print(
            "\nDispatch failed. A common cause is a missing or unauthenticated worker CLI. "
            "Run `vnx doctor` to check worker CLIs,\nand see the dispatch log under "
            ".vnx-data/ for the classified failure reason.",
            file=sys.stderr,
        )

    return 0 if success else 1
