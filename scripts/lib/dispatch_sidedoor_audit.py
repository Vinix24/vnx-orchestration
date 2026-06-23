#!/usr/bin/env python3
"""dispatch_sidedoor_audit.py — reproducible exhaustiveness gate for PR-12.

The single-entry flip (PR-11) is only safe if EVERY dispatch-delivery path routes through
the door. "We wired the N callers" is an assertion; this is the proof (review finding:
exhaustiveness must be verified, not asserted). It scans for files that invoke a lane script
as a DELIVERY path and reports any that are not on the audited allowlist — so a NEW direct
caller trips the gate before the flag is ever flipped.

It already earned its keep: the static scan caught `dispatch-agent.sh` and `dispatch.sh` as
delivery callers the bridge docstring's "4 callers" missed.

Classifier notes (why it is reproducible, not hand-waved):
  * lines inside docstrings/comments are skipped (triple-quote tracking) — a module that only
    *mentions* a lane in its docstring is not a caller (that was the over-flag in v1).
  * the door (`dispatch_cli.py`) and the lane scripts themselves are excluded — they reference
    the lanes legitimately; the bridge is the SANCTIONED path.
  * benchmarks + provider-spawn machinery are excluded — test harnesses / provider_dispatch's
    own internals, not production delivery side-doors.

Run standalone:  python3 scripts/lib/dispatch_sidedoor_audit.py   (exit 1 if a new caller appears)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Set

_LANES = ("subprocess_dispatch", "provider_dispatch", "tmux_interactive_dispatch")

# Real DELIVERY invocations (not a docstring mention): the lane script named in a spawn/exec
# context, or a delivery-function CALL. Comment/docstring lines are skipped before matching.
_DELIVERY_PATTERNS = [
    re.compile(r"(subprocess_dispatch|provider_dispatch|tmux_interactive_dispatch)\.py"),
    re.compile(r"\bdeliver_with_recovery\s*\("),
    re.compile(r"\b[A-Za-z_]*(provider_dispatch|pd)\.main\s*\("),
]

_EXCLUDE_SUBSTR = (
    "/subprocess_dispatch_internals/",  # the lane's own internals
    "/dispatch_bridge.py",              # the SANCTIONED door bridge
    "/dispatch_cli.py",                 # the door — calls the lanes legitimately
    "/dispatch_sidedoor_audit.py",      # this auditor
    "/__pycache__/",
    "/hooks/pretooluse_",               # guard hooks that BLOCK raw spawns (enforcement)
    "/lane_adapter.py",                 # benchmark adapter
    "/benchmark/", "/benchmarks/",      # benchmark harnesses test the lanes directly
    "/provider_spawns/", "/providers/", # provider_dispatch's own spawn machinery
)
_EXCLUDE_BASENAMES = {f"{n}.py" for n in _LANES}

# Audited delivery callers (PR-2). A scanned file outside this set is a NEW side door and
# fails the gate until it is audited (added here) and wired through dispatch_bridge.
KNOWN_DELIVERY_CALLERS = frozenset({
    "scripts/lib/dispatch_deliver.sh",
    "scripts/lib/pool_worker_runner.py",
    "scripts/lib/headless_dispatch_daemon.py",
    "scripts/lib/adapters/claude_adapter.py",
    "scripts/commands/dispatch-agent.sh",     # caught by the scan (not in the docstring's "4")
    "scripts/commands/dispatch.sh",           # caught by the scan (not in the docstring's "4")
    "scripts/lib/plan_gate_panel.py",         # interim side door PR-7 removes
    "vnx_cli/commands/dispatch_agent.py",     # packaged CLI; now routes through deliver_via_door (flip-PR F3)
})


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _code_lines(text: str):
    """Yield code lines, skipping comment lines and lines inside triple-quoted blocks."""
    in_doc = False
    quote = ""
    for line in text.splitlines():
        s = line.strip()
        if in_doc:
            if quote in line:
                in_doc = False
            continue
        if s.startswith("#"):
            continue
        # opening triple-quote with no matching close on the same line → enter docstring
        for q in ('"""', "'''"):
            if s.startswith(q) and line.count(q) == 1:
                in_doc, quote = True, q
                break
        if in_doc:
            continue
        yield line


def scan_delivery_callers(root: Path | None = None) -> Set[str]:
    """Return repo-relative paths of files that invoke a lane script as a delivery path."""
    root = root or _repo_root()
    callers: Set[str] = set()
    for base in ("scripts", "bin", "vnx_cli"):  # vnx_cli: the packaged CLI ships dispatch entrypoints too (codex flip-PR F3)
        for path in (root / base).rglob("*"):
            if not path.is_file() or path.suffix not in (".py", ".sh"):
                continue
            rel = path.relative_to(root).as_posix()
            if any(s in f"/{rel}" for s in _EXCLUDE_SUBSTR) or path.name in _EXCLUDE_BASENAMES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in _code_lines(text):
                if any(p.search(line) for p in _DELIVERY_PATTERNS):
                    callers.add(rel)
                    break
    return callers


def audit(root: Path | None = None) -> dict:
    """Return {known, found, unaudited}. `unaudited` non-empty = a new side door = gate fails."""
    found = scan_delivery_callers(root)
    return {
        "known": set(KNOWN_DELIVERY_CALLERS),
        "found": found,
        "unaudited": found - KNOWN_DELIVERY_CALLERS,
    }


def main() -> int:
    result = audit()
    print(f"delivery callers found: {len(result['found'])}")
    for c in sorted(result["found"]):
        flag = "  [UNAUDITED — new side door]" if c in result["unaudited"] else ""
        print(f"  {c}{flag}")
    if result["unaudited"]:
        print(f"\nFAIL: {len(result['unaudited'])} unaudited delivery caller(s); audit + wire "
              "through dispatch_bridge before flipping VNX_SINGLE_ENTRY_DISPATCH.", file=sys.stderr)
        return 1
    print("\nOK: no unaudited delivery callers — exhaustiveness holds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
