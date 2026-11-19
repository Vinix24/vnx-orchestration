"""The global layer (ADR-087 decision 3), scope and allowed_values in config_registry (G2)."""

import json
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
import config_registry as cr

KEY = "VNX_TAGGER_PROVIDER"  # scope project, string, default "deepseek"
GKEY = "VNX_G2_TEST_GLOBAL_KEY"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (KEY, GKEY):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(k)}", raising=False)
    monkeypatch.setattr(cr, "_db_resolver", None)
    monkeypatch.setattr(cr, "_default_project_id", None)
    monkeypatch.setattr(cr, "_WARNED_GLOBAL_UNREADABLE", set())
    monkeypatch.setattr(cr, "_WARNED_IGNORED_PROJECT_ROW", set())
    monkeypatch.setitem(cr.CONFIG_REGISTRY, GKEY, cr._e(
        GKEY, "string", "gdefault", "intelligence", "test-only global key", scope="global"))


@pytest.fixture
def gfile(tmp_path, monkeypatch):
    path = tmp_path / "g" / "config.json"
    path.parent.mkdir()
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(path))
    return path


def _write_global(path, **values):
    path.write_text(json.dumps({"version": 1, "values": {k: {"value": v} for k, v in values.items()}}))


def _row(value):
    return lambda pid, key: value


@pytest.mark.parametrize("top", ["override", "project_config", "global_config", "env", "default"])
def test_each_layer_beats_every_layer_below_it(top, gfile, monkeypatch):
    order = list(cr.CHAIN_LAYERS)
    below_and_top = order[order.index(top):]
    if "override" in below_and_top:
        monkeypatch.setenv("VNX_OVERRIDE_TAGGER_PROVIDER", "v-override")
    resolver = _row("v-project_config") if "project_config" in below_and_top else None
    if "global_config" in below_and_top:
        _write_global(gfile, **{KEY: "v-global_config"})
    if "env" in below_and_top:
        monkeypatch.setenv(KEY, "v-env")
    want = "deepseek" if top == "default" else f"v-{top}"
    assert cr.resolve_with_source(KEY, "p1", resolver) == (want, top)


def test_get_reads_the_global_value_with_no_store_wired(gfile, monkeypatch):
    _write_global(gfile, **{KEY: "from-global"})
    monkeypatch.setenv(KEY, "from-env")
    assert cr._db_resolver is None and cr._default_project_id is None
    assert cr.get(KEY) == "from-global"


def test_a_missing_global_file_is_no_value_not_an_error(gfile, monkeypatch):
    assert not gfile.exists()
    monkeypatch.setenv(KEY, "from-env")
    assert cr.resolve_with_source(KEY, strict_resolver=True) == ("from-env", "env")


def test_layers_without_global_config_do_not_see_the_global_value(gfile):
    _write_global(gfile, **{KEY: "from-global"})
    assert cr.resolve_with_source(KEY, layers=("override", "project_config", "env", "default")) \
        == ("deepseek", "default")
    assert cr.resolve_with_source(KEY, layers=("global_config",)) == ("from-global", "global_config")


def test_global_key_ignores_a_project_row_with_one_warning(gfile, caplog):
    _write_global(gfile, **{GKEY: "from-global"})
    with caplog.at_level(logging.WARNING, logger="config_registry"):
        for _ in range(3):
            assert cr.resolve_with_source(GKEY, "p1", _row("from-row")) == ("from-global", "global_config")
    warned = [r.getMessage() for r in caplog.records if "scope" in r.getMessage()]
    assert len(warned) == 1 and "p1" in warned[0] and GKEY in warned[0]


def test_global_key_project_row_loses_to_env_and_default(monkeypatch):
    assert cr.resolve_with_source(GKEY, "p1", _row("from-row")) == ("gdefault", "default")
    monkeypatch.setenv(GKEY, "from-env")
    assert cr.resolve_with_source(GKEY, "p1", _row("from-row")) == ("from-env", "env")


