"""OI-679: receipt_processor.sh must not declare-and-assign with substitution.

shellcheck SC2155 ("Declare and assign separately to avoid masking return
values") fires on ``local x="$(cmd)"`` / ``export x=$(cmd)``: the declared
variable captures the command's exit status instead of the shell's, so a
failing substitution is silently masked. The finding flagged four lines in
scripts/receipt_processor.sh (report_name, cutoff, now, mtime).

This test encodes the exact SC2155 rule as a static scan so it needs no
shellcheck binary in CI and fails deterministically on the pre-fix code.
Same rule as tests/test_dispatch_sh_sc2155.py (OI-268).
"""

from __future__ import annotations

import re
from pathlib import Path

_RECEIPT_PROCESSOR_SH = Path(__file__).resolve().parent.parent / "scripts" / "receipt_processor.sh"

# shellcheck SC2155: declare/export/assign in one statement whose RHS contains
# a command substitution. ``local x`` followed by ``x=$(...)`` on a SEPARATE
# line is the compliant idiom and must not match. ``$((...))`` arithmetic
# expansion is NOT a command substitution (shellcheck does not flag it), so
# ``$`` must be followed by ``(`` whose next char is not ``(``.
_SC2155_RE = re.compile(
    r"(?:\b(?:local|export|declare)\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*.*?\$\((?!\())"
)


def test_no_sc2155_declare_assign_in_receipt_processor():
    assert _RECEIPT_PROCESSOR_SH.exists(), f"receipt_processor.sh missing at {_RECEIPT_PROCESSOR_SH}"
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(_RECEIPT_PROCESSOR_SH.read_text(encoding="utf-8").splitlines(), 1):
        if _SC2155_RE.search(line):
            hits.append((lineno, line.strip()))
    assert not hits, (
        "shellcheck SC2155 declare-and-assign patterns (masked return values) at:\n"
        + "\n".join(f"  {ln}: {text}" for ln, text in hits)
    )
