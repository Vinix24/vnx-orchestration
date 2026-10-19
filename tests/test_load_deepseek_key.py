"""test_load_deepseek_key.py — the Python provider-env-file key resolver (OI-2182).

The helper mirrors ``scripts/lib/load_deepseek_key.sh``: parse (never source),
only ``DEEPSEEK_API_KEY``, never print or log the value.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from load_deepseek_key import (  # (path-setup above: scripts/lib injected into sys.path)
    load_deepseek_api_key_from_file,
    resolve_deepseek_api_key,
)

_SECRET = "sk-test-0123456789abcdef"


def _write(tmp_path, text):
    env_file = tmp_path / "provider-usage.env"
    env_file.write_text(text)
    return env_file


class TestLoadDeepSeekKeyFromFile:
    def test_reads_only_the_deepseek_key(self, tmp_path):
        env_file = _write(
            tmp_path,
            "# provider keys\n"
            "OPENROUTER_API_KEY=or-should-not-load\n"
            "export MOONSHOT_API_KEY=ms-should-not-load\n"
            f'export DEEPSEEK_API_KEY="{_SECRET}"\n',
        )
        assert load_deepseek_api_key_from_file(env_file) == _SECRET

    def test_unquoted_value(self, tmp_path):
        env_file = _write(tmp_path, f"DEEPSEEK_API_KEY={_SECRET}\n")
        assert load_deepseek_api_key_from_file(env_file) == _SECRET

    def test_last_matching_line_wins(self, tmp_path):
        env_file = _write(
            tmp_path,
            f"DEEPSEEK_API_KEY=sk-old\nDEEPSEEK_API_KEY={_SECRET}\n",
        )
        assert load_deepseek_api_key_from_file(env_file) == _SECRET

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_deepseek_api_key_from_file(tmp_path / "absent.env") == ""

    def test_missing_key_returns_empty(self, tmp_path):
        env_file = _write(tmp_path, "OPENROUTER_API_KEY=x\n")
        assert load_deepseek_api_key_from_file(env_file) == ""

    def test_empty_value_returns_empty(self, tmp_path):
        env_file = _write(tmp_path, "DEEPSEEK_API_KEY=\n")
        assert load_deepseek_api_key_from_file(env_file) == ""


class TestResolveDeepSeekApiKey:
    def test_env_wins_over_file(self, tmp_path, monkeypatch):
        env_file = _write(tmp_path, f"DEEPSEEK_API_KEY=sk-from-file\n")
        monkeypatch.setenv("DEEPSEEK_API_KEY", _SECRET)
        monkeypatch.setenv("VNX_PROVIDER_ENV_FILE", str(env_file))
        assert resolve_deepseek_api_key() == _SECRET

    def test_file_used_when_env_unset(self, tmp_path, monkeypatch):
        env_file = _write(tmp_path, f'DEEPSEEK_API_KEY="{_SECRET}"\n')
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("VNX_PROVIDER_ENV_FILE", str(env_file))
        assert resolve_deepseek_api_key() == _SECRET

    def test_neither_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("VNX_PROVIDER_ENV_FILE", str(tmp_path / "absent.env"))
        assert resolve_deepseek_api_key() == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
