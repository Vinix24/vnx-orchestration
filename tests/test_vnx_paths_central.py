"""Tests for resolve_central_data_dir added in Phase 6 P3."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from vnx_paths import resolve_central_data_dir


class TestResolveCentralDataDir:
    def test_returns_home_vnx_data_project(self):
        result = resolve_central_data_dir("vnx-dev")
        assert result == Path.home() / ".vnx-data" / "vnx-dev"

    def test_returns_path_object(self):
        result = resolve_central_data_dir("mc")
        assert isinstance(result, Path)

    def test_different_project_ids_produce_different_paths(self):
        a = resolve_central_data_dir("proj-a")
        b = resolve_central_data_dir("proj-b")
        assert a != b

    def test_state_subdir_pattern(self):
        central = resolve_central_data_dir("vnx-dev")
        state = central / "state"
        assert state == Path.home() / ".vnx-data" / "vnx-dev" / "state"

    def test_receipts_path_pattern(self):
        central = resolve_central_data_dir("mc")
        receipts = central / "state" / "t0_receipts.ndjson"
        assert receipts == Path.home() / ".vnx-data" / "mc" / "state" / "t0_receipts.ndjson"

    def test_rejects_empty_project_id(self):
        with pytest.raises(ValueError):
            resolve_central_data_dir("")

    def test_rejects_project_id_with_slash(self):
        with pytest.raises(ValueError):
            resolve_central_data_dir("foo/bar")

    def test_hyphenated_project_id(self):
        result = resolve_central_data_dir("seocrawler-v2")
        assert result.name == "seocrawler-v2"
        assert result.parent.name == ".vnx-data"
