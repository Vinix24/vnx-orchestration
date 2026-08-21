#!/usr/bin/env python3
"""Tests for open_items_from_report — OI-1289: a worker report's ## Open
Items section must land in the open-items ledger, not just markdown.

Covers the five pieces of evidence the dispatch calls for:
  1. Three correctly-formatted items -> all three land in the ledger, with
     dispatch-id and report path attached.
  2. A report that explicitly reports emptiness ("None...") -> zero items.
  3. Reprocessing the same report twice -> item count stays the same
     (dedup_key idempotency).
  4. An introductory prose line in the section -> never becomes an item.
  5. The conservative default severity is pinned to a direct assertion.

Plus: no section at all -> zero items; a title that collides with the
acceptance-criterion guard is skipped, not fatal; and a wiring test proving
report_to_receipt_converter._convert_one_detailed() actually calls into the
sync at the point a report's receipt lands (the "hook onto the existing
processing point" requirement, not a second scanner).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TESTS_DIR.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"

for _p in (str(SCRIPTS_DIR), str(SCRIPTS_LIB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import open_items_from_report  # noqa: E402
from open_items_from_report import sync_open_items_from_report  # noqa: E402


def _load_oim(tmp_path: Path):
    """Load a fresh open_items_manager instance pinned to an isolated STATE_DIR.

    Same pattern as tests/test_open_items_dedup_status.py and
    tests/test_open_items_acceptance_criterion_guard.py — a module-level
    constant (STATE_DIR) resolved at import time means the ambient
    open_items_manager singleton cannot be trusted to point at tmp_path.
    """
    env_patch = {
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(tmp_path / "data" / "state"),
        "VNX_HOME": str(ROOT_DIR),
    }
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)

    mod_name = f"open_items_manager_oireport_{tmp_path.name}"
    with patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(
            mod_name, SCRIPTS_DIR / "open_items_manager.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            del sys.modules[mod_name]
            raise
    return mod


# ---------------------------------------------------------------------------
# 1. Three formatted items land in the ledger
# ---------------------------------------------------------------------------

def test_three_formatted_items_land_in_ledger(tmp_path: Path):
    oim = _load_oim(tmp_path)
    text = (
        "## Open Items\n\n"
        "- [ ] [blocker] Parser crashes on empty input\n"
        "- [ ] [warn] Timeout value is a guess, revisit\n"
        "- [ ] [info] Consider caching the lookup table\n"
    )

    results = sync_open_items_from_report(
        text,
        dispatch_id="20260821-oi-three",
        report_path="/tmp/reports/20260821-oi-three.md",
        oim=oim,
    )

    assert len(results) == 3
    assert all(created for _id, created in results)

    data = oim.load_items()
    items = data["items"]
    assert len(items) == 3

    titles = {i["title"] for i in items}
    assert titles == {
        "Parser crashes on empty input",
        "Timeout value is a guess, revisit",
        "Consider caching the lookup table",
    }
    severities = {i["title"]: i["severity"] for i in items}
    assert severities["Parser crashes on empty input"] == "blocker"
    assert severities["Timeout value is a guess, revisit"] == "warn"
    assert severities["Consider caching the lookup table"] == "info"

    for item in items:
        assert item["origin_dispatch_id"] == "20260821-oi-three"
        assert item["origin_report_path"] == "/tmp/reports/20260821-oi-three.md"


# ---------------------------------------------------------------------------
# 2. Explicit "None" yields zero items
# ---------------------------------------------------------------------------

def test_explicit_none_yields_zero_items(tmp_path: Path):
    oim = _load_oim(tmp_path)
    text = "## Open Items\n\nNone - all work completed and tested.\n"

    results = sync_open_items_from_report(
        text, dispatch_id="20260821-oi-none", report_path="x.md", oim=oim,
    )

    assert results == []
    assert oim.load_items()["items"] == []


def test_no_open_items_section_yields_zero_items(tmp_path: Path):
    """A report with no ## Open Items heading at all also yields zero items."""
    oim = _load_oim(tmp_path)
    text = "## Summary\n\nDid the work.\n"

    results = sync_open_items_from_report(
        text, dispatch_id="20260821-oi-nosection", report_path="x.md", oim=oim,
    )

    assert results == []
    assert oim.load_items()["items"] == []


# ---------------------------------------------------------------------------
# 3. Reprocessing the same report twice is idempotent
# ---------------------------------------------------------------------------

def test_reprocessing_same_report_is_idempotent(tmp_path: Path):
    oim = _load_oim(tmp_path)
    text = "## Open Items\n\n- [ ] [blocker] Something broke in the parser\n"

    first = sync_open_items_from_report(
        text, dispatch_id="20260821-oi-dup", report_path="x.md", oim=oim,
    )
    second = sync_open_items_from_report(
        text, dispatch_id="20260821-oi-dup", report_path="x.md", oim=oim,
    )

    assert len(first) == 1
    assert first[0][1] is True  # created on first pass
    assert len(second) == 1
    assert second[0][1] is False  # deduped on second pass
    assert second[0][0] == first[0][0]  # same item id, not a new one

    assert len(oim.load_items()["items"]) == 1


# ---------------------------------------------------------------------------
# 4. An introductory prose line never becomes an item
# ---------------------------------------------------------------------------

