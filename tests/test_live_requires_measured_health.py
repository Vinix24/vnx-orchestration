#!/usr/bin/env python3
"""Tests for scripts/check_live_requires_measured_health.py (D6b).

Pins the invariant this dispatch introduced: a cockpit subsystem may not
carry status=LIVE while its health reads "unknown". Six subsystems
(cheap-recon-scout, horizon-planning, headless-dispatch-routing,
central-db-routing, smart-router-staging, claude-tmux-serialization) carried
exactly that combination before this dispatch moved them to
ACTIVATE-and-measure in scripts/lib/config_registry.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_live_requires_measured_health as check  # noqa: E402


def test_violations_in_rows_catches_live_with_unknown_health() -> None:
    rows = [
        {"subsystem": "cheap-recon-scout", "status": "LIVE", "health": "unknown — no probe yet"},
        {"subsystem": "provider-routing", "status": "LIVE", "health": "works — dispatch outcomes routed correctly"},
        {"subsystem": "dream-consolidation", "status": "ACTIVATE", "health": "unknown — no cycles run"},
    ]
    assert check.violations_in_rows(rows) == ["cheap-recon-scout"]


def test_violations_in_rows_empty_when_no_live_unknown_combo() -> None:
    rows = [
        {"subsystem": "provider-routing", "status": "LIVE", "health": "works — dispatch outcomes routed correctly"},
        {"subsystem": "cheap-recon-scout", "status": "ACTIVATE", "health": "unknown — no probe yet"},
    ]
    assert check.violations_in_rows(rows) == []


def test_real_repo_tree_has_no_live_unknown_violations() -> None:
    """Regression pin: the six subsystems this dispatch fixed stay fixed."""
    violations = check.find_live_unknown_violations(REPO_ROOT)
    assert violations == []


def test_real_repo_tree_would_have_flagged_the_six_before_the_fix() -> None:
    """Proves the check can fail: replaying the pre-fix status values against
    the real rowset must reproduce exactly the six subsystems named in the
    dispatch (cheap-recon-scout, horizon-planning, headless-dispatch-routing,
    central-db-routing, smart-router-staging, claude-tmux-serialization)."""
    from vnx_cli import _engine
    from vnx_cli.commands import subsystems as sub_mod

    engine_root = _engine.engine_root()
    data_dir = _engine.resolve_data_root(REPO_ROOT)

    rows = sub_mod.build_rows()
    seed_health = sub_mod._parse_seed_health(engine_root)
    sub_mod._attach_health(rows, data_dir, seed_health, use_probe=False)

    pre_fix_six = {
        "cheap-recon-scout",
        "horizon-planning",
        "headless-dispatch-routing",
        "central-db-routing",
        "smart-router-staging",
        "claude-tmux-serialization",
    }
    for row in rows:
        if row["subsystem"] in pre_fix_six:
            row["status"] = "LIVE"

    assert set(check.violations_in_rows(rows)) == pre_fix_six
