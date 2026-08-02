#!/usr/bin/env python3
"""
Test PR Queue Recommendation Engine Integration (PR 2.3)
Tests: State loading, dependency recommendations, blocked PR detection
"""

import os
import sys
import tempfile
import yaml
from pathlib import Path

import pytest

from conftest import pin_vnx_data_dir

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pr_queue_manager import PRQueueManager
from generate_t0_recommendations import (
    RecommendationEngine,
    PR_QUEUE_STATE_FILE,
    STATE_DIR as _RECS_STATE_DIR,
    DISPATCHES_DIR as _RECS_DISPATCHES_DIR,
)


@pytest.fixture(autouse=True)
def _isolate_vnx_data_dir(monkeypatch):
    """Sibling of tests/test_pr_dispatch_integration.py — same PRQueueManager
    write surface, same w19c/OI-934 isolation requirement. See that file's
    fixture docstring for the full incident writeup, and pin_vnx_data_dir's
    docstring (tests/conftest.py) for why VNX_DATA_DIR alone is not enough
    (w22/PR#1333).

    Unlike test_pr_dispatch_integration.py, VNX_STATE_DIR/VNX_DISPATCH_DIR
    are NOT re-pinned to a fresh tmp dir here: generate_t0_recommendations
    resolves and caches STATE_DIR/DISPATCHES_DIR at import time (module-level
    constants, scripts/generate_t0_recommendations.py), so RecommendationEngine
    reads/writes through those frozen paths for the rest of the session. A
    fresh-per-test VNX_STATE_DIR would desync PRQueueManager() (resolves
    fresh every call) from RecommendationEngine (frozen at import) and break
    every assertion that reads back what the other wrote. Re-asserting the
    SAME already-resolved dirs here keeps both aligned; conftest.py's own
    module-level pin (run before this module is even imported) is what keeps
    that cached value isolated from the real central store in the first
    place, including under an ambient-polluted VNX_STATE_DIR.
    """
    isolated = tempfile.mkdtemp(prefix="test_pr_recommendation_integration_")
    pin_vnx_data_dir(monkeypatch, Path(isolated))
    monkeypatch.setenv("VNX_STATE_DIR", str(_RECS_STATE_DIR))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(_RECS_DISPATCHES_DIR))
    monkeypatch.setenv("VNX_PROJECT_ROOT", tempfile.mkdtemp(prefix="test_pr_recommendation_project_root_"))


def test_pr_queue_state_loading():
    """Test 1: PR queue state loading"""
    print("\n" + "="*60)
    print("[Test 1] PR Queue State Loading")
    print("="*60)

    # Setup: Create test PR queue
    manager = PRQueueManager()
    manager.clear_queue()
    manager.set_feature("Test Feature")

    prs = [
        {'id': 'PR-1', 'title': 'Base', 'dependencies': [], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-2', 'title': 'Middle', 'dependencies': ['PR-1'], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-3', 'title': 'Top', 'dependencies': ['PR-2'], 'skill': '@backend-developer', 'size': 100}
    ]

    for pr in prs:
        manager.add_pr(pr)

    # Verify YAML state file was created
    assert PR_QUEUE_STATE_FILE.exists(), "pr_queue_state.yaml should exist"
    print("  ✓ YAML state file created")

    # Load and verify state structure
    with open(PR_QUEUE_STATE_FILE, 'r') as f:
        state = yaml.safe_load(f)

    assert state.get('active_feature', {}).get('name') == 'Test Feature'
    assert state.get('completed_prs') == []
    assert state.get('in_progress') == []
    assert state.get('next_available') == ['PR-1']
    assert state.get('execution_order') == ['PR-1', 'PR-2', 'PR-3']
    print("  ✓ State structure valid")

    # Test recommendation engine loading
    engine = RecommendationEngine()
    engine.load_pr_queue_state()
    assert engine.pr_queue is not None, "Engine should load PR queue"
    assert engine.pr_queue.get('active_feature', {}).get('name') == 'Test Feature'
    print("  ✓ Recommendation engine loads PR queue")

    print("\n✅ Test 1 passed!")


