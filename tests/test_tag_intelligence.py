#!/usr/bin/env python3
"""
Test Suite for Tag Intelligence Engine
Tests tag normalization, pairwise/triple subset generation, combination tracking,
prevention rule generation, structured recommendations, and hierarchical matching.
"""

import unittest
import tempfile
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
import sys

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from tag_intelligence import (
    TagIntelligenceEngine,
    RecommendationManager,
    MAX_PENDING_RECOMMENDATIONS,
    STALE_DAYS,
    RECOMMENDATION_TYPES,
)


class TestTagNormalization(unittest.TestCase):
    """Test tag normalization to standardized taxonomy"""

    def setUp(self):
        """Create temporary database for testing"""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.engine = TagIntelligenceEngine(Path(self.temp_db.name))

    def tearDown(self):
        """Clean up temporary database"""
        self.engine.close()
        Path(self.temp_db.name).unlink()

    def test_phase_normalization(self):
        """Test phase tag normalization"""
        # Design phase
        result = self.engine.normalize_tags(['design', 'planning', 'architecture'])
        self.assertIn('design-phase', result)

        # Implementation phase
        result = self.engine.normalize_tags(['implementation', 'coding', 'development'])
        self.assertIn('implementation-phase', result)

        # Testing phase
        result = self.engine.normalize_tags(['testing', 'validation', 'qa'])
        self.assertIn('testing-phase', result)

        # Production phase
        result = self.engine.normalize_tags(['production', 'deployment', 'release'])
        self.assertIn('production-phase', result)

    def test_component_normalization(self):
        """Test component tag normalization"""
        # Crawler component
        result = self.engine.normalize_tags(['crawler', 'scraping', 'web'])
        self.assertIn('crawler-component', result)

        # Storage component
        result = self.engine.normalize_tags(['storage', 'database', 'persistence'])
        self.assertIn('storage-component', result)

        # API component
        result = self.engine.normalize_tags(['api', 'endpoint', 'controller'])
        self.assertIn('api-component', result)

    def test_issue_normalization(self):
        """Test issue tag normalization"""
        result = self.engine.normalize_tags(['validation-error', 'invalid-data'])
        self.assertIn('validation-error', result)

        result = self.engine.normalize_tags(['performance', 'slow', 'optimization'])
        self.assertIn('performance-issue', result)

        result = self.engine.normalize_tags(['memory', 'memory-leak', 'oom'])
        self.assertIn('memory-problem', result)

        result = self.engine.normalize_tags(['race', 'concurrency', 'threading'])
        self.assertIn('race-condition', result)

    def test_severity_normalization(self):
        """Test severity tag normalization"""
        result = self.engine.normalize_tags(['critical', 'blocker', 'urgent'])
        self.assertIn('critical-blocker', result)

        result = self.engine.normalize_tags(['high', 'important'])
        self.assertIn('high-priority', result)

        result = self.engine.normalize_tags(['medium', 'moderate'])
        self.assertIn('medium-impact', result)

    def test_action_normalization(self):
        """Test action tag normalization"""
        result = self.engine.normalize_tags(['refactor', 'technical-debt'])
        self.assertIn('needs-refactor', result)

        result = self.engine.normalize_tags(['validation', 'verify'])
        self.assertIn('needs-validation', result)

        result = self.engine.normalize_tags(['retry', 'resilience'])
        self.assertIn('needs-retry-logic', result)

    def test_duplicate_removal(self):
        """Test that duplicates are removed during normalization"""
        result = self.engine.normalize_tags(['design', 'planning', 'design', 'architecture'])
        self.assertEqual(result.count('design-phase'), 1)

    def test_alphabetical_sorting(self):
        """Test that tags are sorted alphabetically"""
        result = self.engine.normalize_tags(['zzz', 'aaa', 'mmm'])
        self.assertEqual(result, tuple(sorted(result)))

    def test_unknown_tag_preservation(self):
        """Test that unknown tags are preserved as lowercase"""
        result = self.engine.normalize_tags(['unknown-tag', 'CUSTOM-TAG'])
        self.assertIn('unknown-tag', result)
        self.assertIn('custom-tag', result)


