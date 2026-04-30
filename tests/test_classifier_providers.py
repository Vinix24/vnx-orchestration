"""Tests for classifier provider abstraction (ARC-3)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))

from classifier_providers import get_provider  # noqa: E402
from classifier_providers.base import (  # noqa: E402
    ClassifierProvider,
    ClassifierResult,
    parse_json_block,
)
from classifier_providers.haiku_provider import HaikuProvider  # noqa: E402
from classifier_providers.ollama_provider import OllamaProvider  # noqa: E402


def _make_completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["dummy"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ----------------------------------------------------------------------
# parse_json_block
# ----------------------------------------------------------------------


def test_parse_json_block_pure_json():
    obj = parse_json_block('{"a": 1}')
    assert obj == {"a": 1}


def test_parse_json_block_fenced_code_block():
    text = 'noise\n```json\n{"x": "y"}\n```\nmore noise'
    assert parse_json_block(text) == {"x": "y"}


def test_parse_json_block_embedded_json():
    text = "Here is the result: {\"impact_class\": \"trivial\"} -- end"
    assert parse_json_block(text) == {"impact_class": "trivial"}


def test_parse_json_block_returns_none_when_no_json():
    assert parse_json_block("no json here at all") is None


def test_parse_json_block_handles_empty():
    assert parse_json_block("") is None


# ----------------------------------------------------------------------
# get_provider
# ----------------------------------------------------------------------


def test_get_provider_returns_haiku_by_default():
    prov = get_provider(None)
    assert isinstance(prov, HaikuProvider)
    assert prov.name == "haiku"


def test_get_provider_returns_ollama():
    prov = get_provider("ollama")
    assert isinstance(prov, OllamaProvider)
    assert prov.name == "ollama"


def test_get_provider_unknown_raises():
    with pytest.raises(ValueError):
        get_provider("definitely-not-a-provider")


def test_get_provider_is_case_insensitive():
    prov = get_provider("HAIKU")
    assert isinstance(prov, HaikuProvider)


# ----------------------------------------------------------------------
# Haiku provider
# ----------------------------------------------------------------------


def test_haiku_provider_parses_output():
    prov = HaikuProvider(flat_cost_usd=0.005)
    payload = '{"domain":"governance","outcome_class":"success","impact_class":"trivial","suggested_edit":null}'
    with patch("classifier_providers.haiku_provider.subprocess.run", return_value=_make_completed(payload)):
        result = prov.classify("test prompt")
    assert isinstance(result, ClassifierResult)
    assert result.error is None
    assert result.parsed_json is not None
    assert result.parsed_json["domain"] == "governance"
    assert result.cost_usd == 0.005
    assert result.provider == "haiku"


def test_haiku_provider_handles_nonzero_exit():
    prov = HaikuProvider()
    with patch(
        "classifier_providers.haiku_provider.subprocess.run",
        return_value=_make_completed("", returncode=2, stderr="boom"),
    ):
        result = prov.classify("test")
    assert result.error is not None
    assert "exit 2" in result.error
    assert result.parsed_json is None
    assert result.cost_usd == 0.0


def test_haiku_provider_handles_missing_cli():
    prov = HaikuProvider()
    with patch(
        "classifier_providers.haiku_provider.subprocess.run",
        side_effect=FileNotFoundError("claude"),
    ):
        result = prov.classify("test")
    assert result.error is not None
    assert "not found" in result.error
    assert result.cost_usd == 0.0


def test_haiku_provider_handles_timeout():
    prov = HaikuProvider(timeout_seconds=1)
    err = subprocess.TimeoutExpired(cmd="claude", timeout=1)
    with patch("classifier_providers.haiku_provider.subprocess.run", side_effect=err):
        result = prov.classify("test")
    assert result.error is not None
    assert "timeout" in result.error
    assert result.cost_usd == 0.0


def test_haiku_provider_uses_correct_command():
    prov = HaikuProvider(model="claude-haiku-4-5")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _make_completed('{"ok": true}')

    with patch("classifier_providers.haiku_provider.subprocess.run", side_effect=fake_run):
        prov.classify("test prompt")
    assert captured["cmd"] == ["claude", "--print", "--model", "claude-haiku-4-5"]
    assert captured["kwargs"]["input"] == "test prompt"


# ----------------------------------------------------------------------
# Ollama provider
# ----------------------------------------------------------------------


def test_ollama_provider_parses_output():
    prov = OllamaProvider(model="llama3.1:8b")
    with patch(
        "classifier_providers.ollama_provider.subprocess.run",
        return_value=_make_completed('{"x": 1}'),
    ):
        result = prov.classify("test")
    assert result.error is None
    assert result.parsed_json == {"x": 1}
    assert result.cost_usd == 0.0  # local — always free
    assert result.provider == "ollama"


def test_ollama_provider_uses_correct_command():
    prov = OllamaProvider(model="llama3.1:8b")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _make_completed('{}')

    with patch("classifier_providers.ollama_provider.subprocess.run", side_effect=fake_run):
        prov.classify("hi")
    assert captured["cmd"] == ["ollama", "run", "llama3.1:8b"]
    assert captured["kwargs"]["input"] == "hi"


def test_ollama_provider_handles_missing_cli():
    prov = OllamaProvider()
    with patch(
        "classifier_providers.ollama_provider.subprocess.run",
        side_effect=FileNotFoundError("ollama"),
    ):
        result = prov.classify("test")
    assert result.error is not None
    assert "not found" in result.error


def test_ollama_provider_handles_nonzero_exit():
    prov = OllamaProvider()
    with patch(
        "classifier_providers.ollama_provider.subprocess.run",
        return_value=_make_completed("", returncode=1, stderr="model missing"),
    ):
        result = prov.classify("test")
    assert result.error is not None
    assert "exit 1" in result.error


# ----------------------------------------------------------------------
# Custom provider plug-in path
# ----------------------------------------------------------------------


class _FixtureProvider(ClassifierProvider):
    name = "fixture"

    def __init__(self, response: str):
        self._response = response

    def classify(self, prompt: str, max_tokens: int = 1500) -> ClassifierResult:
        return ClassifierResult(
            raw_response=self._response,
            parsed_json=parse_json_block(self._response),
            cost_usd=0.0,
            latency_ms=1,
            provider=self.name,
        )


def test_fixture_provider_round_trip():
    prov = _FixtureProvider('{"hello": "world"}')
    result = prov.classify("ignore")
    assert result.parsed_json == {"hello": "world"}
    assert result.error is None
