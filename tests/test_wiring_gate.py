#!/usr/bin/env python3
"""Tests for wiring_gate.py — dead-code detection gate."""

import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from wiring_gate import (
    WiringGateResult,
    _extract_new_public_defs,
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
