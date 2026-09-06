#!/usr/bin/env python3
"""Tests for scripts/lib/guard_reachability_calibration.py — the detector's
own tripwire (golf-4 valkuil #4: "je detector mag zelf niet in deze klasse
vallen").

Three things are proven here, matching the dispatch's "bewijs" requirements:
  1. the self-test PASSES today, against the real repo + the real registry
     (``test_selftest_passes_on_real_repo``);
  2. "maak je eigen wachter kapot": adding the calibration field to
     ACCEPTED_GAPS must make the self-test FAIL loud
     (``test_selftest_fails_when_calibration_case_is_suppressed``) — this is
     the literal "onderdruk het bekende geval" instruction;
  3. a scanner regression (the detector silently losing the ability to find
     the known-bad guard) must also fail loud, independent of the
     ACCEPTED_GAPS check (``test_selftest_fails_on_scanner_regression``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = VNX_ROOT / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import guard_reachability_calibration as calibration  # noqa: E402
from guard_reachability_calibration import (  # noqa: E402
    CALIBRATION_CASES,
    CalibrationFetchError,
    SelfTestFailure,
    fetch_pre_fix_function_source,
    run_selftest,
)


def test_fetch_pre_fix_function_source_gets_real_historical_code():
    """Not retyped: pulled straight from git history. Sanity-check the exact
    shape the OI-1632 bug had (a local-var assignment, then a bare-name
    guard) is really there."""
    case = CALIBRATION_CASES[0]
    source = fetch_pre_fix_function_source(VNX_ROOT, case)
    assert "track_id = (spec.track_id or" in source
    assert "if track_id:" in source


def test_fetch_unknown_function_raises_fetch_error():
    from dataclasses import replace

    bogus = replace(CALIBRATION_CASES[0], pre_fix_function="_this_function_never_existed")
    with pytest.raises(CalibrationFetchError):
        fetch_pre_fix_function_source(VNX_ROOT, bogus)


def test_selftest_passes_on_real_repo():
    confirmations = run_selftest(VNX_ROOT, accepted_gap_fields=frozenset())
    assert len(confirmations) == len(CALIBRATION_CASES)
    assert all("OK" in c for c in confirmations)


def test_selftest_fails_when_calibration_case_is_suppressed():
    """bewijs #2: 'Maak je eigen wachter kapot' — mark the KNOWN-BUG field as
    an accepted gap and show the self-test refuses to pass. A confirmed
    historical defect must never be laundered into "designed", even with a
    reason attached — that is precisely how three of the eight golf-4 cases
    stayed hidden."""
    calibration_field = CALIBRATION_CASES[0].field
    with pytest.raises(SelfTestFailure, match="CONFIRMED historical"):
        run_selftest(VNX_ROOT, accepted_gap_fields=frozenset({calibration_field}))


def test_selftest_fails_on_scanner_regression(monkeypatch):
    """A scanner that can no longer find ANY guard must fail the self-test,
    independent of the ACCEPTED_GAPS check — this is the other half of
    valkuil #4 (a broken detector, not a laundered finding)."""

    def _finds_nothing(source, filename, known_attr_fields=frozenset()):
        return []

    monkeypatch.setattr(calibration, "find_guarded_field_refs", _finds_nothing)
    with pytest.raises(SelfTestFailure, match="detector regression"):
        run_selftest(VNX_ROOT, accepted_gap_fields=frozenset())


def test_selftest_fails_loud_on_empty_calibration_cases(monkeypatch):
    monkeypatch.setattr(calibration, "CALIBRATION_CASES", ())
    with pytest.raises(SelfTestFailure, match="empty"):
        run_selftest(VNX_ROOT, accepted_gap_fields=frozenset())
