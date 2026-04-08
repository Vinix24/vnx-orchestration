#!/usr/bin/env python3
"""Headless T0 sandbox setup — creates an isolated fake VNX environment in /tmp.

Usage:
    python3 tests/headless_t0/setup_sandbox.py --create
    python3 tests/headless_t0/setup_sandbox.py --destroy
    python3 tests/headless_t0/setup_sandbox.py --reset
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# Real repo root (resolved from this file's location: tests/headless_t0/setup_sandbox.py)
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SANDBOX_PATH = Path("/tmp/vnx-headless-t0-test")

# Subdirectories to create inside the sandbox
_DIRS = [
    ".vnx-data/state",
    ".vnx-data/dispatches/pending",
    ".vnx-data/dispatches/active",
    ".vnx-data/dispatches/completed",
    ".vnx-data/unified_reports",
    ".vnx-data/events",
    "scripts",
]

# Minimal CLAUDE.md injected into sandbox root — T0 role identity
_SANDBOX_CLAUDE_MD = """\
# T0 — Orchestrator Agent (Sandbox)

You are the VNX T0 orchestrator running in an isolated test sandbox.

## Your Role
- Review worker receipts from .vnx-data/state/t0_receipts.ndjson
- Read referenced reports from .vnx-data/unified_reports/
- Verify worker claims using filesystem tools (Read, Bash, Grep)
- Create dispatches in .vnx-data/dispatches/pending/
- Manage open items via the open_items structure

## Key Paths (relative to cwd)
- `.vnx-data/state/t0_brief.json` — terminal status, queue state
- `.vnx-data/state/t0_receipts.ndjson` — incoming receipts
- `.vnx-data/state/open_items.json` — open items list
- `.vnx-data/state/t0_recommendations.json` — suggested next actions
- `.vnx-data/dispatches/pending/` — write new dispatches here
- `.vnx-data/unified_reports/` — worker reports referenced by receipts

## Dispatch Format
Dispatches must begin with `[[TARGET:A]]`, `[[TARGET:B]]`, or `[[TARGET:C]]`
followed by a Manager Block header section.

