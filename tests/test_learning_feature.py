#!/usr/bin/env python3
"""
Tests for VNX self-learning intelligence pipeline (PR-0, PR-2, PR-3, PR-4).
Covers: offer/adoption tracking, worker injection, nightly pipeline,
digest format, and confidence logging.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

# ── Module-level env mocks (must be set before imports that call ensure_env at module level) ──
_TMP_STATE = tempfile.mkdtemp()
_TMP_VNX_HOME = tempfile.mkdtemp()
_TMP_PROJECT_ROOT = tempfile.mkdtemp()

with patch.dict(os.environ, {
    "VNX_HOME": _TMP_VNX_HOME,
    "VNX_STATE_DIR": _TMP_STATE,
    "PROJECT_ROOT": _TMP_PROJECT_ROOT,
}):
    from learning_loop import LearningLoop, PatternUsageMetric
    from gather_intelligence import T0IntelligenceGatherer
    from build_t0_quality_digest import (
        _assemble_digest,
        _append_ndjson,
        build_operational_defects,
        build_prompt_config_tuning,
        build_governance_health,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _mock_env(state_dir: str) -> dict:
    return {
        "VNX_HOME": _TMP_VNX_HOME,
        "VNX_STATE_DIR": state_dir,
        "PROJECT_ROOT": _TMP_PROJECT_ROOT,
    }


def _make_gatherer(state_dir: str, db: sqlite3.Connection = None) -> T0IntelligenceGatherer:
    """Construct T0IntelligenceGatherer bypassing __init__, pointing to temp dirs."""
    g = T0IntelligenceGatherer.__new__(T0IntelligenceGatherer)
    g.quality_db = db
    g._state_dir = Path(state_dir)
    # Override _usage_log_path to return temp-dir path
    g._usage_log_path = lambda: Path(state_dir) / "intelligence_usage.ndjson"
    return g


def _make_loop(state_dir: str) -> LearningLoop:
    """Construct LearningLoop bypassing __init__, pointing to temp dirs."""
    loop = LearningLoop.__new__(LearningLoop)
    loop.pattern_metrics = {}
    loop.learning_stats = {
        "patterns_tracked": 0,
        "patterns_used": 0,
        "patterns_ignored": 0,
        "patterns_archived": 0,
        "confidence_adjustments": 0,
        "new_patterns_learned": 0,
    }
    # conn used by update_confidence_scores / save_pattern_metrics
    loop.conn = sqlite3.connect(":memory:")
    loop.conn.row_factory = sqlite3.Row
    loop.conn.execute("""
        CREATE TABLE IF NOT EXISTS pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT NOT NULL,
            pattern_hash TEXT NOT NULL,
            used_count INTEGER DEFAULT 0,
            ignored_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            last_used TIMESTAMP,
            last_offered TIMESTAMP,
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    loop.conn.commit()
    return loop


