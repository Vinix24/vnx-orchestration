#!/usr/bin/env python3
"""config-een-oppervlak N7: registry rows for the 12 register keys of INV-2.

Dispatch-ID: dlv-ae424bc6f8b7
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_registry as cr  # noqa: E402

SCOUT = ("codex", "deepseek", "gemini", "ollama")
# key -> (type, default, approval, allowed_values, read_site_wired)
ROWS = {
    "VNX_PAID_LANE_DAILY_BUDGET_USD": ("float", "5.00", True, None, True),
    "VNX_RECEIPT_CLASSIFIER_DAILY_COST_USD": ("float", "0.20", True, None, True),
    "VNX_RECEIPT_CLASSIFIER_MODE": ("string", "batch", False, None, False),
    "VNX_RECEIPT_CLASSIFIER_PROVIDER": ("string", "haiku", False, None, False),
    "VNX_REQUIRE_DISPATCH_TRACK": ("bool", "0", False, None, True),
    "VNX_SCOUT_PROVIDER": ("string", "deepseek", False, SCOUT, True),
    "VNX_SUPERVISOR_MODE": ("string", "legacy", False, ("legacy", "unified"), False),
    "VNX_T0_PROVIDER": ("string", "claude_code", False, None, True),
    "VNX_T0_SKIP_PERMISSIONS": ("bool", "0", True, None, True),
    "VNX_T1_SKIP_PERMISSIONS": ("bool", "0", True, None, True),
    "VNX_T2_SKIP_PERMISSIONS": ("bool", "0", True, None, True),
    "VNX_T3_SKIP_PERMISSIONS": ("bool", "0", True, None, True),
}


@pytest.mark.parametrize("key", sorted(ROWS))
def test_row_has_the_declared_shape(key):
    type_, default, approval, allowed, wired = ROWS[key]
    entry = cr.CONFIG_REGISTRY[key]
    assert (entry.type, entry.default, entry.scope) == (type_, default, "project")
    assert entry.requires_approval is approval
    assert entry.allowed_values == allowed
    assert entry.read_site_wired is wired
    if allowed:
        assert entry.default in allowed


def test_approval_list_is_the_six_waiver_keys():
    assert {k for k in ROWS if cr.CONFIG_REGISTRY[k].requires_approval} == {
        "VNX_PAID_LANE_DAILY_BUDGET_USD", "VNX_RECEIPT_CLASSIFIER_DAILY_COST_USD",
        "VNX_T0_SKIP_PERMISSIONS", "VNX_T1_SKIP_PERMISSIONS",
        "VNX_T2_SKIP_PERMISSIONS", "VNX_T3_SKIP_PERMISSIONS"}


def test_scout_allowed_values_match_the_reader():
    import scout_prepass

    assert set(cr.CONFIG_REGISTRY["VNX_SCOUT_PROVIDER"].allowed_values) == set(scout_prepass._ALLOWED_SCOUT_PROVIDERS)
