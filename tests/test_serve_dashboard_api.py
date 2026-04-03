#!/usr/bin/env python3
"""Tests for serve_dashboard.py API endpoints.

Covers:
  - /api/health: status, uptime, data_sources shape
  - /api/operator/kanban: stages structure, dispatch card shape, total count
  - Empty-response regression: unrecognised /api/* paths now handled
"""

from __future__ import annotations

import sys
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

# Make dashboard importable without running __main__
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import serve_dashboard as sd


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------

class TestApiHealth:

    def test_returns_status_ok(self) -> None:
        result = sd._api_health()
        assert result["status"] == "ok"

    def test_uptime_seconds_is_non_negative_float(self) -> None:
        result = sd._api_health()
        assert isinstance(result["uptime_seconds"], float)
        assert result["uptime_seconds"] >= 0.0

    def test_server_start_is_iso_string(self) -> None:
        result = sd._api_health()
        ts = result["server_start"]
        datetime.fromisoformat(ts.replace("Z", "+00:00"))  # must not raise

    def test_queried_at_is_iso_string(self) -> None:
        result = sd._api_health()
        ts = result["queried_at"]
        datetime.fromisoformat(ts.replace("Z", "+00:00"))

    def test_data_sources_contains_required_keys(self) -> None:
        result = sd._api_health()
        sources = result["data_sources"]
        for key in ("receipts", "dispatches", "reports", "state_dir", "quality_db"):
            assert key in sources

    def test_data_sources_values_are_available_or_unavailable(self) -> None:
        result = sd._api_health()
        for val in result["data_sources"].values():
            assert val in ("available", "unavailable")

    def test_all_sources_available_is_bool(self) -> None:
        result = sd._api_health()
        assert isinstance(result["all_sources_available"], bool)

    def test_all_sources_available_false_when_some_missing(self) -> None:
        # Point paths at a temp dir where nothing exists
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with (
                patch.object(sd, "RECEIPTS_PATH", tmp / "nope.ndjson"),
                patch.object(sd, "DISPATCHES_DIR", tmp / "dispatches"),
                patch.object(sd, "REPORTS_DIR", tmp / "reports"),
                patch.object(sd, "CANONICAL_STATE_DIR", tmp / "state"),
                patch.object(sd, "DB_PATH", tmp / "db.sqlite"),
            ):
                result = sd._api_health()
        assert result["all_sources_available"] is False
        for val in result["data_sources"].values():
            assert val == "unavailable"

    def test_all_sources_available_true_when_all_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            receipts = tmp / "receipts.ndjson"
            dispatches = tmp / "dispatches"
            reports = tmp / "reports"
            state = tmp / "state"
            db = tmp / "db.sqlite"
            receipts.touch()
            dispatches.mkdir()
            reports.mkdir()
            state.mkdir()
            db.touch()
            with (
                patch.object(sd, "RECEIPTS_PATH", receipts),
                patch.object(sd, "DISPATCHES_DIR", dispatches),
                patch.object(sd, "REPORTS_DIR", reports),
                patch.object(sd, "CANONICAL_STATE_DIR", state),
                patch.object(sd, "DB_PATH", db),
            ):
                result = sd._api_health()
        assert result["all_sources_available"] is True
        for val in result["data_sources"].values():
            assert val == "available"


# ---------------------------------------------------------------------------
# /api/operator/kanban
# ---------------------------------------------------------------------------

