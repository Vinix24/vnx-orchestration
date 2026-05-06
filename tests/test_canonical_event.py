#!/usr/bin/env python3
"""Tests for CanonicalEvent schema, validation, serialization, and legacy shim."""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from canonical_event import (
    VALID_EVENT_TYPES,
    VALID_PROVIDERS,
    VALID_TIERS,
    CanonicalEvent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(**overrides) -> CanonicalEvent:
    defaults = dict(
        dispatch_id="d-001",
        terminal_id="T1",
        provider="claude",
        event_type="text",
        data={"text": "hello"},
    )
    defaults.update(overrides)
    return CanonicalEvent(**defaults)


# ---------------------------------------------------------------------------
# Construction and field defaults
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_required_fields_only(self):
        ev = _make_event()
        assert ev.dispatch_id == "d-001"
        assert ev.terminal_id == "T1"
        assert ev.provider == "claude"
        assert ev.event_type == "text"
        assert ev.data == {"text": "hello"}

    def test_default_tier_is_2(self):
        ev = _make_event()
        assert ev.observability_tier == 2

    def test_default_event_id_is_uuid(self):
        import re
        ev = _make_event()
        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            ev.event_id,
        )

    def test_default_timestamp_is_iso(self):
        from datetime import datetime
        ev = _make_event()
        # Must parse without raising
        datetime.fromisoformat(ev.timestamp)

    def test_default_provider_meta_is_empty_dict(self):
        ev = _make_event()
        assert ev.provider_meta == {}

    def test_explicit_tier_values(self):
        for tier in (1, 2, 3):
            ev = _make_event(observability_tier=tier)
            assert ev.observability_tier == tier


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_invalid_provider_raises(self):
        with pytest.raises(ValueError, match="provider"):
            _make_event(provider="openai")

    def test_invalid_event_type_raises(self):
        with pytest.raises(ValueError, match="event_type"):
            _make_event(event_type="unknown_type")

    def test_invalid_tier_zero_raises(self):
        with pytest.raises(ValueError, match="observability_tier"):
            _make_event(observability_tier=0)

    def test_invalid_tier_four_raises(self):
        with pytest.raises(ValueError, match="observability_tier"):
            _make_event(observability_tier=4)

    @pytest.mark.parametrize("provider", sorted(VALID_PROVIDERS))
    def test_all_valid_providers_accepted(self, provider):
        ev = _make_event(provider=provider)
        assert ev.provider == provider

    @pytest.mark.parametrize("event_type", sorted(VALID_EVENT_TYPES))
    def test_all_valid_event_types_accepted(self, event_type):
        ev = _make_event(event_type=event_type)
        assert ev.event_type == event_type

    @pytest.mark.parametrize("tier", sorted(VALID_TIERS))
    def test_all_valid_tiers_accepted(self, tier):
        ev = _make_event(observability_tier=tier)
        assert ev.observability_tier == tier


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_to_dict_keys(self):
        ev = _make_event()
        d = ev.to_dict()
        assert set(d.keys()) == {
            "event_id",
            "dispatch_id",
            "terminal_id",
            "provider",
            "event_type",
            "timestamp",
            "data",
            "observability_tier",
            "provider_meta",
        }

    def test_from_dict_round_trip(self):
        ev = _make_event(observability_tier=1, provider="codex", event_type="tool_use")
        reconstructed = CanonicalEvent.from_dict(ev.to_dict())
        assert reconstructed.dispatch_id == ev.dispatch_id
        assert reconstructed.terminal_id == ev.terminal_id
        assert reconstructed.provider == ev.provider
        assert reconstructed.event_type == ev.event_type
        assert reconstructed.data == ev.data
        assert reconstructed.observability_tier == ev.observability_tier
        assert reconstructed.event_id == ev.event_id
        assert reconstructed.timestamp == ev.timestamp
        assert reconstructed.provider_meta == ev.provider_meta

    def test_to_dict_is_json_serializable(self):
        ev = _make_event()
        json.dumps(ev.to_dict())  # must not raise

    @pytest.mark.parametrize("event_type", sorted(VALID_EVENT_TYPES))
    def test_round_trip_all_event_types(self, event_type):
        ev = _make_event(event_type=event_type)
        reconstructed = CanonicalEvent.from_dict(ev.to_dict())
        assert reconstructed.event_type == event_type

    def test_from_dict_missing_optional_fields_use_defaults(self):
        minimal = {
            "dispatch_id": "d-999",
            "terminal_id": "T2",
            "provider": "gemini",
            "event_type": "init",
            "data": {},
        }
        ev = CanonicalEvent.from_dict(minimal)
        assert ev.observability_tier == 2
        assert ev.provider_meta == {}
        assert ev.event_id  # generated


