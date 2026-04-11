"""vnx status — show current VNX project status."""

import argparse
import json
import os


def vnx_status(args: argparse.Namespace) -> int:
    project_dir = os.path.abspath(getattr(args, "project_dir", "."))
    json_mode = getattr(args, "json_output", False)

    vnx_data = os.path.join(project_dir, ".vnx-data")

    if not os.path.isdir(vnx_data):
        if json_mode:
            print(json.dumps({"initialized": False, "error": "not initialized"}))
        else:
            print("vnx status: not initialized (.vnx-data/ not found). Run `vnx init` first.")
        return 1

    active_count = _count_files(os.path.join(vnx_data, "dispatches", "active"))
    recent = _recent_completed(os.path.join(vnx_data, "dispatches", "completed"), n=5)
    agents = _list_agents(os.path.join(project_dir, "agents"))

    if json_mode:
        print(json.dumps({
            "initialized": True,
            "active_dispatches": active_count,
            "recent_completions": recent,
            "agents": agents,
        }, indent=2))
    else:
        _print_human(active_count, recent, agents)

    return 0


def _count_files(directory: str) -> int:
    """Count files in a directory; return 0 if directory doesn't exist."""
    if not os.path.isdir(directory):
        return 0
    return sum(1 for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f)))


def _recent_completed(directory: str, n: int = 5) -> list:
    """Return the n most recently modified files in directory."""
    if not os.path.isdir(directory):
        return []
    entries = []
    for name in os.listdir(directory):
        full = os.path.join(directory, name)
        if os.path.isfile(full):
            entries.append((os.path.getmtime(full), name))
    entries.sort(reverse=True)
    return [name for _, name in entries[:n]]


def _list_agents(agents_dir: str) -> list:
    """List agent names from agents/ directory (subdirs with CLAUDE.md)."""
    if not os.path.isdir(agents_dir):
        return []
    agents = []
    for entry in sorted(os.listdir(agents_dir)):
        full = os.path.join(agents_dir, entry)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "CLAUDE.md")):
            agents.append(entry)
    return agents


def _print_human(active_count: int, recent: list, agents: list) -> None:
    print(f"Active dispatches : {active_count}")
    print(f"Recent completions: {len(recent)}")
    if recent:
        for name in recent:
            print(f"  - {name}")
    print(f"Agents            : {len(agents)}")
    if agents:
        for name in agents:
            print(f"  - {name}")
    else:
        print("  (none — add agent dirs with CLAUDE.md to agents/)")