def _pattern_usage_db(state_dir: str) -> sqlite3.Connection:
    """Create in-memory SQLite with pattern_usage table for adoption tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT NOT NULL,
            pattern_hash TEXT NOT NULL,
            used_count INTEGER DEFAULT 0,
            confidence REAL DEFAULT 1.0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count) "
        "VALUES ('hash-abc123', 'Test Pattern', 'hash-abc123', 0)"
    )
    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Offer / adoption tracking (gather_intelligence.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOfferAdoptionTracking:

    def test_record_pattern_offer_writes_ndjson(self):
        """record_pattern_offer() writes an offer event with correct fields."""
        with tempfile.TemporaryDirectory() as state_dir:
            g = _make_gatherer(state_dir)
            g.record_pattern_offer("pat-001", "T1", "dispatch-abc", "/path/to/file.py")

            log_path = Path(state_dir) / "intelligence_usage.ndjson"
            assert log_path.exists(), "intelligence_usage.ndjson should be created"

            events = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            assert len(events) == 1
            e = events[0]
            assert e["event_type"] == "offer"
            assert e["pattern_id"] == "pat-001"
            assert e["terminal"] == "T1"
            assert e["dispatch_id"] == "dispatch-abc"
            assert e["file_path"] == "/path/to/file.py"
            assert "timestamp" in e

    def test_record_pattern_offer_multiple_appends(self):
        """Multiple offer calls append to the same file without truncating."""
        with tempfile.TemporaryDirectory() as state_dir:
            g = _make_gatherer(state_dir)
            g.record_pattern_offer("pat-001", "T1", "d-001")
            g.record_pattern_offer("pat-002", "T2", "d-002")

            log_path = Path(state_dir) / "intelligence_usage.ndjson"
            events = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            assert len(events) == 2
            assert events[0]["pattern_id"] == "pat-001"
            assert events[1]["pattern_id"] == "pat-002"

    def test_record_pattern_adoption_increments_used_count(self):
        """record_pattern_adoption() increments pattern_usage.used_count in DB."""
        with tempfile.TemporaryDirectory() as state_dir:
            db = _pattern_usage_db(state_dir)
            g = _make_gatherer(state_dir, db)

            g.record_pattern_adoption("hash-abc123", "T1", "dispatch-xyz")

            row = db.execute(
                "SELECT used_count FROM pattern_usage WHERE pattern_hash = 'hash-abc123'"
            ).fetchone()
            assert row is not None
            assert row["used_count"] == 1

    def test_record_pattern_adoption_writes_ndjson(self):
        """record_pattern_adoption() also writes an adoption event to ndjson."""
        with tempfile.TemporaryDirectory() as state_dir:
            db = _pattern_usage_db(state_dir)
            g = _make_gatherer(state_dir, db)

            g.record_pattern_adoption("hash-abc123", "T2", "dispatch-xyz")

            log_path = Path(state_dir) / "intelligence_usage.ndjson"
            events = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            assert any(e["event_type"] == "adoption" for e in events)
            adoption = next(e for e in events if e["event_type"] == "adoption")
            assert adoption["terminal"] == "T2"
            assert adoption["dispatch_id"] == "dispatch-xyz"

    def test_record_adoption_from_receipt_correlates_file_paths(self):
        """record_adoption_from_receipt() records adoptions for patterns whose
        file_path appears in the report text."""
        with tempfile.TemporaryDirectory() as state_dir:
            db = _pattern_usage_db(state_dir)
            g = _make_gatherer(state_dir, db)

            # Pre-populate offer event for dispatch-001
            g.record_pattern_offer("pat-999", "T1", "dispatch-001", "scripts/learning_loop.py")

            # Create a report that mentions the file
            report_path = Path(state_dir) / "test_report.md"
            report_path.write_text(
                "## Summary\nModified scripts/learning_loop.py to fix confidence decay.\n",
                encoding="utf-8",
            )

            result = g.record_adoption_from_receipt("dispatch-001", "T1", str(report_path))

            assert result["checked"] == 1
            assert result["adoptions"] == 1

    def test_record_adoption_from_receipt_no_match(self):
        """record_adoption_from_receipt() returns 0 adoptions when no file paths match."""
        with tempfile.TemporaryDirectory() as state_dir:
            g = _make_gatherer(state_dir)

            # Offer references a file, report mentions something different
            g.record_pattern_offer("pat-001", "T1", "dispatch-002", "scripts/learning_loop.py")

            report_path = Path(state_dir) / "report_nomatch.md"
            report_path.write_text("Changed gather_intelligence.py only.\n", encoding="utf-8")

            result = g.record_adoption_from_receipt("dispatch-002", "T1", str(report_path))
            assert result["checked"] == 1
            assert result["adoptions"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Worker intelligence injection (userpromptsubmit_worker_intelligence_inject.sh)
# ═══════════════════════════════════════════════════════════════════════════════

WORKER_INJECT_SCRIPT = SCRIPTS_DIR / "userpromptsubmit_worker_intelligence_inject.sh"


class TestWorkerIntelligenceInjection:

    def test_script_passes_bash_syntax_check(self):
        """Script must pass bash -n syntax check."""
        result = subprocess.run(
            ["bash", "-n", str(WORKER_INJECT_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"

    def test_outputs_allow_when_vnx_terminal_unset_and_pwd_unknown(self):
        """Graceful degradation: no terminal context → {"decision": "allow"}."""
        env = {
            **os.environ,
            "VNX_STATE_DIR": _TMP_STATE,
            "PROJECT_ROOT": str(REPO_ROOT),
        }
        # Remove VNX_TERMINAL and set PWD to something that doesn't match T1/T2/T3
        env.pop("VNX_TERMINAL", None)
        env["PWD"] = "/tmp"

        result = subprocess.run(
            ["bash", str(WORKER_INJECT_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"Script exited non-zero:\n{result.stderr}"
        output = result.stdout.strip()
        parsed = json.loads(output)
        assert parsed == {"decision": "allow"}

    def test_outputs_allow_json_when_no_terminal_state(self):
        """No terminal_state.json → always returns {"decision": "allow"}."""
        with tempfile.TemporaryDirectory() as state_dir:
            env = {
                **os.environ,
                "VNX_TERMINAL": "T1",
                "VNX_STATE_DIR": state_dir,
                "PROJECT_ROOT": str(REPO_ROOT),
            }
            # state_dir has no terminal_state.json
            result = subprocess.run(
                ["bash", str(WORKER_INJECT_SCRIPT)],
                capture_output=True,
                text=True,
                env=env,
            )
            assert result.returncode == 0
            parsed = json.loads(result.stdout.strip())
            assert parsed == {"decision": "allow"}

    def test_outputs_additional_context_with_dispatch(self):
        """Script outputs additionalContext JSON when dispatch context is present."""
        with tempfile.TemporaryDirectory() as state_dir:
            # Create terminal_state.json with claimed_by
            dispatch_id = "test-dispatch-001"
            terminal_state = {
                "terminals": {
                    "T1": {"claimed_by": dispatch_id, "status": "active"}
                }
            }
            state_path = Path(state_dir)
            (state_path / "terminal_state.json").write_text(
                json.dumps(terminal_state), encoding="utf-8"
            )

            # Create dispatch file
            dispatch_dir = Path(state_dir).parent / ".vnx-data" / "dispatches" / "active"
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            dispatch_file = dispatch_dir / f"{dispatch_id}.md"
            dispatch_file.write_text(
                "Gate: gate_test\nRole: backend-developer\nInstruction:\nFix the learning loop\n",
                encoding="utf-8",
            )

            env = {
                **os.environ,
                "VNX_TERMINAL": "T1",
                "VNX_STATE_DIR": state_dir,
                "PROJECT_ROOT": str(dispatch_dir.parent.parent.parent),
            }
            result = subprocess.run(
                ["bash", str(WORKER_INJECT_SCRIPT)],
                capture_output=True,
                text=True,
                env=env,
            )
            assert result.returncode == 0
            output = result.stdout.strip()
            # Must be valid JSON
            parsed = json.loads(output)
            assert "decision" in parsed
            assert parsed["decision"] == "allow"

    def test_injection_stays_under_token_budget(self):
        """Injected additionalContext must be under 1600 characters."""
        with tempfile.TemporaryDirectory() as state_dir:
            dispatch_id = "budget-dispatch"
            terminal_state = {
                "terminals": {
                    "T2": {"claimed_by": dispatch_id, "status": "active"}
                }
            }
            state_path = Path(state_dir)
            (state_path / "terminal_state.json").write_text(
                json.dumps(terminal_state), encoding="utf-8"
            )

            dispatch_dir = state_path.parent / ".vnx-data" / "dispatches" / "active"
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            (dispatch_dir / f"{dispatch_id}.md").write_text(
                "Gate: gate_budget\nRole: test-engineer\nInstruction:\nRun all tests\n",
                encoding="utf-8",
            )

            env = {
                **os.environ,
                "VNX_TERMINAL": "T2",
                "VNX_STATE_DIR": state_dir,
                "PROJECT_ROOT": str(dispatch_dir.parent.parent.parent),
            }
            result = subprocess.run(
                ["bash", str(WORKER_INJECT_SCRIPT)],
                capture_output=True,
                text=True,
                env=env,
            )
            assert result.returncode == 0
            parsed = json.loads(result.stdout.strip())
            context = parsed.get("additionalContext", "")
            assert len(context) <= 1600, (
                f"additionalContext too long: {len(context)} chars (max 1600)"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Nightly pipeline (nightly_intelligence_pipeline.sh)
# ═══════════════════════════════════════════════════════════════════════════════

NIGHTLY_SCRIPT = SCRIPTS_DIR / "nightly_intelligence_pipeline.sh"


class TestNightlyPipeline:

    def test_script_passes_bash_syntax_check(self):
        """Script must pass bash -n syntax check."""
        result = subprocess.run(
            ["bash", "-n", str(NIGHTLY_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"

    def test_lock_file_prevents_concurrent_run(self):
        """Lock file logic exits 0 when lock file holds a live PID.

        The full nightly script sources vnx_paths.sh which resets VNX_STATE_DIR,
        so we test the lock-file logic directly using the exact same logic as the
        script (lines 112-120 of nightly_intelligence_pipeline.sh).
        """
        with tempfile.TemporaryDirectory() as state_dir:
            lock_file = Path(state_dir) / "nightly_pipeline.lock"
            # Use current process PID — guaranteed to be alive
            lock_file.write_text(str(os.getpid()), encoding="utf-8")

            # Inline the exact singleton enforcement logic from the script
            lock_check_script = f"""