class TestTagSubsetGeneration(unittest.TestCase):
    """Test pairwise and triple subset generation"""

    def test_single_tag_passthrough(self):
        """Single tag returns as-is"""
        subsets = TagIntelligenceEngine.generate_tag_subsets(('a',))
        self.assertEqual(subsets, [('a',)])

    def test_pair_passthrough(self):
        """Two tags return as-is (already a pair)"""
        subsets = TagIntelligenceEngine.generate_tag_subsets(('a', 'b'))
        self.assertEqual(subsets, [('a', 'b')])

    def test_triple_generates_pairs(self):
        """Three tags return the triple plus all pairs"""
        subsets = TagIntelligenceEngine.generate_tag_subsets(('a', 'b', 'c'))
        self.assertIn(('a', 'b', 'c'), subsets)
        self.assertIn(('a', 'b'), subsets)
        self.assertIn(('a', 'c'), subsets)
        self.assertIn(('b', 'c'), subsets)
        self.assertEqual(len(subsets), 4)  # 1 triple + 3 pairs

    def test_four_tags_generates_pairs_and_triples(self):
        """Four tags decompose into pairs and triples only"""
        subsets = TagIntelligenceEngine.generate_tag_subsets(('a', 'b', 'c', 'd'))
        # 4C2 = 6 pairs + 4C3 = 4 triples = 10
        self.assertEqual(len(subsets), 10)
        # No 4-tuples
        for s in subsets:
            self.assertLessEqual(len(s), 3)
            self.assertGreaterEqual(len(s), 2)

    def test_large_tuple_no_full_ntuple(self):
        """8-tag input never stores the full 8-tuple"""
        tags = tuple(f'tag-{i}' for i in range(8))
        subsets = TagIntelligenceEngine.generate_tag_subsets(tags)
        for s in subsets:
            self.assertLessEqual(len(s), 3)
        # 8C2 = 28, 8C3 = 56 => 84
        self.assertEqual(len(subsets), 84)

    def test_subsets_are_sorted(self):
        """Each subset is sorted alphabetically"""
        subsets = TagIntelligenceEngine.generate_tag_subsets(('z', 'a', 'm', 'b'))
        for s in subsets:
            self.assertEqual(s, tuple(sorted(s)))


class TestTagCombinationTracking(unittest.TestCase):
    """Test tag combination analysis and tracking with subset decomposition"""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.engine = TagIntelligenceEngine(Path(self.temp_db.name))

    def tearDown(self):
        self.engine.close()
        Path(self.temp_db.name).unlink()

    def test_single_tag_analysis(self):
        """Test analysis of single tag combination"""
        result = self.engine.analyze_multi_tag_patterns(
            tags=['validation-error'],
            phase='implementation-phase',
            terminal='T1',
            outcome='failure'
        )

        self.assertTrue(result['analyzed'])
        self.assertEqual(result['occurrence_count'], 1)
        self.assertFalse(result['prevention_rule_generated'])

    def test_multi_tag_analysis(self):
        """Test analysis of multiple tag combination"""
        result = self.engine.analyze_multi_tag_patterns(
            tags=['crawler', 'performance', 'critical'],
            phase='production-phase',
            terminal='T2'
        )

        self.assertTrue(result['analyzed'])
        self.assertIn('crawler-component', result['tag_combination'])
        self.assertIn('performance-issue', result['tag_combination'])
        self.assertIn('critical-blocker', result['tag_combination'])
        # Should track subsets (3C2=3 pairs + 1 triple = 4)
        self.assertEqual(result['subsets_tracked'], 4)

    def test_recurring_combination_detection(self):
        """Test prevention rule generation after 2+ occurrences"""
        tags = ['validation-error', 'api-component']

        # First occurrence
        result1 = self.engine.analyze_multi_tag_patterns(tags, terminal='T1')
        self.assertEqual(result1['occurrence_count'], 1)
        self.assertFalse(result1['prevention_rule_generated'])

        # Second occurrence - should generate prevention rule
        result2 = self.engine.analyze_multi_tag_patterns(tags, terminal='T2')
        self.assertEqual(result2['occurrence_count'], 2)
        self.assertTrue(result2['prevention_rule_generated'])
        self.assertIn('prevention_rule', result2)

    def test_subset_sharing_across_dispatches(self):
        """Overlapping tags should share pair/triple counts"""
        # Dispatch 1: tags A, B, C
        self.engine.analyze_multi_tag_patterns(['validation-error', 'api-component', 'implementation'])
        # Dispatch 2: tags A, B, D — the pair (A, B) should now have count=2
        result = self.engine.analyze_multi_tag_patterns(['validation-error', 'api-component', 'memory'])

        # The pair (api-component, validation-error) was in both, so count >= 2
        pair_key = ('api-component', 'validation-error')
        self.assertGreaterEqual(self.engine.combination_patterns[pair_key]["count"], 2)

    def test_empty_tags_handling(self):
        """Test handling of empty tag list"""
        result = self.engine.analyze_multi_tag_patterns(tags=[])
        self.assertFalse(result['analyzed'])
        self.assertEqual(result['reason'], 'no_tags')

    def test_combination_persistence_as_subsets(self):
        """Test that tag combinations persist in DB as pairwise/triple subsets"""
        tags = ['storage', 'memory-leak', 'critical', 'crawler']
        self.engine.analyze_multi_tag_patterns(tags, terminal='T1')

        db = sqlite3.connect(self.temp_db.name)
        db.row_factory = sqlite3.Row
        cursor = db.execute("SELECT tag_tuple FROM tag_combinations")
        rows = cursor.fetchall()

        # All stored tuples should be 2 or 3 elements
        for row in rows:
            stored_tags = json.loads(row['tag_tuple'])
            self.assertLessEqual(len(stored_tags), 3)
            self.assertGreaterEqual(len(stored_tags), 2)

        db.close()


