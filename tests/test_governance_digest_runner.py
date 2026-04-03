#!/usr/bin/env python3
"""Tests for GovernanceDigestRunner in intelligence_daemon.py.

Quality gate: gate_pr1_digest_runner
- GovernanceDigestRunner produces governance_digest.json under test
- Runner calls F18 extractors and digest builder
- Output matches contracted JSON shape
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers (mirrors test_intelligence_daemon_paths.py pattern)
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

LIB_DIR = SCRIPTS_DIR / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))


def _install_stub_modules():
    """Install lightweight stub modules so intelligence_daemon imports succeed."""
    gather_mod = types.ModuleType("gather_intelligence")
    learning_mod = types.ModuleType("learning_loop")
    cached_mod = types.ModuleType("cached_intelligence")
    tag_mod = types.ModuleType("tag_intelligence")

    class DummyGatherer:
        def __init__(self):
            self.quality_db = None

    class DummyLearningLoop:
        def daily_learning_cycle(self):
            return {"statistics": {}, "pattern_metrics": {}}

    class DummyCachedIntelligence:
        def update_pattern_rankings(self):
            pass

    class DummyTagEngine:
        def extract_tags_from_dispatch(self, _): return []
        def normalize_tags(self, _): return []
        def analyze_multi_tag_patterns(self, *a, **kw): pass
        def close(self): pass

    gather_mod.T0IntelligenceGatherer = DummyGatherer
    learning_mod.LearningLoop = DummyLearningLoop
    cached_mod.CachedIntelligence = DummyCachedIntelligence
    tag_mod.TagIntelligenceEngine = DummyTagEngine

    for name, mod in [
        ("gather_intelligence", gather_mod),
        ("learning_loop", learning_mod),
        ("cached_intelligence", cached_mod),
        ("tag_intelligence", tag_mod),
    ]:
        sys.modules[name] = mod


def _load_daemon_module():
    _install_stub_modules()
    if "intelligence_daemon" in sys.modules:
        del sys.modules["intelligence_daemon"]
    return importlib.import_module("intelligence_daemon")


def _make_daemon(tmp_path, monkeypatch) -> tuple:
    """Return (daemon_module, daemon_instance) wired to tmp_path."""
    vnx_home = tmp_path / "vnx-home"
    state_dir = tmp_path / "data" / "state"
    vnx_home.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_HOME", str(vnx_home))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    mod = _load_daemon_module()
    return mod, state_dir


# ---------------------------------------------------------------------------
# GovernanceDigestRunner — unit tests (direct instantiation)
# ---------------------------------------------------------------------------

class TestGovernanceDigestRunnerUnit:
    """Tests that import GovernanceDigestRunner directly, no IntelligenceDaemon."""

    def _get_runner_class(self):
        mod = _load_daemon_module()
        return mod.GovernanceDigestRunner

    # ── should_run scheduling ─────────────────────────────────────────────

    def test_should_run_returns_true_on_first_call(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        assert runner.should_run() is True

    def test_should_run_false_immediately_after_run_once(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        assert runner.should_run() is False

    def test_should_run_true_after_interval_elapsed(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=1)
        runner.last_run = datetime.now() - timedelta(seconds=2)
        assert runner.should_run() is True

    def test_should_run_false_before_interval_elapsed(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=3600)
        runner.last_run = datetime.now()
        assert runner.should_run() is False

    # ── from_env ─────────────────────────────────────────────────────────

    def test_from_env_uses_default_interval(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_DIGEST_INTERVAL", raising=False)
        cls = self._get_runner_class()
        runner = cls.from_env(tmp_path)
        assert runner.interval == 300

    def test_from_env_reads_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DIGEST_INTERVAL", "120")
        cls = self._get_runner_class()
        runner = cls.from_env(tmp_path)
        assert runner.interval == 120

    def test_from_env_falls_back_on_invalid_value(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DIGEST_INTERVAL", "not_a_number")
        cls = self._get_runner_class()
        runner = cls.from_env(tmp_path)
        assert runner.interval == 300

    # ── run_once — output file ─────────────────────────────────────────────

    def test_run_once_produces_governance_digest_json(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        assert (tmp_path / "governance_digest.json").exists()

    def test_run_once_returns_true_on_success(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        result = runner.run_once()
        assert result is True

    def test_run_once_updates_last_run(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        assert runner.last_run is None
        runner.run_once()
        assert runner.last_run is not None

    def test_run_once_returns_false_on_import_error(self, tmp_path, monkeypatch):
        """If F18 modules are unavailable, run_once returns False without raising."""
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        # Temporarily hide the F18 modules
        orig_governance = sys.modules.pop("governance_signal_extractor", None)
        orig_retro = sys.modules.pop("retrospective_digest", None)
        # Insert broken stubs
        sys.modules["governance_signal_extractor"] = None  # type: ignore
        sys.modules["retrospective_digest"] = None  # type: ignore
        try:
            result = runner.run_once()
        finally:
            # Restore
            if orig_governance is not None:
                sys.modules["governance_signal_extractor"] = orig_governance
            else:
                sys.modules.pop("governance_signal_extractor", None)
            if orig_retro is not None:
                sys.modules["retrospective_digest"] = orig_retro
            else:
                sys.modules.pop("retrospective_digest", None)
        assert result is False

    # ── Output JSON shape ─────────────────────────────────────────────────

    def test_output_contains_required_top_level_keys(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        output = json.loads((tmp_path / "governance_digest.json").read_text())
        required = {
            "runner_version",
            "state_dir",
            "interval_seconds",
            "source_records",
            "generated_at",
            "total_signals_processed",
            "recurring_patterns",
            "recommendations",
        }
        for key in required:
            assert key in output, f"missing key: {key!r}"

    def test_output_runner_version(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        output = json.loads((tmp_path / "governance_digest.json").read_text())
        assert output["runner_version"] == "1.0"

    def test_output_interval_seconds_matches_runner(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=600)
        runner.run_once()
        output = json.loads((tmp_path / "governance_digest.json").read_text())
        assert output["interval_seconds"] == 600

    def test_output_state_dir_matches_runner(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        output = json.loads((tmp_path / "governance_digest.json").read_text())
        assert output["state_dir"] == str(tmp_path)

    def test_output_generated_at_is_iso_timestamp(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        output = json.loads((tmp_path / "governance_digest.json").read_text())
        datetime.fromisoformat(output["generated_at"].replace("Z", "+00:00"))

    def test_output_total_signals_processed_is_int(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        output = json.loads((tmp_path / "governance_digest.json").read_text())
        assert isinstance(output["total_signals_processed"], int)

    def test_output_recurring_patterns_is_list(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        output = json.loads((tmp_path / "governance_digest.json").read_text())
        assert isinstance(output["recurring_patterns"], list)

    def test_output_recommendations_is_list(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        output = json.loads((tmp_path / "governance_digest.json").read_text())
        assert isinstance(output["recommendations"], list)

    def test_output_source_records_contains_gate_and_queue_counts(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        output = json.loads((tmp_path / "governance_digest.json").read_text())
        assert "gate_results" in output["source_records"]
        assert "queue_anomalies" in output["source_records"]

    # ── Signal loading from receipts ──────────────────────────────────────

    def test_gate_results_loaded_from_receipts_ndjson(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        receipts.write_text(
            json.dumps({"event_type": "task_complete", "gate": "gate_pr1_lifecycle",
                        "dispatch_id": "d-001"}) + "\n",
            encoding="utf-8",
        )
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        results = runner._load_gate_results()
        assert len(results) == 1
        assert results[0]["gate_id"] == "gate_pr1_lifecycle"
        assert results[0]["status"] == "pass"

    def test_gate_failure_mapped_correctly(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        receipts.write_text(
            json.dumps({"event_type": "task_failed", "gate": "gate_pr2_tests",
                        "dispatch_id": "d-002"}) + "\n",
            encoding="utf-8",
        )
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        results = runner._load_gate_results()
        assert results[0]["status"] == "fail"

    def test_queue_anomalies_loaded_from_receipts_ndjson(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        receipts.write_text(
            json.dumps({"event_type": "delivery_failure", "dispatch_id": "d-001",
                        "terminal_id": "T1", "reason": "timeout"}) + "\n",
            encoding="utf-8",
        )
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        anomalies = runner._load_queue_anomalies()
        assert len(anomalies) == 1
        assert anomalies[0]["event_type"] == "delivery_failure"

    def test_missing_receipts_returns_empty_lists(self, tmp_path):
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        assert runner._load_gate_results() == []
        assert runner._load_queue_anomalies() == []

    def test_receipts_without_gate_field_are_ignored(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        receipts.write_text(
            json.dumps({"event_type": "task_complete", "dispatch_id": "d-001"}) + "\n",
            encoding="utf-8",
        )
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        assert runner._load_gate_results() == []

    def test_run_once_reflects_receipt_signals_in_output(self, tmp_path):
        """When receipts exist, source_records counts them."""
        receipts = tmp_path / "t0_receipts.ndjson"
        receipts.write_text(
            json.dumps({"event_type": "task_complete", "gate": "gate_pr1_x",
                        "dispatch_id": "d-001"}) + "\n" +
            json.dumps({"event_type": "task_failed", "gate": "gate_pr1_x",
                        "dispatch_id": "d-002"}) + "\n",
            encoding="utf-8",
        )
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)
        runner.run_once()
        output = json.loads((tmp_path / "governance_digest.json").read_text())
        assert output["source_records"]["gate_results"] == 2

    # ── F18 module calls ───────────────────────────────────────────────────

    def test_run_once_calls_collect_governance_signals(self, tmp_path):
        """run_once delegates to collect_governance_signals from F18 extractor."""
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)

        mock_collect = MagicMock(return_value=[])
        with patch.dict("sys.modules", {
            "governance_signal_extractor": types.SimpleNamespace(
                collect_governance_signals=mock_collect
            ),
        }):
            runner.run_once()

        mock_collect.assert_called_once()

    def test_run_once_calls_build_digest(self, tmp_path):
        """run_once delegates to build_digest from F18 retrospective module."""
        from retrospective_digest import RetroDigest
        cls = self._get_runner_class()
        runner = cls(tmp_path, interval=300)

        stub_digest = RetroDigest(
            generated_at=datetime.now().isoformat(),
            total_signals_processed=0,
        )
        mock_build = MagicMock(return_value=stub_digest)
        mock_collect = MagicMock(return_value=[])

        with patch.dict("sys.modules", {
            "governance_signal_extractor": types.SimpleNamespace(
                collect_governance_signals=mock_collect
            ),
            "retrospective_digest": types.SimpleNamespace(
                build_digest=mock_build
            ),
        }):
            runner.run_once()

        mock_build.assert_called_once()


# ---------------------------------------------------------------------------
# GovernanceDigestRunner — integration via IntelligenceDaemon
# ---------------------------------------------------------------------------

class TestGovernanceDigestRunnerInDaemon:
    """Verify IntelligenceDaemon initialises the digest runner."""

    def test_daemon_has_digest_runner_attribute(self, tmp_path, monkeypatch):
        _, state_dir = _make_daemon(tmp_path, monkeypatch)
        mod = _load_daemon_module()
        daemon = mod.IntelligenceDaemon()
        assert hasattr(daemon, "digest_runner")

    def test_digest_runner_state_dir_matches_daemon_state_dir(self, tmp_path, monkeypatch):
        _, state_dir = _make_daemon(tmp_path, monkeypatch)
        mod = _load_daemon_module()
        daemon = mod.IntelligenceDaemon()
        assert daemon.digest_runner.state_dir == daemon.state_dir

    def test_digest_runner_interval_from_env(self, tmp_path, monkeypatch):
        _, state_dir = _make_daemon(tmp_path, monkeypatch)
        monkeypatch.setenv("VNX_DIGEST_INTERVAL", "60")
        mod = _load_daemon_module()
        daemon = mod.IntelligenceDaemon()
        assert daemon.digest_runner.interval == 60