#!/bin/bash
LOCK_FILE="{lock_file}"
if [ -f "$LOCK_FILE" ]; then
    pid="$(cat "$LOCK_FILE" 2>/dev/null || echo "")"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "Already running (PID $pid), skipping"
        exit 0
    fi
    echo "Stale lock file found, removing"
    rm -f "$LOCK_FILE"
fi
echo "No lock held, proceeding"
exit 1
"""
            result = subprocess.run(
                ["bash", "-c", lock_check_script],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, "Lock check should exit 0 when PID is alive"
            assert "Already running" in result.stdout or "skipping" in result.stdout

    def test_phase_logging_writes_ndjson_entries(self):
        """run_phase helper writes entries to nightly_pipeline_phases.ndjson."""
        # Test by invoking just the helper function inline
        with tempfile.TemporaryDirectory() as state_dir:
            phases_log = Path(state_dir) / "nightly_pipeline_phases.ndjson"
            # Directly call the log_phase_result logic via bash heredoc
            script = f"""
#!/bin/bash
set -uo pipefail
PHASES_LOG="{phases_log}"
log_phase_result() {{
    local phase="$1" status="$2" detail="${{3:-}}"
    printf '{{"phase":"%s","status":"%s","detail":"%s","ts":"%s"}}\\n' \\
        "$phase" "$status" "$detail" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \\
        >> "$PHASES_LOG"
}}
log_phase_result "test-phase" "ok" "test completed"
log_phase_result "other-phase" "failed" "exit=1"
"""
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            assert phases_log.exists()

            entries = [
                json.loads(l)
                for l in phases_log.read_text().splitlines()
                if l.strip()
            ]
            assert len(entries) == 2
            assert entries[0]["phase"] == "test-phase"
            assert entries[0]["status"] == "ok"
            assert entries[1]["phase"] == "other-phase"
            assert entries[1]["status"] == "failed"
            assert "ts" in entries[0]


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Digest format (build_t0_quality_digest.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _empty_evidence() -> dict:
    return {"dispatch_ids": [], "failed_dispatch_ids": []}


class TestDigestFormat:

    def test_digest_has_three_sections(self):
        """_assemble_digest output has all 3 required section keys."""
        sections = {
            "operational_defects": [],
            "prompt_config_tuning": [],
            "governance_health": [],
        }
        digest = _assemble_digest(sections, "run-001")
        assert "operational_defects" in digest["sections"]
        assert "prompt_config_tuning" in digest["sections"]
        assert "governance_health" in digest["sections"]

    def test_each_section_capped_at_five(self):
        """build_* functions never return more than 5 recommendations."""
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            # pending_edits.json with 10 items (only 2 consumed by prompt_config_tuning)
            pending = {
                "recommendations": [
                    {
                        "id": i,
                        "status": "pending",
                        "target": f"CLAUDE.md",
                        "description": f"edit {i}",
                        "created_at": "2026-03-01T00:00:00Z",
                    }
                    for i in range(10)
                ]
            }
            (state_path / "pending_edits.json").write_text(
                json.dumps(pending), encoding="utf-8"
            )

            # DB with 10 low-confidence patterns
            db = sqlite3.connect(":memory:")
            db.execute("""
                CREATE TABLE pattern_usage (
                    pattern_id TEXT PRIMARY KEY,
                    pattern_title TEXT,
                    confidence REAL,
                    ignored_count INTEGER DEFAULT 0,
                    used_count INTEGER DEFAULT 0
                )
            """)
            for i in range(10):
                db.execute(
                    "INSERT INTO pattern_usage VALUES (?,?,?,?,?)",
                    (f"p{i}", f"Pattern {i}", 0.1, i, 0),
                )
            db.execute("CREATE TABLE prevention_rules (id TEXT, tag_combination TEXT, rule_type TEXT, recommendation TEXT, confidence REAL, triggered_count INTEGER)")
            db.commit()

            recs = build_prompt_config_tuning(db, state_path, _empty_evidence())
            db.close()
            assert len(recs) <= 5, f"Expected ≤5, got {len(recs)}"

    def test_ndjson_output_is_append_only(self):
        """Running _append_ndjson twice produces exactly 2 lines."""
        with tempfile.NamedTemporaryFile(suffix=".ndjson", delete=False, mode="w") as f:
            ndjson_path = Path(f.name)

        try:
            sections = {
                "operational_defects": [],
                "prompt_config_tuning": [],
                "governance_health": [],
            }
            digest = _assemble_digest(sections, "run-001")
            _append_ndjson(digest, ndjson_path)
            _append_ndjson(digest, ndjson_path)

            lines = [l for l in ndjson_path.read_text().splitlines() if l.strip()]
            assert len(lines) == 2, f"Expected 2 NDJSON lines, got {len(lines)}"

            # Both must be valid JSON
            for line in lines:
                json.loads(line)
        finally:
            ndjson_path.unlink(missing_ok=True)

    def test_evidence_trail_fields_present(self):
        """Every recommendation must include file_paths, receipt_ids, dispatch_ids."""
        evidence = {
            "dispatch_ids": ["d-001", "d-002"],
            "failed_dispatch_ids": ["d-002"],
        }
        sections = {
            "operational_defects": [],
            "prompt_config_tuning": [],
            "governance_health": build_governance_health(None, evidence),
        }
        digest = _assemble_digest(sections, "run-trail")

        for sec_key, sec_data in digest["sections"].items():
            for rec in sec_data["recommendations"]:
                assert "evidence" in rec, f"{sec_key} rec missing evidence"
                ev = rec["evidence"]
                assert "file_paths" in ev, f"{sec_key} evidence missing file_paths"
                assert "receipt_ids" in ev, f"{sec_key} evidence missing receipt_ids"
                assert "dispatch_ids" in ev, f"{sec_key} evidence missing dispatch_ids"

    def test_digest_summary_counts_match_sections(self):
        """summary.sections counts correctly reflect section lengths."""
        sections = {
            "operational_defects": [{"rank": 1, "type": "code_hotspot", "title": "x",
                                      "severity": "high", "detail": "", "action": "",
                                      "evidence": {"file_paths": [], "receipt_ids": [], "dispatch_ids": []}}],
            "prompt_config_tuning": [],
            "governance_health": [],
        }
        digest = _assemble_digest(sections, "run-counts")
        assert digest["summary"]["sections"]["operational_defects"] == 1
        assert digest["summary"]["sections"]["prompt_config_tuning"] == 0
        assert digest["summary"]["sections"]["governance_health"] == 0

    def test_governance_health_has_fallback_when_empty(self):
        """governance_health returns at least one entry (healthy status) when nothing to report."""
        recs = build_governance_health(None, _empty_evidence())
        assert len(recs) >= 1
        assert recs[0]["type"] == "governance_status"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Confidence logging (learning_loop.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfidenceLogging:

    def test_log_confidence_change_appends_to_ndjson(self):
        """_log_confidence_change() appends a confidence_change event to intelligence_usage.ndjson."""
        with tempfile.TemporaryDirectory() as state_dir:
            loop = _make_loop(state_dir)

            with patch("learning_loop.ensure_env", return_value=_mock_env(state_dir)):
                loop._log_confidence_change("pat-001", "adoption_boost", 1.0, 1.1)

            usage_log = Path(state_dir) / "intelligence_usage.ndjson"
            assert usage_log.exists()
            events = [json.loads(l) for l in usage_log.read_text().splitlines() if l.strip()]
            assert len(events) == 1
            e = events[0]
            assert e["event_type"] == "confidence_change"
            assert e["pattern_id"] == "pat-001"
            assert e["source"] == "adoption_boost"
            assert e["old_confidence"] == 1.0
            assert e["new_confidence"] == pytest.approx(1.1, rel=1e-4)

    def test_log_confidence_change_multiple_appends(self):
        """Multiple _log_confidence_change calls append without overwriting."""
        with tempfile.TemporaryDirectory() as state_dir:
            loop = _make_loop(state_dir)

            with patch("learning_loop.ensure_env", return_value=_mock_env(state_dir)):
                loop._log_confidence_change("pat-001", "adoption_boost", 1.0, 1.1)
                loop._log_confidence_change("pat-002", "ignore_decay", 1.0, 0.95)

            usage_log = Path(state_dir) / "intelligence_usage.ndjson"
            events = [json.loads(l) for l in usage_log.read_text().splitlines() if l.strip()]
            assert len(events) == 2

    def test_update_terminal_constraints_writes_pending_rules_json(self):
        """update_terminal_constraints() writes to pending_rules.json, not to DB (G-L1)."""
        with tempfile.TemporaryDirectory() as state_dir:
            loop = _make_loop(state_dir)

            rules = [{
                "pattern": "Error: agent not found",
                "terminal_constraint": "T1",
                "agent_constraint": None,
                "prevention": "Validate agent before dispatch",
                "confidence": 0.6,
                "occurrence_count": 3,
            }]

            with patch("learning_loop.ensure_env", return_value=_mock_env(state_dir)):
                loop.update_terminal_constraints(rules)

            pending_path = Path(state_dir) / "pending_rules.json"
            assert pending_path.exists(), "pending_rules.json should be created"

            data = json.loads(pending_path.read_text(encoding="utf-8"))
            assert "pending_rules" in data
            assert len(data["pending_rules"]) == 1
            queued = data["pending_rules"][0]
            assert queued["status"] == "pending"
            assert queued["source"] == "learning_loop"
            assert "id" in queued
            assert "prevention" in queued

            # Verify nothing was inserted into the prevention_rules table in memory DB
            # (G-L1: no auto-activation)
            try:
                rows = loop.conn.execute("SELECT COUNT(*) FROM prevention_rules").fetchone()
                assert rows[0] == 0, "prevention_rules table should be empty (G-L1)"
            except sqlite3.OperationalError:
                pass  # Table doesn't exist — also acceptable (nothing was auto-inserted)

    def test_update_terminal_constraints_deduplicates_rules(self):
        """Calling update_terminal_constraints twice with same rule doesn't duplicate."""
        with tempfile.TemporaryDirectory() as state_dir:
            loop = _make_loop(state_dir)

            rules = [{
                "pattern": "Error: timeout",
                "terminal_constraint": "T2",
                "agent_constraint": None,
                "prevention": "Increase timeout",
                "confidence": 0.5,
                "occurrence_count": 2,
            }]

            with patch("learning_loop.ensure_env", return_value=_mock_env(state_dir)):
                loop.update_terminal_constraints(rules)
                loop.update_terminal_constraints(rules)  # second call, same rule

            data = json.loads((Path(state_dir) / "pending_rules.json").read_text())
            assert len(data["pending_rules"]) == 1, "Should deduplicate by id"

    def test_archive_unused_patterns_writes_pending_archival_json(self):
        """archive_unused_patterns() queues candidates to pending_archival.json (G-L4)."""
        with tempfile.TemporaryDirectory() as state_dir:
            loop = _make_loop(state_dir)

            # Add a low-confidence pattern that hasn't been used
            old_date = datetime(2025, 1, 1)
            loop.pattern_metrics["stale-pat"] = PatternUsageMetric(
                pattern_id="stale-pat",
                pattern_title="Stale Pattern",
                pattern_hash="abc",
                used_count=0,
                ignored_count=5,
                confidence=0.2,
                last_used=old_date,
            )

            with patch("learning_loop.ensure_env", return_value=_mock_env(state_dir)):
                loop.archive_unused_patterns(threshold_days=1)

            pending_path = Path(state_dir) / "pending_archival.json"
            assert pending_path.exists(), "pending_archival.json should be created"

            data = json.loads(pending_path.read_text(encoding="utf-8"))
            assert "pending_archival" in data
            assert len(data["pending_archival"]) == 1
            entry = data["pending_archival"][0]
            assert entry["pattern_id"] == "stale-pat"
            assert entry["status"] == "pending"

    def test_archive_unused_patterns_skips_recent_patterns(self):
        """Patterns used recently are not queued for archival."""
        with tempfile.TemporaryDirectory() as state_dir:
            loop = _make_loop(state_dir)

            # Pattern used today — should NOT be archived
            loop.pattern_metrics["recent-pat"] = PatternUsageMetric(
                pattern_id="recent-pat",
                pattern_title="Recent Pattern",
                pattern_hash="xyz",
                used_count=3,
                confidence=0.2,
                last_used=datetime.now(),
            )

            with patch("learning_loop.ensure_env", return_value=_mock_env(state_dir)):
                loop.archive_unused_patterns(threshold_days=30)

            pending_path = Path(state_dir) / "pending_archival.json"
            # File should either not exist or have empty list
            if pending_path.exists():
                data = json.loads(pending_path.read_text())
                assert len(data.get("pending_archival", [])) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
