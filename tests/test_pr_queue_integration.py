#!/usr/bin/env python3
"""
Integration Tests for VNX PR Queue System
Tests PR 2.1 through PR 2.5 integration
"""

import os
import pytest
import json
import yaml
import sys
import subprocess
from pathlib import Path
from datetime import datetime

# Add scripts directory to path
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pr_queue_manager import PRQueueManager
from generate_t0_recommendations import RecommendationEngine

# Capture the pristine module-global paths at import time, before any clean_state
# fixture can reassign them. RecommendationEngine reads these globals at call
# time, so a leak would make it read a foreign pr_queue_state.yaml for the rest
# of the session. test_clean_state_does_not_leak_recs_module_globals asserts the
# fixture restores them (OI-953).
import generate_t0_recommendations as _recs
_RECS_STATE_DIR_AT_IMPORT = _recs.STATE_DIR
_RECS_PR_QUEUE_STATE_FILE_AT_IMPORT = _recs.PR_QUEUE_STATE_FILE


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    """Clean state directory before each test"""
    state_dir = tmp_path / "data" / "state"
    vnx_home = tmp_path / "vnx-home"
    state_dir.mkdir(parents=True, exist_ok=True)
    vnx_home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(tmp_path / "data" / "dispatches"))
    monkeypatch.setenv("VNX_HOME", str(vnx_home))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_STATE_SIMPLIFICATION_ROLLBACK", "0")

    import generate_t0_recommendations as recs
    # OI-953: scope the module-global reassignments to this test via monkeypatch.
    # A bare assignment persists on generate_t0_recommendations for the rest of
    # the session, so any later module (test_pr_recommendation_integration.py)
    # that instantiates RecommendationEngine reads the LAST clean_state tmp dir's
    # pr_queue_state.yaml instead of its own — the OI-953 "assert 'E2E Test
    # Feature' == 'Test Feature'" red when test_pr_queue runs first in the CI
    # sweep. monkeypatch restores each global after the test.
    monkeypatch.setattr(recs, "VNX_ROOT", Path(os.environ.get("VNX_HOME", tmp_path)))
    monkeypatch.setattr(recs, "STATE_DIR", state_dir)
    monkeypatch.setattr(recs, "DISPATCHES_DIR", tmp_path / "dispatches")
    monkeypatch.setattr(recs, "LEGACY_STATE_DIR", recs.VNX_ROOT / "state")
    monkeypatch.setattr(recs, "LEGACY_DISPATCHES_DIR", recs.VNX_ROOT / "dispatches")
    monkeypatch.setattr(recs, "RECEIPTS_FILE", state_dir / "t0_receipts.ndjson")
    monkeypatch.setattr(recs, "RECOMMENDATIONS_FILE", state_dir / "t0_recommendations.json")
    monkeypatch.setattr(recs, "ACTIVE_CONFLICTS_FILE", state_dir / "active_conflicts.json")
    monkeypatch.setattr(recs, "PR_QUEUE_STATE_FILE", state_dir / "pr_queue_state.yaml")
    monkeypatch.setattr(recs, "OPEN_ITEMS_DIGEST_FILE", state_dir / "open_items_digest.json")
    monkeypatch.setattr(recs, "STAGING_SEEN_FILE", state_dir / "staging_seen.json")
    monkeypatch.setattr(recs, "LEGACY_RECEIPTS_FILE", recs.LEGACY_STATE_DIR / "t0_receipts.ndjson")
    monkeypatch.setattr(recs, "LEGACY_RECOMMENDATIONS_FILE", recs.LEGACY_STATE_DIR / "t0_recommendations.json")
    monkeypatch.setattr(recs, "LEGACY_PR_QUEUE_STATE_FILE", recs.LEGACY_STATE_DIR / "pr_queue_state.yaml")
    monkeypatch.setattr(recs, "LEGACY_OPEN_ITEMS_DIGEST_FILE", recs.LEGACY_STATE_DIR / "open_items_digest.json")
    monkeypatch.setattr(recs, "LEGACY_STAGING_SEEN_FILE", recs.LEGACY_STATE_DIR / "staging_seen.json")

    # Clean up test files
    test_files = [
        state_dir / "pr_queue_state.yaml",
        state_dir / "t0_recommendations.json",
    ]
    for f in test_files:
        if f.exists():
            f.unlink()

    yield state_dir


