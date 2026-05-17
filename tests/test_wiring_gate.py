#!/usr/bin/env python3
"""Tests for wiring_gate.py — dead-code detection gate."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from wiring_gate import (
    WiringGateError,
    WiringGateResult,
    _extract_new_public_defs,
    _grep_callers,
    _load_skip_list,
    check_pr_wiring,
)


SAMPLE_DIFF = textwrap.dedent("""\
    diff --git a/scripts/lib/new_module.py b/scripts/lib/new_module.py
    new file mode 100644
    --- /dev/null
    +++ b/scripts/lib/new_module.py
    @@ -0,0 +1,12 @@
    +\"\"\"New module.\"\"\"
    +
    +def public_helper():
    +    pass
    +
    +def _private_helper():
    +    pass
    +
    +class PublicWidget:
    +    pass
    +
    +class _InternalWidget:
    +    pass
""")

SAMPLE_DIFF_ASYNC = textwrap.dedent("""\
    diff --git a/scripts/lib/async_module.py b/scripts/lib/async_module.py
    new file mode 100644
    --- /dev/null
    +++ b/scripts/lib/async_module.py
    @@ -0,0 +1,9 @@
    +\"\"\"Async module.\"\"\"
    +
    +async def fetch_data():
    +    pass
    +
    +async def _internal_fetch():
    +    pass
    +
    +def sync_helper():
    +    pass
""")

DIFF_NO_DEFS = textwrap.dedent("""\
    diff --git a/README.md b/README.md
    --- a/README.md
    +++ b/README.md
    @@ -1,3 +1,4 @@
     # Project
    +Added a line.
