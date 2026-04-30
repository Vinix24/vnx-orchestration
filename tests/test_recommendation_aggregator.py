#!/usr/bin/env python3
"""Tests for recommendation_aggregator.py (ARC-4)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from dispatch_parameter_tracker import (
    DispatchOutcome,
    DispatchParameterTracker,
    DispatchParameters,
)
from recommendation_aggregator import (
    _CLUSTER_THRESHOLD,
    _cluster,
    _read_classifier_queue,
    _read_confidence_trends,
    _suggestions_from_classifier,
    _suggestions_from_f57,
    _suggestions_from_learning_loop,
    aggregate,
    write_recommendations,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path


def _seed_experiments(state_dir: Path, n: int = 25) -> None:
    tracker = DispatchParameterTracker(state_dir=state_dir)
    for i in range(n):
        tracker.capture_parameters(
            f"exp-{i:04d}",
            DispatchParameters(
                instruction_char_count=2500 if i % 2 == 0 else 1200,
                context_item_count=2,
                repo_map_symbol_count=10,
                role="backend-developer",
                cognition="high" if i % 2 == 0 else "medium",
                model="sonnet",
                terminal="T1",
                file_count=4,
            ),
        )
        tracker.capture_outcome(
            f"exp-{i:04d}",
            DispatchOutcome(
                cqs=80.0 + (i % 8),
                success=True,
                completion_minutes=5.0,
                test_count=3,
                committed=True,
                lines_changed=100,
            ),
        )


def _write_classifier_queue(state_dir: Path, receipts: list[dict]) -> None:
    queue_path = state_dir / "receipt_classifier_queue.ndjson"
    with queue_path.open("w", encoding="utf-8") as fh:
        for receipt in receipts:
            fh.write(json.dumps({"queued_at": 0.0, "receipt": receipt}))
            fh.write("\n")


def _write_pattern_usage(state_dir: Path, patterns: list[dict]) -> None:
    db_path = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT NOT NULL,
            pattern_hash TEXT NOT NULL DEFAULT '',
            used_count INTEGER DEFAULT 0,
            ignored_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            last_used TIMESTAMP,
            last_offered TIMESTAMP,
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for p in patterns:
        conn.execute(
            """
            INSERT OR REPLACE INTO pattern_usage
                (pattern_id, pattern_title, pattern_hash, confidence, failure_count, used_count)
            VALUES (?, ?, '', ?, ?, ?)
            """,
            (
                p["pattern_id"],
                p.get("pattern_title", p["pattern_id"]),
                p.get("confidence", 0.50),
                p.get("failure_count", 2),
                p.get("used_count", 0),
            ),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Case A: 3 sources contribute → t0_recommendations.json updated
# ---------------------------------------------------------------------------


def test_all_three_sources_contribute(state_dir):
    # Source 1: F57 (dispatch_experiments DB with >= 20 rows)
    _seed_experiments(state_dir, 25)

    # Source 2: classifier queue
    _write_classifier_queue(
        state_dir,
        [{"suggested_improvements": ["Increase instruction detail for complex tasks"]}],
    )

    # Source 3: learning loop low-confidence patterns
    _write_pattern_usage(
        state_dir,
        [{"pattern_id": "p001", "pattern_title": "context injection", "confidence": 0.70}],
    )

    output_path = state_dir / "t0_recommendations.json"
    suggestions = aggregate(state_dir=state_dir)
    write_recommendations(output_path, suggestions)

    assert output_path.is_file()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["total_suggestions"] >= 1
    assert len(payload["suggestions"]) >= 1


def test_all_three_sources_no_db_is_graceful(state_dir):
    # Only classifier queue; no DB for F57 or learning loop
    _write_classifier_queue(
        state_dir,
        [{"suggested_improvements": ["Run more integration tests"]}],
    )
    suggestions = aggregate(state_dir=state_dir)
    assert isinstance(suggestions, list)


# ---------------------------------------------------------------------------
# Case B: dedup — same suggestion across sources counts as N
# ---------------------------------------------------------------------------


def test_dedup_same_text_from_multiple_sources(state_dir):
    shared_text = "Increase instruction detail for complex tasks"
    raw = [
        {"target_file": "CLAUDE.md", "suggestion_text": shared_text, "source": "f57", "confidence": 0.70},
        {"target_file": "CLAUDE.md", "suggestion_text": shared_text, "source": "classifier", "confidence": 0.65},
        {"target_file": "CLAUDE.md", "suggestion_text": shared_text, "source": "learning_loop", "confidence": 0.60},
    ]
    clustered = _cluster(raw)

    assert len(clustered) == 1
    entry = clustered[0]
    assert entry["count"] == 3
    assert set(entry["source"]) == {"f57", "classifier", "learning_loop"}


def test_dedup_count_below_threshold_no_upgrade(state_dir):
    text = "Some unique suggestion"
    raw = [
        {"target_file": "CLAUDE.md", "suggestion_text": text, "source": "f57", "confidence": 0.70},
        {"target_file": "CLAUDE.md", "suggestion_text": text, "source": "classifier", "confidence": 0.65},
    ]
    clustered = _cluster(raw)
    assert len(clustered) == 1
    assert clustered[0]["count"] == 2
    # Confidence boosted by 0.05 per merge but no threshold upgrade
    assert clustered[0]["confidence"] < 0.85


def test_dedup_threshold_upgrade_applied(state_dir):
    text = "Upgrade this suggestion"
    # Seed exactly CLUSTER_THRESHOLD identical suggestions
    raw = [
        {"target_file": "CLAUDE.md", "suggestion_text": text, "source": f"src{i}", "confidence": 0.65}
        for i in range(_CLUSTER_THRESHOLD)
    ]
    clustered = _cluster(raw)
    assert clustered[0]["count"] == _CLUSTER_THRESHOLD
    # With threshold crossed, confidence should be higher than baseline
    assert clustered[0]["confidence"] > 0.65


def test_distinct_suggestions_not_merged(state_dir):
    raw = [
        {"target_file": "CLAUDE.md", "suggestion_text": "First suggestion", "source": "f57", "confidence": 0.70},
        {"target_file": "CLAUDE.md", "suggestion_text": "Second suggestion", "source": "f57", "confidence": 0.70},
    ]
    clustered = _cluster(raw)
    assert len(clustered) == 2


# ---------------------------------------------------------------------------
# Case C: writes structured JSON with target_file, suggestion_text, confidence, source
# ---------------------------------------------------------------------------


def test_output_json_schema(state_dir):
    _seed_experiments(state_dir, 25)
    _write_classifier_queue(
        state_dir,
        [{"recommended_improvements": ["Improve test coverage"]}],
    )
    output_path = state_dir / "t0_recommendations.json"
    suggestions = aggregate(state_dir=state_dir)
    write_recommendations(output_path, suggestions)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert "generated_at" in payload
    assert "aggregator_version" in payload
    assert "total_suggestions" in payload
    assert isinstance(payload["suggestions"], list)

    for s in payload["suggestions"]:
        assert "target_file" in s, f"missing target_file in {s}"
        assert "suggestion_text" in s, f"missing suggestion_text in {s}"
        assert "confidence" in s, f"missing confidence in {s}"
        assert "source" in s, f"missing source in {s}"
        assert isinstance(s["source"], list)
        assert isinstance(s["confidence"], float)
        assert 0.0 <= s["confidence"] <= 1.0


def test_output_replaces_existing_file(state_dir):
    output_path = state_dir / "t0_recommendations.json"
    output_path.write_text(json.dumps({"stale": True}), encoding="utf-8")

    suggestions = aggregate(state_dir=state_dir)
    write_recommendations(output_path, suggestions)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert "stale" not in payload
    assert "generated_at" in payload


def test_read_classifier_queue_nonexistent_returns_empty(state_dir):
    assert _read_classifier_queue(state_dir) == []


def test_read_confidence_trends_no_db_returns_empty(state_dir):
    assert _read_confidence_trends(state_dir) == []


def test_read_confidence_trends_filters_low_confidence(state_dir):
    _write_pattern_usage(
        state_dir,
        [
            {"pattern_id": "low", "pattern_title": "low pattern", "confidence": 0.70},
            {"pattern_id": "high", "pattern_title": "high pattern", "confidence": 0.99},
        ],
    )
    patterns = _read_confidence_trends(state_dir)
    ids = [p["pattern_id"] for p in patterns]
    assert "low" in ids
    assert "high" not in ids
