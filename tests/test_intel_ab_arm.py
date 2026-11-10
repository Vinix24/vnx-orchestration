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
    "window_id": "w1",
    "salt": "salt-a",
    "control_fraction": 0.5,
    "opened_utc": "2026-11-10T08:00:00Z",
    "hash_version": 1,
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


@pytest.mark.parametrize(
    "change",
    [
        {"control_fraction": 0},
        {"control_fraction": 1},
        {"control_fraction": -0.1},
        {"control_fraction": "0.5"},
        {"control_fraction": None},
        {"control_fraction": True},
        {"hash_version": 2},
        {"hash_version": None},
        {"window_id": ""},
        {"salt": "  "},
        {"salt": None},
        {"opened_utc": "gisteren"},
        {"opened_utc": None},
        {"extra": 1},
    ],
)
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


def test_salt_changes_assignment(ab_on):
    ids = [f"dlv-{i}" for i in range(200)]
    other = {**WINDOW, "salt": "salt-b"}
    assert _arms(WINDOW, ids) != _arms(other, ids)


def test_project_id_changes_assignment(ab_on):
    ids = [f"dlv-{i}" for i in range(200)]
    assert _arms(WINDOW, ids, "p1") != _arms(WINDOW, ids, "p2")


def test_config_change_changes_window_stamp(ab_on):
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
    outs = [
        subprocess.run(
            [sys.executable, "-c", code, str(_LIB_DIR)],
            env=env, capture_output=True, text=True, check=True, cwd=tmp_path,
        ).stdout
        for _ in range(2)
    ]
    assert outs[0] == outs[1]
    arm, ab_window = json.loads(outs[0])
    assert arm in {"control", "treatment"} and ab_window.startswith("w1:")


def test_old_random_arm_is_gone():
    assert not hasattr(sel, "_ab_arm")
    assert not hasattr(sel, "AB_CONTROL_FRACTION")
    assert not hasattr(sel, "random")


def test_resolve_arm_placebo_wins(monkeypatch):
    monkeypatch.setenv("VNX_INTEL_PLACEBO_ARM", "1")
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.setenv("VNX_INTEL_AB_WINDOW", json.dumps(WINDOW))
    assert sel._resolve_arm("dlv-x") == "placebo"


def test_resolve_arm_without_window_is_none_not_treatment(monkeypatch):
    monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.delenv("VNX_INTEL_AB_WINDOW", raising=False)
    assert sel._resolve_arm("dlv-x") is None


def test_resolve_arm_uses_hash_in_window(monkeypatch):
    monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", "p1")
    monkeypatch.setenv("VNX_INTEL_AB_WINDOW", json.dumps(WINDOW))
    expected = sel.arm_for_dispatch("dlv-x", "p1", WINDOW)[0]
    assert sel._resolve_arm("dlv-x") == expected
    assert sel._resolve_arm("") is None
