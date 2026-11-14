#!/usr/bin/env python3
"""Characterisation tests for ``ReportParser.extract_metadata`` (golf schoon R15).

Dispatch-ID: dlv-83dc1d32badd

Pins the exact returned dict, including key insertion order, for one report
per extraction branch, plus the exception type on a non-string input. The
expected values were captured from the code before the extract-method
refactor and must stay identical after it.

``report_parser`` has no same-basename sibling under ``scripts/lib/``, but it
is still loaded by path under a unique module name so a CI worker that already
imported another ``report_parser`` cannot hand this file the wrong module.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

_SPEC = importlib.util.spec_from_file_location(
    "_golf_r15_report_parser", REPO / "scripts" / "report_parser.py"
)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

_PAD = "filler line that carries no metadata at all\n" * 60  # ~2700 chars


CASES = {
    "empty": ("", None, [
        ("terminal", "unknown"), ("gate", "unknown"), ("status", "unknown"),
        ("confidence", 0.5), ("task_id", "unknown"), ("dispatch_id", "unknown"),
        ("type", "UNKNOWN"), ("title", "No title"), ("track", "unknown"),
    ]),
    "frontmatter_body_wins": (
        "---\nmodel: kimi-k3\nprovider: kimi\nrole: unknown\n  nested: x\n#c: y\n"
        "Task-ID: fm-task\nempty:\n---\n# Report: Frontmatter case\n"
        "**Model**: sonnet\n**Status**: success\n", None, [
            ("model", "sonnet"), ("provider", "kimi"), ("task_id", "fm-task"),
            ("status", "success"), ("type", "REPORT"), ("title", "Frontmatter case"),
            ("terminal", "unknown"), ("gate", "unknown"), ("confidence", 0.5),
            ("dispatch_id", "unknown"), ("track", "unknown"),
        ]),
    "frontmatter_prose_model_rejected": (
        "---\nmodel: the model field names, the provider\nprovider: claude\n---\nbody\n",
        None, [
            ("provider", "claude"), ("terminal", "unknown"), ("gate", "unknown"),
            ("status", "unknown"), ("confidence", 0.5), ("task_id", "unknown"),
            ("dispatch_id", "unknown"), ("type", "UNKNOWN"), ("title", "No title"),
            ("track", "unknown"),
        ]),
    "bold_header_fields": (
        "# Implementation: Bold fields\n**Terminal**: T2 (Track B)\n**Gate**: gate_x\n"
        "**Confidence**: high\n**Task ID**: task-9\n**Dispatch ID**: 20261114-bold\n"
        "mention `**Model**: inlinecode` here\n**Model**: opus\n", None, [
            ("terminal", "T2"), ("gate", "gate_x"), ("confidence", 0.5),
            ("task_id", "task-9"), ("dispatch_id", "20261114-bold"), ("model", "opus"),
            ("type", "IMPLEMENTATION"), ("title", "Bold fields"), ("status", "unknown"),
            ("track", "B"),
        ]),
    "midfile_model_provider": (
        "# Report: Mid\n" + _PAD + "the **Model**: field names the provider in prose\n"
        "**Model**: deepseek-v4-pro\n**Provider**: deepseek\n", None, [
            ("model", "deepseek-v4-pro"), ("provider", "deepseek"), ("type", "REPORT"),
            ("title", "Mid"), ("terminal", "unknown"), ("gate", "unknown"),
            ("status", "unknown"), ("confidence", 0.5), ("task_id", "unknown"),
            ("dispatch_id", "unknown"), ("track", "unknown"),
        ]),
    "plain_text_identity": (
        "Model: `sonnet`\nProvider: claude\nDispatch-id: plain-777\nSession: sess_42\n",
        None, [
            ("provider", "claude"), ("dispatch_id", "plain-777"), ("session_id", "sess_42"),
            ("terminal", "unknown"), ("gate", "unknown"), ("status", "unknown"),
            ("confidence", 0.5), ("task_id", "unknown"), ("type", "UNKNOWN"),
            ("title", "No title"), ("track", "unknown"),
        ]),
    "plain_text_unknown_model": (
        # The plain-text fallback skips unknown-like values, but the identity
        # block scan after it still takes them (pinned as-is, see report).
        "Model: unknown\nProvider: none\n", None, [
            ("model", "unknown"), ("provider", "none"), ("terminal", "unknown"), ("gate", "unknown"), ("status", "unknown"),
            ("confidence", 0.5), ("task_id", "unknown"), ("dispatch_id", "unknown"),
            ("type", "UNKNOWN"), ("title", "No title"), ("track", "unknown"),
        ]),
    "yaml_block": (
        "**Model**: opus\n```yaml\ndispatch_id: yaml-123\nmodel: sonnet\nStatus: done\n"
        "no colon line\n```\n**Session**: abc-def\n", None, [
            ("model", "opus"), ("session", "abc-def"), ("dispatch_id", "yaml-123"),
            ("status", "done"), ("session_id", "abc-def"), ("terminal", "unknown"),
            ("gate", "unknown"), ("confidence", 0.5), ("task_id", "unknown"),
            ("type", "UNKNOWN"), ("title", "No title"), ("track", "unknown"),
        ]),
    "yaml_model_fills_absent": (
        "```yaml\nmodel: opus\nprovider: claude\n```\n", None, [
            ("model", "opus"), ("provider", "claude"), ("terminal", "unknown"),
            ("gate", "unknown"), ("status", "unknown"), ("confidence", 0.5),
            ("task_id", "unknown"), ("dispatch_id", "unknown"), ("type", "UNKNOWN"),
            ("title", "No title"), ("track", "unknown"),
        ]),
    "table_dispatch_id": (
        "| Field | Value |\n|---|---|\n| **Dispatch-ID** | 20260218-151417-tbl |\n"
        "**Owner**: Track C lead\n", None, [
            ("owner", "Track C lead"), ("dispatch_id", "20260218-151417-tbl"),
            ("terminal", "T3"), ("gate", "unknown"), ("status", "unknown"),
            ("confidence", 0.5), ("task_id", "unknown"), ("type", "UNKNOWN"),
            ("title", "No title"), ("track", "C"),
        ]),
    "identity_block_at_tail_and_cleaning": (
        "# Report: Tail\n" + _PAD * 3 + "\n**Dispatch-ID:** **tail-555**\n"
        "**Model:** opus\nProvider: **\n", None, [
            ("dispatch_id", "tail-555"), ("model", "opus"), ("type", "REPORT"),
            ("title", "Tail"), ("terminal", "unknown"), ("gate", "unknown"),
            ("status", "unknown"), ("confidence", 0.5), ("task_id", "unknown"),
            ("track", "unknown"),
        ]),
    "unknown_dispatch_id_filename_fallback": (
        "**Dispatch-ID**: unknown\n**Confidence**: 0.85\n",
        "20260527-132804-B-pip-repackage_report.md", [
            ("confidence", 0.85), ("dispatch_id", "20260527-132804-B-pip-repackage"),
            ("terminal", "T2"), ("gate", "unknown"), ("status", "unknown"),
            ("task_id", "unknown"), ("type", "UNKNOWN"), ("title", "No title"),
            ("track", "B"),
        ]),
    "filename_unknown_stays_unknown": (
        "body\n", "unknown.md", [
            ("terminal", "unknown"), ("gate", "unknown"), ("status", "unknown"),
            ("confidence", 0.5), ("task_id", "unknown"), ("dispatch_id", "unknown"),
            ("type", "UNKNOWN"), ("title", "No title"), ("track", "unknown"),
        ]),
    "terminal_from_value_and_manager": (
        "**Author**: T-MANAGER\n**Status**: line one\n", None, [
            ("author", "T-MANAGER"), ("status", "line one"), ("terminal", "T-MANAGER"),
            ("gate", "unknown"), ("confidence", 0.5), ("task_id", "unknown"),
            ("dispatch_id", "unknown"), ("type", "UNKNOWN"), ("title", "No title"),
            ("track", "T-MANAGER"),
        ]),
    "terminal_last_resort_scan_inline_code": (
        "`**Owner**: T3` and `**Lead**: Track A`\n", None, [
            ("terminal", "T3"), ("gate", "unknown"), ("status", "unknown"),
            ("confidence", 0.5), ("task_id", "unknown"), ("dispatch_id", "unknown"),
            ("type", "UNKNOWN"), ("title", "No title"), ("track", "C"),
        ]),
    "terminal_last_resort_track_letter": (
        "`**Lead**: Track A`\n", None, [
            ("terminal", "T1"), ("gate", "unknown"), ("status", "unknown"),
            ("confidence", 0.5), ("task_id", "unknown"), ("dispatch_id", "unknown"),
            ("type", "UNKNOWN"), ("title", "No title"), ("track", "A"),
        ]),
    "explicit_terminal_track_value": (
        "**Terminal**: Track B\n**Terminal2**: x\n", None, [
            ("terminal", "T2"), ("terminal2", "x"), ("gate", "unknown"),
            ("status", "unknown"), ("confidence", 0.5), ("task_id", "unknown"),
            ("dispatch_id", "unknown"), ("type", "UNKNOWN"), ("title", "No title"),
            ("track", "B"),
        ]),
    "explicit_terminal_other_value": (
        "**Terminal**: worker-7\n", None, [
            ("terminal", "worker-7"), ("gate", "unknown"), ("status", "unknown"),
            ("confidence", 0.5), ("task_id", "unknown"), ("dispatch_id", "unknown"),
            ("type", "UNKNOWN"), ("title", "No title"), ("track", "worker-7"),
        ]),
}


def _parser(filename):
    parser = _MOD.ReportParser()
    if filename is not None:
        parser._current_filename = filename
    return parser


@pytest.mark.parametrize("name", sorted(CASES))
def test_extract_metadata_pins_dict_and_key_order(name):
    content, filename, expected = CASES[name]
    result = _parser(filename).extract_metadata(content)
    assert list(result.items()) == expected


def test_extract_metadata_does_not_mutate_parser_state():
    parser = _parser(None)
    before = dict(vars(parser))
    parser.extract_metadata("**Model**: opus\n")
    assert dict(vars(parser)) == before


def test_extract_metadata_none_content_raises_type_error():
    with pytest.raises(TypeError):
        _parser(None).extract_metadata(None)


def test_extract_metadata_empty_filename_attr_is_ignored():
    result = _parser("").extract_metadata("")
    assert result["dispatch_id"] == "unknown"
    assert result["terminal"] == "unknown"
