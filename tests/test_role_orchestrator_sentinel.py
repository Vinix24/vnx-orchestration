#!/usr/bin/env python3
"""Pin the canonical T0 role's "no role resolved" sentinel to the code's truth.

Dispatch-ID: 20260815-nn-w1-canonieke-rol-sentinel

The canonical orchestrator role (``.claude/terminals/T0/role-orchestrator.md``)
is synced fleet-wide by ``vnx role sync``, so a wrong sentinel claim there
propagates to every project. This test pins the role's "Role selection (hard)"
section to the sentinel actually defined in ``scripts/lib/dispatch_identity.py``
and guards against the pre-OI-981 drift that described ``backend-developer`` as
the sentinel (and cited a definition site, ``dispatch_govern.py:51``, that never
held one).

The source of truth is ``dispatch_identity.py``; the role file must agree with
it. A content pin, not a behaviour test.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROLE_FILE = REPO / ".claude" / "terminals" / "T0" / "role-orchestrator.md"

sys.path.insert(0, str(REPO / "scripts" / "lib"))

import dispatch_identity


def _role_text() -> str:
    return ROLE_FILE.read_text()


def test_canonical_role_names_the_code_sentinel():
    """The role's sentinel line must name the value dispatch_identity defines as
    ``_IDENTITY_UNRESOLVED`` / ``_FAKE_DEFAULT_ROLE`` (``identity_unresolved``)."""
    text = _role_text()
    assert (
        f"`{dispatch_identity._IDENTITY_UNRESOLVED}` is the sentinel default"
        in text
    )


def test_canonical_role_cites_the_definition_site():
    """The reference must point at dispatch_identity.py:39-40, where the sentinel
    is defined — not the old, wrong ``dispatch_govern.py:51``."""
    text = _role_text()
    assert "scripts/lib/dispatch_identity.py:39-40" in text
    assert "scripts/lib/dispatch_govern.py:51" not in text


def test_canonical_role_never_calls_backend_developer_the_sentinel():
    """Pre-OI-981 drift: the role described ``backend-developer`` as the sentinel.
    It is a real deliberate role now; the sentinel line must not name it as such."""
    text = _role_text()
    assert "`backend-developer` is the sentinel default" not in text


def test_backend_developer_remains_a_deliberate_role_choice():
    """Positive control: the role table's ``backend-developer (deliberate choice)``
    row is a real role, not a sentinel, and must stay."""
    text = _role_text()
    assert "`backend-developer` (deliberate choice)" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
