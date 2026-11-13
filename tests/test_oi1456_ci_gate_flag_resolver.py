"""OI-1456: the unavailable-reason classifier and the availability check read
VNX_CI_GATE_REQUIRED through the same resolver (config_runtime)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import config_registry as cr
import gate_request_handler
import gate_result_parser
from gate_obligation_runner import _TEMPORARY_NOT_EXECUTABLE_REASONS
from review_stack_support import isolate_config_registry

KEY = "VNX_CI_GATE_REQUIRED"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


class _Parser(gate_result_parser.GateResultParserMixin):
    pass


class _Handler(gate_request_handler.GateRequestHandlerMixin):
    pass


def _which(monkeypatch, present: bool):
    monkeypatch.setattr(
        gate_result_parser.shutil, "which", lambda name: "/usr/bin/x" if present else None
    )


def _db(value):
    cr.set_db_resolver(lambda project_id, key: value if key == KEY else None)


def test_db_off_env_on_gh_present_is_provider_disabled_and_availability_agrees(monkeypatch):
    _db("0")
    monkeypatch.setenv(KEY, "1")
    _which(monkeypatch, True)
    reason, detail = _Parser()._classify_unavailable("ci_gate")
    assert reason == "provider_disabled"
    assert detail == "VNX_CI_GATE_REQUIRED resolves to false (config_runtime)"
    assert _Handler()._ci_gate_available() is False


def test_provider_disabled_keeps_the_obligation_pending(monkeypatch):
    _db("0")
    monkeypatch.setenv(KEY, "1")
    _which(monkeypatch, True)
    reason, _ = _Parser()._classify_unavailable("ci_gate")
    assert reason in _TEMPORARY_NOT_EXECUTABLE_REASONS


def test_db_off_and_gh_absent_names_both(monkeypatch):
    _db("0")
    _which(monkeypatch, False)
    reason, detail = _Parser()._classify_unavailable("ci_gate")
    assert reason == "provider_disabled"
    assert "resolves to false (config_runtime)" in detail
    assert "not found in PATH" in detail


def test_db_unset_env_on_gh_present_falls_through_to_not_configured(monkeypatch):
    monkeypatch.setenv(KEY, "1")
    _which(monkeypatch, True)
    assert _Parser()._classify_unavailable("ci_gate")[0] == "provider_not_configured"


def test_db_unset_env_unset_gh_absent_is_not_installed(monkeypatch):
    # registry default for the key is "1", so the gate is not disabled
    _which(monkeypatch, False)
    assert _Parser()._classify_unavailable("ci_gate")[0] == "provider_not_installed"


def test_env_off_without_db_is_provider_disabled(monkeypatch):
    monkeypatch.setenv(KEY, "0")
    _which(monkeypatch, True)
    assert _Parser()._classify_unavailable("ci_gate")[0] == "provider_disabled"


def test_unregistered_gate_still_reads_the_bare_env(monkeypatch):
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
    _which(monkeypatch, True)
    reason, detail = _Parser()._classify_unavailable("codex_gate")
    assert reason == "provider_disabled"
    assert detail == "VNX_CODEX_HEADLESS_ENABLED is set to 0"


def test_unregistered_gate_enabled_is_not_disabled(monkeypatch):
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "1")
    _which(monkeypatch, True)
    assert _Parser()._classify_unavailable("codex_gate")[0] == "provider_not_configured"
