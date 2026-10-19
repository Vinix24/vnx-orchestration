"""Tests for provider_registry exception narrowing (codex R1 fix).

Verifies that get_default_model() fails-fast on missing or malformed registry
files instead of silently returning None.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from providers import provider_registry


class TestProviderRegistryRaisesOnMissingFile:

    def test_raises_file_not_found(self, tmp_path):
        """get_default_model raises FileNotFoundError when registry file is absent."""
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError):
            provider_registry.get_default_model("deepseek", registry_path=missing)

    def test_does_not_return_none_on_missing_file(self, tmp_path):
        """get_default_model must not silently swallow FileNotFoundError."""
        missing = tmp_path / "nonexistent.yaml"
        try:
            result = provider_registry.get_default_model("deepseek", registry_path=missing)
            pytest.fail(f"Expected FileNotFoundError, got {result!r}")
        except FileNotFoundError:
            pass  # expected


class TestProviderRegistryRaisesOnMalformedYaml:

    def test_provider_registry_raises_on_malformed_yaml(self, tmp_path):
        """get_default_model raises ValueError wrapping YAMLError on malformed content."""
        malformed = tmp_path / "bad.yaml"
        malformed.write_text("providers:\n  bad: :::invalid:::\n", encoding="utf-8")
        with pytest.raises(ValueError, match="malformed wave7_models.yaml"):
            provider_registry.get_default_model("deepseek", registry_path=malformed)

    def test_malformed_yaml_does_not_return_none(self, tmp_path):
        """get_default_model must not silently swallow yaml parse errors."""
        malformed = tmp_path / "bad.yaml"
        malformed.write_text("providers:\n  bad: :::invalid:::\n", encoding="utf-8")
        try:
            result = provider_registry.get_default_model("deepseek", registry_path=malformed)
            pytest.fail(f"Expected ValueError, got {result!r}")
        except ValueError:
            pass  # expected


class TestProviderRegistryHappyPath:

    def test_returns_none_for_unknown_provider(self, tmp_path):
        """get_default_model returns None when provider is not in registry (no exception)."""
        registry_yaml = tmp_path / "models.yaml"
        registry_yaml.write_text(
            "providers:\n  deepseek:\n    enabled: false\n    api_key_env: DEEPSEEK_API_KEY\n    models: {}\n",
            encoding="utf-8",
        )
        result = provider_registry.get_default_model("nonexistent", registry_path=registry_yaml)
        assert result is None


class TestProviderRegistryRejectsNonFiniteRates:
    """A price_version rate must be finite and nonnegative before it becomes a price."""

    def _registry(self, tmp_path, input_rate: str) -> Path:
        registry_yaml = tmp_path / "models.yaml"
        registry_yaml.write_text(
            "providers:\n"
            "  acme:\n"
            "    enabled: true\n"
            "    api_key_env: \"\"\n"
            "    models:\n"
            "      acme-flagship:\n"
            "        litellm_name: acme-flagship\n"
            "        cost_input_per_mtok: 1.0\n"
            "        cost_output_per_mtok: 2.0\n"
            "        max_tokens: 4096\n"
            "        supports_streaming: false\n"
            "        supports_tool_calls: false\n"
            "        price_checked_at: '2026-08-18'\n"
            "        price_versions:\n"
            "          - date: '2026-08-18'\n"
            "            source: 'unverified: registry-authored'\n"
            f"            input_per_mtok: {input_rate}\n"
            "            output_per_mtok: 2.0\n",
            encoding="utf-8",
        )
        return registry_yaml

    @pytest.mark.parametrize("bad_rate", [".nan", ".inf", "-.inf", "-1.0"])
    def test_nonfinite_or_negative_rate_is_refused(self, tmp_path, bad_rate):
        path = self._registry(tmp_path, bad_rate)
        with pytest.raises(ValueError) as excinfo:
            provider_registry.load(registry_path=path)
        message = str(excinfo.value)
        assert "acme-flagship" in message, f"must name the model — got: {message!r}"
        assert "input_per_mtok" in message, f"must name the field — got: {message!r}"
        assert "finite" in message, f"must state the finite-nonnegative expectation — got: {message!r}"