""")


class TestExtractNewPublicDefs:
    def test_extracts_public_skips_private(self):
        defs = _extract_new_public_defs(SAMPLE_DIFF)
        names = [d["name"] for d in defs]
        assert "public_helper" in names
        assert "PublicWidget" in names
        assert "_private_helper" not in names
        assert "_InternalWidget" not in names

    def test_assigns_correct_kind(self):
        defs = _extract_new_public_defs(SAMPLE_DIFF)
        by_name = {d["name"]: d for d in defs}
        assert by_name["public_helper"]["kind"] == "function"
        assert by_name["PublicWidget"]["kind"] == "class"

    def test_no_defs_in_non_python(self):
        defs = _extract_new_public_defs(DIFF_NO_DEFS)
        assert defs == []

    def test_records_file_path(self):
        defs = _extract_new_public_defs(SAMPLE_DIFF)
        for d in defs:
            assert d["file"] == "scripts/lib/new_module.py"


class TestLoadSkipList:
    def test_empty_when_no_file(self, tmp_path):
        result = _load_skip_list(tmp_path)
        assert result == set()

    def test_loads_all_categories(self, tmp_path):
        skip_file = tmp_path / "wiring_skip.yaml"
        skip_file.write_text(textwrap.dedent("""\
            library_exports:
              - emit_governance_receipt
            decorator_registry:
              - register_handler
            all_reexports:
              - WiringGateResult
            cli_dispatch:
              - handle_wiring
        """))
        result = _load_skip_list(tmp_path)
        assert result == {
            "emit_governance_receipt",
            "register_handler",
            "WiringGateResult",
            "handle_wiring",
        }

    def test_handles_malformed_yaml(self, tmp_path):
        skip_file = tmp_path / "wiring_skip.yaml"
        skip_file.write_text("not: [valid: yaml: {{")
        result = _load_skip_list(tmp_path)
        assert result == set()


class TestCheckPrWiring:
    @patch("wiring_gate._get_pr_diff", return_value="")
    def test_empty_diff_passes(self, mock_diff):
        result = check_pr_wiring(999)
        assert result.status == "pass"
        assert result.total_checked == 0

    @patch("wiring_gate._get_pr_diff", return_value=DIFF_NO_DEFS)
    def test_no_new_defs_passes(self, mock_diff):
        result = check_pr_wiring(999)
        assert result.status == "pass"

    @patch("wiring_gate._grep_callers", return_value=0)
    @patch("wiring_gate._get_pr_diff", return_value=SAMPLE_DIFF)
    def test_unwired_advisory_by_default(self, mock_diff, mock_grep, monkeypatch):
        monkeypatch.setenv("VNX_WIRING_GATE_REQUIRED", "0")
        result = check_pr_wiring(123)
        assert result.status == "advisory"
        assert len(result.unwired) == 2

    @patch("wiring_gate._grep_callers", return_value=0)
    @patch("wiring_gate._get_pr_diff", return_value=SAMPLE_DIFF)
    def test_unwired_blocks_when_required(self, mock_diff, mock_grep, monkeypatch):
        monkeypatch.setenv("VNX_WIRING_GATE_REQUIRED", "1")
        result = check_pr_wiring(123)
        assert result.status == "fail"
        assert len(result.unwired) == 2

    @patch("wiring_gate._grep_callers", return_value=3)
    @patch("wiring_gate._get_pr_diff", return_value=SAMPLE_DIFF)
    def test_all_wired_passes(self, mock_diff, mock_grep):
        result = check_pr_wiring(123)
        assert result.status == "pass"
        assert result.unwired == []

    @patch("wiring_gate._grep_callers", return_value=0)
    @patch("wiring_gate._get_pr_diff", return_value=SAMPLE_DIFF)
    def test_skip_list_excludes_symbols(self, mock_diff, mock_grep, tmp_path):
        skip_file = tmp_path / "wiring_skip.yaml"
        skip_file.write_text("library_exports:\n  - public_helper\n  - PublicWidget\n")
        result = check_pr_wiring(123, state_dir=tmp_path)
        assert result.status == "pass"
        assert set(result.skipped) == {"public_helper", "PublicWidget"}

    def test_result_to_dict(self):
        from wiring_gate import UnwiredSymbol
        result = WiringGateResult(
            status="advisory",
            unwired=[UnwiredSymbol("foo", "bar.py", 10, "function")],
            skipped=["baz"],
            total_checked=5,
            summary="1 unwired symbol(s): foo",
        )
        d = result.to_dict()
        assert d["status"] == "advisory"
        assert d["unwired"][0]["name"] == "foo"
        assert d["skipped"] == ["baz"]

    @patch("wiring_gate._get_pr_diff", side_effect=WiringGateError("gh pr diff failed"))
    def test_diff_failure_raises_wiring_gate_error(self, mock_diff):
        with pytest.raises(WiringGateError, match="gh pr diff failed"):
            check_pr_wiring(999)

    @patch("wiring_gate._grep_callers", return_value=None)
    @patch("wiring_gate._get_pr_diff", return_value=SAMPLE_DIFF)
    def test_grep_failure_blocks_as_fail(self, mock_diff, mock_grep):
        result = check_pr_wiring(123)
        assert result.status == "fail"
        assert len(result.unwired) == 2
        assert "grep failed" in result.summary


class TestAsyncDefExtraction:
    def test_extracts_async_def(self):
        defs = _extract_new_public_defs(SAMPLE_DIFF_ASYNC)
        names = [d["name"] for d in defs]
        assert "fetch_data" in names
        assert "sync_helper" in names
        assert "_internal_fetch" not in names

    def test_async_def_kind_is_function(self):
        defs = _extract_new_public_defs(SAMPLE_DIFF_ASYNC)
        by_name = {d["name"]: d for d in defs}
        assert by_name["fetch_data"]["kind"] == "function"


class TestGetPrDiffRaisesOnError:
    @patch("wiring_gate.subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30))
    def test_timeout_raises(self, mock_run):
        from wiring_gate import _get_pr_diff
        with pytest.raises(WiringGateError, match="timed out"):
            _get_pr_diff(123)

    @patch("wiring_gate.subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh"))
    def test_called_process_error_raises(self, mock_run):
        from wiring_gate import _get_pr_diff
        with pytest.raises(WiringGateError, match="failed"):
            _get_pr_diff(123)

    @patch("wiring_gate.subprocess.run", side_effect=OSError("no gh binary"))
    def test_os_error_raises(self, mock_run):
        from wiring_gate import _get_pr_diff
        with pytest.raises(WiringGateError, match="failed"):
            _get_pr_diff(123)


class TestGrepCallersReturnsNone:
    @patch("wiring_gate.subprocess.run", side_effect=subprocess.TimeoutExpired("grep", 10))
    def test_timeout_returns_none(self, mock_run):
        result = _grep_callers("some_func", "some_file.py")
        assert result is None

    @patch("wiring_gate.subprocess.run", side_effect=OSError("no grep"))
    def test_os_error_returns_none(self, mock_run):
        result = _grep_callers("some_func", "some_file.py")
        assert result is None

    @patch("wiring_gate.subprocess.run")
    def test_bad_returncode_returns_none(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["grep"], returncode=2, stdout="", stderr="error"
        )
        result = _grep_callers("some_func", "some_file.py")
        assert result is None
