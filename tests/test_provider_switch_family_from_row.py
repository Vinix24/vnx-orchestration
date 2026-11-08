"""The provider family of a gate comes from the ``family`` column of its row (review-lane P9a, OI-2654).

A registered row is decided on the family it names, so a new provider is a row and not an edit
of the hand tables in ``provider_switch``. Any other identifier is decided by those tables as
before, and an unknown identifier still fails closed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import gate_lane_rows
import gate_recorder
import provider_switch
import review_stack_support

SYNTHETIC = "family_probe_gate"
SYNTHETIC_LANE = "family-probe-lane"


def _row(family: str, gate: str = SYNTHETIC) -> gate_lane_rows.GateLaneRow:
    return gate_lane_rows.GateLaneRow(
        gate=gate, lane=SYNTHETIC_LANE, request_provider="probe", dispatcher="provider_dispatch",
        billing=gate_lane_rows.BILLING_METERED, model_env="VNX_FAMILY_PROBE_GATE_MODEL",
        model_default="probe-1", single_shot=False, family=family,
    )


def _register(monkeypatch, family: str) -> None:
    rows = gate_lane_rows.validate_rows((*gate_lane_rows.GATE_LANE_ROWS, _row(family)))
    monkeypatch.setattr(gate_lane_rows, "GATE_LANE_ROWS", rows)
    monkeypatch.setitem(
        gate_recorder.GATE_PROVIDERS, SYNTHETIC,
        (gate_recorder.GATE_PROVIDER_HARNESS_LANE, SYNTHETIC_LANE),
    )


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    isolation = review_stack_support.isolate_config_registry(monkeypatch)
    next(isolation)
    switch = tmp_path / "providers.json"
    monkeypatch.setenv("VNX_PROVIDER_SWITCH_FILE", str(switch))
    state = tmp_path / "state"
    state.mkdir()
    yield state
    next(isolation, None)


def _switch(monkeypatch, tmp_path: Path, **flags: bool) -> None:
    section = {
        f: {"enabled": on, "changed_at": "2026-11-08T08:00:00Z", "changed_by": "op", "reason": "test"}
        for f, on in flags.items()
    }
    (tmp_path / "providers.json").write_text(
        json.dumps({"version": 1, "global": section, "projects": {}}), encoding="utf-8"
    )


@pytest.mark.parametrize("gate,family", [
    ("codex_gate", "codex"),
    ("kimi_gate", "kimi"),
    ("glm_gate", "glm"),
    ("deepseek_gate", "deepseek"),
    ("claude_gate", "claude"),
])
def test_the_five_existing_gates_keep_their_family(gate, family):
    assert provider_switch.family_of(gate) == family


@pytest.mark.parametrize("row", gate_lane_rows.GATE_LANE_ROWS, ids=lambda r: r.gate)
def test_every_registered_row_names_the_family_it_had_before(row):
    assert row.family == row.gate.removesuffix("_gate")
    assert provider_switch.family_of(row.gate) == row.family


def test_a_registered_row_decides_on_its_family_column(monkeypatch):
    _register(monkeypatch, "kimi")

    assert provider_switch.family_of(SYNTHETIC) == "kimi"


def test_identifiers_that_are_not_rows_are_decided_by_the_hand_tables(monkeypatch):
    _register(monkeypatch, "kimi")

    assert provider_switch.family_of("deepseek-flash") == "deepseek"
    assert provider_switch.family_of("openrouter/z-ai/glm-5.2") == "glm"
    assert provider_switch.family_of("codex_gate") == "codex"


def test_an_unknown_identifier_still_fails_closed(monkeypatch):
    _register(monkeypatch, "kimi")

    with pytest.raises(provider_switch.UnknownProviderError):
        provider_switch.family_of("never_registered_gate")


def test_a_row_without_a_family_is_a_registration_error_naming_the_gate():
    with pytest.raises(gate_lane_rows.GateLaneRowError, match="no_family_gate.*family"):
        gate_lane_rows.GateLaneRow(
            gate="no_family_gate", lane="l", request_provider="p", dispatcher="provider_dispatch",
            billing=gate_lane_rows.BILLING_METERED, model_env="E", model_default="m",
            single_shot=False,
        )


@pytest.mark.parametrize("bad", ["", "  ", None, 3])
def test_a_malformed_family_is_refused(bad):
    with pytest.raises(gate_lane_rows.GateLaneRowError, match="family"):
        _row(bad)


def test_a_registered_row_with_family_codex_is_available_when_codex_is_on(monkeypatch, state_dir):
    _register(monkeypatch, "codex")

    assert gate_recorder.gate_is_available(SYNTHETIC, state_dir=state_dir) is True


def test_a_registered_row_follows_its_family_switch(monkeypatch, state_dir, tmp_path):
    _register(monkeypatch, "kimi")

    _switch(monkeypatch, tmp_path, kimi=True)
    assert gate_recorder.gate_is_available(SYNTHETIC, state_dir=state_dir) is True

    _switch(monkeypatch, tmp_path, kimi=False)
    assert gate_recorder.gate_is_available(SYNTHETIC, state_dir=state_dir) is False