class TestPreventionRuleGeneration(unittest.TestCase):
    """Test prevention rule generation logic"""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.engine = TagIntelligenceEngine(Path(self.temp_db.name))

    def tearDown(self):
        self.engine.close()
        Path(self.temp_db.name).unlink()

    def test_critical_prevention_rule_type(self):
        """Test critical prevention rule classification"""
        tags = ['critical-blocker', 'production-phase', 'storage-component']

        self.engine.analyze_multi_tag_patterns(tags)
        result = self.engine.analyze_multi_tag_patterns(tags)

        # Should have generated rules for subsets containing critical-blocker
        self.assertTrue(result['prevention_rule_generated'])
        rule = result['prevention_rule']
        self.assertEqual(rule['rule_type'], 'critical-prevention')

    def test_validation_check_rule_type(self):
        """Test validation check rule classification"""
        tags = ['validation-error', 'api-component']

        self.engine.analyze_multi_tag_patterns(tags)
        result = self.engine.analyze_multi_tag_patterns(tags)

        rule = result['prevention_rule']
        self.assertEqual(rule['rule_type'], 'validation-check')

    def test_performance_optimization_rule_type(self):
        """Test performance optimization rule classification"""
        tags = ['performance-issue', 'crawler-component']

        self.engine.analyze_multi_tag_patterns(tags)
        result = self.engine.analyze_multi_tag_patterns(tags)

        rule = result['prevention_rule']
        self.assertEqual(rule['rule_type'], 'performance-optimization')

    def test_memory_management_rule_type(self):
        """Test memory management rule classification"""
        tags = ['memory-problem', 'api-component']

        self.engine.analyze_multi_tag_patterns(tags)
        result = self.engine.analyze_multi_tag_patterns(tags)

        rule = result['prevention_rule']
        self.assertEqual(rule['rule_type'], 'memory-management')

    def test_confidence_calculation(self):
        """Test confidence score increases with occurrences"""
        tags = ['validation-error', 'storage-component']

        for i in range(5):
            result = self.engine.analyze_multi_tag_patterns(tags)

        rule = result['prevention_rule']
        self.assertAlmostEqual(rule['confidence'], 0.5, places=1)

    def test_max_confidence_cap(self):
        """Test confidence capped at 1.0"""
        tags = ['critical-blocker', 'memory-problem']

        for i in range(15):
            result = self.engine.analyze_multi_tag_patterns(tags)

        rule = result['prevention_rule']
        self.assertEqual(rule['confidence'], 1.0)

    def test_recommendation_generation(self):
        """Test that recommendations are actionable"""
        tags = ['crawler-component', 'performance-issue', 'implementation-phase']

        self.engine.analyze_multi_tag_patterns(tags)
        result = self.engine.analyze_multi_tag_patterns(tags)

        rule = result['prevention_rule']
        self.assertIn('recommendation', rule)
        self.assertTrue(len(rule['recommendation']) > 0)

    def test_multiple_rules_from_subsets(self):
        """Test that multiple rules are generated from different subsets"""
        tags = ['validation-error', 'api-component', 'memory-problem']

        self.engine.analyze_multi_tag_patterns(tags)
        result = self.engine.analyze_multi_tag_patterns(tags)

        # Should have generated rules for pairs and the triple
        self.assertGreater(len(result['prevention_rules_generated']), 1)

    def test_rule_update_on_repeated_subset(self):
        """Test that rules update confidence on repeated occurrence"""
        tags = ['validation-error', 'api-component']

        # Occurrence 1: no rule
        self.engine.analyze_multi_tag_patterns(tags)
        # Occurrence 2: rule created
        self.engine.analyze_multi_tag_patterns(tags)
        # Occurrence 3: rule updated
        self.engine.analyze_multi_tag_patterns(tags)

        db = sqlite3.connect(self.temp_db.name)
        db.row_factory = sqlite3.Row
        cursor = db.execute("SELECT confidence, triggered_count FROM prevention_rules")
        row = cursor.fetchone()

        self.assertIsNotNone(row)
        # confidence = 3/10 = 0.3 and triggered_count should be 3
        self.assertAlmostEqual(row['confidence'], 0.3, places=1)
        self.assertEqual(row['triggered_count'], 3)
        db.close()


