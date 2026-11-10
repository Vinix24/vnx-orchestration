#!/usr/bin/env python3
"""``_injection_why_enabled()`` follows the config_registry precedence chain.

Dispatch-ID: dlv-883228a9d810 (intelligence-effect-measurable PR-1).

The chain is override (``VNX_OVERRIDE_*``) > ``project_config`` row > process env > registry
default. The reader used to consult ``os.environ`` only, so an audited ``project_config`` row was
ignored. Stores here are resolver callables; nothing touches a live store.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts"))

import config_registry as cr  # noqa: E402
import config_runtime  # noqa: E402
import gather_intelligence as gi  # noqa: E402

KEY = "VNX_INJECTION_WHY_ENABLED"
WINDOW_KEYS = ("VNX_INTEL_AB_TEST", "VNX_INTEL_PLACEBO_ARM", "VNX_INTEL_AB_WINDOW")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for k in (KEY, f"VNX_OVERRIDE_{cr._bare(KEY)}"):
        monkeypatch.delenv(k, raising=False)
    # No ambient store: the resolver under test is the only DB layer in play.
    monkeypatch.setattr(config_runtime, "autowire", lambda *a, **kw: False)
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    yield
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


def _row(value):
    return lambda pid, key: value if key == KEY else None


def test_default_is_off():
    assert gi._injection_why_enabled() is False


def test_project_config_row_enables_with_empty_environment():
    cr.set_db_resolver(_row("1"))
    assert gi._injection_why_enabled() is True


def test_project_config_row_off_stays_off():
    cr.set_db_resolver(_row("0"))
    assert gi._injection_why_enabled() is False


def test_environment_still_enables_without_a_row(monkeypatch):
    monkeypatch.setenv(KEY, "true")
    assert gi._injection_why_enabled() is True


def test_override_env_wins_over_the_row(monkeypatch):
    cr.set_db_resolver(_row("1"))
    monkeypatch.setenv(f"VNX_OVERRIDE_{cr._bare(KEY)}", "0")
    assert gi._injection_why_enabled() is False


def test_row_beats_plain_environment(monkeypatch):
    # Registry chain: project_config > env. The row decides, not the process env.
    monkeypatch.setenv(KEY, "1")
    cr.set_db_resolver(_row("0"))
    assert gi._injection_why_enabled() is False


def test_resolver_that_raises_gives_false(monkeypatch):
    def _boom(key):
        raise RuntimeError("config store down")

    monkeypatch.setattr(config_runtime, "get_bool", _boom)
    assert gi._injection_why_enabled() is False


def test_failing_db_resolver_gives_false_not_true():
    def _boom(pid, key):
        raise RuntimeError("db down")

    cr.set_db_resolver(_boom)
    assert gi._injection_why_enabled() is False


def test_at_most_one_store_read_per_call():
    calls = []

    def _counting(pid, key):
        calls.append(key)
        return "1"

    cr.set_db_resolver(_counting)
    assert gi._injection_why_enabled() is True
    assert len(calls) <= 1


def test_malformed_row_is_not_enabled():
    cr.set_db_resolver(_row("maybe"))
    assert gi._injection_why_enabled() is False


@pytest.mark.parametrize("key", WINDOW_KEYS)
def test_window_keys_registered_and_off_or_empty(key):
    entry = cr.CONFIG_REGISTRY[key]
    assert entry.subsystem == "injection-effectiveness-eval-loop"
    assert entry.default in ("0", "")
