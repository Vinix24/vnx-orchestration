"""OF-2 (ADR-092): the table, one snapshot per action and the waived_guarantees list."""
from __future__ import annotations

import pytest
import yaml
from review_stack_support import isolate_config_registry

import config_registry
import forge_protection_drift as fpd
import gate_profile
import optional_features as of

APP, GATES, BP = 4869217, ["vnx-gate/door", "vnx-gate/review"], "scripts/forge/branch_protection.yaml"
WT = f"{BP}@worktree"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "global" / "config.json"))
    for key in ("VNX_HOME", "PROJECT_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
    monkeypatch.setattr("config_runtime.strict_resolver", lambda: None)
    monkeypatch.setattr(gate_profile, "ci_requirement", lambda *a, **k: (True, ["default"]))
    yield from isolate_config_registry(monkeypatch)


def _none(_project_id, _key):
    return None


def _yaml(contexts, **extra):
    checks = [{"context": c, "app_id": APP if c.startswith("vnx-gate/") else 15368} for c in contexts]
    return yaml.safe_dump({
        "branch": "main", "required_status_checks": {"strict": False, "checks": checks}, "pending_checks": [],
        "required_pull_request_reviews": None, "restrictions": None, "repo": {"allow_auto_merge": False},
        "rulesets": [], "app": {"slug": "vnx-gate", "app_id": APP},
        **{f: False for f in fpd._BOOL_TOP_LEVEL_FIELDS}, **extra})


def _write(root, rel, text):
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def _rows(tmp_path, feature, **kw):
    rows = of.waived_guarantees(None, resolver=kw.pop("resolver", _none), project_root=tmp_path, **kw)
    return [(r["key"], r["value"], r["source"]) for r in rows if r["feature"] == feature]


def test_bind_table_to_registry():
    for feature in of.FEATURES:
        assert all(text.strip() for text in feature.off.values())
        if feature.key != of.FILE_KEY:
            allowed = getattr(config_registry.CONFIG_REGISTRY[feature.key], "allowed_values", None)
            assert not allowed or set(feature.on) | set(feature.off) <= set(allowed)
    assert all(text.strip() for _context, text in of.GATE_CHECK_GUARANTEES.values())


def test_ci_row_only_when_no_source_asks(tmp_path, monkeypatch):
    assert _rows(tmp_path, "ci") == []
    monkeypatch.setattr(gate_profile, "ci_requirement", lambda *a, **k: (False, []))
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    assert _rows(tmp_path, "ci") == [("VNX_CI_GATE_REQUIRED", "0", "env")]
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "1")  # the flag reads true where ci_requirement said no
    with pytest.raises(of.OptionalFeatureRefused, match="ci_requirement_unreadable.*'1' \\(source: env\\)"):
        of.snapshot(None, resolver=_none, project_root=tmp_path)


@pytest.mark.parametrize("mode, expected", [
    ("required", []), ("advisory", [("VNX_PLAN_GATE_ENFORCE", "advisory", "env")]),
    ("off", [("VNX_PLAN_GATE_ENFORCE", "off", "env")]),
    (None, [("VNX_PLAN_GATE_ENFORCE", "advisory", "default")])])  # unset: the default is named
def test_plan_gate_modes(tmp_path, monkeypatch, mode, expected):
    if mode is not None:
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", mode)
    assert _rows(tmp_path, "plan_gate") == expected


def _broken(_pid, _key):
    if _key == of.PLAN_GATE_KEY:
        raise OSError("store gone")
    return None


@pytest.mark.parametrize("env, files, resolver, code, named", [
    ("bogus", {}, _none, "plan_gate_mode_unknown", "'bogus' (source: env)"),
    (None, {}, _broken, "plan_gate_mode_unreadable", "VNX_PLAN_GATE_ENFORCE"),
    (None, {BP: "branch: [unclosed"}, _none, "branch_protection_unreadable", WT),
    (None, {BP: _yaml(GATES, enforcement="bogus")}, _none, "branch_protection_unreadable", "bogus"),
    (None, {".vnx/branch_protection.yaml/x": "a directory"}, _none, "branch_protection_unreadable", "not a file"),
])
def test_unknown_or_unreadable_refuses(tmp_path, monkeypatch, env, files, resolver, code, named):
    if env:
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", env)
    for rel, text in files.items():
        _write(tmp_path, rel, text)
    with pytest.raises(of.OptionalFeatureRefused) as exc:
        of.snapshot(None, resolver=resolver, project_root=tmp_path)
    assert exc.value.code == code and named in str(exc.value)


@pytest.mark.parametrize("files, expected", [
    ({}, [("file", "absent", "file_absent")]),
    ({BP: _yaml(["ci / test"])}, [("vnx_gate_door", "not_required", WT), ("vnx_gate_review", "not_required", WT)]),
    ({BP: _yaml(GATES, enforcement="warn")}, [("enforcement", "warn", WT)]),
    ({BP: _yaml(GATES, enforcement="off")}, [("enforcement", "off", WT)]),
    ({BP: _yaml(GATES)}, []),
    ({BP: _yaml(GATES, enforcement="off"), ".vnx/branch_protection.yaml": _yaml(GATES)}, []),
])
def test_branch_protection_rows(tmp_path, files, expected):
    for rel, text in files.items():
        _write(tmp_path, rel, text)
    assert _rows(tmp_path, "branch_protection") == expected
    assert of.snapshot(None, resolver=_none, project_root=tmp_path)["branch_protection"].server_verified is None


def test_passed_text_puts_its_source_on_every_row(tmp_path):
    rows = _rows(tmp_path, "branch_protection", protection_yaml=_yaml(["ci / test"], enforcement="warn"),
                 protection_source=f"{BP}@abc123")
    assert [r[2] for r in rows] == [f"{BP}@abc123"] * 3


def test_one_snapshot_one_read_each(tmp_path, monkeypatch):
    _write(tmp_path, BP, _yaml(GATES))
    calls, parse = [], fpd.parse_protection_config
    monkeypatch.setattr(fpd, "parse_protection_config", lambda text: calls.append("parse") or parse(text))
    monkeypatch.setattr(gate_profile, "ci_requirement", lambda *a, **k: calls.append("ci") or (True, ["default"]))
    rows = of.waived_guarantees(None, snapshot=of.snapshot(None, resolver=_none, project_root=tmp_path))
    assert sorted(calls) == ["ci", "parse"]
    assert [(r["feature"], r["key"]) for r in rows] == [("plan_gate", "VNX_PLAN_GATE_ENFORCE")]
