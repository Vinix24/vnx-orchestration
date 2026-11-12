"""PR-size thresholds per project: VNX_PR_SIZE_WARN / VNX_PR_SIZE_HOLD in the config registry.

Operator decision 2026-11-09: the limit is configurable, not a hard 450. Only the git helpers
of ``check_pr_size`` are patched; the registry, the project_config store (a throwaway
``runtime_coordination.db`` under ``tmp_path``) and the verdict are the real code.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config_registry
import config_store_db
import pre_merge_gate
import review_stack_support

WARN = "VNX_PR_SIZE_WARN"
HOLD = "VNX_PR_SIZE_HOLD"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    yield from review_stack_support.isolate_config_registry(monkeypatch)


def _state(tmp_path: Path, rows: dict) -> Path:
    """A state dir whose store carries ``{project: {key: value}}`` project_config rows."""
    state = tmp_path / "state"
    state.mkdir()
    conn = sqlite3.connect(state / "runtime_coordination.db")
    try:
        config_store_db.ensure_config_tables(conn)
        for project, kv in rows.items():
            for key, value in kv.items():
                config_store_db.set_config(
                    conn, project, key, value, actor="test", approval_id="appr-size")
        conn.commit()
    finally:
        conn.close()
    return state


def _check(lines: int, project: str = "proj-a", state=None):
    with patch.object(pre_merge_gate, "_resolve_merge_base", return_value="a" * 40), \
            patch.object(pre_merge_gate, "_diff_numstat_totals", return_value=(lines, 0)):
        return pre_merge_gate.check_pr_size(
            VNX_ROOT, base_ref="origin/main", project_id=project, state_dir=state)


def test_registry_carries_the_only_defaults():
    assert config_registry.CONFIG_REGISTRY[WARN].default == "450"
    assert config_registry.CONFIG_REGISTRY[HOLD].default == "600"
    assert not hasattr(pre_merge_gate, "PR_SIZE_WARN")
    assert not hasattr(pre_merge_gate, "PR_SIZE_HOLD")


def test_defaults_warn_above_450_hold_above_600(tmp_path):
    state = _state(tmp_path, {})
    assert _check(450, state=state)["detail"] == "450 lines changed vs origin/main"
    warn = _check(451, state=state)
    assert warn["status"] == "GO" and ">450 warn threshold, default" in warn["detail"]
    assert _check(600, state=state)["status"] == "GO"
    hold = _check(601, state=state)
    assert hold["status"] == "HOLD" and ">600 hold threshold, default" in hold["detail"]


def test_project_config_row_holds_that_project_only(tmp_path):
    # Red run on origin/main: this 400-line PR stayed GO because the gate used the constant 600.
    state = _state(tmp_path, {"proj-a": {HOLD: "300"}})
    held = _check(400, "proj-a", state)
    assert held["status"] == "HOLD"
    assert ">300 hold threshold, project_config" in held["detail"]
    other = _check(400, "proj-b", state)
    assert other["status"] == "GO"
    assert ">450" not in other["detail"] and "invalid" not in other["detail"]


def test_project_config_warn_names_its_source(tmp_path):
    state = _state(tmp_path, {"proj-a": {WARN: "300"}})
    result = _check(400, "proj-a", state)
    assert result["status"] == "GO"
    assert ">300 warn threshold, project_config" in result["detail"]


def test_environment_follows_registry_precedence(tmp_path, monkeypatch):
    state = _state(tmp_path, {"proj-a": {HOLD: "300"}})
    monkeypatch.setenv(HOLD, "350")
    # project_config beats the plain environment value.
    assert ">300 hold threshold, project_config" in _check(400, "proj-a", state)["detail"]
    # With no row the environment value applies.
    env = _check(400, "proj-b", state)
    assert env["status"] == "HOLD" and ">350 hold threshold, env" in env["detail"]
    # The operator override beats the row.
    monkeypatch.setenv("VNX_OVERRIDE_PR_SIZE_HOLD", "250")
    over = _check(260, "proj-a", state)
    assert over["status"] == "HOLD" and ">250 hold threshold, override" in over["detail"]


def test_no_project_and_no_state_dir_uses_environment_and_default(monkeypatch):
    assert _check(601, project=None)["status"] == "HOLD"
    monkeypatch.setenv(HOLD, "300")
    assert _check(400, project=None)["status"] == "HOLD"


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "", "4.5", "None"])
def test_invalid_hold_falls_back_to_defaults_and_names_the_key(tmp_path, monkeypatch, bad):
    monkeypatch.setenv(HOLD, bad)
    result = _check(601, state=_state(tmp_path, {}))
    assert result["status"] == "HOLD"
    assert f"invalid {HOLD}: {bad!r}, default 600" in result["detail"]
    assert _check(600)["status"] == "GO"


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_invalid_warn_falls_back_and_keeps_hold_on_default(tmp_path, monkeypatch, bad):
    monkeypatch.setenv(WARN, bad)
    result = _check(500)
    assert f"invalid {WARN}: {bad!r}, default 450" in result["detail"]
    assert ">450 warn threshold" in result["detail"]
    assert _check(601)["status"] == "HOLD"


def test_invalid_row_in_project_config_never_disables_the_check(tmp_path):
    state = _state(tmp_path, {"proj-a": {HOLD: "abc"}})
    result = _check(601, "proj-a", state)
    assert result["status"] == "HOLD"
    assert f"invalid {HOLD}: 'abc', default 600" in result["detail"]


def test_warn_above_hold_falls_back_to_both_defaults(monkeypatch):
    monkeypatch.setenv(WARN, "800")
    monkeypatch.setenv(HOLD, "300")
    result = _check(500)
    assert result["status"] == "GO" and ">450 warn threshold, default" in result["detail"]
    assert "invalid VNX_PR_SIZE_WARN: 800 above VNX_PR_SIZE_HOLD 300" in result["detail"]
    assert _check(601)["status"] == "HOLD"


def test_unreadable_store_is_named_and_falls_back(tmp_path):
    bad_state = tmp_path / "state"
    bad_state.mkdir()
    (bad_state / "runtime_coordination.db").write_text("not a database")
    result = _check(601, "proj-a", bad_state)
    assert result["status"] == "HOLD"
