#!/usr/bin/env python3
"""vnx dispatch-agent — dispatch a task to a named agent."""

import sys
import uuid
from pathlib import Path


def vnx_dispatch_agent(args) -> int:
    agent = args.agent
    instruction = args.instruction
    model = getattr(args, "model", "sonnet")
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # Validate agent CLAUDE.md exists
    agent_claude_md = project_dir / "agents" / agent / "CLAUDE.md"
    if not agent_claude_md.exists():
        print(
            f"Error: agent '{agent}' not found. "
            f"Expected: agents/{agent}/CLAUDE.md",
            file=sys.stderr,
        )
        return 1

    # Generate a dispatch ID
    dispatch_id = f"D-{uuid.uuid4().hex[:8]}"

    # Add scripts/lib/ to sys.path so subprocess_dispatch is importable
    scripts_lib = project_dir / "scripts" / "lib"
    if scripts_lib.is_dir() and str(scripts_lib) not in sys.path:
        sys.path.insert(0, str(scripts_lib))

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
