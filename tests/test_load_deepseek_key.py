"""test_load_deepseek_key.py — the Python provider-env-file key resolver (OI-2182).

The helper mirrors ``scripts/lib/load_deepseek_key.sh``: parse (never source),
only ``DEEPSEEK_API_KEY``, never print or log the value.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from load_deepseek_key import (  # (path-setup above: scripts/lib injected into sys.path)
    load_deepseek_api_key_from_file,
    resolve_deepseek_api_key,
)

_SECRET = "sk-test-0123456789abcdef"
_LOADER_SH = Path(__file__).parent.parent / "scripts" / "lib" / "load_deepseek_key.sh"


def _write(tmp_path, text):
    env_file = tmp_path / "provider-usage.env"
    env_file.write_text(text)
    return env_file


def _write_bytes(tmp_path, data):
    env_file = tmp_path / "provider-usage.env"
    env_file.write_bytes(data)
    return env_file


def _run_shell_loader(tmp_path, env_file):
    """Run the bash loader against an env file, returning the exported value.

    Mirrors scripts/lib/load_deepseek_key.sh exactly (parse, never source) and
    returns "" both when the key is absent/empty and when the loader refuses.
    The value itself is never printed to the test's stdout/stderr beyond the
    captured export below.
    """
    out = subprocess.run(
        [
            "bash", "-c",
            f'source "{_LOADER_SH}"; vnx_load_deepseek_key; printf "%s" "$DEEPSEEK_API_KEY"',
        ],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "VNX_PROVIDER_ENV_FILE": str(env_file),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.stdout


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

    def test_last_assignment_wins_even_when_empty(self, tmp_path):
        """OI-2187: an empty final ``DEEPSEEK_API_KEY=`` must not resurrect an
        earlier key — the last matching assignment wins, empty means no key."""
        env_file = _write(tmp_path, "DEEPSEEK_API_KEY=sk-old\nDEEPSEEK_API_KEY=\n")
        assert load_deepseek_api_key_from_file(env_file) == ""

    def test_non_utf8_env_file_reads_as_missing_key(self, tmp_path):
        """OI-2182: an undecodable provider env file is a missing key, not a
        UnicodeDecodeError out of the resolver."""
        env_file = _write_bytes(tmp_path, b"DEEPSEEK_API_KEY=sk-\xff\xfe\n")
        assert load_deepseek_api_key_from_file(env_file) == ""

    def test_python_loader_agrees_with_shell_loader(self, tmp_path):
        """The Python resolver and scripts/lib/load_deepseek_key.sh must agree
        on the same env-file bytes (OI-2182/OI-2187), including the case where
        the last assignment is empty."""
        cases = {
            "quoted": f'export DEEPSEEK_API_KEY="{_SECRET}"\n',
            "unquoted": f"DEEPSEEK_API_KEY={_SECRET}\n",
            "last-wins-value": f"DEEPSEEK_API_KEY=sk-old\nDEEPSEEK_API_KEY={_SECRET}\n",
            "last-wins-empty": "DEEPSEEK_API_KEY=sk-old\nDEEPSEEK_API_KEY=\n",
            "missing": "OPENROUTER_API_KEY=or-should-not-load\n",
        }
        for name, text in cases.items():
            env_file = _write(tmp_path, text)
            py_value = load_deepseek_api_key_from_file(env_file)
            sh_value = _run_shell_loader(tmp_path, env_file)
            assert py_value == sh_value, (
                f"{name}: python loader returned {py_value!r} but the bash "
                f"loader returned {sh_value!r} for the same bytes"
            )


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
