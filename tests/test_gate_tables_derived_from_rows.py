"""LANE_DISPATCHER_KEYS and GATE_PROVIDERS are derived from GATE_LANE_ROWS (review-lane P9b).

OI-2655 and OI-2656: the lane -> dispatcher table and the gate -> (kind, name) table were
written out by hand next to the rows that already carry both columns. They keep their
names and types; the harness-lane entries now come from the rows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT / "scripts" / "lib", REPO_ROOT / "scripts"):
    sys.path.insert(0, str(_p))

import gate_dispatch_table as table
import gate_lane_rows
import gate_recorder

# The literal tables as they stood on origin/main b09d4d20, the pin the derivation must meet.
EXPECTED_LANE_DISPATCHER_KEYS = {
    "kimi": table.DISPATCHER_GOVERNED,
    "glm-harness": table.DISPATCHER_GOVERNED,
    "deepseek-harness": table.DISPATCHER_GOVERNED,
    "claude": table.DISPATCHER_CLAUDE_LANE,
}
HARNESS = gate_recorder.GATE_PROVIDER_HARNESS_LANE
EXPECTED_GATE_PROVIDERS = {
    "gemini_review": (gate_recorder.GATE_PROVIDER_PATH_BINARY, "gemini"),
    "codex_gate": (gate_recorder.GATE_PROVIDER_PATH_BINARY, "codex"),
    "claude_github_optional": (gate_recorder.GATE_PROVIDER_PATH_BINARY, "gh"),
    "ci_gate": (gate_recorder.GATE_PROVIDER_DETERMINISTIC, "local"),
    "wiring_gate": (gate_recorder.GATE_PROVIDER_DETERMINISTIC, "local"),
    "kimi_gate": (HARNESS, "kimi"),
    "glm_gate": (HARNESS, "glm-harness"),
    "deepseek_gate": (HARNESS, "deepseek-harness"),
    "claude_gate": (HARNESS, "claude"),
}


def _row(**over):
    base = dict(
        gate="synthetic_gate", lane="synthetic-lane", request_provider="synthetic",
        dispatcher=table.DISPATCHER_GOVERNED, billing="provider_metered",
        model_env="VNX_SYNTHETIC_GATE_MODEL", model_default="synthetic-1",
        single_shot=False, family="kimi",
    )
    base.update(over)
    return gate_lane_rows.GateLaneRow(**base)


def test_lane_dispatcher_keys_equal_the_literal_table_entry_for_entry():
    assert table.LANE_DISPATCHER_KEYS == EXPECTED_LANE_DISPATCHER_KEYS


def test_gate_providers_equal_the_literal_table_entry_for_entry():
    assert gate_recorder.GATE_PROVIDERS == EXPECTED_GATE_PROVIDERS


def test_the_module_tables_are_the_derivation_of_the_rows():
    rows = gate_lane_rows.GATE_LANE_ROWS
    assert table.LANE_DISPATCHER_KEYS == table.lane_dispatcher_keys(rows)
    harness = {g: v for g, v in gate_recorder.GATE_PROVIDERS.items() if v[0] == HARNESS}
    assert harness == gate_recorder.harness_providers(rows)


def test_a_synthetic_row_lands_in_both_derived_tables():
    rows = gate_lane_rows.validate_rows((*gate_lane_rows.GATE_LANE_ROWS, _row()))
    assert gate_recorder.harness_providers(rows)["synthetic_gate"] == (HARNESS, "synthetic-lane")
    assert table.lane_dispatcher_keys(rows)["synthetic-lane"] == table.DISPATCHER_GOVERNED


def test_every_derived_dispatcher_resolves_to_a_factory():
    for row in gate_lane_rows.GATE_LANE_ROWS:
        assert table.unresolved_detail(row.gate, row.lane) is None


def test_an_unregistered_lane_still_has_a_detail():
    assert "names no dispatcher" in table.unresolved_detail("never_registered_gate", "no-such-lane")


def test_an_unknown_dispatcher_key_in_a_row_still_has_a_detail(monkeypatch):
    rows = gate_lane_rows.validate_rows((_row(dispatcher="typo_dispatcher"),))
    monkeypatch.setattr(table, "LANE_DISPATCHER_KEYS", table.lane_dispatcher_keys(rows))
    detail = table.unresolved_detail("synthetic_gate", "synthetic-lane")
    assert detail is not None and "typo_dispatcher" in detail


# ---------------------------------------------------------------------------
# The family column is a known family (stoel-2 review of #2509)
# ---------------------------------------------------------------------------


def test_a_typo_family_is_refused_naming_row_and_value():
    with pytest.raises(gate_lane_rows.GateLaneRowError) as err:
        _row(family="kmi")
    assert "'synthetic_gate'" in str(err.value) and "'family'" in str(err.value)
    assert "'kmi'" in str(err.value)


def test_every_known_family_builds():
    import provider_switch

    for family in (*provider_switch.SWITCHABLE_FAMILIES, provider_switch.CLAUDE, provider_switch.CODEX):
        assert _row(family=family).family == family


def test_every_shipped_row_still_builds():
    import provider_switch

    known = set(provider_switch.SWITCHABLE_FAMILIES) | {provider_switch.CLAUDE, provider_switch.CODEX}
    assert {r.gate for r in gate_lane_rows.GATE_LANE_ROWS} == {
        "kimi_gate", "glm_gate", "deepseek_gate", "claude_gate",
    }
    assert all(r.family in known for r in gate_lane_rows.GATE_LANE_ROWS)


@pytest.mark.parametrize("first", ["provider_switch", "gate_lane_rows"])
def test_import_order_does_not_matter(first):
    import subprocess

    code = f"import sys; sys.path.insert(0, sys.argv[1]); import {first}; import provider_switch, gate_lane_rows"
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(REPO_ROOT / "scripts" / "lib")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