# ---------------------------------------------------------------------------
# from_legacy — legacy dict shim
# ---------------------------------------------------------------------------

class TestFromLegacy:
    # The seven dashboard types emitted by subprocess_adapter.py
    @pytest.mark.parametrize("legacy_type,expected_type", [
        ("init", "init"),
        ("thinking", "thinking"),
        ("tool_use", "tool_use"),
        ("tool_result", "tool_result"),
        ("text", "text"),
        ("result", "complete"),   # key migration: result -> complete
        ("error", "error"),
    ])
    def test_legacy_types_accepted(self, legacy_type, expected_type):
        legacy = {"type": legacy_type, "data": {"payload": "x"}}
        ev = CanonicalEvent.from_legacy("claude", legacy)
        assert ev.event_type == expected_type

    def test_from_legacy_defaults_tier_2(self):
        ev = CanonicalEvent.from_legacy("claude", {"type": "text", "data": {}})
        assert ev.observability_tier == 2

    def test_from_legacy_tier_override(self):
        ev = CanonicalEvent.from_legacy("codex", {"type": "text", "data": {}}, observability_tier=1)
        assert ev.observability_tier == 1

    def test_from_legacy_dispatch_id_param_wins(self):
        legacy = {"type": "text", "data": {}, "dispatch_id": "old-id"}
        ev = CanonicalEvent.from_legacy("claude", legacy, dispatch_id="new-id")
        assert ev.dispatch_id == "new-id"

    def test_from_legacy_dispatch_id_falls_back_to_event(self):
        legacy = {"type": "text", "data": {}, "dispatch_id": "from-event"}
        ev = CanonicalEvent.from_legacy("claude", legacy)
        assert ev.dispatch_id == "from-event"

    def test_from_legacy_terminal_id_param_wins(self):
        legacy = {"type": "text", "data": {}, "terminal": "T1"}
        ev = CanonicalEvent.from_legacy("claude", legacy, terminal_id="T2")
        assert ev.terminal_id == "T2"

    def test_from_legacy_terminal_id_falls_back_to_event(self):
        legacy = {"type": "text", "data": {}, "terminal": "T3"}
        ev = CanonicalEvent.from_legacy("claude", legacy)
        assert ev.terminal_id == "T3"

    def test_from_legacy_unknown_type_maps_to_error(self):
        legacy = {"type": "magic_event", "data": {}}
        ev = CanonicalEvent.from_legacy("claude", legacy)
        assert ev.event_type == "error"
        assert ev.provider_meta["legacy_type"] == "magic_event"

    def test_from_legacy_data_non_dict_wrapped(self):
        legacy = {"type": "text", "data": "plain string"}
        ev = CanonicalEvent.from_legacy("claude", legacy)
        assert ev.data == {"value": "plain string"}

    def test_from_legacy_preserves_data_dict(self):
        legacy = {"type": "text", "data": {"text": "hello world"}}
        ev = CanonicalEvent.from_legacy("claude", legacy)
        assert ev.data == {"text": "hello world"}

    @pytest.mark.parametrize("provider", sorted(VALID_PROVIDERS))
    def test_from_legacy_all_providers(self, provider):
        legacy = {"type": "text", "data": {}}
        ev = CanonicalEvent.from_legacy(provider, legacy)
        assert ev.provider == provider

    def test_from_legacy_result_no_provider_meta(self):
        # "result" is a known legacy type; provider_meta should be empty
        legacy = {"type": "result", "data": {"output": "done", "cost": 0.01}}
        ev = CanonicalEvent.from_legacy("claude", legacy)
        assert ev.event_type == "complete"
        assert ev.provider_meta == {}

    def test_from_legacy_empty_event_uses_defaults(self):
        ev = CanonicalEvent.from_legacy("ollama", {})
        assert ev.event_type == "text"
        assert ev.observability_tier == 2
        assert ev.dispatch_id == ""
        assert ev.terminal_id == ""
