"""GATE_LANE_ROWS is the one registration; the four contract views derive from it (review-lane P2).

ADR-046 Amendment 1, A3. The rows live in a stdlib-only module, the four views
in ``gate_lane_contract`` keep their names and are derived, and a bad row is
refused when it is built.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(LIB_DIR))

import gate_billing_table
import gate_lane_contract
import gate_lane_rows
import gate_recorder
from gate_lane_rows import GateLaneRow, GateLaneRowError

DEFAULT_CHARS = 800_000

# The four views as they stood before the rows existed, written out literally.
EXPECTED_MODEL_DEFAULTS = {
    "glm_gate": ("VNX_GLM_GATE_MODEL", "glm-5.2"),
    "kimi_gate": ("VNX_KIMI_GATE_MODEL", "kimi-k3"),
    "deepseek_gate": ("VNX_DEEPSEEK_GATE_MODEL", "deepseek-flash"),
    "claude_gate": ("VNX_CLAUDE_GATE_MODEL", "sonnet"),
}
EXPECTED_TIMEOUT_CONFIG = {
    "kimi_gate": ("VNX_KIMI_GATE_TIMEOUT_SECONDS", 900),
    "glm_gate": ("VNX_GLM_GATE_TIMEOUT_SECONDS", 900),
    "deepseek_gate": ("VNX_DEEPSEEK_GATE_TIMEOUT_SECONDS", 900),
    "claude_gate": ("VNX_CLAUDE_GATE_TIMEOUT_SECONDS", 900),
}
EXPECTED_DIFF_CHAR_CONFIG = {
    "kimi_gate": ("VNX_KIMI_GATE_MAX_DIFF_CHARS", DEFAULT_CHARS),
    "glm_gate": ("VNX_GLM_GATE_MAX_DIFF_CHARS", DEFAULT_CHARS),
    "deepseek_gate": ("VNX_DEEPSEEK_GATE_MAX_DIFF_CHARS", DEFAULT_CHARS),
    "claude_gate": ("VNX_CLAUDE_GATE_MAX_DIFF_CHARS", DEFAULT_CHARS),
}


def _row(**over) -> GateLaneRow:
    base = dict(
        gate="synthetic_gate", lane="synthetic-lane", request_provider="synthetic",
        dispatcher="provider_dispatch", billing="provider_metered",
        model_env="VNX_SYNTHETIC_GATE_MODEL", model_default="synthetic-1",
        single_shot=True, timeout=1234, diff_chars=4321, family="codex",
    )
    base.update(over)
    return GateLaneRow(**base)


def test_views_are_byte_equal_to_the_hand_written_tables():
    assert gate_lane_contract.MODEL_DEFAULTS == EXPECTED_MODEL_DEFAULTS
    assert gate_lane_contract.SINGLE_SHOT_GATES == frozenset({"glm_gate", "kimi_gate"})
    assert isinstance(gate_lane_contract.SINGLE_SHOT_GATES, frozenset)
    assert gate_lane_contract.TIMEOUT_CONFIG == EXPECTED_TIMEOUT_CONFIG
    assert gate_lane_contract.DIFF_CHAR_CONFIG == EXPECTED_DIFF_CHAR_CONFIG
    assert gate_lane_contract.DEFAULT_MAX_DIFF_CHARS == DEFAULT_CHARS
    assert gate_lane_contract.TIMEOUT_SECONDS == 900


def test_contract_views_are_the_derivation_of_the_rows():
    rows = gate_lane_rows.GATE_LANE_ROWS
    assert gate_lane_contract.MODEL_DEFAULTS == gate_lane_rows.model_defaults(rows)
    assert gate_lane_contract.SINGLE_SHOT_GATES == gate_lane_rows.single_shot_gates(rows)
    assert gate_lane_contract.TIMEOUT_CONFIG == gate_lane_rows.timeout_config(rows)
    assert gate_lane_contract.DIFF_CHAR_CONFIG == gate_lane_rows.diff_char_config(rows)


def test_every_row_gate_is_a_harness_lane_gate_with_the_recorder_lane():
    for row in gate_lane_rows.GATE_LANE_ROWS:
        assert gate_recorder.GATE_PROVIDERS[row.gate] == (
            gate_recorder.GATE_PROVIDER_HARNESS_LANE, row.lane,
        )
    harness = {
        g for g, (kind, _) in gate_recorder.GATE_PROVIDERS.items()
        if kind == gate_recorder.GATE_PROVIDER_HARNESS_LANE
    }
    assert harness == {r.gate for r in gate_lane_rows.GATE_LANE_ROWS}


def test_request_provider_labels_are_pinned_and_not_the_lane():
    labels = {r.gate: r.request_provider for r in gate_lane_rows.GATE_LANE_ROWS}
    assert labels == {
        "kimi_gate": "kimi", "glm_gate": "glm",
        "deepseek_gate": "deepseek", "claude_gate": "claude",
    }
    lanes = {r.gate: r.lane for r in gate_lane_rows.GATE_LANE_ROWS}
    assert lanes["glm_gate"] == "glm-harness" != labels["glm_gate"]
    assert lanes["deepseek_gate"] == "deepseek-harness" != labels["deepseek_gate"]


def test_row_billing_matches_the_billing_table():
    for row in gate_lane_rows.GATE_LANE_ROWS:
        assert gate_billing_table.GATE_BILLING[row.gate] == row.billing


def test_billing_values_equal_the_ones_gate_billing_table_knows():
    known = {
        gate_billing_table.GATE_BILLING_SUBSCRIPTION,
        gate_billing_table.GATE_BILLING_METERED,
        gate_billing_table.GATE_BILLING_NONE,
    }
    assert gate_lane_rows.VALID_BILLING == known


def test_synthetic_row_shows_up_in_all_four_views():
    rows = gate_lane_rows.validate_rows(gate_lane_rows.GATE_LANE_ROWS + (_row(),))
    assert gate_lane_rows.model_defaults(rows)["synthetic_gate"] == (
        "VNX_SYNTHETIC_GATE_MODEL", "synthetic-1",
    )
    assert "synthetic_gate" in gate_lane_rows.single_shot_gates(rows)
    assert gate_lane_rows.timeout_config(rows)["synthetic_gate"] == (
        "VNX_SYNTHETIC_GATE_TIMEOUT_SECONDS", 1234,
    )
    assert gate_lane_rows.diff_char_config(rows)["synthetic_gate"] == (
        "VNX_SYNTHETIC_GATE_MAX_DIFF_CHARS", 4321,
    )


def test_contract_loaded_over_a_synthetic_row_carries_it_in_all_four_views(monkeypatch):
    # A fresh copy of gate_lane_contract, not registered in sys.modules, so the
    # identity other tests pin on the real one is untouched.
    monkeypatch.setattr(
        gate_lane_rows, "GATE_LANE_ROWS", gate_lane_rows.GATE_LANE_ROWS + (_row(),),
    )
    spec = importlib.util.spec_from_file_location(
        "gate_lane_contract_fresh", LIB_DIR / "gate_lane_contract.py",
    )
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh.MODEL_DEFAULTS["synthetic_gate"] == ("VNX_SYNTHETIC_GATE_MODEL", "synthetic-1")
    assert "synthetic_gate" in fresh.SINGLE_SHOT_GATES
    assert not fresh.reviews_with_tools("synthetic_gate")
    assert fresh.TIMEOUT_CONFIG["synthetic_gate"] == ("VNX_SYNTHETIC_GATE_TIMEOUT_SECONDS", 1234)
    assert fresh.DIFF_CHAR_CONFIG["synthetic_gate"] == ("VNX_SYNTHETIC_GATE_MAX_DIFF_CHARS", 4321)
    assert "synthetic_gate" not in gate_lane_contract.SINGLE_SHOT_GATES


def test_synthetic_row_that_is_not_single_shot_stays_out_of_that_view():
    rows = gate_lane_rows.validate_rows((_row(single_shot=False),))
    assert gate_lane_rows.single_shot_gates(rows) == frozenset()


@pytest.mark.parametrize("column", ["gate", "lane", "request_provider", "dispatcher",
                                    "model_env", "model_default"])
@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_text_columns_refuse_empty_missing_and_malformed(column, bad):
    with pytest.raises(GateLaneRowError) as err:
        _row(**{column: bad})
    assert repr(column) in str(err.value)


@pytest.mark.parametrize("bad", ["", None, "free", "Subscription", "metered", 1])
def test_unknown_billing_is_refused_with_row_and_column(bad):
    with pytest.raises(GateLaneRowError) as err:
        _row(billing=bad)
    assert "'synthetic_gate'" in str(err.value)
    assert "'billing'" in str(err.value)


@pytest.mark.parametrize("column", ["timeout", "diff_chars"])
@pytest.mark.parametrize("bad", [0, -1, None, "900", 1.5, True])
def test_non_positive_or_malformed_numbers_are_refused(column, bad):
    with pytest.raises(GateLaneRowError) as err:
        _row(**{column: bad})
    assert "'synthetic_gate'" in str(err.value)
    assert repr(column) in str(err.value)


@pytest.mark.parametrize("bad", [None, 1, "yes"])
def test_single_shot_must_be_a_bool(bad):
    with pytest.raises(GateLaneRowError) as err:
        _row(single_shot=bad)
    assert "'single_shot'" in str(err.value)


def test_duplicate_gate_is_refused():
    with pytest.raises(GateLaneRowError) as err:
        gate_lane_rows.validate_rows((_row(), _row(lane="other-lane")))
    assert "'synthetic_gate'" in str(err.value)
    assert "'gate'" in str(err.value)


def test_validate_rows_refuses_a_non_row():
    with pytest.raises(GateLaneRowError):
        gate_lane_rows.validate_rows((("kimi_gate", "kimi"),))


def test_rows_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        gate_lane_rows.GATE_LANE_ROWS[0].billing = "none"


def test_a_row_is_refused_when_the_module_loads(tmp_path):
    # Load-time refusal: a module whose rows hold a bad one fails on import.
    source = (LIB_DIR / "gate_lane_rows.py").read_text()
    marker = 'billing=BILLING_SUBSCRIPTION,\n        model_env="VNX_KIMI_GATE_MODEL"'
    assert marker in source
    broken = tmp_path / "gate_lane_rows.py"
    broken.write_text(source.replace(marker, 'billing="free",\n        model_env="VNX_KIMI_GATE_MODEL"'))
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import gate_lane_rows", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "GateLaneRowError" in result.stderr
    assert "'kimi_gate'" in result.stderr and "'billing'" in result.stderr


def test_rows_module_is_stdlib_only():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import gate_lane_rows; "
        "bad = [m for m in ('config_runtime', 'config_registry', 'unified_report_schema') "
        "if m in sys.modules]; "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(LIB_DIR)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
