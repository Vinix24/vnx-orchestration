#!/usr/bin/env python3
"""
Test PR Queue Integration with Popup Queue
Tests PR 2.5 implementation
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from conftest import pin_vnx_data_dir

VNX_HOME = Path(os.environ.get("VNX_HOME", Path(__file__).resolve().parents[1]))

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pr_queue_manager import PRQueueManager


@pytest.fixture(autouse=True)
def _isolate_vnx_data_dir(monkeypatch):
    """PRQueueManager() resolves VNX_STATE_DIR/VNX_DISPATCH_DIR from
    VNX_DATA_DIR and writes real staging dispatches + queue state there.
    Without an explicit, isolated VNX_DATA_DIR these tests land in the real
    central store (~/.vnx-data/<project>) instead of a throwaway tmp dir
    (w19c/OI-934: this file previously had zero isolation and repeatedly
    wrote staging files into production governance state).

    pin_vnx_data_dir (tests/conftest.py) pins VNX_DATA_DIR *and* the
    VNX_STATE_DIR/VNX_DISPATCH_DIR/etc. subsystem overrides that
    resolve_paths() honors directly — pinning VNX_DATA_DIR alone left a gap
    (w22/PR#1333): a worker/T0 shell that already has VNX_STATE_DIR pointed
    at the real central store overrides the isolated VNX_DATA_DIR outright,
    and PRQueueManager()'s own real-store-under-pytest guard then refuses
    the write with RuntimeError instead of landing in the tmp dir.

    PROJECT_ROOT is isolated too: PRQueueManager.update_markdown() writes
    PR_QUEUE.md to project_root, and project_root resolves via vnx_paths'
    git-toplevel walk regardless of VNX_DATA_DIR — left unpinned, every run
    overwrites this repo's own git-tracked PR_QUEUE.md.

    Module-level autouse (not a test-function parameter) so the file still
    runs standalone via ``python3 tests/test_pr_dispatch_integration.py``
    (main() calls the test functions directly, with no pytest fixture
    injection) exactly as it did before.
    """
    isolated = tempfile.mkdtemp(prefix="test_pr_dispatch_integration_")
    pin_vnx_data_dir(monkeypatch, Path(isolated))
    monkeypatch.setenv("VNX_PROJECT_ROOT", tempfile.mkdtemp(prefix="test_pr_dispatch_project_root_"))


def test_recommendation_command_output():
    """Test that recommendations include dispatch command"""
    print("\n" + "="*60)
    print("Test 2: Recommendation Command Output")
    print("="*60)

    # Setup test PR queue
    manager = PRQueueManager()
    manager.clear_queue()
    manager.set_feature("Test Recommendations")

    # Add a ready PR
    manager.add_pr({
        'id': 'PR-TEST',
        'title': 'Test PR for recommendations',
        'size': 100,
        'dependencies': [],
        'skill': '@developer',
        'track': 'A',
    })

    # Save VNX state (this is what recommendation engine reads)
    manager.save_vnx_state()

    # Verify VNX state file exists
    vnx_state_file = Path(__file__).parent.parent / "state" / "pr_queue_state.yaml"
    if not vnx_state_file.exists():
        print(f"❌ VNX state file not created: {vnx_state_file}")
        return False

    print(f"✅ VNX state file created: {vnx_state_file.name}")

    # Read VNX state
    import yaml
    with open(vnx_state_file, 'r') as f:
        vnx_state = yaml.safe_load(f)

    print(f"\nVNX State:")
    print(f"  Active Feature: {vnx_state.get('active_feature', {}).get('name')}")
    print(f"  Next Available: {vnx_state.get('next_available', [])}")

    if 'PR-TEST' not in vnx_state.get('next_available', []):
        print("❌ PR-TEST should be in next_available")
        return False

    print("✅ PR-TEST correctly marked as available in VNX state")

    # Run recommendation engine
    print("\n[Test] Running recommendation engine...")
    from generate_t0_recommendations import RecommendationEngine

    engine = RecommendationEngine(lookback_minutes=5)
    engine.load_pr_queue_state()

    if not engine.pr_queue:
        print("❌ Recommendation engine failed to load PR queue")
        return False

    print("✅ Recommendation engine loaded PR queue")

    # Generate recommendations
    engine.add_pr_dependency_recommendation()

    # Check if recommendation includes command
    pr_recommendations = [r for r in engine.recommendations if r.get('trigger') == 'pr_ready']

    if not pr_recommendations:
        print("❌ No PR recommendations found")
        return False

    rec = pr_recommendations[0]
    print(f"\nRecommendation:")
    print(f"  Trigger: {rec.get('trigger')}")
    print(f"  Action: {rec.get('action')}")
    print(f"  PR ID: {rec.get('pr_id')}")
    print(f"  Command: {rec.get('command')}")

    if not rec.get('command'):
        print("❌ Recommendation missing 'command' field")
        return False

    expected_command = f"python {VNX_HOME / 'scripts/pr_queue_manager.py'} dispatch {rec['pr_id']}"
    if rec['command'] != expected_command:
        print(f"❌ Command incorrect. Expected: {expected_command}")
        return False

    print("✅ Recommendation includes correct dispatch command")

    return True


def main():
    """Run all tests"""
    print("\n" + "="*70)
    print("PR 2.5: Popup Queue Integration Tests")
    print("="*70)

    tests = [
        ("Recommendation Command Output", test_recommendation_command_output),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ Test failed with exception: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    # Summary
    print("\n" + "="*70)
    print("Test Summary")
    print("="*70)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")

    print(f"\nResults: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 All tests passed!")
        print("\nPR 2.5 implementation complete:")
        print("  ✅ Recommendation engine includes commands")
        return 0
    else:
        print(f"\n❌ {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
