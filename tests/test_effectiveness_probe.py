"""Tests for scripts/lib/effectiveness_probe.py — the base probe framework
(framework-status-audit-and-cockpit PR-5).

Dispatch-ID: 20260712-183939-cockpit-pr5
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from effectiveness_probe import (  # noqa: E402
    EFFECTIVENESS_PROBES,
    PROBE_STATUSES,
    PROBE_TO_BEACON,
    EffectivenessProbe,
    ProbeResult,
    register_probe,
)


class _DummyProbe(EffectivenessProbe):
    subsystem = "dummy-subsystem"

    def probe(self):
        return {"used": 3, "ignored": 1}

    def signal(self, raw):
        return f"used={raw['used']} ignored={raw['ignored']}"

    def health(self, raw):
        return "ok" if raw["ignored"] < raw["used"] else "produces_crap"


def test_dummy_probe_run_returns_probe_result_with_expected_shape():
    result = _DummyProbe().run()

    assert isinstance(result, ProbeResult)
    assert result.status == "ok"
    assert result.signal == "used=3 ignored=1"
    assert result.detail == {"used": 3, "ignored": 1}


def test_probe_result_rejects_status_outside_vocabulary():
    with pytest.raises(ValueError):
        ProbeResult(status="bogus", signal="x", detail={})


def test_probe_result_accepts_every_vocabulary_status():
    for status in PROBE_STATUSES:
        result = ProbeResult(status=status, signal="s", detail={})
        assert result.status == status


def test_abstract_base_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        EffectivenessProbe()  # type: ignore[abstract]


def test_register_probe_decorator_populates_registry():
    EFFECTIVENESS_PROBES.pop("dummy-subsystem", None)
    try:
        @register_probe("dummy-subsystem")
        class _RegisteredProbe(EffectivenessProbe):
            subsystem = "dummy-subsystem"

            def probe(self):
                return {}

            def signal(self, raw):
                return "registered"

            def health(self, raw):
                return "ok"

        assert EFFECTIVENESS_PROBES["dummy-subsystem"] is _RegisteredProbe
    finally:
        EFFECTIVENESS_PROBES.pop("dummy-subsystem", None)


def test_probe_to_beacon_mapping_matches_prd_contract():
    assert PROBE_TO_BEACON["ok"] == "ok"
    assert PROBE_TO_BEACON["degraded"] == "stale"
    assert PROBE_TO_BEACON["produces_crap"] == "fail"
    # "unknown" is deliberately absent: no probe registered -> no beacon written.
    assert "unknown" not in PROBE_TO_BEACON


def test_tampered_signal_classifies_as_produces_crap_not_a_beacon_corrupt_bypass():
    """A tampered/broken hash-chain must map to beacon `fail`, never `corrupt`
    (health_beacon.py reserves `corrupt` for unreadable JSON — see module docstring)."""

    class _TamperProbe(EffectivenessProbe):
        subsystem = "receipt-hash-chain"

        def probe(self):
            return {"chain_status": "broken", "tamper": True}

        def signal(self, raw):
            return "hash-chain broken: tamper detected"

        def health(self, raw):
            return "produces_crap" if raw.get("tamper") else "ok"

    result = _TamperProbe().run()

    assert result.status == "produces_crap"
    assert result.detail["tamper"] is True
    assert PROBE_TO_BEACON[result.status] == "fail"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
