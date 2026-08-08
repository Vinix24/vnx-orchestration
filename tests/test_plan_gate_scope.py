"""The plan-gate scope read-site (VNX_PLAN_GATE_COMPLEX_ONLY, 2026-08-08).

``plan_gate_enforcement.plan_gate_scope`` classifies a plan HEAVY or LIGHT from
the operator's dividing line: HEAVY when the plan touches the dispatch-lane,
store-resolution, a review-gate, or a central-DB schema (ADR-007); LIGHT in all
other cases. Signals are derived from existing plan data (task_class tags,
per-deliverable complexity, named paths), never a new manual field.

Fail-closed: a plan we cannot judge (no text, no task_class, no complexity, no
paths) resolves to HEAVY — a silent fallback to the cheap panel is exactly the
failure this read-site exists to prevent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import plan_gate_enforcement as pge  # noqa: E402


# ---------------------------------------------------------------------------
# Fail-closed: unknown / missing scope input resolves to HEAVY, never LIGHT
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_none_plan_is_heavy(self):
        assert pge.plan_gate_scope(None) == pge.HEAVY

    def test_empty_plan_is_heavy(self):
        assert pge.plan_gate_scope("") == pge.HEAVY
        assert pge.plan_gate_scope("   \n  ") == pge.HEAVY

    def test_missing_every_signal_is_heavy(self):
        assert pge.plan_gate_scope(None, task_class=None, complexity=None, paths=[]) == pge.HEAVY

    def test_unclassified_task_with_empty_plan_is_heavy(self):
        # A task_class that is NOT inherently heavy still has no plan text to
        # judge — fail-closed to heavy, never downgraded to light.
        assert pge.plan_gate_scope(None, task_class="01_code_generation") == pge.HEAVY


# ---------------------------------------------------------------------------
# LIGHT: the operator's "all other cases"
# ---------------------------------------------------------------------------

class TestLight:
    def test_generic_feature_plan_is_light(self):
        plan = (
            "## Problem\nUsers cannot rename widgets.\n"
            "## Approach\nAdd a rename button to the dashboard view.\n"
            "## Deliverables\n- **Task class**: 01_code_generation\n"
        )
        assert pge.plan_gate_scope(plan) == pge.LIGHT

    def test_docs_plan_is_light(self):
        plan = (
            "## Problem\nThe README is stale.\n"
            "## Approach\nUpdate the README and add a changelog entry.\n"
            "**Task class**: 04_documentation\n"
        )
        assert pge.plan_gate_scope(plan) == pge.LIGHT

    def test_light_plan_with_light_paths(self):
        assert pge.plan_gate_scope(None, paths=["src/ui/dashboard.py"]) == pge.LIGHT

    def test_low_complexity_is_light(self):
        assert pge.plan_gate_scope(None, complexity="low") == pge.LIGHT


# ---------------------------------------------------------------------------
# HEAVY: touches the dispatch-lane, store-resolution, a review-gate, or a
# central-DB schema (ADR-007)
# ---------------------------------------------------------------------------

class TestHeavy:
    @pytest.mark.parametrize("marker", [
        # dispatch-lane
        "dispatch_cli", "provider_dispatch", "tmux_interactive_dispatch",
        "subprocess_dispatch", "dispatch_bridge", "dispatch-lane", "dispatch lane",
        "routing_policy", "smart_router", "dispatch_spec",
        # store-resolution
        "resolve_state_dir", "resolve_data_dir", "resolve_central_data_dir",
        "project_root", "vnx_paths", "central store", "vnx_data_dir",
        # review-gate
        "review-gate", "review_gate", "review_floor", "review-floor",
        "evidence_bound_gate", "codex_gate", "gemini_review", "verify_pr",
        "merge gate", "merge-gate", "plan_gate_evidence", "phantom_guard",
        # central-DB schema (ADR-007)
        "adr-007", "central-db", "central_db", "track_open_items",
        "runtime_coordination.db", "schema migration", "schema change",
        "composite key", "unique constraint",
    ])
    def test_domain_marker_is_heavy(self, marker):
        assert pge.plan_gate_scope(f"## Approach\nTouch {marker}.\n") == pge.HEAVY

    def test_task_class_review_is_heavy(self):
        assert pge.plan_gate_scope(None, task_class="02_code_review") == pge.HEAVY

    def test_task_class_inline_in_doc_is_heavy(self):
        plan = "## Deliverables\n- **Task class**: 02_code_review\n"
        assert pge.plan_gate_scope(plan) == pge.HEAVY

    def test_high_complexity_is_heavy(self):
        assert pge.plan_gate_scope(None, complexity="high") == pge.HEAVY
        assert pge.plan_gate_scope(None, complexity="critical") == pge.HEAVY

    def test_complexity_inline_in_doc_is_heavy(self):
        plan = "## Deliverables\n- **Complexity**: High\n"
        assert pge.plan_gate_scope(plan) == pge.HEAVY

    def test_paths_hitting_dispatch_lane_is_heavy(self):
        assert pge.plan_gate_scope(None, paths=["scripts/lib/dispatch_cli.py"]) == pge.HEAVY

    def test_case_insensitive_marker_match(self):
        assert pge.plan_gate_scope("## Approach\nTouch Dispatch_CLI.\n") == pge.HEAVY


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
