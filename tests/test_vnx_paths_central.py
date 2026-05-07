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


class TestPathTraversalRejection:
    """Regression tests for BLOCKING 1 — path traversal via project_id."""

    def test_rejects_dotdot(self):
        with pytest.raises(ValueError):
            resolve_central_data_dir("..")

    def test_rejects_dotdot_slash_foo(self):
        with pytest.raises(ValueError):
            resolve_central_data_dir("../foo")

    def test_rejects_dotfoo(self):
        with pytest.raises(ValueError):
            resolve_central_data_dir(".foo")

    def test_rejects_slash_in_id(self):
        with pytest.raises(ValueError):
            resolve_central_data_dir("a/b")

    def test_rejects_leading_dash(self):
        with pytest.raises(ValueError):
            resolve_central_data_dir("-bad")

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError):
            resolve_central_data_dir("BadProj")

    def test_rejects_single_char(self):
        with pytest.raises(ValueError):
            resolve_central_data_dir("a")

    def test_accepts_vnx_dev(self):
        result = resolve_central_data_dir("vnx-dev")
        assert result == Path.home() / ".vnx-data" / "vnx-dev"

    def test_accepts_mc_prod(self):
        result = resolve_central_data_dir("mc-prod")
        assert result == Path.home() / ".vnx-data" / "mc-prod"

    def test_accepts_two_char_id(self):
        result = resolve_central_data_dir("mc")
        assert result == Path.home() / ".vnx-data" / "mc"
