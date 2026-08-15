#!/usr/bin/env python3
"""Measure whether a scoped worker's argv actually removes the whole mcp__ namespace.

Dispatch 20260815-mcp-surface-probe found that the scoped default shipped in
#1503 (`--mcp-config '{"mcpServers":{}}' --strict-mcp-config --allowedTools ...`)
did NOT reach the ``mcp__`` tools surfaced by an extension bridge
(claude-in-chrome): those tools appear outside ``mcpServers``, so the empty
``--mcp-config`` leaves them connected and ``--allowedTools`` (an allow-list of
built-in tools) does not restrict the ``mcp__`` namespace either. This script is
the repeatable form of that measurement, plus the fix verification.

It drives the REAL claude CLI (``claude -p``) with the exact scope argv that
``worker_permissions.build_claude_scope_args`` assembles, and asks the session to
report every tool whose name begins with ``mcp__``. Two runs, always:

  1. CONTROL — ``requires_mcp=True``: no empty ``--mcp-config`` and no ``mcp__*``
     deny, so the ambient MCP surface stays connected. This proves the probe CAN
     see ``mcp__`` tools when they are present (a scoped result of NONE is only
     meaningful against a control that is non-empty).
  2. SCOPED — the fabric default: empty ``--mcp-config`` + ``--strict-mcp-config``
     + ``--disallowedTools WebSearch,WebFetch,mcp__*``. Expected surface: NONE.

The verdict is BEHAVIORAL: it compares the tool surface the session reports, not
whether a flag string appears in argv. A flag in argv is the label; the tool list
the session actually sees is the behavior.

Exit code 0 on a clean scoped result (control non-empty, scoped empty). Exit 1
if the scoped run still surfaces any ``mcp__`` tool. Exit 2 if the control is
empty (the scoped NONE is then not independently meaningful), or on any spawn
failure. No hardcoded paths: the lib dir resolves relative to this file.

NOTE: the nested ``claude -p`` inherits this process's env, so under the
DeepSeek harness (ANTHROPIC_BASE_URL set) it routes to DeepSeek; the
``--disallowedTools``/``--mcp-config`` filtering happens client-side in the
claude CLI regardless of backend, so the tool-surface measurement is valid.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LIB_DIR = _HERE.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from worker_permissions import (  # noqa: E402
    MCP_NAMESPACE_DENY,
    build_claude_scope_args,
    default_code_worker_profile,
)

_PROMPT = (
    "List every tool available to you whose name begins with the exact prefix "
    "mcp__. Output only the exact tool names, one per line, nothing else. If "
    "there are none, output the single word NONE."
)


def _run(scope_args: list[str], timeout: int) -> tuple[int, str, str]:
    """Spawn ``claude -p`` with *scope_args* and return (rc, stdout, stderr).

    The prompt is placed BEFORE the scope flags: ``--allowedTools`` /
    ``--disallowedTools`` are variadic and would otherwise swallow the prompt.
    """
    cmd = ["claude", "-p", _PROMPT, *scope_args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _surface(stdout: str) -> list[str]:
    """Parse the reported mcp__ tool surface out of the session's stdout."""
    text = stdout.strip()
    if not text:
        return []
    if text.upper() == "NONE":
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("mcp__")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=int,
        default=150,
        help="per-session timeout in seconds (default: 150)",
    )
    args = parser.parse_args()

    profile = default_code_worker_profile()

    # CONTROL: requires_mcp=True keeps the ambient MCP config (no empty
    # --mcp-config, no mcp__* deny). Whatever mcp tools this sees is the
    # ambient surface the scoped run is supposed to remove.
    control_args = build_claude_scope_args(profile, requires_mcp=True)
    rc_c, out_c, err_c = _run(control_args, args.timeout)
    control_surface = _surface(out_c)

    scoped_args = build_claude_scope_args(profile)
    rc_s, out_s, err_s = _run(scoped_args, args.timeout)
    scoped_surface = _surface(out_s)

    print("=" * 72)
    print("CONTROL (requires_mcp=True) — ambient MCP surface")
    print("argv: claude -p <prompt> " + " ".join(control_args))
    print(f"rc: {rc_c}")
    print(f"mcp__ tools seen: {control_surface if control_surface else 'NONE'}")
    if err_c.strip():
        print(f"stderr: {err_c.strip()[:400]}")
    print("=" * 72)
    print("SCOPED (fabric default) — scoped surface")
    print("argv: claude -p <prompt> " + " ".join(scoped_args))
    print(f"rc: {rc_s}")
    print(f"mcp__ tools seen: {scoped_surface if scoped_surface else 'NONE'}")
    if err_s.strip():
        print(f"stderr: {err_s.strip()[:400]}")
    print("=" * 72)

    if rc_c != 0 or rc_s != 0:
        print("VERDICT: spawn failure (non-zero rc) — measurement invalid")
        return 2

    if not control_surface:
        print(
            "VERDICT: control is empty — no ambient mcp__ tools were detected, "
            "so the scoped NONE is not independently meaningful."
        )
        return 2

    if scoped_surface:
        print(
            f"VERDICT: LEAK — the scoped session still surfaces mcp__ tools "
            f"({', '.join(scoped_surface)}). The mcp__ namespace is NOT closed."
        )
        return 1

    print(
        f"VERDICT: closed — control saw {len(control_surface)} ambient mcp__ "
        f"tool(s), the scoped session saw none. {MCP_NAMESPACE_DENY!r} deny "
        "removes the whole namespace."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
