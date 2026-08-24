#!/usr/bin/env python3
"""Tests for config_runtime — the runtime-process façade that autowires config_registry (P0, PR 6).

Dispatch-ID: 20260627-config-runtime / 20260824-alpha-a5-config-reader-fallback (OI-1461)

Covers: autowire binds the DB resolver + default project so a UI-set value is honoured; behaviour
preservation (no DB value / no state → exactly env-or-default); idempotence + fail-soft; and
config_registry's new default-project resolution.

OI-1461: ``_resolve_state_dir`` used to rely SOLELY on ``$VNX_STATE_DIR`` and return None the
moment it was unset — even though ``vnx_paths.resolve_paths()`` (the fabric's canonical resolver)
finds the exact same store without it. Every test below mocks ``vnx_paths.resolve_paths`` to `{}`
by default (via the autouse fixture) so the REAL environment's actual central store — which, on
this repo's own dev machine, genuinely exists at ``~/.vnx-data/vnx-dev/state`` — never leaks into
a "no state dir" test and silently flips its outcome.
"""

import logging
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_registry as cr  # noqa: E402
import config_store_db as cs  # noqa: E402
import config_runtime as crt  # noqa: E402
import vnx_paths  # noqa: E402

PID = "vnx-dev"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in list(cr.CONFIG_REGISTRY):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(k)}", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    # Default: the canonical resolver finds nothing. This is what makes the OLD "no VNX_STATE_DIR
    # -> False" tests still deterministic under the NEW fallback — without this, they would
    # silently pick up this repo's own real ~/.vnx-data/vnx-dev/state store. Tests that want to
    # exercise the fallback override this explicitly.
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {})
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    yield
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


def _state_dir_with(tmp_path, key, value, project_id=PID):
    sd = tmp_path / "state"
    sd.mkdir()
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    entry = cr.CONFIG_REGISTRY[key]
    approval_id = "test-approval" if entry.requires_approval else None
    cs.set_config(conn, project_id, key, value, actor="op", approval_id=approval_id)
    conn.close()
    return sd


# ---------------------------------------------------------------------------
# autowire honours a UI-set value
# ---------------------------------------------------------------------------

def test_autowire_honours_db_value(tmp_path, monkeypatch):
    sd = _state_dir_with(tmp_path, "VNX_SCOUT_PREPASS", "1")
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "0")  # env says off…
    assert crt.autowire(state_dir=sd, project_id=PID) is True
    assert crt.get_bool("VNX_SCOUT_PREPASS") is True  # …DB (UI) wins


def test_get_autowires_from_env(tmp_path, monkeypatch):
    sd = _state_dir_with(tmp_path, "VNX_TAGGER_PROVIDER", "kimi")
    monkeypatch.setenv("VNX_STATE_DIR", str(sd))
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    # no explicit autowire() — get() must autowire from the env
    assert crt.get("VNX_TAGGER_PROVIDER") == "kimi"


# ---------------------------------------------------------------------------
# behaviour preservation + fail-soft
# ---------------------------------------------------------------------------

def test_no_db_value_falls_through_to_env(tmp_path, monkeypatch):
    sd = _state_dir_with(tmp_path, "VNX_TAGGER_ENABLED", "1")  # a DB exists, but for a different key
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "1")
    crt.autowire(state_dir=sd, project_id=PID)
    assert crt.get_bool("VNX_SCOUT_PREPASS") is True   # no DB row → env wins
    assert crt.get_bool("VNX_OUTCOME_GROUNDING_V2") is False  # no DB row, no env → default


def test_autowire_failsoft_without_state_dir():
    assert crt.autowire(state_dir=None, project_id=PID) is False
    # registry stays env-only → default
    assert crt.get_bool("VNX_SCOUT_PREPASS") is False


def test_autowire_failsoft_without_db(tmp_path):
    empty = tmp_path / "no-db"
    empty.mkdir()
    assert crt.autowire(state_dir=empty, project_id=PID) is False


