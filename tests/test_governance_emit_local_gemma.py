"""test_governance_emit_local_gemma.py — Fix 2: _PROVIDER_RE accepts local-gemma.

Verifies that governance_emit does not raise ValueError when provider='local-gemma'
is passed to _validate_provider and emit_dispatch_receipt.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from governance_emit import _validate_provider, emit_dispatch_receipt


@pytest.fixture()
def tmp_state(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


def _base_receipt_kwargs(state_dir):
    return dict(
        dispatch_id="test-local-gemma-001",
        terminal_id="T1",
        provider="local-gemma",
        model="mlx-community/gemma-3-4b-it-4bit",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=3.2,
        token_usage={"input": 50, "output": 20},
        cost_usd=0.0,
        state_dir=state_dir,
    )


class TestValidateProviderLocalGemma:
    def test_local_gemma_is_accepted(self):
        _validate_provider("local-gemma")

    def test_local_gemma_does_not_raise(self):
        try:
            _validate_provider("local-gemma")
        except ValueError as exc:
            pytest.fail(f"_validate_provider raised unexpectedly: {exc}")

    def test_unknown_provider_still_raises(self):
        with pytest.raises(ValueError, match="Invalid provider"):
            _validate_provider("unknown-provider")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Invalid provider"):
            _validate_provider("")


class TestEmitReceiptLocalGemma:
    def test_emit_receipt_with_local_gemma_provider(self, tmp_state):
        path = emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
        assert path.exists()
        import json
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        assert records
        last = records[-1]
        assert last["provider"] == "local-gemma"
        assert last["status"] == "success"
        assert last["cost_usd"] == 0.0

    def test_emit_receipt_local_gemma_does_not_raise(self, tmp_state):
        try:
            emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
        except ValueError as exc:
            pytest.fail(f"emit_dispatch_receipt raised on local-gemma provider: {exc}")