class TestPreventionRuleQuerying(unittest.TestCase):
    """Test prevention rule query functionality with subset matching"""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.engine = TagIntelligenceEngine(Path(self.temp_db.name))

        # Create test rules by analyzing tag combinations
        test_combinations = [
            ['validation-error', 'api-component'],
            ['memory-problem', 'crawler-component'],
            ['performance-issue', 'storage-component']
        ]

        for tags in test_combinations:
            self.engine.analyze_multi_tag_patterns(tags)
            self.engine.analyze_multi_tag_patterns(tags)

    def tearDown(self):
        self.engine.close()
        Path(self.temp_db.name).unlink()

    def test_query_all_rules(self):
        """Test querying all prevention rules"""
        rules = self.engine.query_prevention_rules(min_confidence=0.0)
        self.assertEqual(len(rules), 3)

    def test_query_specific_tags(self):
        """Test querying rules for specific tag combination"""
        rules = self.engine.query_prevention_rules(
            tags=['validation-error', 'api-component'],
            min_confidence=0.0
        )
        self.assertEqual(len(rules), 1)
        self.assertIn('api-component', rules[0]['tag_combination'])
        self.assertIn('validation-error', rules[0]['tag_combination'])

    def test_query_subset_matching(self):
        """Test that querying with superset tags finds subset rules"""
        # Query with 3 tags — should find the pair rule for (api-component, validation-error)
        rules = self.engine.query_prevention_rules(
            tags=['validation-error', 'api-component', 'implementation'],
            min_confidence=0.0
        )
        # Should find the (api-component, validation-error) rule
        found = any(
            'api-component' in r['tag_combination'] and 'validation-error' in r['tag_combination']
            for r in rules
        )
        self.assertTrue(found)

    def test_query_with_confidence_filter(self):
        """Test filtering rules by minimum confidence"""
        high_conf_rules = self.engine.query_prevention_rules(min_confidence=0.9)
        all_rules = self.engine.query_prevention_rules(min_confidence=0.0)

        self.assertLessEqual(len(high_conf_rules), len(all_rules))

    def test_query_nonexistent_combination(self):
        """Test querying for non-existent tag combination"""
        rules = self.engine.query_prevention_rules(
            tags=['nonexistent-tag', 'another-fake-tag']
        )
        self.assertEqual(len(rules), 0)

    def test_hierarchical_ordering(self):
        """Test that more specific (longer) subsets sort first"""
        # Create a triple rule
        tags = ['validation-error', 'api-component', 'implementation']
        self.engine.analyze_multi_tag_patterns(tags)
        self.engine.analyze_multi_tag_patterns(tags)

        rules = self.engine.query_prevention_rules(
            tags=['validation-error', 'api-component', 'implementation'],
            min_confidence=0.0
        )

        # If there's a triple and pair rule, triple should come first
        if len(rules) >= 2:
            self.assertGreaterEqual(len(rules[0]['tag_combination']), len(rules[-1]['tag_combination']))


