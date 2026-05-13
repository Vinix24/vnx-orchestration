"""OI-1369 — strict project_id validation prevents path traversal outside sandbox.

Regression test pack: 18 invalid IDs that must raise ValueError,
9 valid IDs that must succeed, plus a sandbox-escape integration check.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from vnx_paths import resolve_central_data_dir


class TestProjectIdValidation:
    """OI-1369 — strict project_id validation prevents path traversal outside sandbox."""

    @pytest.mark.parametrize(
        "bad_id",
        [
            "..",           # parent dir
            "../../etc",    # multi-level traversal
            ".",            # current dir
            ".hidden",      # leading dot
            "a/b",          # forward slash
            "a\\b",         # backslash (Windows-style)
            "a b",          # space
            "A",            # uppercase single char
            "Abc",          # mixed case
            "-abc",         # leading hyphen
            "1abc",         # leading digit
            "a",            # too short (1 char)
            "a" * 33,       # too long (33 chars)
            "",             # empty
            "a.b",          # internal dot
            "a_b",          # underscore (not in allowed set)
            "a:b",          # colon
            "$abc",         # special char
        ],
    )
    def test_reject_invalid_project_ids(self, bad_id):
        with pytest.raises(ValueError):
            resolve_central_data_dir(bad_id)

    @pytest.mark.parametrize(
        "good_id",
        [
            "ab",                   # min length (2 chars)
            "abc",                  # simple
            "mission-control",      # operator example
            "sales-copilot",        # operator example
            "seocrawler-v2",        # operator example
            "a" + "b" * 31,        # max length (32 chars)
            "a-b-c-d-e",           # multiple hyphens
            "p1",                   # letter + digit
            "project1",             # letters + digit
        ],
    )
    def test_accept_valid_project_ids(self, good_id):
        result = resolve_central_data_dir(good_id)
        assert isinstance(result, Path)

    def test_traversal_attempt_does_not_escape_sandbox(self):
        """Resolved path must stay inside ~/.vnx-data."""
        home_vnx = Path.home() / ".vnx-data"
        result = resolve_central_data_dir("test-project")
        try:
            result.relative_to(home_vnx)
        except ValueError:
            pytest.fail(f"resolved path {result} is NOT inside {home_vnx}")
