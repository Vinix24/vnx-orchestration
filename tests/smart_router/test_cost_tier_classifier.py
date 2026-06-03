"""Tests for classify_dispatch cost-tier classifier (PR-2).

Covers all four tiers with parameterized cases and critical boundary cases:
  - 30 LOC single-file → tier-zero; 31 LOC → tier-low
  - schema-touch → tier-mid; security-touch → tier-high
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

from providers.smart_router.cost_tier import (
    TIER_HIGH,
    TIER_LOW,
    TIER_MID,
    TIER_ZERO,
    classify_dispatch,
)

# (task_spec, file_paths, loc, expected_tier)
_CASES = [
    # tier-zero
    ({"instruction": "fix whitespace"}, ["foo.py"], 30, TIER_ZERO),
    ({"instruction": "rename variable"}, ["bar.py"], 1, TIER_ZERO),
    ({"instruction": "reformat"}, ["x.py"], 0, TIER_ZERO),
    # tier-low (boundary: 31 LOC single file)
    ({"instruction": "fix whitespace"}, ["foo.py"], 31, TIER_LOW),
    ({"instruction": "add helper function"}, ["scripts/foo.py"], 100, TIER_LOW),
    ({"instruction": "update config value"}, ["config.yaml"], 150, TIER_LOW),
    # tier-mid (schema keyword, multi-file >30 LOC, or LOC 151-300)
    ({"instruction": "add schema migration"}, ["migrations/v2.sql"], 50, TIER_MID),
    ({"instruction": "update database table"}, ["db.py"], 20, TIER_MID),
    ({"instruction": "add handler"}, ["a.py", "b.py"], 50, TIER_MID),
    ({"instruction": "edit function"}, ["a.py"], 200, TIER_MID),
    ({"instruction": "add adr for caching"}, ["docs/adr.md"], 80, TIER_MID),
    ({"instruction": "update interface contract"}, ["api.py"], 40, TIER_MID),
    # tier-high (security, arch, or LOC > 300)
    ({"instruction": "fix auth token validation"}, ["auth.py"], 20, TIER_HIGH),
    ({"instruction": "patch security vulnerability"}, ["core.py"], 10, TIER_HIGH),
    ({"instruction": "rewrite the orchestrator"}, ["core.py"], 100, TIER_HIGH),
    ({"instruction": "add feature"}, [], 350, TIER_HIGH),
    ({"instruction": "add button", "tags": ["security"]}, ["ui.py"], 5, TIER_HIGH),
    ({"instruction": "update rbac permission check"}, ["perm.py"], 30, TIER_HIGH),
]


@pytest.mark.parametrize("task_spec,file_paths,loc,expected", _CASES)
def test_classify_dispatch_parametrized(task_spec, file_paths, loc, expected):
    assert classify_dispatch(task_spec, file_paths, loc) == expected


def test_boundary_30_loc_is_zero():
    """Exactly 30 LOC single file → tier-zero."""
    assert classify_dispatch({"instruction": "reformat"}, ["x.py"], 30) == TIER_ZERO


def test_boundary_31_loc_is_low():
    """31 LOC single file → tier-low (first tier above zero)."""
    assert classify_dispatch({"instruction": "reformat"}, ["x.py"], 31) == TIER_LOW


def test_multi_file_at_or_below_30_stays_zero():
    """Multi-file but ≤30 LOC → tier-zero (multi-file rule requires LOC > 30)."""
    assert classify_dispatch({"instruction": "rename"}, ["a.py", "b.py"], 10) == TIER_ZERO


def test_schema_touch_forces_mid():
    """Schema keyword forces tier-mid regardless of LOC or file count."""
    assert classify_dispatch({"instruction": "add index to schema"}, ["m.sql"], 5) == TIER_MID


def test_security_touch_forces_high():
    """Security keyword forces tier-high regardless of LOC."""
    assert classify_dispatch({"instruction": "update jwt token"}, ["x.py"], 5) == TIER_HIGH


def test_empty_spec_zero_loc_is_zero():
    assert classify_dispatch({}, [], 0) == TIER_ZERO


def test_prompt_field_accepted():
    """task_spec 'prompt' key is equivalent to 'instruction'."""
    assert classify_dispatch({"prompt": "update schema migration"}, ["db.py"], 20) == TIER_MID


def test_security_tag_forces_high():
    assert classify_dispatch({"instruction": "update tooltip", "tags": ["security"]}, ["ui.py"], 5) == TIER_HIGH


def test_loc_301_forces_high():
    """LOC = 301 crosses the tier-high threshold."""
    assert classify_dispatch({"instruction": "add feature"}, ["x.py"], 301) == TIER_HIGH


def test_loc_300_mid():
    """LOC = 300 stays at tier-mid (boundary inclusive)."""
    assert classify_dispatch({"instruction": "add feature"}, ["x.py"], 300) == TIER_MID