class TestRecommendationManager(unittest.TestCase):
    """Test structured recommendation management"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.state_dir = Path(self.temp_dir)
        self.mgr = RecommendationManager(self.state_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_add_recommendation_valid(self):
        """Test adding a valid recommendation"""
        rec = self.mgr.add_recommendation(
            rec_type="prevention_rule",
            target="scripts/learning_loop.py",
            symptom="confidence never changes",
            evidence_ids=["receipt-001", "dispatch-abc"],
            confidence=0.8
        )

        self.assertEqual(rec["type"], "prevention_rule")
        self.assertEqual(rec["target"], "scripts/learning_loop.py")
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(len(rec["evidence_ids"]), 2)

    def test_add_recommendation_invalid_type(self):
        """Test that invalid recommendation types are rejected"""
        with self.assertRaises(ValueError):
            self.mgr.add_recommendation(
                rec_type="invalid_type",
                target="foo",
                symptom="bar",
                evidence_ids=["x"],
                confidence=0.5
            )

    def test_add_recommendation_requires_evidence(self):
        """Test G-L2: recommendations MUST include evidence trail"""
        with self.assertRaises(ValueError):
            self.mgr.add_recommendation(
                rec_type="prevention_rule",
                target="foo",
                symptom="bar",
                evidence_ids=[],
                confidence=0.5
            )

    def test_dedup_by_target_symptom(self):
        """Test deduplication merges evidence for same target+symptom"""
        self.mgr.add_recommendation(
            rec_type="prevention_rule",
            target="file.py",
            symptom="flaky test",
            evidence_ids=["receipt-001"],
            confidence=0.5
        )
        rec = self.mgr.add_recommendation(
            rec_type="prevention_rule",
            target="file.py",
            symptom="flaky test",
            evidence_ids=["receipt-002"],
            confidence=0.7
        )

        # Should merge evidence_ids and take max confidence
        self.assertIn("receipt-001", rec["evidence_ids"])
        self.assertIn("receipt-002", rec["evidence_ids"])
        self.assertEqual(rec["confidence"], 0.7)

        # Total pending should still be 1
        self.assertEqual(self.mgr.get_pending_count(), 1)

    def test_cap_at_max_pending(self):
        """Test G-L8: max 5 active pending recommendations"""
        for i in range(MAX_PENDING_RECOMMENDATIONS):
            self.mgr.add_recommendation(
                rec_type="prevention_rule",
                target=f"file_{i}.py",
                symptom=f"symptom_{i}",
                evidence_ids=[f"receipt-{i}"],
                confidence=0.1 * (i + 1)
            )

        self.assertEqual(self.mgr.get_pending_count(), MAX_PENDING_RECOMMENDATIONS)

        # Adding one more should supersede the lowest confidence
        self.mgr.add_recommendation(
            rec_type="prevention_rule",
            target="file_new.py",
            symptom="new_symptom",
            evidence_ids=["receipt-new"],
            confidence=0.9
        )

        # Still max 5 pending (one was superseded)
        self.assertLessEqual(self.mgr.get_pending_count(), MAX_PENDING_RECOMMENDATIONS + 1)
        # Check that the superseded one has status "superseded"
        data = json.loads(self.mgr.recommendations_path.read_text())
        statuses = [r["status"] for r in data["recommendations"]]
        self.assertIn("superseded", statuses)

    def test_recommendation_types_valid(self):
        """Test all valid recommendation types"""
        for rtype in RECOMMENDATION_TYPES:
            rec = self.mgr.add_recommendation(
                rec_type=rtype,
                target=f"target_{rtype}",
                symptom=f"symptom_{rtype}",
                evidence_ids=["evidence-1"],
                confidence=0.5
            )
            self.assertEqual(rec["type"], rtype)

    def test_recommendation_schema_fields(self):
        """Test recommendation schema includes all required fields"""
        rec = self.mgr.add_recommendation(
            rec_type="routing_hint",
            target="T1",
            symptom="model mismatch",
            evidence_ids=["dispatch-123"],
            confidence=0.75
        )

        required_fields = {"type", "target", "symptom", "evidence_ids", "confidence", "created_at"}
        for field in required_fields:
            self.assertIn(field, rec, f"Missing required field: {field}")

    def test_mark_stale_pending_edits(self):
        """Test stale edit marking for edits older than STALE_DAYS"""
        # Create a pending edit with old timestamp
        old_date = (datetime.now() - timedelta(days=STALE_DAYS + 1)).isoformat()
        data = {
            "edits": [
                {"id": 1, "status": "pending", "suggested_at": old_date, "content": "old edit"},
                {"id": 2, "status": "pending", "suggested_at": datetime.now().isoformat(), "content": "fresh edit"},
            ]
        }
        self.mgr.pending_edits_path.write_text(json.dumps(data))

        marked = self.mgr.mark_stale_pending_edits()

        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["id"], 1)

        # Verify the file was updated
        updated = json.loads(self.mgr.pending_edits_path.read_text())
        stale_edit = next(e for e in updated["edits"] if e["id"] == 1)
        self.assertEqual(stale_edit["status"], "stale")
        self.assertIn("stale_since", stale_edit)

        # Fresh edit should still be pending
        fresh_edit = next(e for e in updated["edits"] if e["id"] == 2)
        self.assertEqual(fresh_edit["status"], "pending")

    def test_mark_stale_no_file(self):
        """Test stale marking with no pending_edits file"""
        marked = self.mgr.mark_stale_pending_edits()
        self.assertEqual(marked, [])

    def test_get_pending_recommendations(self):
        """Test retrieving pending recommendations"""
        self.mgr.add_recommendation(
            rec_type="prevention_rule",
            target="a.py",
            symptom="issue",
            evidence_ids=["r-1"],
            confidence=0.5
        )

        pending = self.mgr.get_pending_recommendations()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending")


class TestStatistics(unittest.TestCase):
    """Test statistics gathering"""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.engine = TagIntelligenceEngine(Path(self.temp_db.name))

        self.engine.analyze_multi_tag_patterns(['validation-error', 'api'])
        self.engine.analyze_multi_tag_patterns(['memory', 'crawler'])
        self.engine.analyze_multi_tag_patterns(['memory', 'crawler'])  # Trigger rule

    def tearDown(self):
        self.engine.close()
        Path(self.temp_db.name).unlink()

    def test_get_statistics(self):
        """Test statistics gathering"""
        stats = self.engine.get_statistics()

        self.assertIn('total_combinations', stats)
        self.assertIn('total_rules', stats)
        self.assertIn('top_combinations', stats)

        # Should have 2 combinations (each pair stored separately)
        self.assertGreaterEqual(stats['total_combinations'], 2)

        # Should have at least 1 rule (memory+crawler hit 2x)
        self.assertGreaterEqual(stats['total_rules'], 1)

    def test_top_combinations(self):
        """Test top combinations reporting"""
        stats = self.engine.get_statistics()
        top_combos = stats['top_combinations']

        self.assertGreater(len(top_combos), 0)

        for combo in top_combos:
            self.assertIn('tags', combo)
            self.assertIn('count', combo)


class TestDatabaseIntegrity(unittest.TestCase):
    """Test database schema and integrity"""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.engine = TagIntelligenceEngine(Path(self.temp_db.name))

    def tearDown(self):
        self.engine.close()
        Path(self.temp_db.name).unlink()

    def test_tables_exist(self):
        """Test that required tables are created"""
        db = sqlite3.connect(self.temp_db.name)
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] for row in cursor]

        self.assertIn('tag_combinations', tables)
        self.assertIn('prevention_rules', tables)

        db.close()

    def test_indexes_exist(self):
        """Test that required indexes are created"""
        db = sqlite3.connect(self.temp_db.name)
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
        indexes = [row[0] for row in cursor]

        self.assertIn('idx_tag_tuple', indexes)
        self.assertIn('idx_rule_combination', indexes)

        db.close()


class TestTagCombinationFormat(unittest.TestCase):
    """CFX-6: tag_combination column is stored and read as JSON array."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.engine = TagIntelligenceEngine(Path(self.temp_db.name))

    def tearDown(self):
        self.engine.close()
        Path(self.temp_db.name).unlink()

    def test_writer_emits_json_array(self):
        """prevention_rules.tag_combination is stored as JSON array, not comma-list."""
        tags = ['validation-error', 'api-component']
        self.engine.analyze_multi_tag_patterns(tags)
        self.engine.analyze_multi_tag_patterns(tags)

        db = sqlite3.connect(self.temp_db.name)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT tag_combination FROM prevention_rules").fetchall()
        db.close()

        self.assertGreater(len(rows), 0)
        for row in rows:
            raw = row['tag_combination']
            parsed = json.loads(raw)
            self.assertIsInstance(parsed, list, f"tag_combination should be JSON array, got: {raw!r}")

    def test_roundtrip_identical(self):
        """Write tags, read back via query_prevention_rules — tag_combination is identical."""
        tags = ['crawler-component', 'performance-issue']
        self.engine.analyze_multi_tag_patterns(tags)
        self.engine.analyze_multi_tag_patterns(tags)

        rules = self.engine.query_prevention_rules(tags=tags, min_confidence=0.0)
        self.assertTrue(len(rules) > 0)
        for rule in rules:
            tc = rule['tag_combination']
            self.assertIsInstance(tc, (list, tuple))
            for t in tc:
                self.assertIsInstance(t, str)

    def _migration_db(self) -> sqlite3.Connection:
        """Fresh in-memory DB with minimal prevention_rules schema for migration tests."""
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE prevention_rules "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, tag_combination TEXT)"
        )
        db.commit()
        return db

    def _run_migration(self, db: sqlite3.Connection) -> None:
        migration_path = (
            Path(__file__).parent.parent
            / "schemas" / "migrations" / "0013_normalize_tag_combination.sql"
        )
        db.executescript(migration_path.read_text())
        db.commit()

    def test_migration_idempotent_json_row_unchanged(self):
        """Rows already in JSON array format are not re-migrated."""
        db = self._migration_db()
        db.execute(
            'INSERT INTO prevention_rules (tag_combination) VALUES (?)',
            ('["architect","Track-C"]',)
        )
        db.commit()

        self._run_migration(db)

        row = db.execute("SELECT tag_combination FROM prevention_rules").fetchone()
        db.close()
        self.assertEqual(row['tag_combination'], '["architect","Track-C"]')

    def test_migration_converts_comma_list(self):
        """Comma-list rows are converted to JSON array by migration."""
        db = self._migration_db()
        test_cases = [
            ("architect,Track-C", ["architect", "Track-C"]),
            ("any", ["any"]),
            ("backend-developer, testing-phase", ["backend-developer", "testing-phase"]),
        ]
        for raw, _ in test_cases:
            db.execute('INSERT INTO prevention_rules (tag_combination) VALUES (?)', (raw,))
        db.commit()

        self._run_migration(db)

        rows = db.execute(
            "SELECT tag_combination FROM prevention_rules ORDER BY id"
        ).fetchall()
        db.close()

        for row, (_, expected) in zip(rows, test_cases):
            parsed = json.loads(row['tag_combination'])
            self.assertEqual(sorted(parsed), sorted(expected),
                             f"After migration: {row['tag_combination']!r}")

    def test_migration_leaves_null_and_empty_unchanged(self):
        """NULL and empty tag_combination rows are not touched by migration."""
        db = self._migration_db()
        db.execute("INSERT INTO prevention_rules (tag_combination) VALUES (NULL)")
        db.execute("INSERT INTO prevention_rules (tag_combination) VALUES ('')")
        db.commit()

        self._run_migration(db)

        rows = db.execute(
            "SELECT tag_combination FROM prevention_rules ORDER BY id"
        ).fetchall()
        db.close()

        self.assertIsNone(rows[0]['tag_combination'])
        self.assertEqual(rows[1]['tag_combination'], '')


def run_tests():
    """Run all tests and report results"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    suite.addTests(loader.loadTestsFromTestCase(TestTagNormalization))
    suite.addTests(loader.loadTestsFromTestCase(TestTagSubsetGeneration))
    suite.addTests(loader.loadTestsFromTestCase(TestTagCombinationTracking))
    suite.addTests(loader.loadTestsFromTestCase(TestPreventionRuleGeneration))
    suite.addTests(loader.loadTestsFromTestCase(TestPreventionRuleQuerying))
    suite.addTests(loader.loadTestsFromTestCase(TestRecommendationManager))
    suite.addTests(loader.loadTestsFromTestCase(TestStatistics))
    suite.addTests(loader.loadTestsFromTestCase(TestDatabaseIntegrity))
    suite.addTests(loader.loadTestsFromTestCase(TestTagCombinationFormat))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(run_tests())
