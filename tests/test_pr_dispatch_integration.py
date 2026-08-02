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
    monkeypatch.setenv("VNX_DATA_DIR", isolated)
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_PROJECT_ROOT", tempfile.mkdtemp(prefix="test_pr_dispatch_project_root_"))


def test_pr_dispatch_creation():
    """Test creating a dispatch from a PR"""
    print("\n" + "="*60)
    print("Test 1: PR Dispatch Creation")
    print("="*60)

    # Setup test PR queue
    manager = PRQueueManager()
    manager.clear_queue()
    manager.set_feature("Test Feature PR 2.5")

    # Add test PRs
    test_prs = [
        {
            'id': 'PR-1',
            'title': 'Skills system foundation',
            'description': 'Implement core skills loading and management',
            'size': 150,
            'dependencies': [],
            'skill': '@planner',
            'track': 'A',
            'gate': 'implementation',
            'priority': 'P1',
            'context': '@.claude/skills/skills.yaml',
            'success_criteria': 'Skills load correctly, tests pass'
        },
        {
            'id': 'PR-2',
            'title': 'Dispatcher integration',
            'description': 'Integrate skills with V8 dispatcher',
            'size': 200,
            'dependencies': ['PR-1'],
            'skill': '@backend-developer',
            'track': 'A',
            'gate': 'implementation',
            'priority': 'P1',
            'context': f"@{VNX_HOME / 'scripts/dispatcher.sh'}",
            'success_criteria': 'Dispatcher loads skills, no errors'
        }
    ]

    for pr in test_prs:
        manager.add_pr(pr)
        print(f"Added {pr['id']}: {pr['title']}")

    # Test creating dispatch for PR-1 (no dependencies)
    print("\n[Test] Creating dispatch for PR-1...")
    dispatch_id = manager.create_dispatch_from_pr('PR-1')

    if not dispatch_id:
        print("❌ Failed to create dispatch")
        return False

    # Verify dispatch file exists
    queue_dir = Path(__file__).parent.parent / "dispatches" / "queue"
    dispatch_file = queue_dir / f"{dispatch_id}.md"

    if not dispatch_file.exists():
        print(f"❌ Dispatch file not found: {dispatch_file}")
        return False

    print(f"✅ Dispatch file created: {dispatch_file.name}")

    # Read and verify Manager Block v2 format
    with open(dispatch_file, 'r') as f:
        content = f.read()

    print("\n[Test] Verifying Manager Block v2 format...")

    required_fields = [
        '[[TARGET:A]]',
        'Manager Block',
        'Role: planner',  # Should NOT have @ prefix
        'Track: A',
        'Terminal: T1',
        'Gate: implementation',
        'Priority: P1',
        'Cognition: normal',
        f'Dispatch-ID: {dispatch_id}',
        'Parent-Dispatch: none',
        'On-Success: review',
        'On-Failure: investigation',
        'Reason: Skills system foundation from PR queue',
        'Context: [[@.claude/skills/skills.yaml]]',
        'Instruction:',
        'Implement core skills loading and management',
        'Dependencies:',
        'Success Criteria:',
        '[[DONE]]'
    ]

    missing_fields = []
    for field in required_fields:
        if field not in content:
            missing_fields.append(field)

    if missing_fields:
        print(f"❌ Missing required fields: {missing_fields}")
        print("\n[Dispatch Content]:")
        print(content)
        return False

    # Verify no @ prefix in skill
    if 'Role: @' in content:
        print("❌ Skill has @ prefix (should be removed for v2)")
        return False

    print("✅ All required Manager Block v2 fields present")
    print("✅ Skill correctly formatted (no @ prefix)")

    # Test PR-2 dependency blocking
    print("\n[Test] Testing dependency blocking for PR-2...")
    dispatch_id_2 = manager.create_dispatch_from_pr('PR-2')

    if dispatch_id_2:
        print("❌ PR-2 should be blocked (depends on PR-1)")
        return False

    print("✅ PR-2 correctly blocked by dependencies")

    # Complete PR-1 and retry PR-2
    print("\n[Test] Completing PR-1 and creating PR-2 dispatch...")
    manager.update_pr_status('PR-1', 'completed')

    dispatch_id_2 = manager.create_dispatch_from_pr('PR-2')
    if not dispatch_id_2:
        print("❌ PR-2 should be available after PR-1 completes")
        return False

    print(f"✅ PR-2 dispatch created after dependencies met: {dispatch_id_2}")

    # Clean up test dispatches
    print("\n[Cleanup] Removing test dispatches...")
    if dispatch_file.exists():
        dispatch_file.unlink()
    dispatch_file_2 = queue_dir / f"{dispatch_id_2}.md"
    if dispatch_file_2.exists():
        dispatch_file_2.unlink()

    print("✅ Test dispatches cleaned up")

    return True


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


def test_popup_queue_detection():
    """Test that popup queue detects PR dispatches"""
    print("\n" + "="*60)
    print("Test 3: Popup Queue Detection")
    print("="*60)

    # Create a temporary dispatch
    manager = PRQueueManager()
    manager.clear_queue()
    manager.set_feature("Popup Queue Test")

    manager.add_pr({
        'id': 'PR-POPUP',
        'title': 'Test popup detection',
        'size': 50,
        'dependencies': [],
        'skill': '@developer',
        'track': 'A',
    })

    print("[Test] Creating dispatch for popup queue...")
    dispatch_id = manager.create_dispatch_from_pr('PR-POPUP')

    if not dispatch_id:
        print("❌ Failed to create dispatch")
        return False

    # Check dispatch file exists in queue
    queue_dir = Path(__file__).parent.parent / "dispatches" / "queue"
    dispatch_files = list(queue_dir.glob("*.md"))

    print(f"\nDispatches in queue: {len(dispatch_files)}")
    for f in dispatch_files:
        print(f"  - {f.name}")

    if not any(dispatch_id in f.name for f in dispatch_files):
        print(f"❌ Dispatch not found in queue: {dispatch_id}")
        return False

    print(f"✅ Dispatch found in queue: {dispatch_id}")
    print(f"✅ Popup queue should detect this dispatch")
    print(f"   (Note: Requires queue_popup_watcher.sh to be running)")

    # Verify Manager Block v2 format is valid
    dispatch_file = queue_dir / f"{dispatch_id}.md"
    with open(dispatch_file, 'r') as f:
        content = f.read()

    # Check for v2 markers
    if '[[TARGET:' not in content or '[[DONE]]' not in content:
        print("❌ Missing v2 markers ([[TARGET:]] and [[DONE]])")
        return False

    if 'Role: @' in content:
        print("❌ Skill has @ prefix (v2 format should remove it)")
        return False

    print("✅ Manager Block v2 format valid for popup queue")

    # Clean up
    print("\n[Cleanup] Removing test dispatch...")
    dispatch_file.unlink()

    return True


def main():
    """Run all tests"""
    print("\n" + "="*70)
    print("PR 2.5: Popup Queue Integration Tests")
    print("="*70)

    tests = [
        ("PR Dispatch Creation", test_pr_dispatch_creation),
        ("Recommendation Command Output", test_recommendation_command_output),
        ("Popup Queue Detection", test_popup_queue_detection),
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
        print("  ✅ PR dispatch creation working")
        print("  ✅ Manager Block v2 format correct")
        print("  ✅ Recommendation engine includes commands")
        print("  ✅ Popup queue integration ready")
        return 0
    else:
        print(f"\n❌ {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
