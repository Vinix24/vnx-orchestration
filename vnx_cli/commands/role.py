#!/usr/bin/env python3
"""vnx role — Python entrypoint for the fleet-wide canonical T0 role sync.

Delegates to bin/vnx's bash implementation (`cmd_role_sync`) instead of
reimplementing the marked-block merge logic a second time: a single
implementation shared by both CLI surfaces guarantees dual-CLI parity by
construction rather than by ongoing maintenance discipline. Same precedent as
`patch_agent_files` in scripts/vnx_init.py, which shells out to
`bin/vnx patch-agent-files`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from vnx_cli import _engine


def vnx_role(args) -> int:
    """Dispatch `vnx role <subcommand>`."""
    sub = getattr(args, "role_subcommand", None)
    if sub is None:
        print("Usage: vnx role sync [--apply|--dry-run] [--project-dir DIR]", file=sys.stderr)
        return 1
    if sub == "sync":
        return _vnx_role_sync(args)
    print(f"ERROR: [role] unknown subcommand: {sub!r} (try 'vnx role sync')", file=sys.stderr)
    return 1


def _vnx_role_sync(args) -> int:
    """`vnx role sync` — refresh the canonical T0 role in the target repo.

    Resolves the bash implementation from the SAME engine tree this Python CLI
    was loaded from (`_engine.engine_root()`), so the shipped canonical role
    (`.claude/terminals/T0/role-orchestrator.md`) that bash's `$VNX_HOME`
    resolves to is exactly the tree backing this command.
    """
    engine_root = _engine.engine_root()
    vnx_bin = engine_root / "bin" / "vnx"
    if not vnx_bin.exists():
        print(
            f"ERROR: [role-sync] bash implementation not found: {vnx_bin}\n"
            "  'vnx role sync' requires a full VNX checkout (bin/vnx present) — "
            "not available from this install.",
            file=sys.stderr,
        )
        return 1

    cmd = [str(vnx_bin), "role", "sync"]

    # Default "." means "let bash resolve the target from cwd's git root" —
    # the Bug-2 fix. An explicit --project-dir is a deliberate override, passed
    # straight through (resolved to an absolute path so it survives regardless
    # of the subprocess's cwd).
    project_dir = getattr(args, "project_dir", ".") or "."
    if project_dir != ".":
        cmd.extend(["--project-dir", str(Path(project_dir).resolve())])

    cmd.append("--apply" if getattr(args, "apply", False) else "--dry-run")

    result = subprocess.run(cmd)
    return result.returncode
