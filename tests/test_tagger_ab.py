"""Tests for the tagger A/B harness (D4 — tagger as optional precision).

Covers:
- selector _relevance_score works with tagger OFF (no hard dependency on LLM tags)
- derive_tags is the sole tag source when tagger disabled
- _sample_patterns: DB missing, table missing, normal case
- _run_ab_comparison: both arms, provider-unavailable case, rescue rate
- _cmd_tagger_ab: no-DB path, seeded-sample with mocked LLM
- Decision criterion thresholds in the report output
- VNX_TAGGER_ENABLED default is OFF (no accidental default-on flip)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# Selector works without the tagger (no hard dependency)
# ---------------------------------------------------------------------------

def test_relevance_score_positive_without_tagger(monkeypatch):
    """_relevance_score returns > 0 via derive_tags when VNX_TAGGER_ENABLED is unset."""
    monkeypatch.delenv("VNX_TAGGER_ENABLED", raising=False)

    from intelligence_selector import _relevance_score

    class _Item:
        title = "fix dispatch lane bug"
        content = ""
        confidence = 0.8
        scope_tags = None  # no stored tags — tagger was never on
        item_class = "proven_pattern"
        last_seen = None

    query_scope = ["dispatch", "fix_bug"]
    score = _relevance_score(_Item(), query_scope)

    # derive_tags("fix dispatch lane bug") finds ["dispatch", "fix_bug"] → overlap 2
    # score = 0.8 * (1 + 2) * 1.0 * 1.2 = 2.88
    assert score > 0
    assert score > 0.8  # overlap must have factored in (score > confidence baseline)


def test_relevance_score_equals_derive_tags_score_when_tagger_disabled(monkeypatch):
    """With tagger disabled, _relevance_score is identical to using only derive_tags."""
    monkeypatch.delenv("VNX_TAGGER_ENABLED", raising=False)

    from intelligence_selector import _relevance_score, _CLASS_WEIGHT, _recency_decay
    from vnx_tag_vocabulary import derive_tags

    class _Item:
        title = "receipt audit ndjson provenance"
        content = ""
        confidence = 0.7
        scope_tags = None
        item_class = "proven_pattern"
        last_seen = None

    query_scope = ["receipts_audit", "fix_bug"]

    det_tags = set(derive_tags(f"{_Item.title} {_Item.content}"))
    expected_overlap = len(det_tags & set(query_scope))
    weight = _CLASS_WEIGHT.get("proven_pattern", 1.0)
    recency = _recency_decay(None)
    expected_score = 0.7 * (1.0 + expected_overlap) * recency * weight

    actual = _relevance_score(_Item(), query_scope)
    assert abs(actual - expected_score) < 0.001


def test_stored_scope_tags_add_on_top_of_derive_tags(monkeypatch):
    """Items with stored scope_tags (from tagger persist) get higher overlap than
    those with only derive_tags, proving stored tags complement the deterministic floor."""
    monkeypatch.delenv("VNX_TAGGER_ENABLED", raising=False)

    from intelligence_selector import _relevance_score

    class _ItemNoStored:
        title = "wire the integration hook"
        content = ""
        confidence = 0.6
        scope_tags = None
        item_class = "proven_pattern"
        last_seen = None

    class _ItemWithStored:
        title = "wire the integration hook"
        content = ""
        confidence = 0.6
        scope_tags = ["providers_routing", "wire_integration"]  # stored from tagger
        item_class = "proven_pattern"
        last_seen = None

    query_scope = ["providers_routing", "wire_integration", "intelligence"]
    score_without = _relevance_score(_ItemNoStored(), query_scope)
    score_with = _relevance_score(_ItemWithStored(), query_scope)

    # With stored tags, overlap is higher → score must be higher
    assert score_with >= score_without


# ---------------------------------------------------------------------------
# derive_tags no-dependency check (VNX_TAGGER_ENABLED default is OFF)
# ---------------------------------------------------------------------------

def test_tagger_enabled_default_is_off():
    """VNX_TAGGER_ENABLED must default to disabled — no silent default-on flip."""
    env_backup = os.environ.pop("VNX_TAGGER_ENABLED", None)
    try:
        import vnx_tagger
        assert vnx_tagger.is_enabled() is False
    finally:
        if env_backup is not None:
            os.environ["VNX_TAGGER_ENABLED"] = env_backup


def test_derive_tags_path_active_when_tagger_off(monkeypatch):
    """Selector uses derive_tags and returns non-empty tags even when tagger is disabled."""
    monkeypatch.delenv("VNX_TAGGER_ENABLED", raising=False)

    from vnx_tag_vocabulary import derive_tags
    tags = derive_tags("dispatch lane bug fix")
    assert "dispatch" in tags
    assert "fix_bug" in tags


# ---------------------------------------------------------------------------
# _sample_patterns
# ---------------------------------------------------------------------------

def test_sample_patterns_missing_db(tmp_path):
    from vnx_cli.commands.learning import _sample_patterns
    result = _sample_patterns(tmp_path / "nonexistent.db", n=10)
    assert result == []


def test_sample_patterns_missing_table(tmp_path):
    db = tmp_path / "qi.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
    conn.commit(); conn.close()

    from vnx_cli.commands.learning import _sample_patterns
    result = _sample_patterns(db, n=10)
    assert result == []


def test_sample_patterns_returns_seeded_sample(tmp_path):
    db = tmp_path / "qi.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE success_patterns "
        "(id INTEGER PRIMARY KEY, title TEXT, description TEXT, tags TEXT)"
    )
    for i in range(30):
        conn.execute(
            "INSERT INTO success_patterns(id,title,description) VALUES (?,?,?)",
            (i + 1, f"pattern {i}", f"desc {i}"),
        )
    conn.commit(); conn.close()

    from vnx_cli.commands.learning import _sample_patterns

    # Returns at most n patterns
    result = _sample_patterns(db, n=10, seed=42)
    assert len(result) == 10

    # Deterministic: same seed → same order
    result2 = _sample_patterns(db, n=10, seed=42)
    assert [r["id"] for r in result] == [r["id"] for r in result2]

    # Different seed → different (possibly) order
    result3 = _sample_patterns(db, n=10, seed=99)
    # (May coincidentally match if tiny sample; just assert no crash + right size)
    assert len(result3) == 10


def test_sample_patterns_fewer_than_n_available(tmp_path):
    db = tmp_path / "qi.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE success_patterns (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO success_patterns(id,title) VALUES (1,'only one')")
    conn.commit(); conn.close()

    from vnx_cli.commands.learning import _sample_patterns
    result = _sample_patterns(db, n=20, seed=42)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# _run_ab_comparison
# ---------------------------------------------------------------------------

_FAKE_PATTERNS = [
    {"id": 1, "title": "fix dispatch lane regression", "description": "", "stored_tags": ""},
    {"id": 2, "title": "intelligence selector inject", "description": "", "stored_tags": ""},
    {"id": 3, "title": "governance gate phantom receipt", "description": "", "stored_tags": ""},
    {"id": 4, "title": "no matching keywords here xyz", "description": "", "stored_tags": ""},
]


def test_run_ab_comparison_provider_unavailable(monkeypatch):
    """When provider is unavailable, LLM arm returns empty tags and zero cost."""
    from vnx_cli.commands.learning import _run_ab_comparison

    result = _run_ab_comparison(_FAKE_PATTERNS, provider_available=False)

    assert result["n_sampled"] == 4
    assert result["provider_available"] is False
    assert result["total_cost_usd"] == 0.0
    assert result["cost_per_pattern_usd"] == 0.0
    assert result["rescued"] == 0  # no LLM → no new tags
    # WITH == WITHOUT when provider unavailable
    assert result["avg_overlap_with"] == result["avg_overlap_without"]


def test_run_ab_comparison_llm_adds_new_tags(monkeypatch):
    """When LLM arm adds new tags, rescue_rate and overlap improve."""
    import vnx_tagger as _tagger

    # Mock: LLM adds "harden" for all patterns
    monkeypatch.setattr(
        _tagger, "_llm_tags_with_cost",
        lambda text, paths=None, enabled_override=False: (["harden"], 0.00005),
    )

    from vnx_cli.commands.learning import _run_ab_comparison

    patterns = [
        {"id": 1, "title": "guard boundary harden edge", "description": "", "stored_tags": ""},
        {"id": 2, "title": "no keywords", "description": "", "stored_tags": ""},
    ]

    result = _run_ab_comparison(patterns, provider_available=True)

    assert result["provider_available"] is True
    assert result["total_cost_usd"] == pytest.approx(0.0001, abs=1e-8)  # 2 × 0.00005
    # "harden" is in _AB_QUERY_SCOPES (governance_gates + harden), so at least
    # one scope gains overlap when "harden" is added.
    assert result["avg_overlap_with"] >= result["avg_overlap_without"]


def test_run_ab_comparison_rescue_rate(monkeypatch):
    """rescue_rate is the fraction of patterns where LLM added at least one new tag."""
    import vnx_tagger as _tagger

    call_count = [0]

    def _mock_llm(text, paths=None, enabled_override=False):
        call_count[0] += 1
        # Add a new tag only for the first pattern
        if call_count[0] == 1:
            return ["harden"], 0.00005
        return [], 0.0

    monkeypatch.setattr(_tagger, "_llm_tags_with_cost", _mock_llm)

    from vnx_cli.commands.learning import _run_ab_comparison

    patterns = [
        {"id": 1, "title": "simple text no det keywords", "description": "", "stored_tags": ""},
        {"id": 2, "title": "also plain text", "description": "", "stored_tags": ""},
    ]

    result = _run_ab_comparison(patterns, provider_available=True)

    assert result["rescued"] == 1
    assert result["rescue_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# _cmd_tagger_ab integration
# ---------------------------------------------------------------------------

def test_cmd_tagger_ab_no_db(tmp_path, capsys):
    """_cmd_tagger_ab exits 1 with a helpful message when QI DB is absent."""
    import vnx_paths

    # Point the data root at a temp dir that has a state/ subdirectory but no DB
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    with patch("vnx_cli._engine.resolve_data_root", return_value=tmp_path):
        args = argparse.Namespace(project_dir=".", sample=5, seed=42)
        from vnx_cli.commands.learning import _cmd_tagger_ab
        rc = _cmd_tagger_ab(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "no patterns" in captured.err or "quality_intelligence" in captured.err


def _make_qi_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE success_patterns "
        "(id INTEGER PRIMARY KEY, title TEXT, description TEXT, tags TEXT)"
    )
    for i in range(10):
        conn.execute(
            "INSERT INTO success_patterns(id,title,description) VALUES (?,?,?)",
            (i + 1, f"fix dispatch lane pattern {i}", f"desc {i}"),
        )
    conn.commit()
    conn.close()


def test_cmd_tagger_ab_provider_unavailable(tmp_path, capsys, monkeypatch):
    """_cmd_tagger_ab exits 0 and prints the WITHOUT arm when provider is unavailable."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    _make_qi_db(state_dir / "quality_intelligence.db")

    import vnx_tagger as _tagger
    monkeypatch.setattr(_tagger, "get_tagger_provider_name", lambda: "deepseek")

    # Make provider unavailable
    from classifier_providers.base import ClassifierResult
    class _FakeProv:
        def is_available(self): return False

    monkeypatch.setattr("classifier_providers.get_provider", lambda name=None: _FakeProv())

    with patch("vnx_cli._engine.resolve_data_root", return_value=tmp_path):
        args = argparse.Namespace(project_dir=".", sample=5, seed=42)
        from vnx_cli.commands.learning import _cmd_tagger_ab
        rc = _cmd_tagger_ab(args)

    assert rc == 0
    captured = capsys.readouterr()
    # Should note provider unavailability
    assert "not available" in captured.err or "unavailable" in captured.err.lower()
    # Output should contain the report header
    assert "A/B" in captured.out or "tagger" in captured.out.lower()


