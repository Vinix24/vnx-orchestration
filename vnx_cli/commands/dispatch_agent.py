#!/usr/bin/env python3
"""vnx dispatch-agent — dispatch a task to a named agent."""

import sys
import uuid
from pathlib import Path

from vnx_cli import _engine


def _resolve_agent_path(project_dir: Path, agent: str) -> Path | None:
    """Resolve an agent CLAUDE.md from project agents/, examples/, or packaged examples."""
    candidates = [
        project_dir / "agents" / agent / "CLAUDE.md",
        project_dir / "examples" / agent / "CLAUDE.md",
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


def vnx_dispatch_agent(args) -> int:
    agent = args.agent
    instruction = getattr(args, "instruction", None)
    model = getattr(args, "model", "sonnet")
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

    # Resolve instruction: explicit arg > default_instruction in config.yaml
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

    # Generate a dispatch ID
    dispatch_id = f"D-{uuid.uuid4().hex[:8]}"

    # Add the packaged engine to sys.path so subprocess_dispatch is importable
    # for both editable checkouts and pip-installed wheels.
    _engine.ensure_engine_on_path()

    try:
        from subprocess_dispatch import deliver_with_recovery  # type: ignore[import]
        from dispatch_bridge import deliver_via_door  # type: ignore[import]
    except ImportError as exc:
        print(
            f"Error: could not import subprocess_dispatch: {exc}\n"
            "Ensure scripts/lib/ exists in the project directory.",
            file=sys.stderr,
        )
        return 1

    print(f"Dispatching to agent '{agent}' (dispatch_id={dispatch_id}) ...")

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
        model=model,
    )

    status = "done" if success else "failed"
    print(f"dispatch_id : {dispatch_id}")
    print(f"status      : {status}")

    return 0 if success else 1
