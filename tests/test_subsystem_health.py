"""Tests for scripts/lib/subsystem_health.py — the probe aggregator
(framework-status-audit-and-cockpit PR-5).

Dispatch-ID: 20260712-183939-cockpit-pr5
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import subsystem_health  # noqa: E402
from effectiveness_probe import EFFECTIVENESS_PROBES, EffectivenessProbe  # noqa: E402


class _OkProbe(EffectivenessProbe):
    subsystem = "probe-ok-subsystem"

    def probe(self):
        return {"signal": "fine"}

    def signal(self, raw):
        return "all good"

    def health(self, raw):
        return "ok"


class _CrapProbe(EffectivenessProbe):
    subsystem = "probe-crap-subsystem"

    def probe(self):
        return {"tamper": True}

    def signal(self, raw):
        return "broken chain"

    def health(self, raw):
        return "produces_crap"


def test_aggregate_runs_registered_probes_and_reports_unknown_for_the_rest(monkeypatch, tmp_path):
    monkeypatch.setitem(EFFECTIVENESS_PROBES, "probe-ok-subsystem", _OkProbe)
    monkeypatch.setitem(EFFECTIVENESS_PROBES, "probe-crap-subsystem", _CrapProbe)

    results = subsystem_health.aggregate(
        state_dir=tmp_path,
        subsystems=["probe-ok-subsystem", "probe-crap-subsystem", "no-probe-subsystem"],
    )

    assert results["probe-ok-subsystem"]["status"] == "ok"
    assert results["probe-crap-subsystem"]["status"] == "produces_crap"
    assert results["probe-crap-subsystem"]["detail"]["tamper"] is True
    assert results["no-probe-subsystem"]["status"] == "unknown"
    assert results["no-probe-subsystem"]["signal"] == "no probe registered"


def test_aggregate_emits_beacons_under_health_dir_for_probed_subsystems(monkeypatch, tmp_path):
    monkeypatch.setitem(EFFECTIVENESS_PROBES, "probe-ok-subsystem", _OkProbe)
    monkeypatch.setitem(EFFECTIVENESS_PROBES, "probe-crap-subsystem", _CrapProbe)

    subsystem_health.aggregate(
        state_dir=tmp_path,
        subsystems=["probe-ok-subsystem", "probe-crap-subsystem"],
    )

    ok_beacon = tmp_path / "health" / "probe-ok-subsystem.json"
    crap_beacon = tmp_path / "health" / "probe-crap-subsystem.json"
    assert ok_beacon.exists()
    assert crap_beacon.exists()

    ok_payload = json.loads(ok_beacon.read_text(encoding="utf-8"))
    crap_payload = json.loads(crap_beacon.read_text(encoding="utf-8"))
    assert ok_payload["status"] == "ok"
    # produces_crap -> beacon "fail", never "corrupt" (see effectiveness_probe.py docstring).
    assert crap_payload["status"] == "fail"
    assert crap_payload["details"]["tamper"] is True


def test_aggregate_writes_no_beacon_for_unknown_status(tmp_path):
    subsystem_health.aggregate(state_dir=tmp_path, subsystems=["no-probe-subsystem"])

    beacon_path = tmp_path / "health" / "no-probe-subsystem.json"
    assert not beacon_path.exists()


def test_known_subsystems_includes_registry_and_probe_names(monkeypatch):
    monkeypatch.setitem(EFFECTIVENESS_PROBES, "probe-ok-subsystem", _OkProbe)

    names = subsystem_health.known_subsystems()

    # Flag-backed (config_registry.CONFIG_REGISTRY) and flag-less
    # (CONFIG_REGISTRY_SUBSYSTEMS) entries are both present.
    assert "governance-enforcement-stack" in names
    assert "phantom_guard" in names
    # A subsystem with only a registered probe (no registry entry) is present too.
    assert "probe-ok-subsystem" in names
    assert names == sorted(set(names))


def test_aggregate_default_subsystems_covers_the_known_universe(monkeypatch, tmp_path):
    monkeypatch.setitem(EFFECTIVENESS_PROBES, "probe-ok-subsystem", _OkProbe)

    results = subsystem_health.aggregate(state_dir=tmp_path)

    assert results["probe-ok-subsystem"]["status"] == "ok"
    # "docs-bloat" carries no probe by PRD design (PR-11 is pure docs cleanup, no
    # code/health surface) — it stays the "no probe registered" exemplar even
    # after PR-7 registers real probes for governance/plan-gate/migration.
    assert results["docs-bloat"]["status"] == "unknown"
    assert results["docs-bloat"]["signal"] == "no probe registered"


def test_aggregate_wires_governance_plan_gate_and_migration_probes(tmp_path):
    """PR-7 acceptance criteria: ``vnx subsystems --probe`` (which calls
    aggregate()) returns real health — not 'no probe registered' — for all
    three subsystems, using each probe's default (real repo/state) resolution."""
    results = subsystem_health.aggregate(
        state_dir=tmp_path,
        subsystems=["governance-enforcement-stack", "plan-gate-panel", "migration-mechanisms"],
    )

    for name in ("governance-enforcement-stack", "plan-gate-panel", "migration-mechanisms"):
        assert results[name]["signal"] != "no probe registered"
        assert results[name]["status"] in {"ok", "degraded", "produces_crap", "unknown"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