def test_cmd_tagger_ab_with_mocked_llm(tmp_path, capsys, monkeypatch):
    """_cmd_tagger_ab exits 0 and prints precision-lift + cost when LLM arm works."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    _make_qi_db(state_dir / "quality_intelligence.db")

    import vnx_tagger as _tagger
    monkeypatch.setattr(_tagger, "get_tagger_provider_name", lambda: "deepseek")

    # Provider available
    class _FakeProv:
        def is_available(self): return True

    monkeypatch.setattr("classifier_providers.get_provider", lambda name=None: _FakeProv())

    # LLM adds "harden" with a small cost
    monkeypatch.setattr(
        _tagger, "_llm_tags_with_cost",
        lambda text, paths=None, enabled_override=False: (["harden"], 0.0001),
    )

    with patch("vnx_cli._engine.resolve_data_root", return_value=tmp_path):
        args = argparse.Namespace(project_dir=".", sample=5, seed=42)
        from vnx_cli.commands.learning import _cmd_tagger_ab
        rc = _cmd_tagger_ab(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "A/B" in out or "overlap" in out.lower()
    assert "rescue" in out.lower()
    assert "cost" in out.lower()
    # Decision criterion must appear
    assert "ENABLE" in out or "HOLD OFF" in out


def test_cmd_tagger_ab_decision_criterion_appears(tmp_path, capsys, monkeypatch):
    """Report always prints the default-on decision criterion regardless of verdict."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    _make_qi_db(state_dir / "quality_intelligence.db")

    import vnx_tagger as _tagger
    monkeypatch.setattr(_tagger, "get_tagger_provider_name", lambda: "deepseek")

    class _FakeProv:
        def is_available(self): return True

    monkeypatch.setattr("classifier_providers.get_provider", lambda name=None: _FakeProv())

    # Zero cost + zero new tags → HOLD OFF
    monkeypatch.setattr(
        _tagger, "_llm_tags_with_cost",
        lambda text, paths=None, enabled_override=False: ([], 0.0),
    )

    with patch("vnx_cli._engine.resolve_data_root", return_value=tmp_path):
        args = argparse.Namespace(project_dir=".", sample=5, seed=42)
        from vnx_cli.commands.learning import _cmd_tagger_ab
        _cmd_tagger_ab(args)

    out = capsys.readouterr().out
    assert "20%" in out or "0.001" in out  # thresholds are printed


