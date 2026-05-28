#!/usr/bin/env python3
"""vnx dispatch-agent — dispatch a task to a named agent."""

import sys
import uuid
from pathlib import Path

from vnx_cli import _engine


def _resolve_agent_path(project_dir: Path, agent: str) -> Path | None:
    """Resolve an agent CLAUDE.md from project agents/ or examples/."""
    candidates = [
        project_dir / "agents" / agent / "CLAUDE.md",
        project_dir / "examples" / agent / "CLAUDE.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def vnx_dispatch_agent(args) -> int:
    agent = args.agent
    instruction = args.instruction
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

    # Generate a dispatch ID
    dispatch_id = f"D-{uuid.uuid4().hex[:8]}"

    # Add the packaged engine to sys.path so subprocess_dispatch is importable
    # for both editable checkouts and pip-installed wheels.
    _engine.ensure_engine_on_path()

    try:
        from subprocess_dispatch import deliver_with_recovery  # type: ignore[import]
    except ImportError as exc:
        print(
            f"Error: could not import subprocess_dispatch: {exc}\n"
            "Ensure scripts/lib/ exists in the project directory.",
            file=sys.stderr,
        )
        return 1

    print(f"Dispatching to agent '{agent}' (dispatch_id={dispatch_id}) ...")

    success = deliver_with_recovery(
        terminal_id=agent,
        instruction=instruction,
        model=model,
        dispatch_id=dispatch_id,
        role=agent,
    )

    status = "done" if success else "failed"
    print(f"dispatch_id : {dispatch_id}")
    print(f"status      : {status}")

    return 0 if success else 1
