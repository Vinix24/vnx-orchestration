"""Tests for intelligence_aggregator.py — Wave 5 PR-5.4."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.lib.intelligence_aggregator import (
    GlobalPattern,
    IntelligenceAggregator,
    CrossProjectRecommendation,
    normalize_family_key,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_quality_db(path: Path, project_id: str, patterns: list[dict]) -> Path:
    """Create a minimal quality_intelligence.db with success_patterns rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            pattern_data TEXT NOT NULL,
            confidence_score REAL DEFAULT 0.0,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT,
            first_seen TEXT,
            last_used TEXT,
            project_id TEXT
        );
    """)
    for p in patterns:
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, "
            " confidence_score, usage_count, first_seen, last_used, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p.get("pattern_type", "approach"),
                p.get("category", "governance"),
                p["title"],
                p.get("description", p["title"]),
                json.dumps({}),
                p.get("confidence_score", 0.7),
                p.get("usage_count", 1),
                p.get("first_seen", "2026-01-01T00:00:00+00:00"),
                p.get("last_used", "2026-05-01T00:00:00+00:00"),
                project_id,
            ),
        )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# normalize_family_key
# ---------------------------------------------------------------------------


def test_normalize_strips_paths():
    raw = "gate passed for /Users/vvd/Development/project/scripts/lib/foo.py"
    family = normalize_family_key(raw)
    assert "/Users" not in family
    assert "<path>" in family


def test_normalize_strips_dispatch_ids():
    raw = "dispatch 20260516-wave5-pr4-foo completed successfully"
    family = normalize_family_key(raw)
    assert "20260516" not in family
    assert "<dispatch_id>" in family


# ---------------------------------------------------------------------------
# mine_global_patterns
# ---------------------------------------------------------------------------


def test_mine_global_patterns_finds_cross_project(tmp_path):
    shared_title = "gate passed for pr review"
    db_a = _make_quality_db(
        tmp_path / "proj-a" / "quality_intelligence.db",
        "proj-a",
        [
            {"title": shared_title, "usage_count": 3, "confidence_score": 0.8},
        ],
    )
    db_b = _make_quality_db(
        tmp_path / "proj-b" / "quality_intelligence.db",
        "proj-b",
        [
            {"title": shared_title, "usage_count": 3, "confidence_score": 0.75},
        ],
    )
    agg = IntelligenceAggregator({"proj-a": db_a, "proj-b": db_b})
    patterns = agg.mine_global_patterns(min_projects=2, min_confidence=0.0)

    assert len(patterns) == 1
    p = patterns[0]
    assert isinstance(p, GlobalPattern)
    assert "proj-a" in p.occurrences
    assert "proj-b" in p.occurrences
    assert p.total_occurrences == 6  # 3+3


def test_mine_respects_min_projects(tmp_path):
    shared_title = "atomic write pattern via tmp rename"
    db_a = _make_quality_db(
        tmp_path / "proj-a" / "quality_intelligence.db",
        "proj-a",
        [{"title": shared_title, "usage_count": 5, "confidence_score": 0.9}],
    )
    # proj-b has a DIFFERENT pattern — shared_title only in proj-a
    db_b = _make_quality_db(
        tmp_path / "proj-b" / "quality_intelligence.db",
        "proj-b",
        [{"title": "something completely different", "usage_count": 2, "confidence_score": 0.8}],
    )
    agg = IntelligenceAggregator({"proj-a": db_a, "proj-b": db_b})
    patterns = agg.mine_global_patterns(min_projects=2, min_confidence=0.0)
    families = [p.pattern_family for p in patterns]
    # shared_title only in 1 project → must be excluded with min_projects=2
    norm = normalize_family_key(shared_title)
    assert norm not in families


def test_mine_respects_min_confidence(tmp_path):
    shared_title = "low confidence pattern across projects"
    db_a = _make_quality_db(
        tmp_path / "proj-a" / "quality_intelligence.db",
        "proj-a",
        [{"title": shared_title, "usage_count": 2, "confidence_score": 0.3}],
    )
    db_b = _make_quality_db(
        tmp_path / "proj-b" / "quality_intelligence.db",
        "proj-b",
        [{"title": shared_title, "usage_count": 2, "confidence_score": 0.3}],
    )
    agg = IntelligenceAggregator({"proj-a": db_a, "proj-b": db_b})
    # With min_confidence=0.6, avg_conf=0.3 must be excluded.
    patterns = agg.mine_global_patterns(min_projects=2, min_confidence=0.6)
    assert len(patterns) == 0


# ---------------------------------------------------------------------------
# recommend_cross_project
# ---------------------------------------------------------------------------


def test_recommend_cross_project_matches_context(tmp_path):
    proven_title = "use beta laplace smoothing for confidence updates"
    db_a = _make_quality_db(
        tmp_path / "proj-a" / "quality_intelligence.db",
        "proj-a",
        [{"title": proven_title, "usage_count": 10, "confidence_score": 0.85}],
    )
    # proj-b does not have this pattern yet
    db_b = _make_quality_db(
        tmp_path / "proj-b" / "quality_intelligence.db",
        "proj-b",
        [{"title": "unrelated gate check", "usage_count": 1, "confidence_score": 0.7}],
    )
    agg = IntelligenceAggregator({"proj-a": db_a, "proj-b": db_b})
    recs = agg.recommend_cross_project("proj-b", max_recommendations=5)

    assert len(recs) >= 1
    rec = recs[0]
    assert isinstance(rec, CrossProjectRecommendation)
    assert rec.source_project == "proj-a"
    assert rec.target_project == "proj-b"
    # Cross-project confidence must be decayed (0.7x)
    assert rec.confidence < 0.85


def test_recommend_unknown_target_returns_empty(tmp_path):
    db_a = _make_quality_db(
        tmp_path / "proj-a" / "quality_intelligence.db",
        "proj-a",
        [{"title": "some pattern", "confidence_score": 0.8}],
    )
    agg = IntelligenceAggregator({"proj-a": db_a})
    recs = agg.recommend_cross_project("nonexistent", max_recommendations=5)
    assert recs == []


# ---------------------------------------------------------------------------
# aggregate_recurrence
# ---------------------------------------------------------------------------


def test_aggregate_recurrence_normalized_family(tmp_path):
    # Both projects have patterns that normalize to the same family key.
    title_with_path = "gate passed for /Users/vvd/project/scripts/check.py"
    title_clean = "gate passed for <path>"  # what normalize_family_key produces

    db_a = _make_quality_db(
        tmp_path / "proj-a" / "quality_intelligence.db",
        "proj-a",
        [{"title": title_with_path, "usage_count": 4, "confidence_score": 0.7}],
    )
    db_b = _make_quality_db(
        tmp_path / "proj-b" / "quality_intelligence.db",
        "proj-b",
        [{"title": title_with_path, "usage_count": 2, "confidence_score": 0.7}],
    )
    agg = IntelligenceAggregator({"proj-a": db_a, "proj-b": db_b})
    counts = agg.aggregate_recurrence(title_with_path)

    assert counts.get("proj-a") == 4
    assert counts.get("proj-b") == 2
    # Searching by the already-normalized key should also work.
    counts2 = agg.aggregate_recurrence(title_clean)
    assert counts2.get("proj-a") == 4


# ---------------------------------------------------------------------------
# export_global_facet
# ---------------------------------------------------------------------------


def test_export_global_facet_json(tmp_path):
    shared_title = "atomic dispatch write via pending folder"
    db_a = _make_quality_db(
        tmp_path / "proj-a" / "quality_intelligence.db",
        "proj-a",
        [{"title": shared_title, "usage_count": 3, "confidence_score": 0.75}],
    )
    db_b = _make_quality_db(
        tmp_path / "proj-b" / "quality_intelligence.db",
        "proj-b",
        [{"title": shared_title, "usage_count": 2, "confidence_score": 0.8}],
    )
    agg = IntelligenceAggregator({"proj-a": db_a, "proj-b": db_b})
    out = tmp_path / "output" / "global_facet.json"
    agg.export_global_facet(out)

    assert out.exists(), "Output file must be created"
    data = json.loads(out.read_text())

    assert "generated_at" in data
    assert "patterns" in data
    assert isinstance(data["patterns"], list)
    assert data["pattern_count"] == len(data["patterns"])

    # Verify schema matches GlobalPattern structure
    if data["patterns"]:
        p = data["patterns"][0]
        for key in ("pattern_id", "pattern_family", "occurrences_per_project",
                    "total_occurrences", "avg_confidence"):
            assert key in p, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# Privacy: no raw project-specific strings in global facet
# ---------------------------------------------------------------------------


def test_privacy_no_raw_strings_in_global(tmp_path):
    sensitive_path = "/Users/vvd/Development/seocrawler/scripts/secret.py"
    title_with_path = f"gate passed for {sensitive_path}"
    sensitive_dispatch = "20260516-wave5-pr4-foo"
    title_with_dispatch = f"dispatch {sensitive_dispatch} succeeded"

    db_a = _make_quality_db(
        tmp_path / "proj-a" / "quality_intelligence.db",
        "proj-a",
        [
            {"title": title_with_path, "usage_count": 3, "confidence_score": 0.8},
            {"title": title_with_dispatch, "usage_count": 2, "confidence_score": 0.7},
        ],
    )
    db_b = _make_quality_db(
        tmp_path / "proj-b" / "quality_intelligence.db",
        "proj-b",
        [
            {"title": title_with_path, "usage_count": 2, "confidence_score": 0.75},
            {"title": title_with_dispatch, "usage_count": 1, "confidence_score": 0.65},
        ],
    )
    agg = IntelligenceAggregator({"proj-a": db_a, "proj-b": db_b})
    out = tmp_path / "global_facet.json"
    agg.export_global_facet(out)

    data = json.loads(out.read_text())
    facet_str = json.dumps(data)

    # Absolute paths must not appear in the global facet.
    assert sensitive_path not in facet_str, (
        f"Privacy violation: absolute path {sensitive_path!r} leaked into global facet"
    )
    # Project-specific dispatch IDs (date-prefixed tokens) must not appear.
    assert sensitive_dispatch not in facet_str, (
        f"Privacy violation: dispatch ID {sensitive_dispatch!r} leaked into global facet"
    )