class TestOperatorKanban:

    def test_returns_stages_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(sd, "DISPATCHES_DIR", Path(tmpdir) / "dispatches"):
                result = sd._operator_get_kanban()
        assert "stages" in result

    def test_returns_total_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(sd, "DISPATCHES_DIR", Path(tmpdir) / "dispatches"):
                result = sd._operator_get_kanban()
        assert "total" in result

    def test_empty_dispatch_dir_yields_zero_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(sd, "DISPATCHES_DIR", Path(tmpdir) / "dispatches"):
                result = sd._operator_get_kanban()
        assert result["total"] == 0

    def test_stages_contains_expected_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(sd, "DISPATCHES_DIR", Path(tmpdir) / "dispatches"):
                result = sd._operator_get_kanban()
        stages = result["stages"]
        for key in ("staging", "pending", "active", "review", "done"):
            assert key in stages

    def test_stages_values_are_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(sd, "DISPATCHES_DIR", Path(tmpdir) / "dispatches"):
                result = sd._operator_get_kanban()
        for stage_list in result["stages"].values():
            assert isinstance(stage_list, list)

    def test_dispatch_card_shape_with_staged_dispatch(self) -> None:
        """A .md file in staging/ produces a card with the expected fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatches_dir = Path(tmpdir) / "dispatches"
            staging = dispatches_dir / "staging"
            staging.mkdir(parents=True)
            (staging / "test-dispatch-001.md").write_text(
                "[[TARGET: T1]]\nDispatch-ID: test-dispatch-001\nPR-ID: PR-1\n",
                encoding="utf-8",
            )
            with (
                patch.object(sd, "DISPATCHES_DIR", dispatches_dir),
                patch.object(sd, "REPORTS_DIR", Path(tmpdir) / "reports"),
            ):
                result = sd._operator_get_kanban()

        assert result["total"] == 1
        cards = result["stages"]["staging"]
        assert len(cards) == 1
        card = cards[0]
        for key in ("id", "file", "stage", "duration_secs", "duration_label", "has_receipt"):
            assert key in card, f"card missing key {key!r}"

    def test_total_matches_sum_of_stage_cards(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatches_dir = Path(tmpdir) / "dispatches"
            for stage_dir_name in ("staging", "pending", "active"):
                d = dispatches_dir / stage_dir_name
                d.mkdir(parents=True)
                (d / f"dispatch-{stage_dir_name}.md").write_text(
                    "[[TARGET: T1]]\nDispatch-ID: d-x\n",
                    encoding="utf-8",
                )
            with (
                patch.object(sd, "DISPATCHES_DIR", dispatches_dir),
                patch.object(sd, "REPORTS_DIR", Path(tmpdir) / "reports"),
            ):
                result = sd._operator_get_kanban()

        total = sum(len(v) for v in result["stages"].values())
        assert result["total"] == total

    def test_degraded_response_on_unexpected_error(self) -> None:
        """_scan_dispatches() crash is caught and returned as degraded dict."""
        def _boom():
            raise RuntimeError("unexpected failure")

        with patch.object(sd, "_scan_dispatches", _boom):
            result = sd._operator_get_kanban()

        assert result.get("degraded") is True
        assert len(result.get("degraded_reasons", [])) == 1


# ---------------------------------------------------------------------------
# Regression: unrecognised /api/* must not return HTML
# ---------------------------------------------------------------------------

class TestApiCatchAll:
    """Verify the API-path handler helpers return dict (not None/HTML) for all routes."""

    def test_health_returns_dict(self) -> None:
        result = sd._api_health()
        assert isinstance(result, dict)

    def test_kanban_returns_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(sd, "DISPATCHES_DIR", Path(tmpdir) / "dispatches"):
                result = sd._operator_get_kanban()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# /api/operator/gate/config (GET)
# ---------------------------------------------------------------------------

class TestGateConfigGet:

    def test_returns_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result = sd._operator_get_gate_config({}, config_path=cfg)
        assert isinstance(result, dict)

    def test_contains_required_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result = sd._operator_get_gate_config({}, config_path=cfg)
        for key in ("project", "gates", "queried_at", "config_path"):
            assert key in result, f"missing key: {key!r}"

    def test_gates_is_dict_when_no_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result = sd._operator_get_gate_config({}, config_path=cfg)
        assert isinstance(result["gates"], dict)

    def test_project_none_when_not_specified(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result = sd._operator_get_gate_config({}, config_path=cfg)
        assert result["project"] is None

    def test_project_param_reflected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result = sd._operator_get_gate_config({"project": ["alpha"]}, config_path=cfg)
        assert result["project"] == "alpha"

    def test_returns_per_project_gates_after_toggle(self) -> None:
        """Gates toggled for a project appear in the GET response for that project."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            sd._operator_post_gate_toggle(
                {"project": "alpha", "gate": "gemini_review", "enabled": False},
                config_path=cfg,
            )
            result = sd._operator_get_gate_config({"project": ["alpha"]}, config_path=cfg)
        assert result["gates"]["gemini_review"]["enabled"] is False

    def test_queried_at_is_iso_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result = sd._operator_get_gate_config({}, config_path=cfg)
        datetime.fromisoformat(result["queried_at"].replace("Z", "+00:00"))

    def test_all_projects_returned_when_no_project_param(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            sd._operator_post_gate_toggle(
                {"project": "alpha", "gate": "gemini_review", "enabled": True},
                config_path=cfg,
            )
            sd._operator_post_gate_toggle(
                {"project": "beta", "gate": "codex_gate", "enabled": False},
                config_path=cfg,
            )
            result = sd._operator_get_gate_config({}, config_path=cfg)
        gates = result["gates"]
        assert "alpha" in gates
        assert "beta" in gates


# ---------------------------------------------------------------------------
# /api/operator/gate/toggle (POST)
# ---------------------------------------------------------------------------

class TestGateTogglePost:

    def test_success_returns_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result, status = sd._operator_post_gate_toggle(
                {"project": "alpha", "gate": "gemini_review", "enabled": True},
                config_path=cfg,
            )
        assert status == 200

    def test_success_returns_success_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result, _ = sd._operator_post_gate_toggle(
                {"project": "alpha", "gate": "gemini_review", "enabled": True},
                config_path=cfg,
            )
        assert result["status"] == "success"

    def test_persists_enabled_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            sd._operator_post_gate_toggle(
                {"project": "myproject", "gate": "codex_gate", "enabled": True},
                config_path=cfg,
            )
            data = sd._read_gate_config(cfg)
        assert data["gates"]["myproject"]["codex_gate"]["enabled"] is True

    def test_persists_enabled_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            sd._operator_post_gate_toggle(
                {"project": "myproject", "gate": "codex_gate", "enabled": False},
                config_path=cfg,
            )
            data = sd._read_gate_config(cfg)
        assert data["gates"]["myproject"]["codex_gate"]["enabled"] is False

    def test_toggle_updates_existing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            sd._operator_post_gate_toggle(
                {"project": "p", "gate": "g", "enabled": True}, config_path=cfg
            )
            sd._operator_post_gate_toggle(
                {"project": "p", "gate": "g", "enabled": False}, config_path=cfg
            )
            data = sd._read_gate_config(cfg)
        assert data["gates"]["p"]["g"]["enabled"] is False

    def test_missing_project_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            _, status = sd._operator_post_gate_toggle(
                {"gate": "gemini_review", "enabled": True}, config_path=cfg
            )
        assert status == 400

    def test_missing_gate_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            _, status = sd._operator_post_gate_toggle(
                {"project": "alpha", "enabled": True}, config_path=cfg
            )
        assert status == 400

    def test_non_bool_enabled_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            _, status = sd._operator_post_gate_toggle(
                {"project": "alpha", "gate": "gemini_review", "enabled": "yes"},
                config_path=cfg,
            )
        assert status == 400

    def test_response_contains_action_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result, _ = sd._operator_post_gate_toggle(
                {"project": "alpha", "gate": "gemini_review", "enabled": True},
                config_path=cfg,
            )
        assert result["action"] == "gate/toggle"

    def test_response_echoes_project_and_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result, _ = sd._operator_post_gate_toggle(
                {"project": "myproj", "gate": "codex_gate", "enabled": False},
                config_path=cfg,
            )
        assert result["project"] == "myproj"
        assert result["gate"] == "codex_gate"
        assert result["enabled"] is False

    def test_response_has_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            result, _ = sd._operator_post_gate_toggle(
                {"project": "p", "gate": "g", "enabled": True}, config_path=cfg
            )
        datetime.fromisoformat(result["timestamp"].replace("Z", "+00:00"))

    def test_creates_config_file_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            assert not cfg.exists()
            sd._operator_post_gate_toggle(
                {"project": "p", "gate": "g", "enabled": True}, config_path=cfg
            )
            assert cfg.exists()

    def test_multiple_projects_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "governance_gates.yaml"
            sd._operator_post_gate_toggle(
                {"project": "alpha", "gate": "gemini_review", "enabled": True},
                config_path=cfg,
            )
            sd._operator_post_gate_toggle(
                {"project": "beta", "gate": "codex_gate", "enabled": False},
                config_path=cfg,
            )
            data = sd._read_gate_config(cfg)
        assert data["gates"]["alpha"]["gemini_review"]["enabled"] is True
        assert data["gates"]["beta"]["codex_gate"]["enabled"] is False