def test_tagger_enabled_flag_not_set_by_ab(tmp_path, monkeypatch):
    """tagger-ab must NOT set VNX_TAGGER_ENABLED=1 as a side effect."""
    monkeypatch.delenv("VNX_TAGGER_ENABLED", raising=False)

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    _make_qi_db(state_dir / "quality_intelligence.db")

    import vnx_tagger as _tagger
    monkeypatch.setattr(_tagger, "get_tagger_provider_name", lambda: "deepseek")

    class _FakeProv:
        def is_available(self): return True

    monkeypatch.setattr("classifier_providers.get_provider", lambda name=None: _FakeProv())
    monkeypatch.setattr(
        _tagger, "_llm_tags_with_cost",
        lambda text, paths=None, enabled_override=False: (["harden"], 0.0001),
    )

    with patch("vnx_cli._engine.resolve_data_root", return_value=tmp_path):
        args = argparse.Namespace(project_dir=".", sample=5, seed=42)
        from vnx_cli.commands.learning import _cmd_tagger_ab
        _cmd_tagger_ab(args)

    # VNX_TAGGER_ENABLED must still be absent after the command
    assert os.environ.get("VNX_TAGGER_ENABLED") is None


# ---------------------------------------------------------------------------
# tag_overlap helper
# ---------------------------------------------------------------------------

def test_tag_overlap_empty():
    from vnx_cli.commands.learning import _tag_overlap
    assert _tag_overlap(set(), ["dispatch", "fix_bug"]) == 0


def test_tag_overlap_partial():
    from vnx_cli.commands.learning import _tag_overlap
    assert _tag_overlap({"dispatch", "harden"}, ["dispatch", "fix_bug"]) == 1


def test_tag_overlap_full():
    from vnx_cli.commands.learning import _tag_overlap
    assert _tag_overlap({"dispatch", "fix_bug"}, ["dispatch", "fix_bug"]) == 2
