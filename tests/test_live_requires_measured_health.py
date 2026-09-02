#!/usr/bin/env python3
"""Tests for scripts/check_live_requires_measured_health.py (D6b + OI-1593).

Pins the invariant this dispatch introduced: a cockpit subsystem may not
carry status=LIVE while its health reads "unknown". Six subsystems
(cheap-recon-scout, horizon-planning, headless-dispatch-routing,
central-db-routing, smart-router-staging, claude-tmux-serialization) carried
exactly that combination before this dispatch moved them to
ACTIVATE-and-measure in scripts/lib/config_registry.py.

OI-1593 hardened the filter from a prefix match on the literal string
'unknown' to an allowlist of known-measured categories (works, degraded,
produces-crap, stale). A prefix match let an empty health cell, or any
future beacon category the filter had never seen, pass LIVE silently —
exactly the "unmeasured" state this check exists to catch.
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


def test_violations_in_rows_catches_live_with_empty_health() -> None:
    """OI-1593: an empty health cell does not start with 'unknown', so the
    old prefix filter passed it silently. Empty means nobody wrote a
    reading — it must be a violation, same as an explicit 'unknown'."""
    rows = [
        {"subsystem": "cheap-recon-scout", "status": "LIVE", "health": ""},
    ]
    assert check.violations_in_rows(rows) == ["cheap-recon-scout"]


def test_violations_in_rows_catches_live_with_unrecognized_category() -> None:
    """OI-1593: a beacon category this check has never seen (e.g. a future
    'absent — no beacon file') must fail closed as a violation, not pass
    because it happens not to start with 'unknown'."""
    rows = [
        {"subsystem": "cheap-recon-scout", "status": "LIVE", "health": "absent — no beacon file"},
    ]
    assert check.violations_in_rows(rows) == ["cheap-recon-scout"]


def test_violations_in_rows_allows_live_with_works_health() -> None:
    rows = [
        {"subsystem": "provider-routing", "status": "LIVE", "health": "works — dispatch outcomes routed correctly"},
    ]
    assert check.violations_in_rows(rows) == []


def test_violations_in_rows_allows_live_with_stale_health() -> None:
    """OI-1593: 'stale' means a probe DID run and produced a reading that has
    since aged out — it is measured, just old. Whether LIVE+stale SHOULD be
    legal is a separate design question this check does not take a position
    on; it is pinned here as explicitly out of scope for this filter."""
    rows = [
        {"subsystem": "provider-routing", "status": "LIVE", "health": "stale — ok"},
    ]
    assert check.violations_in_rows(rows) == []


def test_violations_in_rows_allows_live_with_beacon_ok_health() -> None:
    """OI-1596: the allowlist only covered the seed-prose vocabulary
    (works/degraded/produces-crap/stale). A live beacon reporting a bare
    'ok' — no ' — detail' suffix, since ``_attach_health`` only appends one
    when a signal is present — used to be reported as a VIOLATION with the
    message "health is unmeasured", the opposite of what was measured:
    'ok' is a genuine, healthy reading from ``health_beacon.all_beacons()``."""
    rows = [
        {"subsystem": "provider-routing", "status": "LIVE", "health": "ok"},
    ]
    assert check.violations_in_rows(rows) == []


def test_violations_in_rows_allows_live_with_beacon_fail_health() -> None:
    """OI-1596: 'fail' is measured-and-bad, not unmeasured. Whether
    LIVE+fail SHOULD be legal is a separate design question this check does
    not take a position on, same as LIVE+degraded/produces-crap — this check
    only asserts a reading exists, it never judges whether the reading is
    good."""
    rows = [
        {"subsystem": "provider-routing", "status": "LIVE", "health": "fail"},
    ]
    assert check.violations_in_rows(rows) == []


def test_violations_in_rows_catches_live_with_bare_unknown_beacon_health() -> None:
    """'unknown' with no ' — detail' suffix (the bare beacon form, e.g. an
    event-driven beacon past its freshness backstop) must stay a violation —
    both vocabularies agree 'unknown' means not genuinely measured."""
    rows = [
        {"subsystem": "provider-routing", "status": "LIVE", "health": "unknown"},
    ]
    assert check.violations_in_rows(rows) == ["provider-routing"]


def test_violations_in_rows_catches_live_with_bare_absent_beacon_health() -> None:
    """'absent' (an expected component that never once wrote a beacon) must
    stay a violation — it is the beacon vocabulary's own "never measured"
    category, distinct from but just as unmeasured as seed 'unknown'."""
    rows = [
        {"subsystem": "provider-routing", "status": "LIVE", "health": "absent"},
    ]
    assert check.violations_in_rows(rows) == ["provider-routing"]


def test_violations_in_rows_catches_live_with_fabricated_category() -> None:
    """Fail-closed property, independent of any real category: a health
    string neither vocabulary has ever produced must be a violation."""
    rows = [
        {"subsystem": "provider-routing", "status": "LIVE", "health": "frobnicated"},
    ]
    assert check.violations_in_rows(rows) == ["provider-routing"]
