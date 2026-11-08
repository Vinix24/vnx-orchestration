"""Capability matrix per lane (permissions P3): six full rows, a closed enum, fail-closed lookup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import permission_harness_map as phm


@pytest.mark.parametrize("lane", phm.LANES)
def test_each_lane_has_a_full_row_with_a_named_mechanism(lane):
    row = phm.capability_row(lane)
    assert set(row) == set(phm.DIMENSIONS)
    for cell in row.values():
        assert cell["level"] in phm.LEVELS
        assert cell["mechanism"].strip()


@pytest.mark.parametrize("lane", [None, "", "gemini", "CLAUDE_HEADLESS", 5])
def test_unknown_lane_is_unsupported_for_every_dimension(lane):
    assert {c["level"] for c in phm.capability_row(lane).values()} == {phm.UNSUPPORTED}


def test_kimi_tools_deny_is_unsupported_and_codex_is_not_a_claude_copy():
    assert phm.capability_for("kimi", "tools_deny")["level"] == phm.UNSUPPORTED
    assert phm.capability_for("claude_headless", "tools_deny")["level"] == phm.SUPPORTED
    assert phm.capability_for("codex", "filesystem_write")["level"] == phm.DEGRADED


def test_harness_lanes_cannot_carry_hooks():
    for lane in ("deepseek_harness", "glm_harness"):
        assert phm.capability_for(lane, "hooks")["level"] == phm.UNSUPPORTED


def test_unknown_dimension_raises():
    with pytest.raises(KeyError):
        phm.capability_for("kimi", "telepathy")


def test_material_record_without_digest_is_unresolved():
    rec = phm.material_record("kimi", None, ["--yolo"], "d1")
    assert rec["policy_state"] == "unresolved" and rec["policy_digest"] is None
    assert rec["lane"] == "kimi" and rec["lane_known"] and "tools_deny" in rec["unsupported"]
