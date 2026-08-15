"""Doc-claim drift guards for OI-1223 / OI-1225.

Two cases where a comment or docstring asserted a default that the resolver
contradicts:

  * OI-1223 — the claude headless billing label was documented as
    ``api_metered``. Billing is auth-derived, not lane-derived: without an own
    ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` a headless dispatch bills as
    ``subscription``. Resolver: ``dispatch_plan.claude_auth_is_api_metered``.
  * OI-1225 — three sites in ``tmux_interactive_dispatch.py`` (plus a doc code
    block) claimed ADR-012 enforcement was "default ON since 15-08". That flip
    was reverted; the resolver
    ``worker_permissions.worker_permission_enforcement_enabled`` returns False
    by default.

Each guard first anchors on the resolver's actual value (so a future
legitimate change fails the anchor loudly and forces the guard to be revisited)
and then asserts the stale claim string is absent from every site that used to
carry it.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from dispatch_plan import claude_auth_is_api_metered  # noqa: E402
from worker_permissions import worker_permission_enforcement_enabled  # noqa: E402


def _read(*parts: str) -> str:
    return (REPO_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_headless_billing_is_auth_derived_not_lane_derived():
    # Resolver anchor: no own key, no redirect -> the claude lane (headless or
    # tmux) bills as "subscription", never "api_metered".
    assert claude_auth_is_api_metered({}) is False

    sites = {
        "scripts/lib/dispatch_envelope.py": ("api_metered billing",),
        "scripts/lib/dispatch_cli.py": ("headless api_metered",),
        "scripts/commands/dispatch.sh": ("Headless (api_metered)",),
        "docs/core/110_SMART_ROUTER_DESIGN.md": (
            "Explicit opt-in to API-key billing",
        ),
    }
    for rel, forbidden in sites.items():
        text = _read(*rel.split("/"))
        for claim in forbidden:
            assert claim not in text, (
                f"{rel}: {claim!r} hardcodes a lane-derived billing label; "
                "billing is auth-derived (dispatch_plan.claude_auth_is_api_metered)"
            )


def test_enforcement_default_claim_matches_resolver(monkeypatch):
    monkeypatch.delenv("VNX_ENFORCE_WORKER_PERMISSIONS", raising=False)
    monkeypatch.delenv("VNX_WORKER_ENFORCEMENT_SKIP", raising=False)
    # Resolver anchor: the 15-08 flip was reverted, so the default is OFF. If a
    # future remeasurement re-flips this to ON, this assertion fails first and
    # forces the stale-claim guard below to be revisited instead of drifting.
    assert worker_permission_enforcement_enabled() is False

    for rel in (
        "scripts/lib/tmux_interactive_dispatch.py",
        "docs/operations/WORKER_PERMISSIONS.md",
    ):
        text = _read(*rel.split("/"))
        assert "default ON since 15-08" not in text, (
            f"{rel}: 'default ON since 15-08' contradicts the resolver "
            "(worker_permission_enforcement_enabled default OFF)"
        )
        assert "ADR-012 enforcement default ON" not in text, (
            f"{rel}: 'ADR-012 enforcement default ON' contradicts the resolver "
            "(worker_permission_enforcement_enabled default OFF)"
        )
