"""Tests for the model-agnostic VNX LLM tagger (build-step 3b).

No real LLM calls — the classifier provider is mocked.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

import vnx_tagger  # noqa: E402
from classifier_providers.base import ClassifierResult, get_provider  # noqa: E402


class _FakeProvider:
    name = "fake"

    def __init__(self, parsed=None, error=None, available=True):
        self._parsed = parsed
        self._error = error
        self._available = available

    def is_available(self):
        return self._available

    def classify(self, prompt, _max_tokens=1500):
        return ClassifierResult(
            raw_response="", parsed_json=self._parsed, cost_usd=0.0,
            latency_ms=1, provider=self.name, error=self._error,
        )


def test_default_disabled(monkeypatch):
    monkeypatch.delenv("VNX_TAGGER_ENABLED", raising=False)
    assert vnx_tagger.is_enabled() is False
    assert vnx_tagger.llm_tags("fix a dispatch bug", ["x.py"]) == []


def test_provider_name_default_and_override(monkeypatch):
    monkeypatch.delenv("VNX_TAGGER_PROVIDER", raising=False)
    assert vnx_tagger.get_tagger_provider_name() == "deepseek"
    monkeypatch.setenv("VNX_TAGGER_PROVIDER", "ollama")
    assert vnx_tagger.get_tagger_provider_name() == "ollama"


def test_llm_tags_validated_against_vocab(monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    fake = _FakeProvider(parsed={"tags": ["dispatch", "bogus_tag", "fix_bug"]})
    monkeypatch.setattr("classifier_providers.get_provider", lambda name=None: fake)
    tags = vnx_tagger.llm_tags("fix the dispatch lane", ["scripts/lib/dispatch_cli.py"])
    assert "dispatch" in tags and "fix_bug" in tags
    assert "bogus_tag" not in tags  # off-vocab snapped out


def test_llm_tags_fail_silent_on_error(monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    monkeypatch.setattr("classifier_providers.get_provider",
                        lambda name=None: _FakeProvider(error="boom"))
    assert vnx_tagger.llm_tags("x", ["y.py"]) == []


def test_llm_tags_empty_when_provider_unavailable(monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    monkeypatch.setattr("classifier_providers.get_provider",
                        lambda name=None: _FakeProvider(available=False))
    assert vnx_tagger.llm_tags("x", ["y.py"]) == []


def test_enrich_combines_deterministic_and_llm(monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    # LLM adds 'harden'; deterministic floor already finds dispatch/fix_bug.
    fake = _FakeProvider(parsed={"tags": ["harden"]})
    monkeypatch.setattr("classifier_providers.get_provider", lambda name=None: fake)
    tags = vnx_tagger.enrich_tags("fix a dispatch bug", ["scripts/lib/dispatch_cli.py"])
    assert "dispatch" in tags  # deterministic
    assert "harden" in tags    # llm-added
    assert tags.count("dispatch") == 1  # deduped


def test_enrich_is_deterministic_floor_when_disabled(monkeypatch):
    monkeypatch.delenv("VNX_TAGGER_ENABLED", raising=False)
    from vnx_tag_vocabulary import derive_tags
    text, paths = "migrate the schema project_id", ["schemas/x.sql"]
    assert vnx_tagger.enrich_tags(text, paths) == derive_tags(text, paths)


# --- DeepSeek provider registration + safety -------------------------------

def test_deepseek_provider_registered():
    from classifier_providers.deepseek_provider import DeepSeekProvider
    prov = get_provider("deepseek")
    assert isinstance(prov, DeepSeekProvider)
    assert prov.name == "deepseek"


def test_deepseek_requires_own_key(monkeypatch):
    from classifier_providers.deepseek_provider import DeepSeekProvider
    prov = DeepSeekProvider()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # Without the own key it is unavailable (never rides the OAuth subscription).
    assert prov.is_available() is False


def test_deepseek_harness_env_points_at_deepseek(monkeypatch):
    from classifier_providers.deepseek_provider import DeepSeekProvider
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env = DeepSeekProvider()._harness_env()
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
