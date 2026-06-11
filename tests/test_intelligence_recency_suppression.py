#!/usr/bin/env python3
"""
Unit tests for injection-history-aware recency suppression.

Verifies that IntelligenceSelector._select_standard_classes and
_query_recent_injected_ids suppress candidates whose item_ids appear in the
last N dispatches for the same task_class (root-cause fix for 93.4% duplicate
injection rate in coding_interactive).

Test matrix:
  (a) item recently injected → suppressed, alternative selected
  (b) pool goes empty after suppression → vangnet passes best through + logs reason
  (c) different task_class → no suppression applied
  (d) window size respects VNX_INTEL_SUPPRESS_WINDOW env var
  (e) corrupt items_json row → skipped without crash
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from typing import List

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_selector import (
    CONFIDENCE_THRESHOLDS,
    EVIDENCE_THRESHOLDS,
    IntelligenceItem,
    IntelligenceSelector,
    SuppressionRecord,
)
from runtime_coordination import get_connection, init_schema


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_item(
    item_id: str,
    item_class: str = "proven_pattern",
    confidence: float = 0.85,
    evidence_count: int = 5,
    content: str = "Some pattern content",
) -> IntelligenceItem:
    return IntelligenceItem(
        item_id=item_id,
        item_class=item_class,
        title=f"Pattern {item_id}",
        content=content,
        confidence=confidence,
        evidence_count=evidence_count,
        last_seen="2026-06-01T00:00:00Z",
        scope_tags=["coding_interactive"],
        content_hash=f"hash_{item_id}",
    )


def _setup_coord_db(state_dir: Path) -> None:
    """Initialise a minimal runtime_coordination.db with intelligence_injections table."""
    state_dir.mkdir(parents=True, exist_ok=True)
    init_schema(str(state_dir))


def _insert_injection(
    state_dir: Path,
    dispatch_id: str,
    task_class: str,
    item_ids: List[str],
    injected_at: str = "2026-06-10T12:00:00Z",
) -> None:
    """Insert a synthetic intelligence_injections row into the coord DB."""
    items_json = json.dumps([{"item_id": iid} for iid in item_ids])
    injection_id = str(uuid.uuid4())
    with get_connection(state_dir) as conn:
        conn.execute(
            """INSERT INTO intelligence_injections
               (injection_id, dispatch_id, injection_point, task_class,
                items_injected, items_suppressed, payload_chars,
                items_json, suppressed_json, injected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                injection_id, dispatch_id, "dispatch_create", task_class,
                len(item_ids), 0, 200,
                items_json, "[]", injected_at,
            ),
        )
        conn.commit()


def _make_selector(state_dir: Path) -> IntelligenceSelector:
    return IntelligenceSelector(quality_db_path=None, coord_db_state_dir=state_dir)


# ---------------------------------------------------------------------------
# (a) Recently injected item → suppressed, alternative selected
# ---------------------------------------------------------------------------

