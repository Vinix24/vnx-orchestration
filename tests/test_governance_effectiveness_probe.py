"""Tests for scripts/lib/governance_effectiveness_probe.py
(framework-status-audit-and-cockpit PR-7).

Dispatch-ID: 20260712-185712-cockpit-pr7
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

from effectiveness_probe import EFFECTIVENESS_PROBES  # noqa: E402
from governance_effectiveness_probe import (  # noqa: E402
    GovernanceEffectivenessProbe,
)
from ndjson_hash_chain import append_chained_entry  # noqa: E402


def _ledger_path(repo_root: Path) -> Path:
    return repo_root / ".vnx-attest" / "plan-gates.ndjson"


def test_registered_under_governance_enforcement_stack():
    assert EFFECTIVENESS_PROBES["governance-enforcement-stack"] is GovernanceEffectivenessProbe


def test_unknown_when_ledger_absent(tmp_path):
    result = GovernanceEffectivenessProbe(repo_root=tmp_path).run()

    assert result.status == "unknown"
    assert result.detail["ledger_exists"] is False
    assert "detection_limitation" in result.detail


def test_unchained_ledger_is_ok_not_produces_crap(tmp_path):
    """An unchained ledger (no prev_hash) is the expected PARK state, not a failure."""
    ledger = _ledger_path(tmp_path)
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"type": "plan_gate_pass", "track_id": "t1"}) + "\n")

    result = GovernanceEffectivenessProbe(repo_root=tmp_path).run()

    assert result.status == "ok"
    assert result.detail["chain_status"] == "unchained"
    assert result.detail["entry_count"] == 1


def test_verified_chain_is_ok(tmp_path):
    ledger = _ledger_path(tmp_path)
    append_chained_entry(ledger, {"type": "plan_gate_pass", "track_id": "t1", "resolver": "attest"})
    append_chained_entry(ledger, {"type": "plan_gate_pass", "track_id": "t2", "resolver": "attest"})

    result = GovernanceEffectivenessProbe(repo_root=tmp_path).run()

    assert result.status == "ok"
    assert result.detail["chain_status"] in ("verified", "verified-segmented")
    assert result.detail["entry_count"] == 2


def test_broken_chain_is_produces_crap_with_tamper_detail_not_corrupt(tmp_path):
    """A tampered chain (prev_hash present but wrong) maps to produces_crap (beacon
    `fail`), never `corrupt` — that vocabulary is reserved for unreadable JSON."""
    ledger = _ledger_path(tmp_path)
    append_chained_entry(ledger, {"type": "plan_gate_pass", "track_id": "t1", "resolver": "attest"})
    append_chained_entry(ledger, {"type": "plan_gate_pass", "track_id": "t2", "resolver": "attest"})

    lines = ledger.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[-1])
    tampered["prev_hash"] = "f" * 64
    lines[-1] = json.dumps(tampered)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = GovernanceEffectivenessProbe(repo_root=tmp_path).run()

    assert result.status == "produces_crap"
    assert result.detail["chain_status"] == "broken"
    assert result.detail["tamper"] is True
    assert "TAMPER" in result.signal


def test_detection_limitation_caveat_always_present(tmp_path):
    result = GovernanceEffectivenessProbe(repo_root=tmp_path).run()
    assert "#1086" in result.detail["detection_limitation"]


def test_default_construction_resolves_real_repo_root_without_crashing():
    """The zero-arg constructor path is what subsystem_health.aggregate() actually
    calls (probe_cls()); it must resolve a real repo_root and never raise."""
    result = GovernanceEffectivenessProbe().run()
    assert result.status in {"ok", "degraded", "produces_crap", "unknown"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
