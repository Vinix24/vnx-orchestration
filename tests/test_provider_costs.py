"""test_provider_costs.py — Unit tests for provider_costs.py.

Tests:
- emit_provider_cost writes correct NDJSON
- emit_provider_cost raises on write failure (no silent except)
- record_id is stable across same inputs
- billing_mode is set correctly
- project_id defaults and overrides
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import provider_costs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _last_event(costs_path: Path) -> dict:
    lines = costs_path.read_text().strip().splitlines()
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# emit_provider_cost — correct NDJSON output
# ---------------------------------------------------------------------------

class TestEmitProviderCost:
    def test_writes_ndjson_with_required_fields(self, tmp_path, monkeypatch):
        costs_path = tmp_path / "events" / "provider_costs.ndjson"
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: costs_path)

        provider_costs.emit_provider_cost(
            provider="codex",
            model="gpt-5.5",
            input_tokens=1000,
            output_tokens=500,
            cost_usd_estimate=0.00075,
            dispatch_id="test-dispatch-001",
            project_id="test-project",
        )

        assert costs_path.exists()
        event = _last_event(costs_path)

        assert event["provider"] == "codex"
        assert event["model"] == "gpt-5.5"
        assert event["input_tokens"] == 1000
        assert event["output_tokens"] == 500
        assert event["cost_usd_estimate"] == 0.00075
        assert event["dispatch_id"] == "test-dispatch-001"
        assert event["project_id"] == "test-project"
        assert "record_id" in event
        assert "timestamp" in event
        assert len(event["record_id"]) == 32

    def test_billing_mode_metered_when_cost_provided(self, tmp_path, monkeypatch):
        costs_path = tmp_path / "events" / "provider_costs.ndjson"
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: costs_path)

        provider_costs.emit_provider_cost(
            provider="gemini",
            model="gemini-2.5-pro",
            input_tokens=2000,
            output_tokens=800,
            cost_usd_estimate=0.001,
            dispatch_id="test-d-002",
        )
        event = _last_event(costs_path)
        assert event["billing_mode"] == "metered"

    def test_billing_mode_subscription_for_kimi(self, tmp_path, monkeypatch):
        costs_path = tmp_path / "events" / "provider_costs.ndjson"
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: costs_path)

        provider_costs.emit_provider_cost(
            provider="kimi",
            model="kimi-k2.6",
            input_tokens=None,
            output_tokens=None,
            cost_usd_estimate=None,
            dispatch_id="test-d-003",
        )
        event = _last_event(costs_path)
        assert event["billing_mode"] == "subscription"
        assert event["cost_usd_estimate"] is None

    def test_metadata_included_when_provided(self, tmp_path, monkeypatch):
        costs_path = tmp_path / "events" / "provider_costs.ndjson"
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: costs_path)

        provider_costs.emit_provider_cost(
            provider="claude",
            model="sonnet",
            input_tokens=500,
            output_tokens=200,
            cost_usd_estimate=0.0045,
            dispatch_id="test-d-004",
            metadata={"gate": "peer_review", "round": 1},
        )
        event = _last_event(costs_path)
        assert event["metadata"]["gate"] == "peer_review"
        assert event["metadata"]["round"] == 1

    def test_appends_multiple_events(self, tmp_path, monkeypatch):
        costs_path = tmp_path / "events" / "provider_costs.ndjson"
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: costs_path)

        for i in range(3):
            provider_costs.emit_provider_cost(
                provider="claude",
                model="sonnet",
                input_tokens=100 * i,
                output_tokens=50 * i,
                cost_usd_estimate=0.001 * i,
                dispatch_id=f"test-d-00{i + 5}",
            )

        lines = costs_path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_project_id_defaults_to_vnx_dev(self, tmp_path, monkeypatch):
        costs_path = tmp_path / "events" / "provider_costs.ndjson"
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: costs_path)
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

        provider_costs.emit_provider_cost(
            provider="claude",
            model="sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd_estimate=0.001,
        )
        event = _last_event(costs_path)
        assert event["project_id"] == "vnx-dev"


# ---------------------------------------------------------------------------
# emit_provider_cost — raises on write failure
# ---------------------------------------------------------------------------

class TestEmitProviderCostFailure:
    def test_raises_on_write_failure(self, tmp_path, monkeypatch):
        costs_path = tmp_path / "events" / "provider_costs.ndjson"
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: costs_path)

        # Patch Path.open to raise to verify emit raises on write failure
        from pathlib import Path
        with patch.object(Path, "open", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                provider_costs.emit_provider_cost(
                    provider="codex",
                    model="gpt-5.5",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd_estimate=0.001,
                    dispatch_id="fail-test-001",
                )


# ---------------------------------------------------------------------------
# record_id stability
# ---------------------------------------------------------------------------

class TestRecordIdStability:
    def test_record_id_stable_for_same_inputs(self):
        r1 = provider_costs._make_record_id("dispatch-abc", "2026-05-29T12:00:00Z")
        r2 = provider_costs._make_record_id("dispatch-abc", "2026-05-29T12:00:00Z")
        assert r1 == r2

    def test_record_id_differs_for_different_dispatch(self):
        r1 = provider_costs._make_record_id("dispatch-abc", "2026-05-29T12:00:00Z")
        r2 = provider_costs._make_record_id("dispatch-xyz", "2026-05-29T12:00:00Z")
        assert r1 != r2

    def test_record_id_differs_for_different_timestamp(self):
        r1 = provider_costs._make_record_id("dispatch-abc", "2026-05-29T12:00:00Z")
        r2 = provider_costs._make_record_id("dispatch-abc", "2026-05-29T12:00:01Z")
        assert r1 != r2

    def test_record_id_length_32(self):
        r = provider_costs._make_record_id("d", "t")
        assert len(r) == 32

    def test_record_id_none_dispatch(self):
        r = provider_costs._make_record_id(None, "2026-05-29T12:00:00Z")
        assert len(r) == 32

    def test_record_id_differs_for_different_token_counts_same_second(self):
        # Two emits in the same second with different token counts must not collide.
        r1 = provider_costs._make_record_id(
            "dispatch-abc", "2026-05-29T12:00:00Z",
            project_id="proj", event_type="provider_cost",
            input_tokens=100, output_tokens=50,
        )
        r2 = provider_costs._make_record_id(
            "dispatch-abc", "2026-05-29T12:00:00Z",
            project_id="proj", event_type="provider_cost",
            input_tokens=200, output_tokens=80,
        )
        assert r1 != r2

    def test_timestamp_includes_microseconds(self, tmp_path, monkeypatch):
        # emit_provider_cost must store a microsecond-precision timestamp.
        costs_path = tmp_path / "events" / "provider_costs.ndjson"
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: costs_path)

        provider_costs.emit_provider_cost(
            provider="claude",
            model="sonnet",
            input_tokens=10,
            output_tokens=5,
            cost_usd_estimate=0.0001,
            dispatch_id="ts-test-001",
        )
        event = _last_event(costs_path)
        ts = event["timestamp"]
        # ISO 8601 with microseconds: contains a dot before the timezone marker
        assert "." in ts, f"expected microseconds in timestamp, got: {ts}"
