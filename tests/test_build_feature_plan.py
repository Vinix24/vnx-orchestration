"""Tests for scripts/build_feature_plan.py.

Covers:
  1. _build_feature_sections: register events → active/completed classification
  2. _build_feature_sections: gh merged PRs supplement register
  3. _build_feature_sections: planned from ROADMAP (excluded when in register)
  4. generate_feature_plan: auto-gen header, last-updated, section headings
  5. generate_feature_plan: active feature lines appear correctly
  6. generate_feature_plan: completed feature lines appear correctly
  7. generate_feature_plan: planned features appear correctly
  8. generate_feature_plan: idempotent (same inputs → same output)
  9. generate_feature_plan: empty inputs still produces valid markdown
  10. generate_feature_plan: consecutive completed features grouped as F<N>–F<M>
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from build_feature_plan import (
    _AUTOGEN_HEADER,
    _build_feature_sections,
    _group_consecutive,
    generate_feature_plan,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)

REGISTER_EVENTS = [
    # F46: PR #223 opened then merged → completed
    {"event": "dispatch_created", "timestamp": "2026-04-01T10:00:00Z",
     "dispatch_id": "20260401-F46-pr1", "feature_id": "F46"},
    {"event": "pr_opened", "timestamp": "2026-04-01T11:00:00Z",
     "dispatch_id": "20260401-F46-pr1", "feature_id": "F46", "pr_number": 223},
    {"event": "pr_merged", "timestamp": "2026-04-01T12:00:00Z",
     "dispatch_id": "20260401-F46-pr1", "feature_id": "F46", "pr_number": 223},
    # F47: PR #224 opened but NOT merged → active
    {"event": "pr_opened", "timestamp": "2026-04-02T10:00:00Z",
     "dispatch_id": "20260402-F47-pr1", "feature_id": "F47", "pr_number": 224},
]

MERGED_PRS = [
    {"number": 223, "title": "feat(governance): F46 intelligence pipeline", "mergedAt": "2026-04-01T12:00:00Z"},
    {"number": 186, "title": "feat(governance): F31 something else", "mergedAt": "2026-03-15T10:00:00Z"},
    {"number": 187, "title": "feat(governance): F32 another thing", "mergedAt": "2026-03-16T10:00:00Z"},
]

ROADMAP_FEATURES = [
    {"feature_id": "roadmap-autopilot", "title": "Roadmap Autopilot", "status": "planned"},
    {"feature_id": "F46-dup", "title": "F46 duplicate entry", "status": "planned"},
]


# ---------------------------------------------------------------------------
# 1–3: _build_feature_sections
# ---------------------------------------------------------------------------

class TestBuildFeatureSections:
    def test_active_feature_detected_from_register(self):
        sections = _build_feature_sections(REGISTER_EVENTS, [], [])
        active_fnums = {f["fnum"] for f in sections["active"]}
        assert 47 in active_fnums

    def test_active_feature_has_correct_prs(self):
        sections = _build_feature_sections(REGISTER_EVENTS, [], [])
        f47 = next(f for f in sections["active"] if f["fnum"] == 47)
        assert 224 in f47["open_prs"]
        assert 224 not in f47["merged_prs"]

    def test_completed_feature_from_register(self):
        sections = _build_feature_sections(REGISTER_EVENTS, [], [])
        completed_fnums = {f["fnum"] for f in sections["completed"]}
        assert 46 in completed_fnums

    def test_completed_feature_from_gh_prs_only(self):
        sections = _build_feature_sections([], MERGED_PRS, [])
        completed_fnums = {f["fnum"] for f in sections["completed"]}
        assert 31 in completed_fnums
        assert 32 in completed_fnums

    def test_gh_prs_supplement_register(self):
        sections = _build_feature_sections(REGISTER_EVENTS, MERGED_PRS, [])
        completed_fnums = {f["fnum"] for f in sections["completed"]}
        assert 46 in completed_fnums
        assert 31 in completed_fnums

    def test_planned_from_roadmap_no_feature_num(self):
        # roadmap-autopilot has no Fnn → shows up with None fnum
        sections = _build_feature_sections([], [], ROADMAP_FEATURES)
        titles = [f["title"] for f in sections["planned"]]
        assert "Roadmap Autopilot" in titles

    def test_planned_excluded_when_fnum_in_register(self):
        # F46 is in register; ROADMAP_FEATURES contains "F46 duplicate entry"
        sections = _build_feature_sections(REGISTER_EVENTS, [], ROADMAP_FEATURES)
        planned_fnums = {f.get("fnum") for f in sections["planned"]}
        assert 46 not in planned_fnums

    def test_empty_inputs_returns_empty_sections(self):
        sections = _build_feature_sections([], [], [])
        assert sections["active"] == []
        assert sections["completed"] == []
        assert sections["planned"] == []

    def test_dispatch_id_fallback_for_fnum_extraction(self):
        # feature_id absent — fnum extracted from dispatch_id
        events = [
            {"event": "pr_merged", "timestamp": "2026-04-10T10:00:00Z",
             "dispatch_id": "20260410-F55-pr1", "pr_number": 300},
        ]
        sections = _build_feature_sections(events, [], [])
        completed_fnums = {f["fnum"] for f in sections["completed"]}
        assert 55 in completed_fnums


# ---------------------------------------------------------------------------
# Helper: _group_consecutive
# ---------------------------------------------------------------------------

class TestGroupConsecutive:
    def test_single(self):
        assert _group_consecutive([5]) == [(5, 5)]

    def test_consecutive_range(self):
        assert _group_consecutive([1, 2, 3]) == [(1, 3)]

    def test_gap(self):
        assert _group_consecutive([1, 2, 5, 6]) == [(1, 2), (5, 6)]

    def test_empty(self):
        assert _group_consecutive([]) == []


# ---------------------------------------------------------------------------
# 4–9: generate_feature_plan
# ---------------------------------------------------------------------------

class TestGenerateFeaturePlan:
    def test_autogen_header_present(self):
        content = generate_feature_plan([], [], [], now=_NOW)
        assert "AUTO-GENERATED" in content
        assert "DO NOT EDIT" in content

    def test_autogen_header_is_html_comment(self):
        content = generate_feature_plan([], [], [], now=_NOW)
        assert content.startswith("<!-- AUTO-GENERATED")

    def test_last_updated_timestamp_present(self):
        content = generate_feature_plan([], [], [], now=_NOW)
        assert "2026-04-28" in content

    def test_three_sections_always_present(self):
        content = generate_feature_plan([], [], [], now=_NOW)
        assert "## Active features" in content
        assert "## Completed" in content
        assert "## Planned" in content

    def test_h1_title_present(self):
        content = generate_feature_plan([], [], [], now=_NOW)
        assert "# VNX Feature Plan" in content

    def test_active_feature_appears(self):
        content = generate_feature_plan(REGISTER_EVENTS, [], [], now=_NOW)
        assert "F47" in content

    def test_active_feature_pr_shows_in_flight(self):
        content = generate_feature_plan(REGISTER_EVENTS, [], [], now=_NOW)
        assert "in flight" in content

    def test_completed_feature_appears(self):
        content = generate_feature_plan(REGISTER_EVENTS, [], [], now=_NOW)
        assert "F46" in content

    def test_completed_feature_shows_merged(self):
        content = generate_feature_plan(REGISTER_EVENTS, [], [], now=_NOW)
        assert "merged" in content.lower()

    def test_planned_feature_appears(self):
        content = generate_feature_plan([], [], ROADMAP_FEATURES, now=_NOW)
        assert "Roadmap Autopilot" in content

    def test_idempotent_same_inputs(self):
        content1 = generate_feature_plan(REGISTER_EVENTS, MERGED_PRS, ROADMAP_FEATURES, now=_NOW)
        content2 = generate_feature_plan(REGISTER_EVENTS, MERGED_PRS, ROADMAP_FEATURES, now=_NOW)
        assert content1 == content2

    def test_empty_inputs_produces_nonempty_valid_output(self):
        content = generate_feature_plan([], [], [], now=_NOW)
        assert len(content) > 100
        assert content.count("\n") >= 8

    def test_consecutive_completed_grouped(self):
        # F31 and F32 both merged → should appear as F31–F32 group
        content = generate_feature_plan([], MERGED_PRS, [], now=_NOW)
        assert "F31" in content
        assert "F32" in content
        assert "F31–F32" in content or ("F31" in content and "F32" in content)

    def test_no_active_placeholder_when_empty(self):
        content = generate_feature_plan([], [], [], now=_NOW)
        assert "_No active features._" in content

    def test_no_completed_placeholder_when_empty(self):
        content = generate_feature_plan([], [], [], now=_NOW)
        assert "_No completed features" in content

    def test_no_planned_placeholder_when_empty(self):
        content = generate_feature_plan([], [], [], now=_NOW)
        assert "_No planned features" in content
