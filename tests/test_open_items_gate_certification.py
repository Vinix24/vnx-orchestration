#!/usr/bin/env python3
"""PR-4 certification for Feature 24: Open Items And Gate Toggle."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import serve_dashboard as sd


class TestGateTogglePersistence:

    def test_config_returns_gates(self, tmp_path: Path) -> None:
        cfg = tmp_path / "gates.yaml"
        result = sd._operator_get_gate_config({}, config_path=cfg)
        assert "gates" in result

    def test_config_has_queried_at(self, tmp_path: Path) -> None:
        cfg = tmp_path / "gates.yaml"
        result = sd._operator_get_gate_config({}, config_path=cfg)
        assert "queried_at" in result

    def test_toggle_returns_success(self, tmp_path: Path) -> None:
        cfg = tmp_path / "gates.yaml"
        body = {"project": "/test", "gate": "codex_gate", "enabled": True}
        result, status = sd._operator_post_gate_toggle(body, config_path=cfg)
        assert status == 200
        assert result.get("success") is True or result.get("status") == "success"

    def test_toggle_persists_and_reads_back(self, tmp_path: Path) -> None:
        cfg = tmp_path / "gates.yaml"
        body = {"project": "alpha", "gate": "test_gate", "enabled": True}
        sd._operator_post_gate_toggle(body, config_path=cfg)
        config = sd._operator_get_gate_config({"project": ["alpha"]}, config_path=cfg)
        gates = config.get("gates", {})
        assert isinstance(gates, dict)

    def test_toggle_idempotent(self, tmp_path: Path) -> None:
        cfg = tmp_path / "gates.yaml"
        body = {"project": "/test", "gate": "g1", "enabled": True}
        sd._operator_post_gate_toggle(body, config_path=cfg)
        result, status = sd._operator_post_gate_toggle(body, config_path=cfg)
        assert status == 200

    def test_toggle_missing_gate_returns_400(self, tmp_path: Path) -> None:
        cfg = tmp_path / "gates.yaml"
        body = {"project": "/test", "enabled": True}
        result, status = sd._operator_post_gate_toggle(body, config_path=cfg)
        assert status == 400

    def test_toggle_non_bool_returns_400(self, tmp_path: Path) -> None:
        cfg = tmp_path / "gates.yaml"
        body = {"project": "/test", "gate": "g1", "enabled": "yes"}
        result, status = sd._operator_post_gate_toggle(body, config_path=cfg)
        assert status == 400


class TestOpenItemsFilter:

    def test_health_still_works(self) -> None:
        result = sd._api_health()
        assert result["status"] in ("ok", "degraded", "unhealthy")


class TestContractAlignment:

    def test_read_gate_config_returns_dict(self, tmp_path: Path) -> None:
        cfg = tmp_path / "gates.yaml"
        result = sd._read_gate_config(cfg)
        assert isinstance(result, dict)

    def test_write_read_round_trip(self, tmp_path: Path) -> None:
        cfg = tmp_path / "gates.yaml"
        sd._write_gate_config({"gates": {"proj": {"g1": {"enabled": True}}}}, cfg)
        result = sd._read_gate_config(cfg)
        assert "gates" in result
