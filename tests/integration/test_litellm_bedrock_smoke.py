"""Integration smoke test: LiteLLMAdapter -> Bedrock -> real completion.

Marked @pytest.mark.requires_aws — skipped automatically when AWS credentials
are absent (no AWS_ACCESS_KEY_ID or AWS_PROFILE in environment).

To run manually:
  AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=us-east-1 \
    pytest tests/integration/test_litellm_bedrock_smoke.py -v
"""
from __future__ import annotations

import os

import pytest

from adapters.litellm_adapter import LiteLLMAdapter
from canonical_event import CanonicalEvent


def _has_aws_creds() -> bool:
    return bool(
        os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("AWS_PROFILE")
        or os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    )


pytestmark = pytest.mark.requires_aws


@pytest.fixture(autouse=True)
def skip_without_aws():
    if not _has_aws_creds():
        pytest.skip("AWS credentials not present — set AWS_ACCESS_KEY_ID or AWS_PROFILE")


class TestLiteLLMBedrockSmoke:
    """Live tests that spawn a real litellm subprocess and call Bedrock."""

    def test_bedrock_claude_emits_events(self):
        adapter = LiteLLMAdapter("T2", litellm_model="bedrock/claude-sonnet-4-6")
        result = adapter.execute(
            "Reply with exactly: SMOKE_OK",
            {"dispatch_id": "bedrock-smoke-001", "terminal_id": "T2"},
        )
        assert result.status == "done", f"Unexpected status: {result.status!r} output={result.output!r}"
        assert result.event_count > 0, "No events emitted"
        assert "SMOKE_OK" in result.output

    def test_bedrock_events_are_canonical(self):
        adapter = LiteLLMAdapter("T2", litellm_model="bedrock/claude-sonnet-4-6")
        result = adapter.execute(
            "Reply with exactly: CANONICAL_OK",
            {"dispatch_id": "bedrock-canonical-001", "terminal_id": "T2"},
        )
        for event_dict in result.events:
            # Each stored event must round-trip through CanonicalEvent.from_dict
            event = CanonicalEvent.from_dict(event_dict)
            assert event.provider == "litellm"
            assert event.observability_tier == 1

    def test_bedrock_event_types_include_text_and_complete(self):
        adapter = LiteLLMAdapter("T2", litellm_model="bedrock/claude-sonnet-4-6")
        result = adapter.execute(
            "Reply with exactly: TYPES_OK",
            {"dispatch_id": "bedrock-types-001", "terminal_id": "T2"},
        )
        types = {e["event_type"] for e in result.events}
        assert "text" in types, f"Expected text event, got: {types}"
        assert "complete" in types, f"Expected complete event, got: {types}"

    def test_bedrock_missing_creds_returns_error(self, monkeypatch):
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKEKEYID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKESECRET")

        adapter = LiteLLMAdapter("T2", litellm_model="bedrock/claude-sonnet-4-6")
        result = adapter.execute(
            "Hello",
            {"dispatch_id": "bedrock-creds-fail-001", "terminal_id": "T2"},
        )
        assert result.status == "failed"
        error_events = [e for e in result.events if e["event_type"] == "error"]
        assert error_events, "Expected at least one error event for bad creds"
