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
