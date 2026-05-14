"""Regression tests: silent-except hardening in append_receipt_internals/session_resolver.py (OI-1437).

Covers the 4 findings narrowed in chore/cleanup-session-resolver-silent-except:
1. _rsi_check_env_session: OSError on mkdir/write_text — logs debug, does not raise
2. _rsi_check_provider_files: OSError on read_text — logs debug, continues iteration
3. _resolve_session_id: OSError on current_session file read — logs debug, falls through
4. _resolve_model_provider: OSError/JSONDecodeError on panes.json — logs debug, uses heuristic
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import append_receipt_internals.session_resolver as sr
from append_receipt_internals.common import register_facade, _facade_modules

_LOGGER = "append_receipt_internals.session_resolver"


@pytest.fixture(autouse=True)
def register_sr_with_facade():
    """Register session_resolver module with facade proxy for all tests."""
    register_facade(sr)
    yield
    if sr in _facade_modules:
        _facade_modules.remove(sr)


def _make_receipt(terminal: str = "T1", session_id: str | None = None) -> dict:
    receipt: dict = {"terminal": terminal}
    if session_id:
        receipt["session_id"] = session_id
    return receipt


# ---------------------------------------------------------------------------
# 1. Clean default env — module importable, resolver returns without raising
# ---------------------------------------------------------------------------

class TestRunsCleanOnDefaultEnv:
    def test_resolve_session_id_returns_string(self, tmp_path):
        """_resolve_session_id returns a string in a clean env without raising."""
        result = sr._resolve_session_id(_make_receipt(), state_dir=tmp_path)
        assert isinstance(result, str)

    def test_explicit_session_id_returned_directly(self, tmp_path):
        """session_id in receipt is returned without touching the filesystem."""
        receipt = _make_receipt(session_id="explicit-session-abc")
        result = sr._resolve_session_id(receipt, state_dir=tmp_path)
        assert result == "explicit-session-abc"

    def test_resolve_model_provider_returns_dict(self, tmp_path):
        """_resolve_model_provider returns a dict with model and provider keys."""
        result = sr._resolve_model_provider("T1", tmp_path)
        assert isinstance(result, dict)
        assert "model" in result
        assert "provider" in result

    def test_known_terminal_heuristic_provider(self, tmp_path):
        """Standard terminal names resolve to claude_code provider via heuristic."""
        for terminal in ("T0", "T1", "T2", "T3", "T-MANAGER"):
            result = sr._resolve_model_provider(terminal, tmp_path)
            assert result["provider"] == "claude_code", f"{terminal} should be claude_code"

    def test_gemini_terminal_resolves_provider(self, tmp_path):
        """Gemini terminal name resolves to gemini_cli provider via heuristic."""
        result = sr._resolve_model_provider("GEMINI-T2", tmp_path)
        assert result["provider"] == "gemini_cli"


# ---------------------------------------------------------------------------
# 2. Corrupt session data — errors logged at DEBUG, not raised
# ---------------------------------------------------------------------------

class TestCorruptSessionDataLogsWarning:
    def test_corrupt_panes_json_logs_debug(self, tmp_path, caplog):
        """Finding 4: corrupt panes.json logs debug, fallback heuristic still runs."""
        panes_json = tmp_path / "panes.json"
        panes_json.write_text("NOT VALID JSON {{{", encoding="utf-8")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            result = sr._resolve_model_provider("T1", tmp_path)

        assert isinstance(result, dict)
        assert result["provider"] == "claude_code"
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("panes.json" in m for m in debug_msgs), (
            f"Expected panes.json debug message; got: {debug_msgs}"
        )

    def test_oserror_reading_panes_json_logs_debug(self, tmp_path, caplog):
        """Finding 4: OSError reading panes.json is logged at DEBUG."""
        panes_json = tmp_path / "panes.json"
        panes_json.write_text("{}", encoding="utf-8")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            with patch(
                "append_receipt_internals.session_resolver.Path.read_text",
                side_effect=OSError("simulated disk error"),
            ):
                result = sr._resolve_model_provider("T1", tmp_path)

        assert isinstance(result, dict)
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("panes.json" in m for m in debug_msgs), (
            f"Expected panes.json debug message; got: {debug_msgs}"
        )

    def test_oserror_on_current_session_file_logs_debug(self, tmp_path, caplog, monkeypatch):
        """Finding 3: OSError reading current_session file is logged at DEBUG."""
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("GEMINI_SESSION_ID", raising=False)
        monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
        monkeypatch.delenv("KIMI_SESSION_ID", raising=False)

        terminal = "T1"
        session_file = tmp_path / f"current_session_{terminal}"
        session_file.write_text("some-value", encoding="utf-8")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            with patch(
                "append_receipt_internals.session_resolver.Path.read_text",
                side_effect=OSError("simulated read failure"),
            ):
                result = sr._resolve_session_id(_make_receipt(terminal), state_dir=tmp_path)

        assert isinstance(result, str)
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("session" in m.lower() for m in debug_msgs), (
            f"Expected session-related debug message; got: {debug_msgs}"
        )

    def test_persist_session_oserror_logs_debug(self, tmp_path, caplog, monkeypatch):
        """Finding 1: OSError when persisting env session id is logged at DEBUG."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session-xyz")
        session_file = tmp_path / "current_session_T1"

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            with patch(
                "append_receipt_internals.session_resolver.Path.write_text",
                side_effect=OSError("read-only filesystem"),
            ):
                result = sr._rsi_check_env_session("T1", tmp_path, session_file)

        assert result == "env-session-xyz"
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("persist" in m.lower() or "session" in m.lower() for m in debug_msgs), (
            f"Expected persist/session debug message; got: {debug_msgs}"
        )