def test_global_key_with_a_failing_resolver_is_not_a_project_error():
    def boom(pid, key):
        raise RuntimeError("db locked")
    assert cr.resolve_with_source(GKEY, "p1", boom, strict_resolver=True) == ("gdefault", "default")


@pytest.mark.parametrize("content", ["{not json", '{"version": 2, "values": {}}', '{"values": {}}',
                                     '{"version": 1, "values": []}'])
def test_unreadable_global_file_strict_stops_on_global_config_error(gfile, monkeypatch, content):
    gfile.write_text(content)
    monkeypatch.setenv(KEY, "from-env")
    assert cr.resolve_with_source(KEY, strict_resolver=True) == (None, "global_config_error")


def test_unreadable_global_file_not_strict_falls_through_with_one_warning(gfile, monkeypatch, caplog):
    gfile.write_text("{not json")
    monkeypatch.setenv(KEY, "from-env")
    with caplog.at_level(logging.WARNING, logger="config_registry"):
        for _ in range(3):
            assert cr.resolve_with_source(KEY) == ("from-env", "env")
        assert cr.get(KEY) == "from-env"
    warned = [r.getMessage() for r in caplog.records if "unreadable" in r.getMessage()]
    assert len(warned) == 1 and str(gfile) in warned[0] and "JSONDecodeError" in warned[0]


# config-een-oppervlak PR-1 gave these keys a scope and allowed_values (tests/test_config_registry_pr1_rows.py).
_PR1_ROWS = {k for k, e in cr.CONFIG_REGISTRY.items() if e.read_site_wired is False and e.scope == "global"} | {
    "VNX_ENFORCEMENT_PROFILE"}


def test_every_existing_key_has_scope_project_and_no_allowed_values():
    for key, entry in cr.CONFIG_REGISTRY.items():
        if key != GKEY and key not in _PR1_ROWS:
            assert (entry.scope, entry.allowed_values) == ("project", None), key


@pytest.mark.parametrize("kwargs", [{"scope": "machine"}, {"scope": None}, {"allowed_values": ()},
                                    {"allowed_values": ["a"]}, {"allowed_values": ("a", 1)}])
def test_bad_scope_or_allowed_values_is_refused(kwargs):
    assert cr._e("VNX_X", "enum", "a", "gate", "x", allowed_values=("a", "b")).allowed_values == ("a", "b")
    with pytest.raises(ValueError):
        cr._e("VNX_X", "enum", "a", "gate", "x", **kwargs)


def test_autouse_fixture_hides_the_developers_global_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".vnx").mkdir(parents=True)
    _write_global(home / ".vnx" / "config.json", **{KEY: "from-developer-home"})
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    assert Path(os.environ["VNX_GLOBAL_CONFIG_FILE"]).is_relative_to(tmp_path)
    assert cr.resolve_with_source(KEY) == ("deepseek", "default")


def test_strict_callers_fail_closed_on_an_unreadable_global_file(gfile, tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import dispatch_bridge
    import main_window as mw
    import pre_merge_gate

    for key in (mw.UNVERIFIED_MAX_KEY, mw.RUN_MAX_AGE_KEY, "VNX_PR_SIZE_WARN", "VNX_PR_SIZE_HOLD"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(key)}", raising=False)
    gfile.write_text("{not json")
    _, _, invalid = mw.read_admission_config(None, None)
    assert f"{mw.UNVERIFIED_MAX_KEY}: {mw.GLOBAL_CONFIG_UNREADABLE}" in invalid
    assert mw.admit(mw.MainWindow(verdict="green"), max_unverified=None, invalid_config=invalid).reason \
        == "config_unreadable"
    sizes = pre_merge_gate._pr_size_thresholds(None, None)
    assert any(i.startswith("global_config_error VNX_PR_SIZE_HOLD") for i in sizes["invalid"])
    import config_store_db  # a readable store with no row: only the global layer is unreadable
    monkeypatch.setattr(config_store_db, "make_db_resolver", lambda *a, **k: (lambda pid, key: None))
    with pytest.raises(ValueError, match="global_config_error"):
        dispatch_bridge._doc_style_setting("p1", tmp_path)