@pytest.fixture
def sample_feature_plan(tmp_path):
    """Create a sample FEATURE_PLAN.md for testing"""
    # Note: PR IDs must match format '## PR-X:' for validation
    plan_content = """# Feature: VNX PR Queue Integration

## PR-1: Queue Structure
Dependencies: []
Size: 200 lines
Skill: @architect

Implement core PR queue manager with state persistence.

## PR-2: Dependency Checker
Dependencies: [PR-1]
Size: 150 lines
Skill: @backend-developer

Add dependency validation and topological sort.

## PR-3: Recommendation Integration
Dependencies: [PR-1, PR-2]
Size: 100 lines
Skill: @backend-developer

Integrate PR queue with recommendation engine.
"""

    plan_file = tmp_path / "FEATURE_PLAN.md"
    plan_file.write_text(plan_content)
    return plan_file


class TestPRQueueStateCreation:
    """Test PR queue state file creation (PR 2.1 + 2.4)"""

    def test_pr_queue_state_creation(self, clean_state, sample_feature_plan):
        """Test PR queue state file is created correctly"""
        manager = PRQueueManager()
        manager.clear_queue()
        manager.set_feature("VNX PR Queue Integration")

        # Validate feature plan
        valid, error = manager.validate_feature_plan(str(sample_feature_plan))
        assert valid, f"Feature plan invalid: {error}"

        # Add PRs from feature plan (matching the fixture PR IDs)
        prs = [
            {
                'id': 'PR-1',
                'title': 'Queue Structure',
                'size': 200,
                'dependencies': [],
                'skill': '@architect',
                'track': 'A'
            },
            {
                'id': 'PR-2',
                'title': 'Dependency Checker',
                'size': 150,
                'dependencies': ['PR-1'],
                'skill': '@backend-developer',
                'track': 'A'
            },
            {
                'id': 'PR-3',
                'title': 'Recommendation Integration',
                'size': 100,
                'dependencies': ['PR-1', 'PR-2'],
                'skill': '@backend-developer',
                'track': 'A'
            }
        ]

        for pr in prs:
            manager.add_pr(pr)

        # Check VNX state file created
        vnx_state_file = clean_state / "pr_queue_state.yaml"
        assert vnx_state_file.exists(), "VNX state file not created"

        # Read and validate state
        with open(vnx_state_file, 'r') as f:
            state = yaml.safe_load(f)

        assert state['active_feature']['name'] == "VNX PR Queue Integration"
        assert 'PR-1' in state['next_available'], "PR-1 should be available"
        assert state['in_progress'] == []
        assert state['completed_prs'] == []

    def test_pr_queue_state_uses_vnx_state_dir(self, clean_state):
        """Test PR queue state writes to VNX_STATE_DIR"""
        manager = PRQueueManager()
        manager.clear_queue()
        manager.set_feature("Canonical Path Test")

        expected_state_file = clean_state / "pr_queue_state.yaml"
        assert manager.vnx_state_file == expected_state_file
        assert expected_state_file.exists(), "VNX state file not created in VNX_STATE_DIR"

    def test_execution_order_validation(self):
        """Test execution order with topological sort (PR 2.2)"""
        manager = PRQueueManager()
        manager.clear_queue()

        # Add PRs
        prs = [
            {'id': 'PR-1', 'dependencies': [], 'title': 'Base', 'skill': '@developer', 'size': 100},
            {'id': 'PR-2', 'dependencies': ['PR-1'], 'title': 'Middle', 'skill': '@developer', 'size': 100},
            {'id': 'PR-3', 'dependencies': ['PR-1', 'PR-2'], 'title': 'Top', 'skill': '@developer', 'size': 100}
        ]

        for pr in prs:
            manager.add_pr(pr)

        # Check execution order
        success, order, error = manager.get_execution_order()
        assert success, f"Execution order failed: {error}"
        assert order == ["PR-1", "PR-2", "PR-3"], f"Order incorrect: {order}"

    def test_circular_dependency_detection(self):
        """Test circular dependency detection (PR 2.2)"""
        manager = PRQueueManager()
        manager.clear_queue()

        # Create circular dependencies
        circular_prs = [
            {'id': 'PR-A', 'dependencies': ['PR-C'], 'title': 'A', 'skill': '@developer', 'size': 100},
            {'id': 'PR-B', 'dependencies': ['PR-A'], 'title': 'B', 'skill': '@developer', 'size': 100},
            {'id': 'PR-C', 'dependencies': ['PR-B'], 'title': 'C', 'skill': '@developer', 'size': 100}
        ]

        for pr in circular_prs:
            manager.add_pr(pr)

        # Check execution order fails
        success, order, error = manager.get_execution_order()
        assert not success, "Should detect circular dependency"
        assert "Circular dependency" in error, f"Error should mention circular dependency: {error}"

    def test_pr_queue_dual_write_disabled_by_default(self, clean_state):
        """Canonical cutover: legacy mirrors should be off by default."""
        manager = PRQueueManager()
        manager.clear_queue()
        manager.set_feature("Canonical-only")

        legacy_state_file = Path(os.environ["VNX_HOME"]) / "state" / "pr_queue_state.yaml"
        assert manager.dual_write_legacy is False
        assert not legacy_state_file.exists()

    def test_pr_queue_dual_write_enabled_with_rollback(self, clean_state, monkeypatch):
        """Rollback flag should temporarily re-enable legacy mirror writes."""
        monkeypatch.setenv("VNX_STATE_SIMPLIFICATION_ROLLBACK", "1")
        monkeypatch.delenv("VNX_PR_QUEUE_DUAL_WRITE_LEGACY", raising=False)
        monkeypatch.delenv("VNX_STATE_DUAL_WRITE_LEGACY", raising=False)

        manager = PRQueueManager()
        manager.clear_queue()
        manager.set_feature("Rollback")

        legacy_state_file = manager.legacy_state_dir / "pr_queue_state.yaml"
        assert manager.dual_write_legacy is True
        assert legacy_state_file.exists()


