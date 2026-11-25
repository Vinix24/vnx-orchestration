"""ADR-097: register ci_run/1 configuration before M3a/M3b add readers."""

import sys
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
import config_global
import config_registry as cr
import config_store_db as cs

BUDGET = "VNX_CI_RECORD_MAX_CALLS"
ROWS = {
    "VNX_CI_WORKFLOWS": ("string", "", "M3a"),
    BUDGET: ("integer", "1000", "M3a"),
    "VNX_CI_JUNIT_ARTIFACT": ("string", "", "M3b"),
}
INVALID = ("0", "-1", "abc", "", "1.5", "NaN", "Infinity", "1e3")


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    for key in ROWS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{key[4:]}", raising=False)
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "global.json"))
    monkeypatch.setattr(cr, "_db_resolver", None)


@pytest.mark.parametrize("key", ROWS)
def test_ci_run_key_contract(key):
    entry = cr.CONFIG_REGISTRY[key]
    type_, default, owner = ROWS[key]
    assert (entry.type, entry.default, entry.scope) == (type_, default, "project")
    assert entry.read_site_wired is False
    assert entry.category == "gate"
    assert entry.subsystem == "governance-enforcement-stack"
    assert "ADR-097" in entry.description and owner in entry.description
    assert cr.get(key) == default
    assert key not in cr.canonical_flags().values()


@pytest.mark.parametrize("value", INVALID)
@pytest.mark.parametrize("source", ("env", "override", "global_config", "project_config"))
def test_invalid_budget_is_rejected_in_every_layer(value, source, monkeypatch, tmp_path):
    if source == "project_config":
        with closing(sqlite3.connect(tmp_path / "runtime_coordination.db")) as conn:
            cs.set_config(conn, "project-a", BUDGET, value, actor="test")
        monkeypatch.setattr(cr, "_db_resolver", cs.make_db_resolver(lambda _: tmp_path))
    elif source == "global_config":
        config_global.set_global(BUDGET, value, actor="test", reason="validate budget")
    else:
        monkeypatch.setenv("VNX_OVERRIDE_CI_RECORD_MAX_CALLS" if source == "override" else BUDGET, value)
    with pytest.raises(ValueError, match=BUDGET):
        cr.resolve_with_source(BUDGET, "project-a")
    with pytest.raises(ValueError, match=BUDGET):
        cr.get(BUDGET, "project-a")


@pytest.mark.parametrize("value", ("1", "1000", "1001"))
def test_positive_budget_is_accepted(value, monkeypatch):
    monkeypatch.setenv(BUDGET, value)
    assert cr.get(BUDGET) == value


@pytest.mark.parametrize("key", ("VNX_CI_WORKFLOWS", "VNX_CI_JUNIT_ARTIFACT"))
def test_empty_adapter_values_are_accepted(key, monkeypatch):
    monkeypatch.setenv(key, "")
    assert cr.resolve_with_source(key) == ("", "env")


@pytest.mark.parametrize("value", (*INVALID, None, 1.5, True))
def test_constraint_rejects_invalid_values_and_defaults(value):
    entry = cr.CONFIG_REGISTRY[BUDGET]
    with pytest.raises(ValueError, match=BUDGET):
        entry.validate(value)
    with pytest.raises(ValueError, match=BUDGET):
        replace(entry, default=value)


@pytest.mark.parametrize("kwargs", ({"minimum": True}, {"minimum": 1.5}, {"type": "string"}))
def test_constraint_declaration_is_checked(kwargs):
    with pytest.raises(ValueError, match=BUDGET):
        replace(cr.CONFIG_REGISTRY[BUDGET], **kwargs)


@pytest.mark.parametrize("source", ("global_config", "project_config"))
def test_unreadable_budget_source_does_not_fall_back(source, monkeypatch, tmp_path):
    if source == "global_config":
        (tmp_path / "global.json").mkdir()
    else:
        (tmp_path / "runtime_coordination.db").mkdir()
        monkeypatch.setattr(cr, "_db_resolver", cs.make_db_resolver(lambda _: tmp_path, raise_on_error=True))
    with pytest.raises(ValueError, match=f"{BUDGET}.*{source}_error"):
        cr.get(BUDGET, "project-a")
    assert cr.resolve_with_source(BUDGET, "project-a") == (None, f"{source}_error")


def test_missing_selected_layer_remains_explicitly_unset():
    assert cr.resolve_with_source(BUDGET, layers=("env",)) == (None, "unset")


def test_legacy_unconstrained_integer_keeps_its_behavior(monkeypatch):
    key = "VNX_MAIN_RUN_MAX_AGE_MIN"
    monkeypatch.setenv(key, "abc")
    assert cr.get(key) == "abc"


def test_explicit_project_store_validates_the_selected_tenant(tmp_path):
    with closing(sqlite3.connect(tmp_path / "runtime_coordination.db")) as conn:
        cs.set_config(conn, "project-a", BUDGET, "0", actor="test")
        cs.set_config(conn, "project-a-other", BUDGET, "1000", actor="test")
    with pytest.raises(ValueError, match=BUDGET):
        cr.get_for_project(BUDGET, "project-a", tmp_path)
    assert cr.get_for_project(BUDGET, "project-a-other", tmp_path) == "1000"


def test_explicit_project_store_read_error_is_visible(tmp_path):
    (tmp_path / "runtime_coordination.db").mkdir()
    with pytest.raises(ValueError, match=f"{BUDGET}.*project_config_error"):
        cr.get_for_project(BUDGET, "project-a", tmp_path)


def test_inventory_and_bool_view_do_not_hide_invalid_budget(monkeypatch):
    monkeypatch.setenv(BUDGET, "0")
    with pytest.raises(ValueError, match=BUDGET):
        cr.all_effective()
    with pytest.raises(ValueError, match=BUDGET):
        cr.get_bool(BUDGET)


@pytest.mark.parametrize("verb", ("get", "explain", "list"))
def test_cli_reports_unreadable_budget_without_losing_diagnostics(verb, tmp_path):
    from scripts import config_cli

    (tmp_path / "runtime_coordination.db").mkdir()
    output = []
    args = [verb] if verb == "list" else [verb, BUDGET]
    assert config_cli.main(args, project_id="project-a", state_dir=tmp_path, out=output.append) == 3
    assert "project_config_error" in "\n".join(output)


def test_cli_can_explain_default_budget(tmp_path):
    from scripts import config_cli

    output = []
    assert config_cli.main(["explain", BUDGET], project_id="project-a", state_dir=tmp_path,
                           out=output.append) == 0
    assert "default: 1000" in output[0] and "winner: default" in output[0]
