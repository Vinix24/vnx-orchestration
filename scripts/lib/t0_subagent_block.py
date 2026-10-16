#!/usr/bin/env python3
"""t0_subagent_block — the command the settings templates register for the T0 subagent block.

``scripts/hooks/pretooluse_t0_no_subagents.py`` recognises a T0 session by its cwd OR by the
launch directory in its transcript path, because the cwd of a T0 session drifts: 3 of 143 real
T0 sessions also carried the operator's home directory as cwd. The hook shim's rule 1 tests the
cwd and exits 0 when it lies outside a project with a T0 folder, which is exactly the moment the
transcript test exists for. So this entry is started without the shim, in every session, and
decides itself.

``registered_command(vnx_home)`` renders the inline command: it tests that the entry exists,
runs it with ``python3`` and maps every non-zero exit to 1, the same exit contract as
``hook_coverage.registered_command``. No case exits 2, which Claude Code would read as a block
of every Agent call in every session: the entry missing (exit 1 with the path on stderr), the
entry removed between the test and the start (python3 exits 2), or no ``python3`` (127). The
entry decides through JSON on stdout and never through its exit code.

The three settings templates carry this command; a test pins each one to this function's
output. ``vnx_settings_merge`` and ``vnx init`` render the VNX home through it at render time,
so a value outside the allowlist of ``hook_coverage.registered_command`` is refused there.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import hook_coverage as hc

ENTRY = "pretooluse_t0_no_subagents.py"
MATCHER = "Agent|Task"


def registered_command(vnx_home: str = hc.DEFAULT_VNX_HOME) -> str:
    """The inline command for the subagent block, with ``vnx_home`` the directory holding
    ``scripts/hooks/pretooluse_t0_no_subagents.py``. Raises ValueError naming the value when
    ``vnx_home`` is not one of the forms ``hook_coverage.registered_command`` accepts."""
    # One allowlist: the value is checked by the function that defines it.
    hc.registered_command(ENTRY, "project", vnx_home)
    entry = f"{vnx_home.rstrip('/')}/scripts/hooks/{ENTRY}"
    return (
        f"sh -c 'f=\"{entry}\"; "
        "if [ ! -f \"$f\" ]; then echo \"vnx: hook entry missing: $f\" >&2; exit 1; fi; "
        "python3 \"$f\"; rc=$?; [ \"$rc\" -eq 0 ] || rc=1; exit \"$rc\"'"
    )
