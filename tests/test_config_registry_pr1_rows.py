#!/usr/bin/env python3
"""config-een-oppervlak PR-1 (ADR-087): the 18 registry rows, behaviour-neutral.

Dispatch-ID: dlv-255c2991e902
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_registry as cr  # noqa: E402
import config_store_db as cs  # noqa: E402

LLM = ("auto", "ollama-only", "claude-only", "deepseek-harness")
# key -> (type, default, scope, allowed_values)
ROWS = {
    "VNX_ANALYZER_LLM": ("string", "ollama-only", "global", LLM),
    "VNX_ANALYZER_RESTRICTED_CLAUDE_CAP": ("integer", "", "global", None),
    "VNX_OLLAMA_HOST": ("string", "http://localhost:11434", "global", None),
    "VNX_OLLAMA_MODEL": ("string", "qwen2.5-coder:14b", "global", None),
    "VNX_DIGEST_EMAIL": ("string", "", "global", None),
    "VNX_SMTP_HOST": ("string", "smtp.gmail.com", "global", None),
    "VNX_RECEIPT_CLASSIFIER_ENABLED": ("bool", "0", "project", None),
    "VNX_AUTO_REPORT": ("bool", "0", "project", None),
    "VNX_ENFORCE_WORKER_PERMISSIONS": ("bool", "0", "project", None),
    "VNX_PROVENANCE_ENFORCEMENT": ("bool", "0", "project", None),
    "VNX_PROVENANCE_LEGACY_ACCEPTED": ("bool", "1", "project", None),
    "VNX_SLUG_ENFORCEMENT": ("bool", "0", "project", None),
    "VNX_QUEUE_POPUP_ENABLED": ("bool", "1", "project", None),
    "VNX_T1_PROVIDER": ("string", "", "project", None),
    "VNX_T2_PROVIDER": ("string", "", "project", None),
    "VNX_T3_PROVIDER": ("string", "", "project", None),
    "VNX_ENFORCEMENT_PROFILE": ("string", "shadow", "project", ("shadow", "enforced")),
    "VNX_ACCOUNT_DIRS": ("string", "[]", "global", None),
}


@pytest.fixture
def conn(tmp_path):
    with closing(sqlite3.connect(tmp_path / "runtime_coordination.db")) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for k in list(ROWS) + [f"VNX_OVERRIDE_{k[4:]}" for k in ROWS]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "no-global.json"))
    cr.set_db_resolver(None)


@pytest.mark.parametrize("key", sorted(ROWS))
def test_row_has_the_declared_shape(key):
    type_, default, scope, allowed = ROWS[key]
    entry = cr.CONFIG_REGISTRY[key]
    assert (entry.type, entry.default, entry.scope, entry.allowed_values) == (type_, default, scope, allowed)
    assert entry.read_site_wired is False and entry.writable_from_ui is True
    if allowed:
        assert entry.default in allowed


def test_exactly_the_six_v21_keys_plus_account_dirs_are_global():
    expected = {"VNX_OLLAMA_HOST", "VNX_OLLAMA_MODEL", "VNX_ANALYZER_LLM", "VNX_ANALYZER_RESTRICTED_CLAUDE_CAP",
                "VNX_DIGEST_EMAIL", "VNX_SMTP_HOST", "VNX_ACCOUNT_DIRS"}
    assert {k for k, e in cr.CONFIG_REGISTRY.items() if e.scope == "global"} == expected


def test_approval_flags():
    assert {k for k in ROWS if cr.CONFIG_REGISTRY[k].requires_approval} == {
        "VNX_ENFORCEMENT_PROFILE", "VNX_ACCOUNT_DIRS"}


def test_global_key_refuses_a_project_row(conn):
    with pytest.raises(PermissionError, match="VNX_ANALYZER_LLM is a global key"):
        cs.set_config(conn, "vnx-dev", "VNX_ANALYZER_LLM", "auto", actor="op")


def test_enforcement_profile_refuses_unknown_value_and_needs_approval(conn):
    with pytest.raises(ValueError, match="strict"):
        cs.set_config(conn, "vnx-dev", "VNX_ENFORCEMENT_PROFILE", "strict", actor="op", approval_id="ap-1")
    with pytest.raises(PermissionError, match="approval_id"):
        cs.set_config(conn, "vnx-dev", "VNX_ENFORCEMENT_PROFILE", "enforced", actor="op")
    assert cs.read_config(conn, "vnx-dev", "VNX_ENFORCEMENT_PROFILE") is None
    cs.set_config(conn, "vnx-dev", "VNX_ENFORCEMENT_PROFILE", "enforced", actor="op", approval_id="ap-1")
    assert cs.read_config(conn, "vnx-dev", "VNX_ENFORCEMENT_PROFILE") == "enforced"


@pytest.mark.parametrize("n", (1, 2, 3))
def test_terminal_provider_default_is_empty_and_comes_from_default(n):
    value, source = cr.resolve_with_source(f"VNX_T{n}_PROVIDER", "vnx-dev", use_default_resolver=False)
    assert (value, source) == ("", "default")
