#!/usr/bin/env python3
"""vnx status — show current VNX project dispatch and agent status."""

import json
import os
from pathlib import Path


def vnx_status(args) -> int:
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    emit_json = getattr(args, "json", False)

    vnx_data = project_dir / ".vnx-data"

    if not vnx_data.is_dir():
        if emit_json:
            print(json.dumps({"initialized": False, "error": "not initialized"}))
        else:
            print("VNX project not initialized. Run `vnx init` first.")
        return 1

    # Active dispatches
    active_dir = vnx_data / "dispatches" / "active"
    active_files = list(active_dir.glob("*")) if active_dir.is_dir() else []
    active_count = len([f for f in active_files if f.is_file()])

    # Recent completed dispatches (last 5 by mtime)
    completed_dir = vnx_data / "dispatches" / "completed"
    completed_files: list[Path] = []
    if completed_dir.is_dir():
        completed_files = sorted(
            [f for f in completed_dir.iterdir() if f.is_file()],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )[:5]

    recent_completions = [f.name for f in completed_files]

    # Agents
    agents_dir = project_dir / "agents"
    agent_names: list[str] = []
    if agents_dir.is_dir():
        agent_names = sorted(
            d.name for d in agents_dir.iterdir() if d.is_dir()
        )

    if emit_json:
        output = {
            "initialized": True,
            "project_dir": str(project_dir),
            "active_dispatches": active_count,
            "recent_completions": recent_completions,
            "agents": agent_names,
            "agent_count": len(agent_names),
        }
        print(json.dumps(output, indent=2))
    else:
        print(f"VNX status — {project_dir}")
        print()
        print(f"  Active dispatches : {active_count}")
        print(f"  Agents            : {len(agent_names)}")
        if agent_names:
            for name in agent_names:
                print(f"    - {name}")
        else:
            print("    (none — add subdirs to agents/)")
        print()
        if recent_completions:
            print("  Recent completions (last 5):")
            for name in recent_completions:
                print(f"    - {name}")
        else:
            print("  Recent completions: none")

    return 0