def test_autowire_is_idempotent(tmp_path):
    sd = _state_dir_with(tmp_path, "VNX_SCOUT_PREPASS", "1")
    assert crt.autowire(state_dir=sd, project_id=PID) is True
    # A second call with the same (state_dir, project_id) is a fast no-op and still returns True.
    assert crt.autowire(state_dir=sd, project_id=PID) is True
    assert crt.get_bool("VNX_SCOUT_PREPASS") is True


# ---------------------------------------------------------------------------
# OI-1461: canonical-resolver fallback when VNX_STATE_DIR is unset
# ---------------------------------------------------------------------------

def test_get_falls_back_to_canonical_resolver_without_env(tmp_path, monkeypatch):
    """The bug: a real operator-set value (VNX_CI_GATE_REQUIRED=1 in project_config) was
    invisible to config_runtime.get() whenever the process started without VNX_STATE_DIR
    exported — even though vnx_paths.resolve_paths() finds the same store fine. Without the
    fix this asserts "0" (the registry default) instead of the DB value "1"."""
    sd = _state_dir_with(tmp_path, "VNX_CI_GATE_REQUIRED", "1")
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {"VNX_STATE_DIR": str(sd)})
    monkeypatch.setattr(vnx_paths, "project_id_from_state_dir", lambda _sd: PID)

    assert crt.get("VNX_CI_GATE_REQUIRED") == "1"
    assert crt.get_bool("VNX_CI_GATE_REQUIRED") is True


def test_autowire_warns_loudly_when_store_unfindable(monkeypatch, caplog):
    """Fail-loud requirement: a genuinely unfindable store (no env var, canonical resolver also
    comes up empty) must be visibly DIFFERENT from a flag the operator turned off, or an
    operator-set '1' and a missing store both read back as the same silent '0'. This must fail
    on the absence of the WARNING, not on a missing symbol — autowire() already returned False
    before this fix; what was missing is that it did so silently."""
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {})  # canonical resolver: nothing found
    with caplog.at_level(logging.WARNING, logger="config_runtime"):
        result = crt.autowire(project_id=PID)
    assert result is False
    assert any(
        "state-dir" in rec.message.lower() and "config_runtime" in rec.name
        for rec in caplog.records
    ), f"expected a loud WARNING naming the unresolved state-dir; got: {[r.message for r in caplog.records]}"


def test_explicit_env_state_dir_still_wins(tmp_path, monkeypatch):
    """Control: an explicit VNX_STATE_DIR must keep winning outright — the canonical resolver
    must not even be consulted when it is set. Proven by making the mocked resolver raise."""
    sd = _state_dir_with(tmp_path, "VNX_SCOUT_PREPASS", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(sd))
    monkeypatch.setenv("VNX_PROJECT_ID", PID)

    def _must_not_be_called():
        raise AssertionError("canonical resolver must not be consulted when VNX_STATE_DIR is set")

    monkeypatch.setattr(vnx_paths, "resolve_paths", _must_not_be_called)

    assert crt.get_bool("VNX_SCOUT_PREPASS") is True


# ---------------------------------------------------------------------------
# config_registry default-project resolution
# ---------------------------------------------------------------------------

def test_default_project_used_when_arg_omitted():
    cr.set_db_resolver(lambda pid, key: "1" if (pid == PID and key == "VNX_SCOUT_PREPASS") else None)
    # without a default project, an omitted project_id resolves to None → resolver returns None
    assert cr.get("VNX_SCOUT_PREPASS") == "0"
    cr.set_default_project_id(PID)
    assert cr.get("VNX_SCOUT_PREPASS") == "1"  # now the omitted arg resolves to the default project


def test_explicit_project_id_beats_default():
    cr.set_default_project_id("default-proj")
    seen = {}
    def _res(pid, key):
        seen["pid"] = pid
        return None
    cr.set_db_resolver(_res)
    cr.get("VNX_SCOUT_PREPASS", project_id="explicit-proj")
    assert seen["pid"] == "explicit-proj"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
