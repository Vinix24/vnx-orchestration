"""tests/test_dispatch_sidedoor_audit.py — the PR-12 exhaustiveness gate.

This is the regression gate for PR-11 (the single-entry flip): if a NEW file invokes a lane
script as a delivery path without going through dispatch_bridge, this test fails — forcing it
to be audited + wired before the flag can flip. Turns the review's "prove exhaustiveness, do
not assert it" finding into an executable check.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_sidedoor_audit as audit_mod  # noqa: E402


def test_no_unaudited_side_door_callers():
    result = audit_mod.audit()
    assert result["unaudited"] == set(), (
        "New direct lane-script delivery caller(s) appeared — audit them and wire through "
        "dispatch_bridge before flipping VNX_SINGLE_ENTRY_DISPATCH: "
        + ", ".join(sorted(result["unaudited"]))
    )


def test_scan_still_detects_known_callers():
    # guards against the scanner silently going blind (e.g. a regex/docstring-skip regression):
    # the known delivery callers must still be detected.
    found = audit_mod.scan_delivery_callers()
    for caller in (
        "scripts/lib/plan_gate_panel.py",
        "scripts/commands/dispatch.sh",
        "scripts/lib/pool_worker_runner.py",
    ):
        assert caller in found, f"scanner no longer detects {caller}"


def test_docstring_mention_is_not_a_caller():
    # the over-flag fix: a lane named only in a docstring/comment must NOT be a caller.
    found = audit_mod.scan_delivery_callers()
    for reference_only in (
        "scripts/lib/governance_emit.py",   # docstring: "Used by both subprocess_dispatch.py..."
        "scripts/lib/smart_router.py",      # docstring: "...in provider_dispatch.py"
        "scripts/lib/dispatch_cli.py",      # the door itself (excluded)
    ):
        assert reference_only not in found, f"{reference_only} false-flagged as a caller"
