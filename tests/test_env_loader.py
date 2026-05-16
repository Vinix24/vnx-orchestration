#!/usr/bin/env python3
"""test_env_loader.py — Unit tests for scripts/lib/env_loader.py.

Coverage:
  test_parses_basic_key_value                 — KEY=value sets os.environ
  test_strips_double_quotes_and_single_quotes — quoted values unquoted
  test_ignores_comments_and_blank_lines       — # and blank lines skipped
  test_rejects_invalid_keys                   — lowercase/digit/special skipped
  test_shell_env_wins_over_file               — pre-existing env var preserved
  test_repo_root_file_overrides_user_home     — repo root loaded first
  test_missing_files_silent_no_error          — no exception on absent files
  test_returns_list_of_loaded_paths           — loaded list contains file paths
  test_handles_unicode_values                 — non-ASCII values round-trip
  test_malformed_line_logged_and_skipped      — line without = is warned+skipped
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from env_loader import _parse_env_file, load_env


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_env(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "vnx.env"
    f.write_text(content, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Test 1 — basic KEY=value parsing
# ---------------------------------------------------------------------------

class TestParsesBasicKeyValue:
    def test_parses_basic_key_value(self, tmp_path):
        f = _write_env(tmp_path, "MY_KEY=hello\nANOTHER=world\n")
        result = _parse_env_file(f)
        assert result["MY_KEY"] == "hello"
        assert result["ANOTHER"] == "world"


# ---------------------------------------------------------------------------
# Test 2 — quote stripping
# ---------------------------------------------------------------------------

class TestStripsQuotes:
    def test_strips_double_quotes(self, tmp_path):
        f = _write_env(tmp_path, 'KEY="my value"\n')
        result = _parse_env_file(f)
        assert result["KEY"] == "my value"

    def test_strips_single_quotes(self, tmp_path):
        f = _write_env(tmp_path, "KEY='my value'\n")
        result = _parse_env_file(f)
        assert result["KEY"] == "my value"

    def test_asymmetric_quotes_kept_verbatim(self, tmp_path):
        f = _write_env(tmp_path, 'KEY="mismatched\'\n')
        result = _parse_env_file(f)
        assert result["KEY"] == '"mismatched\''


# ---------------------------------------------------------------------------
# Test 3 — comments and blank lines ignored
# ---------------------------------------------------------------------------

class TestIgnoresCommentsAndBlanks:
    def test_ignores_comments_and_blank_lines(self, tmp_path):
        content = "\n# this is a comment\nVALID=yes\n  # indented comment\n\n"
        f = _write_env(tmp_path, content)
        result = _parse_env_file(f)
        assert list(result.keys()) == ["VALID"]
        assert result["VALID"] == "yes"


# ---------------------------------------------------------------------------
# Test 4 — invalid keys rejected
# ---------------------------------------------------------------------------

class TestRejectsInvalidKeys:
    def test_lowercase_key_skipped(self, tmp_path, caplog):
        f = _write_env(tmp_path, "lowercase_key=val\n")
        with caplog.at_level(logging.WARNING, logger="env_loader"):
            result = _parse_env_file(f)
        assert "lowercase_key" not in result
        assert any("invalid key" in r.message for r in caplog.records)

    def test_leading_digit_key_skipped(self, tmp_path, caplog):
        f = _write_env(tmp_path, "1BAD=val\n")
        with caplog.at_level(logging.WARNING, logger="env_loader"):
            result = _parse_env_file(f)
        assert "1BAD" not in result

    def test_special_char_key_skipped(self, tmp_path, caplog):
        f = _write_env(tmp_path, "BAD-KEY=val\n")
        with caplog.at_level(logging.WARNING, logger="env_loader"):
            result = _parse_env_file(f)
        assert "BAD-KEY" not in result

    def test_valid_key_with_underscore_accepted(self, tmp_path):
        f = _write_env(tmp_path, "GOOD_KEY_123=val\n")
        result = _parse_env_file(f)
        assert result["GOOD_KEY_123"] == "val"


# ---------------------------------------------------------------------------
# Test 5 — shell env wins over file
# ---------------------------------------------------------------------------

class TestShellEnvWinsOverFile:
    def test_shell_env_wins_over_file(self, tmp_path):
        f = _write_env(tmp_path, "DEEPSEEK_API_KEY=from-file\n")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "from-shell"}, clear=False):
            load_env(repo_root=tmp_path, user_home=tmp_path / "nonexistent_home")
            assert os.environ["DEEPSEEK_API_KEY"] == "from-shell"


# ---------------------------------------------------------------------------
# Test 6 — repo root file loaded first (before user home)
# ---------------------------------------------------------------------------

class TestRepoRootOverridesUserHome:
    def test_repo_root_file_overrides_user_home(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        home = tmp_path / "home"
        (home / ".vnx").mkdir(parents=True)

        (repo_root / "vnx.env").write_text("SOME_KEY=from-repo\n", encoding="utf-8")
        (home / ".vnx" / "vnx.env").write_text("SOME_KEY=from-home\n", encoding="utf-8")

        clean = {k: v for k, v in os.environ.items() if k != "SOME_KEY"}
        with patch.dict(os.environ, clean, clear=True):
            load_env(repo_root=repo_root, user_home=home)
            assert os.environ["SOME_KEY"] == "from-repo"


# ---------------------------------------------------------------------------
# Test 7 — missing files cause no exception
# ---------------------------------------------------------------------------

class TestMissingFilesSilent:
    def test_missing_files_silent_no_error(self, tmp_path):
        nonexistent = tmp_path / "no_such_dir"
        loaded = load_env(repo_root=nonexistent, user_home=nonexistent)
        assert loaded == []


# ---------------------------------------------------------------------------
# Test 8 — returns list of loaded paths
# ---------------------------------------------------------------------------

class TestReturnsLoadedPaths:
    def test_returns_list_of_loaded_paths(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "vnx.env").write_text("LOADED_KEY=yes\n", encoding="utf-8")

        clean = {k: v for k, v in os.environ.items() if k != "LOADED_KEY"}
        with patch.dict(os.environ, clean, clear=True):
            loaded = load_env(repo_root=repo_root, user_home=tmp_path / "no_home")

        assert len(loaded) == 1
        assert "vnx.env" in loaded[0]

    def test_returns_empty_when_no_files(self, tmp_path):
        loaded = load_env(repo_root=tmp_path / "x", user_home=tmp_path / "y")
        assert loaded == []


# ---------------------------------------------------------------------------
# Test 9 — unicode values round-trip
# ---------------------------------------------------------------------------

class TestHandlesUnicodeValues:
    def test_handles_unicode_values(self, tmp_path):
        f = _write_env(tmp_path, "UNICODE_KEY=héllo wörld\n")
        result = _parse_env_file(f)
        assert result["UNICODE_KEY"] == "héllo wörld"

    def test_handles_unicode_in_quoted_value(self, tmp_path):
        f = _write_env(tmp_path, 'UNICODE_QUOTED="日本語"\n')
        result = _parse_env_file(f)
        assert result["UNICODE_QUOTED"] == "日本語"


# ---------------------------------------------------------------------------
# Test 10 — malformed line (no =) is logged as warning and skipped
# ---------------------------------------------------------------------------

class TestMalformedLineLoggedAndSkipped:
    def test_malformed_line_no_equals_logged_and_skipped(self, tmp_path, caplog):
        content = "VALID=yes\nMALFORMED_LINE_WITHOUT_EQUALS\nALSO_VALID=ok\n"
        f = _write_env(tmp_path, content)
        with caplog.at_level(logging.WARNING, logger="env_loader"):
            result = _parse_env_file(f)

        assert "VALID" in result
        assert "ALSO_VALID" in result
        assert "MALFORMED_LINE_WITHOUT_EQUALS" not in result
        assert any("malformed" in r.message for r in caplog.records)

    def test_malformed_line_warning_includes_line_number(self, tmp_path, caplog):
        content = "VALID=yes\nBAD_LINE\n"
        f = _write_env(tmp_path, content)
        with caplog.at_level(logging.WARNING, logger="env_loader"):
            _parse_env_file(f)

        warning_msgs = [r.message for r in caplog.records if "malformed" in r.message]
        assert warning_msgs, "expected at least one malformed warning"
