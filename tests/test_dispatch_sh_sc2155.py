"""OI-268: dispatch.sh must not declare-and-assign with command substitution.

shellcheck SC2155 ("Declare and assign separately to avoid masking return
values") fires on ``local x="$(cmd)"`` / ``export x=$(cmd)``: the variable
captures the command's exit status instead of the shell's, so a failing
substitution is silently masked. The finding flagged two lines in
scripts/commands/dispatch.sh (active_path, completed_path).

This test encodes the exact SC2155 rule as a static scan so it needs no
shellcheck binary in CI and fails deterministically on the pre-fix code.
"""

from __future__ import annotations

import re
from pathlib import Path

_DISPATCH_SH = Path(__file__).resolve().parent.parent / "scripts" / "commands" / "dispatch.sh"

# shellcheck SC2155: declare/export/assign in one statement whose RHS contains
# a command substitution. ``local x`` followed by ``x=$(...)`` on a SEPARATE
# line is the compliant idiom and must not match.
_SC2155_RE = re.compile(
    r"(?:\b(?:local|export|declare)\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*.*?\$\()"
)


def test_no_sc2155_declare_assign_in_dispatch_sh():
    assert _DISPATCH_SH.exists(), f"dispatch.sh missing at {_DISPATCH_SH}"
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(_DISPATCH_SH.read_text(encoding="utf-8").splitlines(), 1):
        if _SC2155_RE.search(line):
            hits.append((lineno, line.strip()))
    assert not hits, (
        "shellcheck SC2155 declare-and-assign patterns (masked return values) at:\n"
        + "\n".join(f"  {ln}: {text}" for ln, text in hits)
    )
