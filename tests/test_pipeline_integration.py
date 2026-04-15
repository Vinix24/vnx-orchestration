#!/usr/bin/env python3
"""tests/test_pipeline_integration.py — F59-PR3 integration tests.

Covers:
  - Nightly pipeline runs behavioral analysis phases (shell script phase injection)
  - Dispatch enricher adds file affinities from behavioral patterns
  - Dispatch enricher adds duration baseline from behavioral patterns
  - Behavioral API endpoint returns valid structure
  - Dispatch list API returns valid structure
  - SSE endpoint headers are correct
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import threading
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make scripts/lib importable
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(_REPO_ROOT / "dashboard"))


# ---------------------------------------------------------------------------
# Helper: create an in-memory DB with behavior_analysis patterns
# ---------------------------------------------------------------------------

def _make_test_db(path: Path) -> None:
    """Populate a minimal quality_intelligence.db with behavior_analysis fixtures."""
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT,
            category TEXT,
            title TEXT,
            description TEXT,
            pattern_data TEXT,
            confidence_score REAL DEFAULT 0.5,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT,
            first_seen TEXT,
            last_used TEXT
        );
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT,
            rule_type TEXT,
            description TEXT,
            recommendation TEXT,
            confidence REAL DEFAULT 0.5,
            created_at TEXT,
            triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT,
            source TEXT
        );
    """)
    # Insert an affinity pattern
    affinity_data = json.dumps({
        "files": ["scripts/lib/dispatch_enricher.py", "scripts/lib/repo_map.py"],
        "co_occurrence": 0.75,
        "count": 12,
    })
    con.execute(
        """INSERT INTO success_patterns
           (pattern_type, category, title, description, pattern_data, confidence_score,
            usage_count, source_dispatch_ids, first_seen, last_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("behavioral", "behavior_analysis",
         "Files co-occur: scripts/lib/dispatch_enricher.py + scripts/lib/repo_map.py",
         "These files appear together often.",
         affinity_data, 0.75, 12,
         json.dumps(["d1", "d2"]),
         "2026-04-01T00:00:00Z", "2026-04-14T00:00:00Z"),
    )
    # Insert a duration baseline pattern
    baseline_data = json.dumps({
        "role": "backend-developer",
        "avg_seconds": 420.0,
        "count": 8,
        "min_seconds": 200.0,
        "max_seconds": 700.0,
    })
    con.execute(
        """INSERT INTO success_patterns
           (pattern_type, category, title, description, pattern_data, confidence_score,
            usage_count, source_dispatch_ids, first_seen, last_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("behavioral", "behavior_analysis",
         "Expected duration: backend-developer",
         "Average 7 minutes.",
         baseline_data, 0.8, 8,
         json.dumps(["d1", "d2"]),
         "2026-04-01T00:00:00Z", "2026-04-14T00:00:00Z"),
    )
    # Insert a prevention rule
    con.execute(
        """INSERT INTO prevention_rules
           (tag_combination, rule_type, description, recommendation, confidence,
            created_at, triggered_count, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("bash_error", "behavior_analysis",
         "ModuleNotFoundError: No module named 'repo_map'",
         "Add scripts/lib to sys.path before importing repo_map.",
         0.7, "2026-04-14T00:00:00Z", 5, "behavior_analysis"),
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# 1. test_nightly_pipeline_runs_behavioral_analysis
# ---------------------------------------------------------------------------

class TestNightlyPipelineBehavioralPhase(unittest.TestCase):
    """Verify that the behavioral analysis phases are present in the pipeline script."""

    def test_nightly_pipeline_runs_behavioral_analysis(self):
        """Pipeline script must contain phase names for event-analyze and pattern-extract."""
        pipeline_script = _REPO_ROOT / "scripts" / "nightly_intelligence_pipeline.sh"
        self.assertTrue(pipeline_script.exists(), "nightly_intelligence_pipeline.sh not found")

        text = pipeline_script.read_text(encoding="utf-8")
        self.assertIn("event_analyzer.py", text,
                      "Pipeline must call event_analyzer.py")
        self.assertIn("pattern_extractor.py", text,
                      "Pipeline must call pattern_extractor.py")
        self.assertIn("dispatch_behaviors.json", text,
                      "Pipeline must pass dispatch_behaviors.json as output/input")
        # Phases should appear before Phase 4 (learning cycle)
        pos_event = text.find("event_analyzer.py")
        pos_learning = text.find("4-learning-cycle")
        self.assertLess(pos_event, pos_learning,
                        "Behavioral analysis must run before learning cycle")


# ---------------------------------------------------------------------------
# 2. test_enricher_adds_file_affinities
# ---------------------------------------------------------------------------

class TestEnricherFileAffinities(unittest.TestCase):

    def test_enricher_adds_file_affinities(self):
        """DispatchEnricher Layer 4 appends file affinity suggestions when DB has patterns."""
        from dispatch_enricher import DispatchEnricher

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "quality_intelligence.db"
            _make_test_db(db_path)

            enricher = DispatchEnricher()

            with patch.object(
                DispatchEnricher,
                "_intelligence_db_path",
                return_value=db_path,
            ):
                instruction = (
                    "### Key files to read first\n"
                    "- `scripts/lib/dispatch_enricher.py`\n"
                    "- `scripts/lib/repo_map.py`\n"
                )
                metadata = {"role": "backend-developer", "track": "A"}
                enriched = enricher.enrich(instruction, metadata)

            # Layer 4 should NOT suggest files already in the target set
            # but it will still produce the section header when overlap exists
            # Both files are in the affinity pair — no new file to suggest, section omitted
            # Let's use only one file to trigger a suggestion
            instruction2 = (
                "### Key files to read first\n"
                "- `scripts/lib/dispatch_enricher.py`\n"
            )
            with patch.object(
                DispatchEnricher,
                "_intelligence_db_path",
                return_value=db_path,
            ):
                enriched2 = enricher.enrich(instruction2, metadata)

            self.assertIn("Suggested Additional Context Files", enriched2,
                          "Layer 4 must inject file affinity section")
            self.assertIn("repo_map.py", enriched2,
                          "Layer 4 must suggest the co-occurring file")


# ---------------------------------------------------------------------------
# 3. test_enricher_adds_duration_baseline
# ---------------------------------------------------------------------------

class TestEnricherDurationBaseline(unittest.TestCase):

    def test_enricher_adds_duration_baseline(self):
        """DispatchEnricher Layer 5 appends duration baseline for the dispatch role."""
        from dispatch_enricher import DispatchEnricher

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "quality_intelligence.db"
            _make_test_db(db_path)

            enricher = DispatchEnricher()
            instruction = "Do some backend work."
            metadata = {"role": "backend-developer", "track": "A", "no_repo_map": True}

            with patch.object(
                DispatchEnricher,
                "_intelligence_db_path",
                return_value=db_path,
            ):
                enriched = enricher.enrich(instruction, metadata)

            self.assertIn("Expected Duration", enriched,
                          "Layer 5 must inject duration baseline section")
            self.assertIn("backend-developer", enriched,
                          "Duration section must mention the role")
            self.assertIn("7.0 minutes", enriched,
                          "Duration section must show the computed average minutes")


# ---------------------------------------------------------------------------
# 4. test_behavioral_api_returns_data
# ---------------------------------------------------------------------------

class TestBehavioralAPI(unittest.TestCase):

    def test_behavioral_api_returns_data(self):
        """_intelligence_get_behavioral_summary returns dict with expected keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "quality_intelligence.db"
            _make_test_db(db_path)

            with patch.dict("os.environ", {"VNX_STATE_DIR": tmpdir}):
                # Re-import to pick up patched env
                import importlib
                import intelligence_dashboard_data
                importlib.reload(intelligence_dashboard_data)
                from intelligence_dashboard_data import get_behavioral_summary
                result = get_behavioral_summary()

        expected_keys = {
            "rework_files", "common_errors", "file_affinities",
            "duration_baselines", "exploration_insight",
            "total_dispatches_analyzed", "patterns_generated",
        }
        self.assertEqual(expected_keys, set(result.keys()),
                         f"Unexpected keys: {set(result.keys()) ^ expected_keys}")

        self.assertIsInstance(result["file_affinities"], list)
        self.assertIsInstance(result["duration_baselines"], list)
        self.assertIsInstance(result["common_errors"], list)

        # Should have the fixture patterns
        self.assertGreater(len(result["file_affinities"]), 0,
                           "Expected file affinity records from test DB")
        self.assertGreater(len(result["duration_baselines"]), 0,
                           "Expected duration baseline records from test DB")


# ---------------------------------------------------------------------------
# 5. test_dispatch_list_api
# ---------------------------------------------------------------------------

class TestDispatchListAPI(unittest.TestCase):

    def test_dispatch_list_api(self):
        """_scan_dispatches returns dict with 'stages' key and string dispatch IDs."""
        # Import api_operator via serve_dashboard path
        sys.path.insert(0, str(_REPO_ROOT / "dashboard"))
        try:
            from api_operator import _scan_dispatches
        except ImportError as exc:
            self.skipTest(f"api_operator not importable: {exc}")

        # Minimal mock for DISPATCHES_DIR in api_operator
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatches_dir = Path(tmpdir) / "dispatches"
            for stage in ("pending", "active", "completed", "staging", "rejected"):
                (dispatches_dir / stage).mkdir(parents=True)

            import api_operator
            original = api_operator.DISPATCHES_DIR
            try:
                api_operator.DISPATCHES_DIR = dispatches_dir
                result = _scan_dispatches()
            finally:
                api_operator.DISPATCHES_DIR = original

        self.assertIn("stages", result, "dispatch list must have 'stages' key")
        self.assertIn("total", result, "dispatch list must have 'total' key")
        self.assertIsInstance(result["total"], int)


# ---------------------------------------------------------------------------
# 6. test_sse_endpoint_streams_events
# ---------------------------------------------------------------------------

class TestSSEEndpoint(unittest.TestCase):

    def test_sse_endpoint_streams_events(self):
        """handle_events_stream sends SSE headers and data lines from NDJSON file."""
        sys.path.insert(0, str(_REPO_ROOT / "dashboard"))
        try:
            from api_intelligence import handle_events_stream
        except ImportError as exc:
            self.skipTest(f"api_intelligence not importable: {exc}")

        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir) / "events"
            events_dir.mkdir()
            events_file = events_dir / "T1.ndjson"
            # Write a test event that will appear after the stream starts
            test_event = json.dumps({
                "type": "tool_use",
                "timestamp": "2026-04-14T12:00:00Z",
                "terminal": "T1",
            })

            # Mock serve_dashboard constants so VNX_DATA_DIR points to tmpdir
            mock_sd = MagicMock()
            mock_sd.VNX_DATA_DIR = Path(tmpdir)
            mock_sd.REPORTS_DIR = Path(tmpdir) / "unified_reports"
            mock_sd.RECEIPTS_PATH = Path(tmpdir) / "t0_receipts.ndjson"

            # Build a fake handler that records writes
            written_chunks = []

            class FakeWfile:
                def write(self, data):
                    written_chunks.append(data)
                def flush(self):
                    pass

            mock_handler = MagicMock()
            mock_handler.wfile = FakeWfile()

            # Create the events file empty, then write an event after a delay
            events_file.write_text("")

            import api_intelligence
            original_sd_fn = api_intelligence._sd

            def mock_sd_fn():
                return mock_sd

            api_intelligence._sd = mock_sd_fn

            # Run handle_events_stream in a thread with a very short poll,
            # inject a line, then break connection after first keepalive
            def write_event_and_break():
                time.sleep(0.1)
                with open(events_file, "a") as fh:
                    fh.write(test_event + "\n")
                time.sleep(0.2)
                # Simulate disconnect
                mock_handler.wfile.write = lambda d: (_ for _ in ()).throw(BrokenPipeError())

            thread = threading.Thread(target=write_event_and_break, daemon=True)
            thread.start()

            try:
                with patch("api_intelligence._SSE_POLL_INTERVAL", 0.05):
                    with patch("api_intelligence._SSE_KEEPALIVE_INTERVAL", 0.05):
                        handle_events_stream(mock_handler, "T1")
            finally:
                api_intelligence._sd = original_sd_fn
                thread.join(timeout=2.0)

            # Verify SSE response headers were sent
            mock_handler.send_response.assert_called_once_with(200)
            header_calls = [str(call) for call in mock_handler.send_header.call_args_list]
            content_type_sent = any("text/event-stream" in c for c in header_calls)
            self.assertTrue(content_type_sent,
                            "SSE endpoint must send Content-Type: text/event-stream")

            # Verify at least one data line was written
            all_written = b"".join(
                chunk for chunk in written_chunks if isinstance(chunk, bytes)
            )
            self.assertIn(b"data:", all_written,
                          "SSE endpoint must write 'data:' lines")


if __name__ == "__main__":
    unittest.main(verbosity=2)
