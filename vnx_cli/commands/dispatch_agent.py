"""vnx dispatch-agent — dispatch a task to a named agent."""

import argparse
import os
import sys
import uuid


def vnx_dispatch_agent(args: argparse.Namespace) -> int:
    agent = args.agent
    instruction = args.instruction
    model = getattr(args, "model", "sonnet")
    project_dir = os.path.abspath(getattr(args, "project_dir", "."))

    # Validate agent exists
    agent_claude_md = os.path.join(project_dir, "agents", agent, "CLAUDE.md")
    if not os.path.isfile(agent_claude_md):
        print(
            f"[x] dispatch-agent: agent '{agent}' not found.\n"
            f"    Expected: {agent_claude_md}\n"
            f"    Run `ls agents/` to see available agents."
        )
        return 1

    dispatch_id = f"dispatch-{uuid.uuid4().hex[:12]}"
    print(f"Dispatching to agent '{agent}' (dispatch_id={dispatch_id}, model={model})")

    # Add scripts/lib/ to sys.path so subprocess_dispatch is importable
    scripts_lib = os.path.join(project_dir, "scripts", "lib")
    if scripts_lib not in sys.path:
        sys.path.insert(0, scripts_lib)

    try:
        from subprocess_dispatch import deliver_with_recovery  # type: ignore
    except ImportError as exc:
        print(
            f"[x] dispatch-agent: could not import deliver_with_recovery.\n"
            f"    Tried scripts/lib/ at: {scripts_lib}\n"
            f"    Error: {exc}\n"
            f"    Ensure scripts/lib/subprocess_dispatch.py is present."
        )
        return 1

    success = deliver_with_recovery(
        terminal_id=agent,
        instruction=instruction,
        model=model,
        dispatch_id=dispatch_id,
        role=agent,
    )

    if success:
        print(f"[ok] dispatch_id={dispatch_id} status=done")
        return 0
    else:
        print(f"[x]  dispatch_id={dispatch_id} status=failed")
        return 1
