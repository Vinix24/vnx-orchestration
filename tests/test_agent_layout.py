"""VNX_AGENT_LAYOUT flag: registry default, off/shadow/on, an unknown value reads as off."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import config_registry as cr


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("VNX_AGENT_LAYOUT", "VNX_OVERRIDE_AGENT_LAYOUT", "VNX_AGENT_FOLDERS"):
        monkeypatch.delenv(key, raising=False)
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


def test_registry_knows_agent_layout_with_default_off():
    entry = cr.CONFIG_REGISTRY["VNX_AGENT_LAYOUT"]
    assert entry.default == "off"
    assert entry.subsystem and entry.status in cr.ALLOWED_STATUSES


def test_default_is_off_and_not_shadow():
    import agent_layout

    assert agent_layout.layout_mode() == "off"
    assert agent_layout.is_shadow() is False


def test_shadow_is_read(monkeypatch):
    import agent_layout

    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")
    assert agent_layout.layout_mode() == "shadow"
    assert agent_layout.is_shadow() is True


def test_shadow_is_case_and_space_tolerant(monkeypatch):
    import agent_layout

    monkeypatch.setenv("VNX_AGENT_LAYOUT", " Shadow ")
    assert agent_layout.layout_mode() == "shadow"


def test_on_is_read_without_a_warning(monkeypatch, caplog):
    import agent_layout

    monkeypatch.setenv("VNX_AGENT_LAYOUT", " On ")
    with caplog.at_level(logging.WARNING, logger=agent_layout.logger.name):
        assert agent_layout.layout_mode() == "on"
        assert agent_layout.is_on() is True
        assert agent_layout.is_shadow() is False
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


@pytest.mark.parametrize("value", ["banana", "1", "true", "shadow2"])
def test_unknown_value_reads_as_off_with_one_warning_per_call(monkeypatch, caplog, value):
    import agent_layout

    monkeypatch.setenv("VNX_AGENT_LAYOUT", value)
    with caplog.at_level(logging.WARNING, logger=agent_layout.logger.name):
        assert agent_layout.layout_mode() == "off"
        first = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(first) == 1
        assert value in first[0].getMessage()
        assert agent_layout.is_shadow() is False
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_off_logs_nothing(monkeypatch, caplog):
    import agent_layout

    monkeypatch.setenv("VNX_AGENT_LAYOUT", "off")
    with caplog.at_level(logging.WARNING, logger=agent_layout.logger.name):
        assert agent_layout.layout_mode() == "off"
    assert not caplog.records


def test_off_leaves_agent_folders_enabled_true(monkeypatch):
    import agent_layout
    import agent_resolver

    monkeypatch.setenv("VNX_AGENT_LAYOUT", "off")
    assert agent_layout.layout_mode() == "off"
    assert agent_resolver.agent_folders_enabled() is True
    monkeypatch.setenv("VNX_AGENT_FOLDERS", "0")
    assert agent_resolver.agent_folders_enabled() is False


def test_shadow_does_not_change_agent_folders_enabled(monkeypatch):
    import agent_resolver

    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")
    assert agent_resolver.agent_folders_enabled() is True
