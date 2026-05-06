"""Tests for vnx_paths.resolve_central_data_dir (Phase 6 P3)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from vnx_paths import resolve_central_data_dir


class TestResolveCentralDataDir:
    def test_valid_project_id_returns_home_vnx_data_subdir(self):
        result = resolve_central_data_dir("vnx-dev")
        assert result == Path.home() / ".vnx-data" / "vnx-dev"

    def test_returns_path_object(self):
        result = resolve_central_data_dir("mc")
        assert isinstance(result, Path)

    def test_project_id_is_last_component(self):
        result = resolve_central_data_dir("sales-copilot")
        assert result.name == "sales-copilot"

    def test_parent_is_home_vnx_data(self):
        result = resolve_central_data_dir("seocrawler-v2")
        assert result.parent == Path.home() / ".vnx-data"

    def test_multiple_projects_have_different_dirs(self):
        a = resolve_central_data_dir("project-a")
        b = resolve_central_data_dir("project-b")
        assert a != b

    def test_invalid_uppercase_raises_value_error(self):
        with pytest.raises(ValueError, match="invalid project_id"):
            resolve_central_data_dir("MyProject")

    def test_invalid_leading_digit_raises_value_error(self):
        with pytest.raises(ValueError, match="invalid project_id"):
            resolve_central_data_dir("1project")

    def test_invalid_underscore_raises_value_error(self):
        with pytest.raises(ValueError, match="invalid project_id"):
            resolve_central_data_dir("my_project")

    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError, match="invalid project_id"):
            resolve_central_data_dir("")

    def test_too_long_raises_value_error(self):
        too_long = "a" + "x" * 32
        with pytest.raises(ValueError, match="invalid project_id"):
            resolve_central_data_dir(too_long)

    def test_minimum_length_valid(self):
        result = resolve_central_data_dir("ab")
        assert result.name == "ab"

    def test_maximum_length_valid(self):
        project_id = "a" + "b" * 31
        result = resolve_central_data_dir(project_id)
        assert result.name == project_id

    def test_hyphen_allowed_in_middle(self):
        result = resolve_central_data_dir("vnx-dev-local")
        assert result.name == "vnx-dev-local"