class TestRecentlyInjectedItemSuppressed:
    """Candidate with item_id in recent injection history gets suppressed."""

    def test_dominant_item_suppressed_alternative_selected(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        _setup_coord_db(state_dir)

        # item A has been injected in the last 10 dispatches for coding_interactive
        dominant_id = "intel_sp_1"
        alt_id = "intel_sp_2"

        for i in range(3):
            _insert_injection(state_dir, f"dispatch-{i}", "coding_interactive", [dominant_id])

        monkeypatch.setenv("VNX_INTEL_SUPPRESS_WINDOW", "10")

        selector = _make_selector(state_dir)
        recent_ids = selector._query_recent_injected_ids("coding_interactive")

        assert dominant_id in recent_ids, "Dominant item must appear in recent_ids"

        dominant = _make_item(dominant_id, confidence=1.0, evidence_count=10)
        alternative = _make_item(alt_id, confidence=0.75, evidence_count=4)
        candidates = {"proven_pattern": [dominant, alternative], "failure_prevention": [], "recent_comparable": []}

        selected, suppressed = selector._select_standard_classes(candidates, recent_ids=recent_ids)

        selected_ids = [i.item_id for i in selected]
        assert alt_id in selected_ids, "Alternative must be selected when dominant is suppressed"
        assert dominant_id not in selected_ids, "Dominant item must not be injected"

        suppression_reasons = [s.reason for s in suppressed]
        assert any("recently injected" in r for r in suppression_reasons), (
            f"Expected suppression reason mentioning 'recently injected', got: {suppression_reasons}"
        )


# ---------------------------------------------------------------------------
# (b) Pool empty after suppression → vangnet passes best through
# ---------------------------------------------------------------------------

class TestVangnetWhenPoolGoesEmpty:
    """When all diverse candidates are recently injected, the best is allowed through."""

    def test_best_passes_through_when_all_suppressed(self, tmp_path, monkeypatch, caplog):
        import logging

        state_dir = tmp_path / "state"
        _setup_coord_db(state_dir)

        item_id = "intel_sp_1"
        _insert_injection(state_dir, "dispatch-0", "coding_interactive", [item_id])

        monkeypatch.setenv("VNX_INTEL_SUPPRESS_WINDOW", "10")

        selector = _make_selector(state_dir)
        recent_ids = frozenset({item_id})

        only_candidate = _make_item(item_id, confidence=0.9, evidence_count=3)
        candidates = {"proven_pattern": [only_candidate], "failure_prevention": [], "recent_comparable": []}

        with caplog.at_level(logging.DEBUG, logger="intelligence_selector"):
            selected, suppressed = selector._select_standard_classes(candidates, recent_ids=recent_ids)

        selected_ids = [i.item_id for i in selected]
        assert item_id in selected_ids, (
            "Vangnet: best candidate must pass through when entire pool is recently injected"
        )

        vangnet_logged = any(
            "vangnet" in record.message.lower() or "vangnet" in record.getMessage().lower()
            for record in caplog.records
        )
        assert vangnet_logged, "Vangnet activation must be logged at DEBUG level"


# ---------------------------------------------------------------------------
# (c) Different task_class → no suppression
# ---------------------------------------------------------------------------

class TestDifferentTaskClassNotSuppressed:
    """Injection history for a different task_class must not affect current selection."""

    def test_other_task_class_injection_does_not_suppress(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        _setup_coord_db(state_dir)

        item_id = "intel_sp_1"
        # Record injection only under research_structured, not coding_interactive
        _insert_injection(state_dir, "dispatch-0", "research_structured", [item_id])

        monkeypatch.setenv("VNX_INTEL_SUPPRESS_WINDOW", "10")

        selector = _make_selector(state_dir)
        # Query for coding_interactive — must return empty set
        recent_ids = selector._query_recent_injected_ids("coding_interactive")

        assert item_id not in recent_ids, (
            "Item injected under a different task_class must not appear in recent_ids for current class"
        )

        candidate = _make_item(item_id, confidence=0.9, evidence_count=5)
        candidates = {"proven_pattern": [candidate], "failure_prevention": [], "recent_comparable": []}

        selected, suppressed = selector._select_standard_classes(candidates, recent_ids=recent_ids)

        selected_ids = [i.item_id for i in selected]
        assert item_id in selected_ids, (
            "Candidate must be selected when its injection was for a different task_class"
        )


# ---------------------------------------------------------------------------
# (d) Window size respects VNX_INTEL_SUPPRESS_WINDOW env var
# ---------------------------------------------------------------------------

class TestWindowSizeEnvVar:
    """VNX_INTEL_SUPPRESS_WINDOW controls how many recent dispatches are checked."""

    def test_window_zero_disables_suppression(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        _setup_coord_db(state_dir)

        item_id = "intel_sp_1"
        _insert_injection(state_dir, "dispatch-0", "coding_interactive", [item_id])

        monkeypatch.setenv("VNX_INTEL_SUPPRESS_WINDOW", "0")

        selector = _make_selector(state_dir)
        recent_ids = selector._query_recent_injected_ids("coding_interactive")

        assert len(recent_ids) == 0, (
            "Window=0 must disable suppression; recent_ids must be empty"
        )

    def test_window_one_captures_only_latest_dispatch(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        _setup_coord_db(state_dir)

        old_item = "intel_sp_old"
        new_item = "intel_sp_new"

        # Insert old dispatch first (earlier timestamp), then new
        _insert_injection(
            state_dir, "dispatch-old", "coding_interactive", [old_item],
            injected_at="2026-06-01T00:00:00Z",
        )
        _insert_injection(
            state_dir, "dispatch-new", "coding_interactive", [new_item],
            injected_at="2026-06-10T12:00:00Z",
        )

        monkeypatch.setenv("VNX_INTEL_SUPPRESS_WINDOW", "1")

        selector = _make_selector(state_dir)
        recent_ids = selector._query_recent_injected_ids("coding_interactive")

        assert new_item in recent_ids, "Most recent item must be in window=1 suppression set"
        assert old_item not in recent_ids, "Older dispatch must be outside window=1"

    def test_window_large_captures_all(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        _setup_coord_db(state_dir)

        item_ids = [f"intel_sp_{i}" for i in range(5)]
        for i, iid in enumerate(item_ids):
            _insert_injection(
                state_dir, f"dispatch-{i}", "coding_interactive", [iid],
                injected_at=f"2026-06-0{i+1}T00:00:00Z",
            )

        monkeypatch.setenv("VNX_INTEL_SUPPRESS_WINDOW", "100")

        selector = _make_selector(state_dir)
        recent_ids = selector._query_recent_injected_ids("coding_interactive")

        for iid in item_ids:
            assert iid in recent_ids, f"{iid} must be in suppression set with window=100"


# ---------------------------------------------------------------------------
# (e) Corrupt items_json row → skipped without crash
# ---------------------------------------------------------------------------

class TestCorruptItemsJsonRow:
    """Malformed items_json in the DB must not crash _query_recent_injected_ids."""

    def _insert_raw_injection(
        self,
        state_dir: Path,
        dispatch_id: str,
        task_class: str,
        items_json_raw: str,
    ) -> None:
        injection_id = str(uuid.uuid4())
        with get_connection(state_dir) as conn:
            conn.execute(
                """INSERT INTO intelligence_injections
                   (injection_id, dispatch_id, injection_point, task_class,
                    items_injected, items_suppressed, payload_chars,
                    items_json, suppressed_json)
                   VALUES (?, ?, ?, ?, 0, 0, 0, ?, '[]')""",
                (injection_id, dispatch_id, "dispatch_create", task_class, items_json_raw),
            )
            conn.commit()

    def test_corrupt_json_skipped(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        _setup_coord_db(state_dir)

        # Insert one corrupt row and one valid row
        self._insert_raw_injection(state_dir, "bad-dispatch", "coding_interactive", "NOT_VALID_JSON{{{")
        valid_id = "intel_sp_42"
        _insert_injection(state_dir, "good-dispatch", "coding_interactive", [valid_id])

        monkeypatch.setenv("VNX_INTEL_SUPPRESS_WINDOW", "10")

        selector = _make_selector(state_dir)
        # Must not raise
        recent_ids = selector._query_recent_injected_ids("coding_interactive")

        assert valid_id in recent_ids, "Valid row must be parsed correctly despite corrupt sibling"

    def test_non_list_json_skipped(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        _setup_coord_db(state_dir)

        # items_json is valid JSON but not a list
        self._insert_raw_injection(
            state_dir, "obj-dispatch", "coding_interactive", '{"item_id": "intel_sp_1"}'
        )
        valid_id = "intel_sp_99"
        _insert_injection(state_dir, "good-dispatch", "coding_interactive", [valid_id])

        monkeypatch.setenv("VNX_INTEL_SUPPRESS_WINDOW", "10")

        selector = _make_selector(state_dir)
        recent_ids = selector._query_recent_injected_ids("coding_interactive")

        assert valid_id in recent_ids, "Valid list row must be parsed even when another row is non-list JSON"
        # Non-list row must not contribute random keys
        assert "intel_sp_1" not in recent_ids, "Non-list JSON row must be skipped entirely"

    def test_missing_coord_db_returns_empty(self, tmp_path, monkeypatch):
        # state_dir exists but no DB file — must return frozenset() gracefully
        state_dir = tmp_path / "nonexistent_state"
        # Do not call _setup_coord_db — directory absent
        monkeypatch.setenv("VNX_INTEL_SUPPRESS_WINDOW", "10")

        selector = _make_selector(state_dir)
        recent_ids = selector._query_recent_injected_ids("coding_interactive")

        assert recent_ids == frozenset(), "Absent coord DB must return empty frozenset without raising"
