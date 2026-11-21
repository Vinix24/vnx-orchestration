"""morning round D1a: the pure morning-pause core and its four registry keys (ADR-095)."""
import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import config_registry as cr  # noqa: E402
import morning_pause as mp  # noqa: E402

AMS = ZoneInfo("Europe/Amsterdam")
DEFAULTS = {
    "VNX_MORNING_PAUSE_ENABLED": "true",
    "VNX_MORNING_PAUSE_START": "07:00",
    "VNX_MORNING_PAUSE_DEADLINE": "09:00",
    "VNX_MORNING_PAUSE_TZ": "Europe/Amsterdam",
}


def _window(**over):
    return mp.window_from_config({**DEFAULTS, **over}.get)


def _ams(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=AMS)


@pytest.mark.parametrize("decided,paused,reason", [
    (False, True, "open_points"),
    (True, False, "decided"),
    (None, True, "decision_unknown"),
])
def test_inside_the_window_follows_the_decision(decided, paused, reason):
    v = mp.is_paused(_window(), _ams(2026, 11, 19, 8), decided=decided)
    assert (v.paused, v.reason) == (paused, reason)


def test_disabled_is_never_paused():
    v = mp.is_paused(_window(VNX_MORNING_PAUSE_ENABLED="false"), _ams(2026, 11, 19, 8), decided=False)
    assert (v.paused, v.reason) == (False, "disabled")


@pytest.mark.parametrize("decided", [False, True, None])
def test_before_start_is_not_paused(decided):
    v = mp.is_paused(_window(), _ams(2026, 11, 19, 6, 59), decided=decided)
    assert (v.paused, v.reason) == (False, "before_start")


@pytest.mark.parametrize("decided", [False, True, None])
def test_after_deadline_is_never_paused(decided):
    for hh, mm in ((9, 0), (9, 1), (23, 59)):
        v = mp.is_paused(_window(), _ams(2026, 11, 19, hh, mm), decided=decided)
        assert (v.paused, v.reason) == (False, "after_deadline")


def test_start_boundary_is_inside_and_reasons_are_the_closed_set():
    v = mp.is_paused(_window(), _ams(2026, 11, 19, 7), decided=False)
    assert v.paused and v.reason in mp.REASONS


@pytest.mark.parametrize("day,summer", [
    ((2026, 3, 29), True), ((2026, 10, 25), False),
    ((2027, 3, 28), True), ((2027, 10, 31), False),
])
def test_dst_change_days(day, summer):
    # 07:00 local is 05:00Z in summer time and 06:00Z in winter time (the change is at 01:00Z).
    utc_hour = 5 if summer else 6
    y, mo, d = day
    at_start = datetime(y, mo, d, utc_hour, 0, tzinfo=timezone.utc)
    before = datetime(y, mo, d, utc_hour - 1, 59, tzinfo=timezone.utc)
    assert mp.is_paused(_window(), at_start, decided=False).paused is True
    assert mp.is_paused(_window(), before, decided=False).reason == "before_start"
    deadline = datetime(y, mo, d, utc_hour + 2, 0, tzinfo=timezone.utc)
    assert mp.is_paused(_window(), deadline, decided=False).reason == "after_deadline"


def test_naive_now_raises():
    with pytest.raises(ValueError, match="timezone-aware"):
        mp.is_paused(_window(), datetime(2026, 11, 19, 8), decided=False)


@pytest.mark.parametrize("key,value", [
    ("VNX_MORNING_PAUSE_START", "7:00"), ("VNX_MORNING_PAUSE_START", "25:00"),
    ("VNX_MORNING_PAUSE_START", ""), ("VNX_MORNING_PAUSE_START", None),
    ("VNX_MORNING_PAUSE_DEADLINE", "9:00"), ("VNX_MORNING_PAUSE_DEADLINE", "09:60"),
    ("VNX_MORNING_PAUSE_DEADLINE", ""),
    ("VNX_MORNING_PAUSE_TZ", "Mars/Olympus"), ("VNX_MORNING_PAUSE_TZ", ""),
    ("VNX_MORNING_PAUSE_TZ", None),
    ("VNX_MORNING_PAUSE_ENABLED", "maybe"), ("VNX_MORNING_PAUSE_ENABLED", None),
])
def test_bad_config_raises_naming_the_key(key, value):
    with pytest.raises(ValueError, match=key):
        _window(**{key: value})


@pytest.mark.parametrize("start,deadline", [("09:00", "09:00"), ("10:00", "09:00")])
def test_start_not_before_deadline_raises(start, deadline):
    with pytest.raises(ValueError, match="VNX_MORNING_PAUSE_START"):
        _window(VNX_MORNING_PAUSE_START=start, VNX_MORNING_PAUSE_DEADLINE=deadline)


def test_is_paused_with_a_hand_built_bad_window_raises():
    bad = mp.PauseWindow(True, "7:00", "09:00", "Europe/Amsterdam")
    with pytest.raises(ValueError, match="VNX_MORNING_PAUSE_START"):
        mp.is_paused(bad, _ams(2026, 11, 19, 8), decided=False)


def test_registry_keys_have_the_defaults_and_project_scope():
    expected = {
        "VNX_MORNING_PAUSE_ENABLED": ("bool", "true"),
        "VNX_MORNING_PAUSE_START": ("string", "07:00"),
        "VNX_MORNING_PAUSE_DEADLINE": ("string", "09:00"),
        "VNX_MORNING_PAUSE_TZ": ("string", "Europe/Amsterdam"),
    }
    for key, (type_, default) in expected.items():
        e = cr.CONFIG_REGISTRY[key]
        assert (e.type, e.default, e.scope) == (type_, default, "project")
        # ENABLED stays wired so canonical_flags() has a candidate for the new subsystem
        assert e.read_site_wired is True
    assert {cr.CONFIG_REGISTRY[k].subsystem for k in expected} == {"morning-pause"}


def test_canonical_flags_unchanged_for_every_main_subsystem():
    from vnx_cli.commands.subsystems import _canonical_flags

    base = subprocess.run(
        ["git", "show", "origin/main:scripts/lib/config_registry.py"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    ).stdout
    spec = importlib.util.spec_from_loader("config_registry_main_snapshot", loader=None)
    snap = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = snap  # dataclasses resolve the module through sys.modules
    try:
        exec(compile(base, "config_registry_main", "exec"), snap.__dict__)  # origin/main's real text
    finally:
        del sys.modules[spec.name]
    # config-een-oppervlak PR-6v deletes this subsystem with VNX_PLAN_GATE_COMPLEX_ONLY on purpose
    removed = {"plan-gate-task-class-scope"}
    old = {e.subsystem: k for k, e in snap.CONFIG_REGISTRY.items() if e.subsystem and e.subsystem not in removed}
    now = _canonical_flags(cr)
    # every subsystem origin/main knows keeps its flag; a missing one shows as None
    assert {k: now.get(k) for k in old} == old
    assert now["morning-pause"] == "VNX_MORNING_PAUSE_ENABLED"
    assert {k: v for k, v in cr.canonical_flags().items() if k in old} == {
        k: v for k, v in snap.canonical_flags().items() if k not in removed}
    assert cr.canonical_flags()["morning-pause"] == "VNX_MORNING_PAUSE_ENABLED"