class TestRecommendationEngineIntegration:
    """Test recommendation engine PR queue integration (PR 2.3)"""

    def test_recommendation_engine_loads_pr_state(self, clean_state):
        """Test recommendation engine reads PR queue state"""
        # Create PR queue state
        pr_state_file = clean_state / "pr_queue_state.yaml"
        pr_state_content = {
            'active_feature': {
                'name': 'Test Feature',
                'plan_file': 'FEATURE_PLAN.md'
            },
            'completed_prs': ['PR-1', 'PR-2'],
            'in_progress': None,
            'blocked': [],
            'next_available': ['PR-3'],
            'execution_order': ['PR-1', 'PR-2', 'PR-3'],
            'updated_at': datetime.now().isoformat()
        }

        with open(pr_state_file, 'w') as f:
            yaml.dump(pr_state_content, f)

        # Load recommendation engine
        engine = RecommendationEngine(lookback_minutes=5)
        engine.load_pr_queue_state()

        # Check PR queue loaded
        assert engine.pr_queue is not None, "PR queue should be loaded"
        assert engine.pr_queue['completed_prs'] == ['PR-1', 'PR-2']
        assert engine.pr_queue['next_available'] == ['PR-3']

    def test_pr_ready_recommendation_generation(self, clean_state):
        """Test PR_READY recommendation is generated"""
        # Create PR queue state with ready PR
        pr_state_file = clean_state / "pr_queue_state.yaml"
        pr_state_content = {
            'active_feature': {
                'name': 'Test Feature',
                'plan_file': 'FEATURE_PLAN.md'
            },
            'completed_prs': [],
            'in_progress': None,
            'blocked': [],
            'next_available': ['PR-1'],
            'execution_order': ['PR-1', 'PR-2'],
            'updated_at': datetime.now().isoformat()
        }

        with open(pr_state_file, 'w') as f:
            yaml.dump(pr_state_content, f)

        # Load recommendation engine and generate recommendations
        engine = RecommendationEngine(lookback_minutes=5)
        engine.load_pr_queue_state()
        engine.add_pr_dependency_recommendation()

        # Verify PR_READY recommendation exists
        pr_ready = [r for r in engine.recommendations if r['trigger'] == 'pr_ready']
        assert len(pr_ready) > 0, "Should generate PR_READY recommendation"
        assert pr_ready[0]['pr_id'] == 'PR-1'
        assert 'command' in pr_ready[0], "Should include dispatch command"
        assert 'dispatch PR-1' in pr_ready[0]['command']

    def test_pr_blocked_recommendation(self, clean_state):
        """Test PR_BLOCKED recommendation for dependencies"""
        # Create manager with blocked PR
        manager = PRQueueManager()
        manager.clear_queue()
        manager.set_feature("Test Feature")

        manager.add_pr({
            'id': 'PR-1',
            'dependencies': [],
            'title': 'Base',
            'skill': '@developer',
            'size': 100
        })
        manager.add_pr({
            'id': 'PR-2',
            'dependencies': ['PR-1'],
            'title': 'Dependent',
            'skill': '@developer',
            'size': 100
        })

        # Set PR-2 as in_progress (but dependencies not met)
        manager.update_pr_status('PR-2', 'in_progress')

        # Load recommendation engine
        engine = RecommendationEngine(lookback_minutes=5)
        engine.load_pr_queue_state()
        engine.add_pr_dependency_recommendation()

        # Verify PR_BLOCKED recommendation exists
        pr_blocked = [r for r in engine.recommendations if r['trigger'] == 'pr_blocked']
        assert len(pr_blocked) > 0, "Should generate PR_BLOCKED recommendation"
        assert pr_blocked[0]['pr_id'] == 'PR-2'

    def test_recommendation_inputs_do_not_read_legacy_by_default(self, clean_state, monkeypatch):
        """Canonical cutover: legacy-only sources should be ignored unless rollback is enabled."""
        import generate_t0_recommendations as recs

        monkeypatch.setenv("VNX_STATE_SIMPLIFICATION_ROLLBACK", "0")
        legacy_state = Path(recs.LEGACY_STATE_DIR)
        legacy_state.mkdir(parents=True, exist_ok=True)
        dispatches_dir = Path(recs.DISPATCHES_DIR)
        (dispatches_dir / "completed").mkdir(parents=True, exist_ok=True)

        # Keep canonical missing on purpose, place source files in legacy.
        dispatch_id = "DISP-PR-123"
        (dispatches_dir / "completed" / f"{dispatch_id}.md").write_text(
            "Gate: implementation\nOn-Success: review\nProgram: system\n",
            encoding="utf-8",
        )
        (legacy_state / "t0_receipts.ndjson").write_text(
            json.dumps(
                {
                    "event_type": "task_complete",
                    "status": "success",
                    "dispatch_id": dispatch_id,
                    "timestamp": datetime.now().isoformat(),
                    "gate": "implementation",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (legacy_state / "pr_queue_state.yaml").write_text(
            yaml.safe_dump(
                {
                    "active_feature": {"name": "Fallback Feature"},
                    "completed_prs": [],
                    "in_progress": None,
                    "blocked": [],
                    "next_available": ["PR-7"],
                    "execution_order": ["PR-7"],
                    "updated_at": datetime.now().isoformat(),
                }
            ),
            encoding="utf-8",
        )
        (legacy_state / "open_items_digest.json").write_text(
            json.dumps(
                {
                    "summary": {"open_count": 1, "blocker_count": 1, "warn_count": 0, "info_count": 0},
                    "top_blockers": [{"id": "OI-001", "title": "Critical blocker", "pr_id": "PR-7"}],
                }
            ),
            encoding="utf-8",
        )

        engine = RecommendationEngine(lookback_minutes=60)
        engine.run()

        triggers = {rec.get("trigger") for rec in engine.recommendations}
        assert "task_success" not in triggers
        assert "pr_ready" not in triggers
        assert "open_item_blocker" not in triggers

    def test_recommendation_inputs_preserved_with_legacy_fallback_in_rollback_mode(self, clean_state, monkeypatch):
        """Rollback mode keeps legacy source coverage available during cutover."""
        import generate_t0_recommendations as recs

        monkeypatch.setenv("VNX_STATE_SIMPLIFICATION_ROLLBACK", "1")
        legacy_state = Path(recs.LEGACY_STATE_DIR)
        legacy_state.mkdir(parents=True, exist_ok=True)
        dispatches_dir = Path(recs.DISPATCHES_DIR)
        (dispatches_dir / "completed").mkdir(parents=True, exist_ok=True)

        dispatch_id = "DISP-PR-999"
        (dispatches_dir / "completed" / f"{dispatch_id}.md").write_text(
            "Gate: implementation\nOn-Success: review\nProgram: system\n",
            encoding="utf-8",
        )
        (legacy_state / "t0_receipts.ndjson").write_text(
            json.dumps(
                {
                    "event_type": "task_complete",
                    "status": "success",
                    "dispatch_id": dispatch_id,
                    "timestamp": datetime.now().isoformat(),
                    "gate": "implementation",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (legacy_state / "pr_queue_state.yaml").write_text(
            yaml.safe_dump(
                {
                    "active_feature": {"name": "Rollback Feature"},
                    "completed_prs": [],
                    "in_progress": None,
                    "blocked": [],
                    "next_available": ["PR-9"],
                    "execution_order": ["PR-9"],
                    "updated_at": datetime.now().isoformat(),
                }
            ),
            encoding="utf-8",
        )
        (legacy_state / "open_items_digest.json").write_text(
            json.dumps(
                {
                    "summary": {"open_count": 1, "blocker_count": 1, "warn_count": 0, "info_count": 0},
                    "top_blockers": [{"id": "OI-009", "title": "Rollback blocker", "pr_id": "PR-9"}],
                }
            ),
            encoding="utf-8",
        )

        engine = RecommendationEngine(lookback_minutes=60)
        engine.run()

        triggers = {rec.get("trigger") for rec in engine.recommendations}
        assert "task_success" in triggers
        assert "pr_ready" in triggers
        assert "open_item_blocker" in triggers


class TestDispatchCreation:
    """Test PR dispatch creation (PR 2.5)"""

    def test_dispatch_creation_from_pr(self, tmp_path):
        """Test PR creates valid dispatch file"""
        manager = PRQueueManager()
        manager.clear_queue()
        manager.set_feature("Test Feature")

        # Add test PR
        manager.add_pr({
            'id': 'PR-TEST',
            'title': 'Test PR',
            'description': 'Test description for dispatch',
            'track': 'A',
            'skill': '@developer',
            'dependencies': [],
            'size': 100,
            'gate': 'implementation',
            'priority': 'P1',
            'success_criteria': 'Tests pass'
        })

        # Create dispatch
        dispatch_id = manager.create_dispatch_from_pr('PR-TEST')
        assert dispatch_id is not None, "Should create dispatch ID"

        # Verify dispatch file exists
        staging_dir = manager.dispatch_dir / "staging"
        dispatch_file = staging_dir / f"{dispatch_id}.md"
        assert dispatch_file.exists(), f"Dispatch file should exist: {dispatch_file}"

        # Verify Manager Block v2 format
        content = dispatch_file.read_text()
        assert '[[TARGET:A]]' in content, "Should have TARGET marker"
        assert 'Role: developer' in content, "Should have skill without @ prefix"
        assert 'Track: A' in content, "Should have track"
        assert 'Gate: implementation' in content, "Should have gate"
        assert 'Test description for dispatch' in content, "Should have description"
        assert '[[DONE]]' in content, "Should have DONE marker"

        # Cleanup
        dispatch_file.unlink()

    def test_dispatch_blocked_by_dependencies(self):
        """Test dispatch creation is blocked by dependencies"""
        manager = PRQueueManager()
        manager.clear_queue()

        # Add PRs with dependencies
        manager.add_pr({
            'id': 'PR-1',
            'dependencies': [],
            'title': 'Base',
            'skill': '@developer',
            'size': 100
        })
        manager.add_pr({
            'id': 'PR-2',
            'dependencies': ['PR-1'],
            'title': 'Dependent',
            'skill': '@developer',
            'size': 100
        })

        # Try to create dispatch for PR-2 (should fail)
        dispatch_id = manager.create_dispatch_from_pr('PR-2')
        assert dispatch_id is None, "Should not create dispatch with unmet dependencies"

        # Complete PR-1
        manager.update_pr_status('PR-1', 'completed')

        # Now PR-2 should work
        dispatch_id = manager.create_dispatch_from_pr('PR-2')
        assert dispatch_id is not None, "Should create dispatch after dependencies met"

        # Cleanup
        staging_dir = manager.dispatch_dir / "staging"
        dispatch_file = staging_dir / f"{dispatch_id}.md"
        if dispatch_file.exists():
            dispatch_file.unlink()

    def test_manager_block_v2_format_validation(self):
        """Test Manager Block v2 format is correct"""
        manager = PRQueueManager()
        manager.clear_queue()

        manager.add_pr({
            'id': 'PR-FORMAT',
            'title': 'Format test',
            'description': 'Testing v2 format',
            'track': 'B',
            'skill': '@backend-developer',
            'dependencies': [],
            'size': 150,
            'gate': 'review',
            'priority': 'P2',
        })

        # Create dispatch
        dispatch_id = manager.create_dispatch_from_pr('PR-FORMAT')
        assert dispatch_id is not None

        # Read dispatch file
        staging_dir = manager.dispatch_dir / "staging"
        dispatch_file = staging_dir / f"{dispatch_id}.md"
        content = dispatch_file.read_text()

        # Validate v2 format fields
        required_v2_fields = [
            '[[TARGET:B]]',
            'Manager Block',
            'Role: backend-developer',  # NO @ prefix
            'Track: B',
            'Terminal: T2',
            'Gate: review',
            'Priority: P2',
            'Cognition: normal',
            f'Dispatch-ID: {dispatch_id}',
            'Parent-Dispatch: none',
            'On-Success: review',
            'On-Failure: investigation',
            'Context: [[@FEATURE_PLAN.md]]',
            'Instruction:',
            '[[DONE]]'
        ]

        for field in required_v2_fields:
            assert field in content, f"Missing v2 field: {field}"

        # Verify NO @ prefix in Role
        assert 'Role: @' not in content, "Role should NOT have @ prefix in v2"

        # Cleanup
        dispatch_file.unlink()


class TestWorkflowIntegration:
    """Test complete workflow integration"""

    def test_end_to_end_workflow(self, clean_state):
        """Test complete workflow: queue → recommendation → dispatch"""
        # Step 1: Create PR queue
        manager = PRQueueManager()
        manager.clear_queue()
        manager.set_feature("E2E Test Feature")

        manager.add_pr({
            'id': 'PR-E2E',
            'title': 'End-to-end test',
            'description': 'Complete workflow test',
            'dependencies': [],
            'skill': '@developer',
            'track': 'A',
            'size': 100,
        })

        # Verify VNX state created
        vnx_state_file = clean_state / "pr_queue_state.yaml"
        assert vnx_state_file.exists()

        # Step 2: Load recommendation engine
        engine = RecommendationEngine(lookback_minutes=5)
        engine.load_pr_queue_state()
        assert engine.pr_queue is not None

        # Step 3: Generate recommendations
        engine.add_pr_dependency_recommendation()
        pr_ready = [r for r in engine.recommendations if r['trigger'] == 'pr_ready']
        assert len(pr_ready) > 0
        assert 'command' in pr_ready[0]

        # Step 4: Create dispatch (simulating T0 running the command)
        dispatch_id = manager.create_dispatch_from_pr('PR-E2E')
        assert dispatch_id is not None

        # Step 5: Verify dispatch in queue
        staging_dir = manager.dispatch_dir / "staging"
        dispatch_file = staging_dir / f"{dispatch_id}.md"
        assert dispatch_file.exists()

        # Step 6: Verify Manager Block v2 format
        content = dispatch_file.read_text()
        assert '[[TARGET:A]]' in content
        assert 'Role: developer' in content  # No @ prefix
        assert '[[DONE]]' in content

        # Cleanup
        dispatch_file.unlink()

    def test_dependency_chain_workflow(self):
        """Test workflow with dependency chain"""
        manager = PRQueueManager()
        manager.clear_queue()
        manager.set_feature("Dependency Chain Test")

        # Create dependency chain: PR-1 → PR-2 → PR-3
        prs = [
            {'id': 'PR-1', 'dependencies': [], 'title': 'Base', 'skill': '@developer', 'size': 100},
            {'id': 'PR-2', 'dependencies': ['PR-1'], 'title': 'Middle', 'skill': '@developer', 'size': 100},
            {'id': 'PR-3', 'dependencies': ['PR-2'], 'title': 'Top', 'skill': '@developer', 'size': 100}
        ]

        for pr in prs:
            manager.add_pr(pr)

        # Only PR-1 should be available
        next_pr = manager.get_next_pr()
        assert next_pr['id'] == 'PR-1'

        # Create dispatch for PR-1
        dispatch_id_1 = manager.create_dispatch_from_pr('PR-1')
        assert dispatch_id_1 is not None

        # PR-2 should still be blocked
        dispatch_id_2 = manager.create_dispatch_from_pr('PR-2')
        assert dispatch_id_2 is None

        # Complete PR-1
        manager.update_pr_status('PR-1', 'completed')

        # Now PR-2 should be available
        next_pr = manager.get_next_pr()
        assert next_pr['id'] == 'PR-2'

        dispatch_id_2 = manager.create_dispatch_from_pr('PR-2')
        assert dispatch_id_2 is not None

        # Complete PR-2
        manager.update_pr_status('PR-2', 'completed')

        # Now PR-3 should be available
        next_pr = manager.get_next_pr()
        assert next_pr['id'] == 'PR-3'

        dispatch_id_3 = manager.create_dispatch_from_pr('PR-3')
        assert dispatch_id_3 is not None

        # Cleanup
        staging_dir = manager.dispatch_dir / "staging"
        for dispatch_id in [dispatch_id_1, dispatch_id_2, dispatch_id_3]:
            if dispatch_id:
                dispatch_file = staging_dir / f"{dispatch_id}.md"
                if dispatch_file.exists():
                    dispatch_file.unlink()


def test_clean_state_does_not_leak_recs_module_globals():
    """OI-953 regression: clean_state's generate_t0_recommendations module-global
    reassignments must be scoped to the fixture.

    Before the monkeypatch fix, the last clean_state test left STATE_DIR and
    PR_QUEUE_STATE_FILE pointing at its tmp dir for the rest of the session.
    test_pr_recommendation_integration.py (which runs after this file in the CI
    sweep, and whose RecommendationEngine reads those globals at call time) then
    read the stale 'E2E Test Feature' queue left here — the OI-953
    "assert 'E2E Test Feature' == 'Test Feature'" red. This test runs last in
    this file (definition order), so every clean_state test has executed and any
    leak would already have happened.
    """
    import generate_t0_recommendations as recs

    assert recs.STATE_DIR == _RECS_STATE_DIR_AT_IMPORT, (
        f"recs.STATE_DIR leaked to {recs.STATE_DIR!s}; "
        f"expected it restored to the import-time value {_RECS_STATE_DIR_AT_IMPORT!s}"
    )
    assert recs.PR_QUEUE_STATE_FILE == _RECS_PR_QUEUE_STATE_FILE_AT_IMPORT, (
        "recs.PR_QUEUE_STATE_FILE leaked out of the import-time isolation tree; "
        "a later RecommendationEngine() would read a foreign, stale queue state"
    )


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])
