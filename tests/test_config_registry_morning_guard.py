"""The registry-wipe helper keeps the conftest morning-pause guard (OI-3071)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import morning_pause
import morning_pause_gate
from review_stack_support import isolate_config_registry


@pytest.fixture(autouse=True)
def _wipe(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


def test_isolate_config_registry_leaves_the_morning_pause_off_inside_the_window(tmp_path):
    now = datetime(2026, 11, 24, 7, 30, tzinfo=ZoneInfo("Europe/Amsterdam"))
    verdict = morning_pause_gate.morning_pause_verdict("tproj", now, state_dir=tmp_path)
    assert verdict.paused is False
    assert verdict.reason == morning_pause.REASON_DISABLED