## Rules
- Always verify claims before approving (check files, line counts, test results)
- Never approve a receipt without reading the referenced report_path
- Do not merge without gate evidence
- Write decisions as structured output when asked
"""


# ---------------------------------------------------------------------------
# Core sandbox management
# ---------------------------------------------------------------------------

def create_sandbox(sandbox: Path = SANDBOX_PATH, repo_root: Path = REPO_ROOT) -> Path:
    """Build the full sandbox directory structure with initial fake state."""
    sandbox.mkdir(parents=True, exist_ok=True)

    for d in _DIRS:
        (sandbox / d).mkdir(parents=True, exist_ok=True)

    # CLAUDE.md — T0 identity
    (sandbox / "CLAUDE.md").write_text(_SANDBOX_CLAUDE_MD)

    # Symlink scripts/lib → real repo scripts/lib
    scripts_lib_link = sandbox / "scripts" / "lib"
    if not scripts_lib_link.exists():
        scripts_lib_link.symlink_to(repo_root / "scripts" / "lib")

    # Symlink individual manager scripts
    for script_name in ("open_items_manager.py", "pr_queue_manager.py"):
        link = sandbox / "scripts" / script_name
        if not link.exists():
            link.symlink_to(repo_root / "scripts" / script_name)

    # Initial state files
    _write_initial_state(sandbox)

    return sandbox


def _write_initial_state(sandbox: Path) -> None:
    """Write default fake state files into the sandbox."""
    from fake_data import (  # type: ignore[import]
        fake_dispatch,
        fake_open_items,
        fake_t0_brief,
    )

    state = sandbox / ".vnx-data" / "state"

    # t0_brief.json — T1 working, T2/T3 idle
    (state / "t0_brief.json").write_text(
        json.dumps(
            fake_t0_brief(
                t1_status="working",
                t2_status="idle",
                t3_status="idle",
                active=1,
                t1_dispatch="20260407-sandbox-init-A",
            ),
            indent=2,
        )
    )

    # t0_receipts.ndjson — empty initially
    (state / "t0_receipts.ndjson").write_text("")

    # open_items.json — 2 blockers, 3 warnings
    (state / "open_items.json").write_text(
        json.dumps(fake_open_items(blockers=2, warnings=3), indent=2)
    )

    # progress_state.yaml — minimal
    (state / "progress_state.yaml").write_text(
        "version: '1.0'\n"
        "updated_at: '2026-04-07T12:00:00Z'\n"
        "tracks:\n"
        "  A:\n"
        "    current_gate: implementation\n"
        "    status: working\n"
        "    active_dispatch_id: 20260407-sandbox-init-A\n"
        "  B:\n"
        "    current_gate: implementation\n"
        "    status: idle\n"
        "    active_dispatch_id: null\n"
        "  C:\n"
        "    current_gate: implementation\n"
        "    status: idle\n"
        "    active_dispatch_id: null\n"
    )

    # t0_recommendations.json — suggest dispatching Track B
    (state / "t0_recommendations.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-07T12:00:00Z",
                "total_recommendations": 1,
                "recommendations": [
                    {
                        "trigger": "idle_terminal",
                        "action": "create_dispatch",
                        "gate": "implementation",
                        "reason": "T2 is idle and Track B has pending work",
                        "priority": "P1",
                        "suggested_role": "test-engineer",
                        "suggested_terminal": "T2",
                    }
                ],
            },
            indent=2,
        )
    )

    # Active dispatch
    active_dispatch = fake_dispatch(
        dispatch_id="20260407-sandbox-init-A",
        track="A",
        terminal="T1",
        role="backend-developer",
        instruction="Implement the sandbox feature as specified.",
    )
    (sandbox / ".vnx-data" / "dispatches" / "active" / "20260407-sandbox-init-A.md").write_text(
        active_dispatch
    )


def destroy_sandbox(sandbox: Path = SANDBOX_PATH) -> None:
    """Remove the sandbox directory entirely."""
    if sandbox.exists():
        shutil.rmtree(sandbox)


def reset_sandbox(sandbox: Path = SANDBOX_PATH, repo_root: Path = REPO_ROOT) -> Path:
    """Destroy then recreate the sandbox."""
    destroy_sandbox(sandbox)
    return create_sandbox(sandbox, repo_root)


# ---------------------------------------------------------------------------
# State mutation helpers
# ---------------------------------------------------------------------------

def inject_receipt(
    dispatch_id: str,
    terminal: str,
    status: str,
    report_path: str,
    track: str = "A",
    gate: str = "implementation",
    sandbox: Path = SANDBOX_PATH,
) -> None:
    """Append a fake receipt line to t0_receipts.ndjson."""
    from fake_data import fake_receipt  # type: ignore[import]

    receipts_file = sandbox / ".vnx-data" / "state" / "t0_receipts.ndjson"
    line = fake_receipt(dispatch_id, terminal, status, report_path, track, gate)
    with open(receipts_file, "a") as f:
        f.write(line + "\n")


def inject_report(filename: str, content: str, sandbox: Path = SANDBOX_PATH) -> Path:
    """Write a fake unified report to .vnx-data/unified_reports/."""
    reports_dir = sandbox / ".vnx-data" / "unified_reports"
    report_path = reports_dir / filename
    report_path.write_text(content)
    return report_path


def set_terminal_status(
    terminal: str,
    status: str,
    dispatch_id: str | None = None,
    sandbox: Path = SANDBOX_PATH,
) -> None:
    """Update a terminal's status in t0_brief.json."""
    brief_file = sandbox / ".vnx-data" / "state" / "t0_brief.json"
    brief = json.loads(brief_file.read_text())

    term = brief["terminals"].get(terminal, {})
    term["status"] = status
    term["ready"] = status == "idle"
    if dispatch_id and status != "idle":
        term["current_task"] = dispatch_id
    elif "current_task" in term and status == "idle":
        del term["current_task"]
    brief["terminals"][terminal] = term

    # Mirror in tracks
    track_map = {"T1": "A", "T2": "B", "T3": "C"}
    track_key = track_map.get(terminal)
    if track_key and track_key in brief.get("tracks", {}):
        brief["tracks"][track_key]["status"] = status
        brief["tracks"][track_key]["active_dispatch_id"] = dispatch_id

    brief_file.write_text(json.dumps(brief, indent=2))


def write_fake_file(
    relative_path: str,
    content: str,
    sandbox: Path = SANDBOX_PATH,
) -> Path:
    """Write an arbitrary file into the sandbox for claim-verification tests."""
    target = sandbox / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_sys_path() -> None:
    """Ensure tests/headless_t0/ is on sys.path so fake_data imports work."""
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


def main(argv: list[str] | None = None) -> int:
    _add_sys_path()

    parser = argparse.ArgumentParser(description="Manage headless T0 test sandbox")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="Create sandbox")
    group.add_argument("--destroy", action="store_true", help="Destroy sandbox")
    group.add_argument("--reset", action="store_true", help="Destroy then create sandbox")
    parser.add_argument(
        "--sandbox",
        type=Path,
        default=SANDBOX_PATH,
        help=f"Sandbox path (default: {SANDBOX_PATH})",
    )
    args = parser.parse_args(argv)

    if args.create:
        path = create_sandbox(args.sandbox)
        print(f"Sandbox created: {path}")
    elif args.destroy:
        destroy_sandbox(args.sandbox)
        print(f"Sandbox destroyed: {args.sandbox}")
    elif args.reset:
        path = reset_sandbox(args.sandbox)
        print(f"Sandbox reset: {path}")
    return 0


if __name__ == "__main__":
    _add_sys_path()
    sys.exit(main())
