"""Review-lane P2b: gate billing and the Gate enum read the harness-lane rows.

A gate in ``gate_lane_rows.GATE_LANE_ROWS`` needs no second hand-written line in
``gate_billing_table`` or ``dispatch_spec``. The synthetic-row tests run in a fresh
interpreter with the rows patched before either module is imported, so they
exercise the real import-time derivation and not a copy of it.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import dispatch_spec
import gate_billing_table
import gate_lane_rows

ROWS = gate_lane_rows.GATE_LANE_ROWS

SYNTHETIC_PROBE = """
import sys
sys.path.insert(0, {lib!r})
import gate_lane_rows
row = gate_lane_rows.GateLaneRow(
    gate="synthetic_gate", lane="synthetic", request_provider="synthetic",
    dispatcher="provider_dispatch", billing="provider_metered",
    model_env="VNX_SYNTHETIC_GATE_MODEL", model_default="m", single_shot=True,
)
gate_lane_rows.GATE_LANE_ROWS = gate_lane_rows.GATE_LANE_ROWS + (row,)
import gate_billing_table, dispatch_spec
assert gate_billing_table.GATE_BILLING.get("synthetic_gate") == "provider_metered", (
    "synthetic_gate has no billing entry"
)
assert dispatch_spec.Gate("synthetic_gate").name == "SYNTHETIC_GATE", "synthetic_gate is no Gate member"
assert "synthetic_gate" in dispatch_spec.REGISTERED_GATE_NAMES, "synthetic_gate is not registered"
"""


def test_every_row_gate_is_billed_with_the_row_billing():
    for row in ROWS:
        assert gate_billing_table.GATE_BILLING[row.gate] == row.billing, row.gate
        assert gate_billing_table.gate_billing(row.gate) == row.billing, row.gate


def test_every_row_gate_is_a_gate_member_with_the_same_value():
    for row in ROWS:
        member = dispatch_spec.Gate(row.gate)
        assert member.value == row.gate
        assert member.name == row.gate.upper()
        assert row.gate in dispatch_spec.REGISTERED_GATE_NAMES


def test_existing_member_names_and_values_are_byte_equal_to_before():
    assert [(m.name, m.value) for m in dispatch_spec.Gate] == [
        ("CODEX_GATE", "codex_gate"),
        ("CLAUDE_GITHUB_OPTIONAL", "claude_github_optional"),
        ("CI_GATE", "ci_gate"),
        ("WIRING_GATE", "wiring_gate"),
        ("KIMI_GATE", "kimi_gate"),
        ("GLM_GATE", "glm_gate"),
        ("DEEPSEEK_GATE", "deepseek_gate"),
        ("CLAUDE_GATE", "claude_gate"),
    ]
    assert dispatch_spec.Gate.KIMI_GATE == "kimi_gate"
    assert dispatch_spec.GATES_OUTSIDE_ENUM == frozenset()
    assert dispatch_spec.REGISTERED_GATE_NAMES == frozenset(m.value for m in dispatch_spec.Gate)


def test_billing_table_values_are_byte_equal_to_before():
    assert gate_billing_table.GATE_BILLING == {
        "codex_gate": "subscription",
        "kimi_gate": "subscription",
        "claude_gate": "subscription",
        "gemini_review": "subscription",
        "glm_gate": "provider_metered",
        "deepseek_gate": "provider_metered",
        "claude_github_optional": "none",
        "ci_gate": "none",
        "wiring_gate": "none",
    }
    assert gate_billing_table.GATE_BILLING_SUBSCRIPTION == "subscription"
    assert gate_billing_table.GATE_BILLING_METERED == "provider_metered"
    assert gate_billing_table.GATE_BILLING_NONE == "none"


@pytest.mark.parametrize("bad", ["", " ", "unknown_gate", "KIMI_GATE"])
def test_gate_still_refuses_empty_and_unknown_values(bad):
    with pytest.raises(ValueError):
        dispatch_spec.Gate(bad)


def test_unclassified_gate_still_raises():
    with pytest.raises(gate_billing_table.UnknownGateProvider):
        gate_billing_table.gate_billing("not_a_gate")


def test_synthetic_row_reaches_billing_and_enum_in_a_fresh_interpreter():
    result = subprocess.run(
        [sys.executable, "-I", "-c", SYNTHETIC_PROBE.format(lib=str(LIB))],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_billing_from_rows_adds_a_synthetic_row_and_refuses_a_double_registration():
    row = gate_lane_rows.GateLaneRow(
        gate="synthetic_gate", lane="synthetic", request_provider="synthetic",
        dispatcher="provider_dispatch", billing="provider_metered",
        model_env="VNX_SYNTHETIC_GATE_MODEL", model_default="m", single_shot=False,
    )
    table = gate_billing_table.billing_from_rows(ROWS + (row,))
    assert table["synthetic_gate"] == "provider_metered"
    clash = gate_lane_rows.GateLaneRow(
        gate="codex_gate", lane="x", request_provider="x", dispatcher="provider_dispatch",
        billing="none", model_env="E", model_default="m", single_shot=False,
    )
    with pytest.raises(ValueError, match="codex_gate"):
        gate_billing_table.billing_from_rows((clash,))


def test_gate_members_adds_a_synthetic_row_and_refuses_a_collision():
    row = gate_lane_rows.GateLaneRow(
        gate="synthetic_gate", lane="synthetic", request_provider="synthetic",
        dispatcher="provider_dispatch", billing="none",
        model_env="VNX_SYNTHETIC_GATE_MODEL", model_default="m", single_shot=False,
    )
    assert dispatch_spec.gate_members(ROWS + (row,))["SYNTHETIC_GATE"] == "synthetic_gate"
    clash = gate_lane_rows.GateLaneRow(
        gate="ci_gate", lane="x", request_provider="x", dispatcher="provider_dispatch",
        billing="none", model_env="E", model_default="m", single_shot=False,
    )
    with pytest.raises(ValueError, match="CI_GATE"):
        dispatch_spec.gate_members((clash,))


@pytest.mark.parametrize("module", ["gate_billing_table", "dispatch_spec"])
def test_modules_import_only_stdlib_and_gate_lane_rows(module):
    tree = ast.parse((LIB / f"{module}.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add((node.module or "").split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"__future__", "gate_lane_rows"}
    assert sorted(imported - allowed) == []
    assert "gate_lane_rows" in imported
