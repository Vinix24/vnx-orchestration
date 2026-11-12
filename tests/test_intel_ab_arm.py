"""Tests for the deterministic A/B arm (effect PR-2).

The arm is a pure function of the frozen window, the project and the dispatch.
Anything invalid or disabled gives no arm at all, never a silent 'treatment'.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(_LIB_DIR))

import intelligence_selector as sel  # noqa: E402

WINDOW = {
    "window_id": "w1", "salt": "salt-a", "control_fraction": 0.5,
    "opened_utc": "2026-11-10T08:00:00Z", "hash_version": 1,
}


@pytest.fixture
def ab_on(monkeypatch):
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)


def _arms(window, ids, project="p1"):
    return [sel.arm_for_dispatch(i, project, window)[0] for i in ids]


def test_same_input_same_arm_and_window_stamp(ab_on):
    first = sel.arm_for_dispatch("dlv-x", "p1", dict(WINDOW))
    second = sel.arm_for_dispatch("dlv-x", "p1", dict(WINDOW))
    assert first == second
    arm, ab_window = first
    assert arm in {"control", "treatment"}
    wid, sha = ab_window.split(":")
    assert wid == "w1" and len(sha) == 16


def test_control_share_is_half_over_1000_ids(ab_on):
    arms = _arms(WINDOW, [f"dlv-{i}" for i in range(1000)])
    share = arms.count("control") / 1000
    assert 0.45 <= share <= 0.55
    assert set(arms) == {"control", "treatment"}


def test_disabled_flag_gives_no_arm(monkeypatch):
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "0")
    assert sel.arm_for_dispatch("dlv-x", "p1", dict(WINDOW)) == (None, None)
    monkeypatch.delenv("VNX_INTEL_AB_TEST")
    assert sel.arm_for_dispatch("dlv-x", "p1", dict(WINDOW)) == (None, None)


@pytest.mark.parametrize("window", [None, {}, "x", [], {"window_id": "w1"}])
def test_missing_or_malformed_window_gives_no_arm(ab_on, window):
    assert sel.arm_for_dispatch("dlv-x", "p1", window) == (None, None)


@pytest.mark.parametrize("change", [
    {"control_fraction": 0}, {"control_fraction": 1}, {"control_fraction": -0.1},
    {"control_fraction": "0.5"}, {"control_fraction": None}, {"control_fraction": True},
    {"hash_version": 2}, {"hash_version": None}, {"window_id": ""}, {"salt": "  "},
    {"salt": None}, {"opened_utc": "gisteren"}, {"opened_utc": None}, {"extra": 1},
])
def test_invalid_window_field_gives_no_arm(ab_on, change):
    window = {**WINDOW, **change}
    assert sel.arm_for_dispatch("dlv-x", "p1", window) == (None, None)
    assert sel.load_window_config(json.dumps(window)) is None


@pytest.mark.parametrize("missing", sorted(WINDOW))
def test_missing_window_field_gives_no_arm(ab_on, missing):
    window = {k: v for k, v in WINDOW.items() if k != missing}
    assert sel.arm_for_dispatch("dlv-x", "p1", window) == (None, None)
    assert sel.load_window_config(json.dumps(window)) is None


@pytest.mark.parametrize("raw", [None, "", "  ", "{not json", "[]", "null", "3"])
def test_load_window_config_rejects_unusable_text(raw):
    assert sel.load_window_config(raw) is None


def test_load_window_config_accepts_valid_window():
    assert sel.load_window_config(json.dumps(WINDOW)) == WINDOW


def test_load_window_config_reads_registry_key(monkeypatch):
    monkeypatch.setenv("VNX_INTEL_AB_WINDOW", json.dumps(WINDOW))
    assert sel.load_window_config() == WINDOW
    monkeypatch.setenv("VNX_INTEL_AB_WINDOW", "{broken")
    assert sel.load_window_config() is None


@pytest.mark.parametrize("dispatch_id,project_id", [
    ("", "p1"), ("dlv-x", ""), (None, "p1"), ("dlv-x", None), ("  ", "p1"), (7, "p1"),
])
def test_empty_ids_give_no_arm(ab_on, dispatch_id, project_id):
    assert sel.arm_for_dispatch(dispatch_id, project_id, dict(WINDOW)) == (None, None)


@pytest.mark.parametrize("what", ["salt", "project", "config"])
def test_input_change_changes_assignment_or_stamp(ab_on, what):
    ids = [f"dlv-{i}" for i in range(200)]
    if what == "salt":
        assert _arms(WINDOW, ids) != _arms({**WINDOW, "salt": "salt-b"}, ids)
    elif what == "project":
        assert _arms(WINDOW, ids, "p1") != _arms(WINDOW, ids, "p2")
    else:
        _, a = sel.arm_for_dispatch("dlv-x", "p1", dict(WINDOW))
        _, b = sel.arm_for_dispatch("dlv-x", "p1", {**WINDOW, "control_fraction": 0.4})
        assert a != b


def test_two_subprocesses_agree(tmp_path):
    code = (
        "import json,sys;sys.path.insert(0, sys.argv[1]);"
        "import intelligence_selector as s;"
        "print(json.dumps(s.arm_for_dispatch('dlv-x','p1',s.load_window_config())))"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "VNX_INTEL_AB_TEST": "1",
        "VNX_INTEL_AB_WINDOW": json.dumps(WINDOW),
        "VNX_PROJECT_ID": "p1",
        "HOME": str(tmp_path),
    }
    run = lambda: subprocess.run([sys.executable, "-c", code, str(_LIB_DIR)], env=env, capture_output=True, text=True, check=True, cwd=tmp_path).stdout
    outs = [run(), run()]
    assert outs[0] == outs[1]
    arm, ab_window = json.loads(outs[0])
    assert arm in {"control", "treatment"} and ab_window.startswith("w1:")


@pytest.mark.parametrize("flag", ["1", "0", None])
def test_derive_arm_is_the_writers_arm_and_reads_no_flag(monkeypatch, flag):
    ids = [f"dlv-{i}" for i in range(200)]
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    written = [sel.arm_for_dispatch(i, "p1", dict(WINDOW)) for i in ids]
    monkeypatch.delenv("VNX_INTEL_AB_TEST") if flag is None else monkeypatch.setenv("VNX_INTEL_AB_TEST", flag)

    def no_read(*_args, **_kwargs):
        raise AssertionError("derive_arm read a flag or the environment")

    import config_runtime
    for target, name in ((sel, "_ab_test_enabled"), (config_runtime, "get"), (config_runtime, "get_bool")):
        monkeypatch.setattr(target, name, no_read)
    monkeypatch.setattr(sel, "os", None)  # an environment read inside the selector now fails
    assert [(sel.derive_arm(dict(WINDOW), "p1", i), sel.window_stamp(dict(WINDOW))) for i in ids] == written


@pytest.mark.parametrize("window,dispatch_id,project_id", [
    (None, "dlv-x", "p1"), ("x", "dlv-x", "p1"), ({**WINDOW, "salt": ""}, "dlv-x", "p1"),
    ({**WINDOW, "extra": 1}, "dlv-x", "p1"), (dict(WINDOW), "", "p1"), (dict(WINDOW), "dlv-x", None),
])
def test_derive_arm_and_window_stamp_give_none_on_invalid_input(window, dispatch_id, project_id):
    assert sel.derive_arm(window, project_id, dispatch_id) is None
    if not isinstance(window, dict) or sel.load_window_config(json.dumps(window)) is None:
        assert sel.window_stamp(window) is None


def test_old_random_arm_is_gone():
    assert not any(hasattr(sel, n) for n in ("_ab_arm", "AB_CONTROL_FRACTION", "random"))


@pytest.mark.parametrize("dispatch_id,assigned", [("dlv-c", "control"), ("dlv-a", "treatment")])
def test_resolve_arm_placebo_wins(monkeypatch, dispatch_id, assigned):
    monkeypatch.setenv("VNX_INTEL_PLACEBO_ARM", "1")
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", "p1")
    monkeypatch.setenv("VNX_INTEL_AB_WINDOW", json.dumps(WINDOW))
    assert sel.arm_for_dispatch(dispatch_id, "p1", WINDOW)[0] == assigned
    assert sel._resolve_arm(dispatch_id) == ("placebo", None)


def test_resolve_arm_without_window_is_none_not_treatment(monkeypatch):
    monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.delenv("VNX_INTEL_AB_WINDOW", raising=False)
    assert sel._resolve_arm("dlv-x") == (None, None)


def test_resolve_arm_uses_hash_in_window(monkeypatch):
    monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", "p1")
    monkeypatch.setenv("VNX_INTEL_AB_WINDOW", json.dumps(WINDOW))
    expected = sel.arm_for_dispatch("dlv-x", "p1", WINDOW)
    assert sel._resolve_arm("dlv-x") == expected
    assert sel._resolve_arm("") == (None, None)


# --- an unassigned dispatch is never recorded as treatment (round 2) ---------


@pytest.fixture
def captured(monkeypatch):
    """Patch only the selection and the two audit writers; the arm wiring runs for real."""
    from intelligence_sources._models import InjectionResult

    audit = []

    def fake_select(self, **kwargs):
        return InjectionResult(
            injection_point="dispatch_create",
            injected_at="2026-11-10T00:00:00Z",
            items=[],
            suppressed=[],
            task_class="coding",
            dispatch_id=kwargs.get("dispatch_id", ""),
        )

    monkeypatch.setattr(sel.IntelligenceSelector, "select", fake_select)
    for name in ("emit_event", "record_injection"):
        monkeypatch.setattr(sel.IntelligenceSelector, name, lambda self, r, _n=name, **kw: audit.append((_n, r.ab_arm)))
    return audit


def _run_both(tmp_path):
    direct = sel.select_intelligence("dlv-x", "dispatch_create", quality_db_path=tmp_path / "q.db", coord_state_dir=tmp_path)
    ctx = sel.build_intelligence_context(dispatch_id="dlv-x", role="backend-developer", quality_db_path=tmp_path / "q.db", coord_state_dir=tmp_path)
    return direct, ctx.result


@pytest.mark.parametrize("ab_flag,window_env", [
    ("1", None),
    ("1", "not-json"),
    ("1", json.dumps({**WINDOW, "control_fraction": 7})),
    (None, json.dumps(WINDOW)),
])
def test_disabled_or_invalid_window_records_no_arm(monkeypatch, tmp_path, captured, ab_flag, window_env):
    monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)
    for key, val in (("VNX_INTEL_AB_TEST", ab_flag), ("VNX_INTEL_AB_WINDOW", window_env)):
        monkeypatch.delenv(key, raising=False) if val is None else monkeypatch.setenv(key, val)
    direct, ctx = _run_both(tmp_path)
    assert direct.ab_arm is None and ctx.ab_arm is None
    assert captured and all(arm is None for _, arm in captured)


def test_assignment_returning_none_records_no_arm(monkeypatch, tmp_path, captured):
    monkeypatch.setattr(sel, "_resolve_arm", lambda dispatch_id: (None, None))
    direct, ctx = _run_both(tmp_path)
    assert direct.ab_arm is None and ctx.ab_arm is None
    assert captured and all(arm is None for _, arm in captured)


def test_valid_window_records_assigned_arm(monkeypatch, tmp_path, captured):
    import dispatch_register
    monkeypatch.setattr(dispatch_register, "_resolve_central_data_dir", lambda pid: tmp_path / "central" / pid)
    monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", "p1")
    monkeypatch.setenv("VNX_INTEL_AB_WINDOW", json.dumps(WINDOW))
    expected = sel.arm_for_dispatch("dlv-x", "p1", WINDOW)[0]
    assert expected in ("treatment", "control")
    direct, ctx = _run_both(tmp_path)
    assert direct.ab_arm == expected and ctx.ab_arm == expected
    assert captured and all(arm == expected for _, arm in captured)
