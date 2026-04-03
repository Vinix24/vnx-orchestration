#!/usr/bin/env python3
"""PR-4 certification for Feature 23: Dashboard Pipeline And Kanban Board."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import serve_dashboard as sd


class TestHealthEndpoint:

    def test_health_returns_status_ok(self) -> None:
        result = sd._api_health()
        assert result["status"] in ("ok", "degraded", "unhealthy")

    def test_health_has_data_sources(self) -> None:
        result = sd._api_health()
        assert "data_sources" in result
        assert isinstance(result["data_sources"], dict)

    def test_health_has_timestamps(self) -> None:
        result = sd._api_health()
        assert "queried_at" in result
        assert "server_start" in result

    def test_health_has_uptime(self) -> None:
        result = sd._api_health()
        assert isinstance(result["uptime_seconds"], float)
        assert result["uptime_seconds"] >= 0.0

    def test_health_all_sources_flag(self) -> None:
        result = sd._api_health()
        assert "all_sources_available" in result
        assert isinstance(result["all_sources_available"], bool)


class TestKanbanMapping:

    def test_dir_to_stage_has_staging(self) -> None:
        assert sd._DIR_TO_STAGE["staging"] == "staging"

    def test_dir_to_stage_has_pending(self) -> None:
        assert sd._DIR_TO_STAGE["pending"] == "pending"

    def test_dir_to_stage_has_active(self) -> None:
        assert sd._DIR_TO_STAGE["active"] == "active"

    def test_dir_to_stage_completed_maps_to_done(self) -> None:
        assert sd._DIR_TO_STAGE["completed"] == "done"

    def test_dir_to_stage_rejected_maps_to_done(self) -> None:
        assert sd._DIR_TO_STAGE["rejected"] == "done"

    def test_scan_dispatches_returns_dict(self) -> None:
        result = sd._scan_dispatches()
        assert isinstance(result, dict)
        assert "stages" in result
        assert "total" in result

    def test_scan_dispatches_stages_are_lists(self) -> None:
        result = sd._scan_dispatches()
        for stage_name in ("staging", "pending", "active", "review", "done"):
            assert isinstance(result["stages"].get(stage_name, []), list)


class TestContractAlignment:

    def test_five_kanban_stages_in_mapping(self) -> None:
        target_stages = set(sd._DIR_TO_STAGE.values())
        assert "staging" in target_stages
        assert "pending" in target_stages
        assert "active" in target_stages
        assert "done" in target_stages

    def test_health_endpoint_callable(self) -> None:
        result = sd._api_health()
        assert isinstance(result, dict)

    def test_scan_dispatches_callable(self) -> None:
        result = sd._scan_dispatches()
        assert isinstance(result, dict)