def test_dependency_recommendations():
    """Test 2: Dependency recommendations"""
    print("\n" + "="*60)
    print("[Test 2] Dependency Recommendations")
    print("="*60)

    # Setup: PR queue with dependencies
    manager = PRQueueManager()
    manager.clear_queue()
    manager.set_feature("Dependency Test")

    prs = [
        {'id': 'PR-1', 'title': 'Base', 'dependencies': [], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-2', 'title': 'Depends on 1', 'dependencies': ['PR-1'], 'skill': '@backend-developer', 'size': 100}
    ]

    for pr in prs:
        manager.add_pr(pr)

    # Scenario 1: Next PR available (PR-1)
    engine = RecommendationEngine()
    engine.load_pr_queue_state()
    engine.add_pr_dependency_recommendation()

    pr_ready_recs = [r for r in engine.recommendations if r['trigger'] == 'pr_ready']
    assert len(pr_ready_recs) == 1, "Should have 1 PR ready recommendation"
    assert pr_ready_recs[0]['pr_id'] == 'PR-1'
    print("  ✓ Recommends next available PR (PR-1)")

    # Scenario 2: PR in progress, dependencies met
    manager.update_pr_status('PR-1', 'in_progress')
    engine2 = RecommendationEngine()
    engine2.load_pr_queue_state()
    engine2.add_pr_dependency_recommendation()

    # Should have no recommendations (PR-1 in progress, no PR ready)
    pr_recs = [r for r in engine2.recommendations if r['trigger'] in ['pr_ready', 'pr_blocked']]
    assert len(pr_recs) == 0, "No recommendations when PR in progress and none ready"
    print("  ✓ No recommendations when PR in progress")

    # Scenario 3: Complete PR-1, PR-2 becomes available
    manager.update_pr_status('PR-1', 'completed')
    engine3 = RecommendationEngine()
    engine3.load_pr_queue_state()
    engine3.add_pr_dependency_recommendation()

    pr_ready_recs = [r for r in engine3.recommendations if r['trigger'] == 'pr_ready']
    assert len(pr_ready_recs) == 1, "Should recommend PR-2 after PR-1 completes"
    assert pr_ready_recs[0]['pr_id'] == 'PR-2'
    print("  ✓ Recommends next PR (PR-2) after dependency completes")

    print("\n✅ Test 2 passed!")


def test_blocked_pr_scenario():
    """Test 3: Blocked PR detection"""
    print("\n" + "="*60)
    print("[Test 3] Blocked PR Detection")
    print("="*60)

    # Setup: Start PR with unmet dependencies (manual override scenario)
    manager = PRQueueManager()
    manager.clear_queue()
    manager.set_feature("Blocked Test")

    prs = [
        {'id': 'PR-1', 'title': 'Base', 'dependencies': [], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-2', 'title': 'Needs PR-1', 'dependencies': ['PR-1'], 'skill': '@backend-developer', 'size': 100}
    ]

    for pr in prs:
        manager.add_pr(pr)

    # Scenario: Force PR-2 to in_progress without completing PR-1 (shouldn't happen, but test detection)
    manager.update_pr_status('PR-2', 'in_progress')

    # Load and check for blocked recommendation
    engine = RecommendationEngine()
    engine.load_pr_queue_state()
    engine.add_pr_dependency_recommendation()

    blocked_recs = [r for r in engine.recommendations if r['trigger'] == 'pr_blocked']
    assert len(blocked_recs) >= 1, "Should detect blocked PR"
    blocked_pr_ids = [r['pr_id'] for r in blocked_recs]
    assert 'PR-2' in blocked_pr_ids
    assert 'PR-1' in blocked_recs[0]['reason']
    print(f"  ✓ Detected blocked PR: {blocked_recs[0]['reason']}")

    print("\n✅ Test 3 passed!")


def test_empty_queue():
    """Test 4: Empty PR queue (no false positives)"""
    print("\n" + "="*60)
    print("[Test 4] Empty PR Queue Handling")
    print("="*60)

    # Setup: Empty queue
    manager = PRQueueManager()
    manager.clear_queue()

    # Test recommendation engine with empty queue
    engine = RecommendationEngine()
    engine.load_pr_queue_state()
    engine.add_pr_dependency_recommendation()

    pr_recs = [r for r in engine.recommendations if r['trigger'] in ['pr_ready', 'pr_blocked']]
    assert len(pr_recs) == 0, "No recommendations for empty queue"
    print("  ✓ No false positive recommendations")

    print("\n✅ Test 4 passed!")


def main():
    """Run all integration tests"""
    print("\n" + "="*60)
    print("PR 2.3: PR Queue Recommendation Integration Tests")
    print("="*60)

    try:
        test_pr_queue_state_loading()
        test_dependency_recommendations()
        test_blocked_pr_scenario()
        test_empty_queue()

        print("\n" + "="*60)
        print("✅ All PR 2.3 tests passed!")
        print("="*60)

        print("\nIntegration complete:")
        print("  • PR queue state written to pr_queue_state.yaml")
        print("  • Recommendation engine loads queue state")
        print("  • PR ready/blocked recommendations generated")
        print("  • Dependency tracking integrated")

    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
