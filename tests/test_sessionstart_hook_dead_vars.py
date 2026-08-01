"""OI-684: hooks/sessionstart.sh must not carry dead ROLE/TRACK assignments.

shellcheck SC2034 ("ROLE appears unused") fired on hooks/sessionstart.sh: ROLE
and TRACK were assigned from the terminal-directory case and from the
CLAUDE_ROLE/CLAUDE_TRACK env overrides, but never read anywhere. The hook's
only output is the JSON additionalContext built from ADDITIONAL_CONTEXT;
ROLE/TRACK are not exported and never reach that output, so they are dead.

The fix removed the assignments (and the now-dead env-override block). This
test is a static scan asserting no ``ROLE=`` / ``TRACK=`` variable assignment
remains. ``CLAUDE_ROLE=`` / ``CLAUDE_TRACK=`` env READS are also gone; "Role:"
prose inside the additionalContext text is not an assignment and must not
match.
"""

from __future__ import annotations

import re
from pathlib import Path

_SESSIONSTART_SH = Path(__file__).resolve().parent.parent / "hooks" / "sessionstart.sh"

# A variable assignment ``ROLE=...`` / ``TRACK=...``. The word-boundary before
# the name keeps ``CLAUDE_ROLE`` (env read) from matching — the char before
# "ROLE" there is ``_``, a word char, so ``\b`` does not hold.
_ROLE_TRACK_ASSIGN_RE = re.compile(r"\b(?:ROLE|TRACK)\s*=")


def test_no_dead_role_track_assignments_in_sessionstart():
    assert _SESSIONSTART_SH.exists(), f"sessionstart.sh missing at {_SESSIONSTART_SH}"
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(_SESSIONSTART_SH.read_text(encoding="utf-8").splitlines(), 1):
        if _ROLE_TRACK_ASSIGN_RE.search(line):
            hits.append((lineno, line.strip()))
    assert not hits, (
        "dead ROLE/TRACK assignments (shellcheck SC2034, OI-684) at:\n"
        + "\n".join(f"  {ln}: {text}" for ln, text in hits)
    )