def test_prose_intro_line_is_not_an_item(tmp_path: Path):
    oim = _load_oim(tmp_path)
    text = (
        "## Open Items\n\n"
        "The following still needs follow-up before this can close:\n\n"
        "- [ ] [warn] Double-check the retry budget\n"
    )

    results = sync_open_items_from_report(
        text, dispatch_id="20260821-oi-prose", report_path="x.md", oim=oim,
    )

    assert len(results) == 1
    items = oim.load_items()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Double-check the retry budget"


# ---------------------------------------------------------------------------
# 5. Conservative default severity is pinned
# ---------------------------------------------------------------------------

def test_default_severity_falls_back_to_info():
    """Locks the chosen default (info) so a later change is visible, not silent.

    An item's own [blocker]/[warn]/[info] tag is honored directly (the report
    being explicit about it, per OI-1289) — the fallback only fires for a
    token outside that vocabulary, which is the safety net against an
    uncontrolled blocker count.
    """
    assert open_items_from_report._DEFAULT_SEVERITY == "info"
    assert open_items_from_report._normalize_severity("blocker") == "blocker"
    assert open_items_from_report._normalize_severity("WARN") == "warn"
    assert open_items_from_report._normalize_severity("Info") == "info"
    assert open_items_from_report._normalize_severity("critical") == "info"
    assert open_items_from_report._normalize_severity("") == "info"


def test_unrecognized_severity_token_defaults_to_info_end_to_end(tmp_path: Path):
    """End-to-end: even if extraction ever hands back an out-of-vocabulary
    severity, the ledger entry is conservative (info), never a blocker."""
    oim = _load_oim(tmp_path)

    # sync_open_items_from_report() does `from validate_report import
    # extract_open_items` locally on every call, so patching
    # validate_report's own definition is what actually takes effect.
    import validate_report

    original = validate_report.extract_open_items
    try:
        validate_report.extract_open_items = lambda _content: [
            ("critical", "Unrecognized severity token")
        ]
        results = sync_open_items_from_report(
            "## Open Items\n\n- [ ] [critical] irrelevant, patched above\n",
            dispatch_id="20260821-oi-defaultsev",
            report_path="x.md",
            oim=oim,
        )
    finally:
        validate_report.extract_open_items = original

    assert len(results) == 1
    items = oim.load_items()["items"]
    assert items[0]["severity"] == "info"


# ---------------------------------------------------------------------------
# Acceptance-criterion guard: a bad title is skipped, not fatal
# ---------------------------------------------------------------------------

def test_acceptance_criterion_title_is_skipped_not_fatal(tmp_path: Path):
    oim = _load_oim(tmp_path)
    text = (
        "## Open Items\n\n"
        "- [ ] [info] CI green\n"
        "- [ ] [warn] Real problem needing follow-up\n"
    )

    results = sync_open_items_from_report(
        text, dispatch_id="20260821-oi-guard", report_path="x.md", oim=oim,
    )

    assert len(results) == 1
    items = oim.load_items()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Real problem needing follow-up"


# ---------------------------------------------------------------------------
# Wiring: report_to_receipt_converter hooks into this sync at the point a
# report's receipt lands — no second report scanner.
# ---------------------------------------------------------------------------

def test_convert_one_detailed_triggers_open_items_sync(tmp_path: Path, monkeypatch):
    import report_to_receipt_converter as rtc

    calls = []

    def _fake_sync(text, *, dispatch_id, report_path, oim=None):
        calls.append(dispatch_id)
        return [("OI-999", True)]

    monkeypatch.setattr(
        "open_items_from_report.sync_open_items_from_report", _fake_sync
    )

    report = tmp_path / "20260821-oi-wire.md"
    report.write_text(
        "**Dispatch-ID**: 20260821-oi-wire\n"
        "**Model**: sonnet\n"
        "**Provider**: claude\n\n"
        "## Summary\n\n"
        "Implemented the sync path end to end and wired it into the converter.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 3 passed\n\n"
        "## Open Items\n\n- [ ] [warn] Revisit the timeout value later\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_file = str(state_dir / "t0_receipts.ndjson")

    result, outcome = rtc._convert_one_detailed(report, receipts_file=receipts_file)

    assert outcome == "appended"
    assert calls == ["20260821-oi-wire"]


def test_convert_one_detailed_skips_sync_when_dry_run(tmp_path: Path, monkeypatch):
    """A dry-run must leave zero observable state — the sync must not fire."""
    import report_to_receipt_converter as rtc

    calls = []

    def _fake_sync(text, *, dispatch_id, report_path, oim=None):
        calls.append(dispatch_id)
        return [("OI-999", True)]

    monkeypatch.setattr(
        "open_items_from_report.sync_open_items_from_report", _fake_sync
    )

    report = tmp_path / "20260821-oi-wire-dry.md"
    report.write_text(
        "**Dispatch-ID**: 20260821-oi-wire-dry\n"
        "**Model**: sonnet\n"
        "**Provider**: claude\n\n"
        "## Summary\n\n"
        "Implemented the sync path end to end and wired it into the converter.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 3 passed\n\n"
        "## Open Items\n\n- [ ] [warn] Revisit the timeout value later\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_file = str(state_dir / "t0_receipts.ndjson")

    result, outcome = rtc._convert_one_detailed(
        report, receipts_file=receipts_file, dry_run=True,
    )

    assert outcome == "would_append"
    assert calls == []
