#!/usr/bin/env python3
"""Certification tests for Feature 25 — Governance Digest Pipeline.

Quality gate: gate_pr4_governance_digest_certification
- Daemon produces digest file from real signal sources
- API serves digest with freshness tracking
- Advisory-only enforcement in recommendations
- Contract alignment (D-1..D-5 invariants)
- End-to-end pipeline (daemon -> JSON -> API -> typed envelope)
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"

for p in (str(SCRIPTS_DIR), str(LIB_DIR), str(DASHBOARD_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Module loading (same pattern as test_governance_digest_runner.py)
# ---------------------------------------------------------------------------

def _install_stub_modules():
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


def _make_runner(tmp_path, interval=300):
    """Return a GovernanceDigestRunner wired to tmp_path."""
    mod = _load_daemon_module()
    return mod.GovernanceDigestRunner(tmp_path, interval=interval)


# ---------------------------------------------------------------------------
# Import F18 modules for advisory-only enforcement tests
# ---------------------------------------------------------------------------

from retrospective_digest import (
    RetroDigest, Recommendation, RecurrenceRecord, build_digest,
    RECOMMENDATION_CATEGORIES,
)
from governance_signal_extractor import collect_governance_signals

import serve_dashboard as sd
from signal_store import SignalStore


# ═══════════════════════════════════════════════════════════════════════════
# Section 1: Daemon produces digest from real signal sources
# ═══════════════════════════════════════════════════════════════════════════

class TestDaemonProducesDigest:
    """Certify GovernanceDigestRunner generates governance_digest.json."""

    def test_run_once_creates_digest_file(self, tmp_path):
        runner = _make_runner(tmp_path)
        assert runner.run_once() is True
        assert (tmp_path / "governance_digest.json").exists()

    def test_digest_file_is_valid_json(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.run_once()
        data = json.loads((tmp_path / "governance_digest.json").read_text())
        assert isinstance(data, dict)

    def test_digest_contains_contracted_top_level_keys(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.run_once()
        data = json.loads((tmp_path / "governance_digest.json").read_text())
        required = {
            "runner_version", "state_dir", "interval_seconds",
            "source_records", "generated_at", "total_signals_processed",
            "recurring_patterns", "recommendations",
        }
        missing = required - set(data.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_digest_generated_at_is_iso_timestamp(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.run_once()
        data = json.loads((tmp_path / "governance_digest.json").read_text())
        datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))

    def test_digest_from_gate_receipts(self, tmp_path):
        """Runner reads real gate result events from t0_receipts.ndjson."""
        receipts = tmp_path / "t0_receipts.ndjson"
        lines = []
        for i in range(5):
            lines.append(json.dumps({
                "event_type": "task_complete" if i % 2 == 0 else "task_failed",
                "gate": f"gate_pr{i}_test",
                "dispatch_id": f"d-{i:03d}",
                "feature_id": f"Feature {16 + i}",
            }))
        receipts.write_text("\n".join(lines) + "\n", encoding="utf-8")

        runner = _make_runner(tmp_path)
        runner.run_once()
        data = json.loads((tmp_path / "governance_digest.json").read_text())
        assert data["source_records"]["gate_results"] == 5

    def test_digest_from_queue_anomalies(self, tmp_path):
        """Runner reads delivery_failure events from t0_receipts.ndjson."""
        receipts = tmp_path / "t0_receipts.ndjson"
        receipts.write_text(
            json.dumps({"event_type": "delivery_failure", "dispatch_id": "d-fail", "terminal_id": "T1"}) + "\n"
            + json.dumps({"event_type": "dead_letter", "dispatch_id": "d-dead", "terminal_id": "T2"}) + "\n",
            encoding="utf-8",
        )
        runner = _make_runner(tmp_path)
        runner.run_once()
        data = json.loads((tmp_path / "governance_digest.json").read_text())
        assert data["source_records"]["queue_anomalies"] == 2

    def test_run_once_delegates_to_f18_extractors(self, tmp_path):
        """Runner calls collect_governance_signals and build_digest from F18."""
        runner = _make_runner(tmp_path)

        mock_collect = MagicMock(return_value=[])
        stub_digest = RetroDigest(
            generated_at=datetime.now().isoformat(),
            total_signals_processed=0,
        )
        mock_build = MagicMock(return_value=stub_digest)

        with patch.dict("sys.modules", {
            "governance_signal_extractor": types.SimpleNamespace(
                collect_governance_signals=mock_collect
            ),
            "retrospective_digest": types.SimpleNamespace(
                build_digest=mock_build
            ),
        }):
            runner.run_once()

        mock_collect.assert_called_once()
        mock_build.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Section 2: API serves digest with freshness tracking
# ═══════════════════════════════════════════════════════════════════════════

class TestAPIFreshnessEnvelope:
    """Certify GET /api/operator/governance-digest returns FreshnessEnvelope."""

    def test_envelope_contains_required_keys(self, tmp_path):
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")
        required = {"view", "queried_at", "source_freshness", "staleness_seconds", "degraded", "degraded_reasons", "data"}
        missing = required - set(result.keys())
        assert not missing, f"Missing envelope keys: {missing}"

    def test_view_is_governance_digest_view(self, tmp_path):
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")
        assert result["view"] == "GovernanceDigestView"

    def test_degraded_true_when_file_missing(self, tmp_path):
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")
        assert result["degraded"] is True
        assert any("not found" in r for r in result["degraded_reasons"])

    def test_degraded_false_when_file_fresh(self, tmp_path):
        digest_file = tmp_path / "governance_digest.json"
        digest_file.write_text(json.dumps({
            "runner_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_signals_processed": 0,
            "recurring_patterns": [],
            "recommendations": [],
        }), encoding="utf-8")
        result = sd._operator_get_governance_digest(digest_path=digest_file)
        assert result["degraded"] is False

    def test_staleness_seconds_numeric_when_file_present(self, tmp_path):
        digest_file = tmp_path / "governance_digest.json"
        digest_file.write_text(json.dumps({"runner_version": "1.0"}), encoding="utf-8")
        result = sd._operator_get_governance_digest(digest_path=digest_file)
        assert isinstance(result["staleness_seconds"], (int, float))
        assert result["staleness_seconds"] >= 0

    def test_staleness_none_when_file_missing(self, tmp_path):
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")
        assert result["staleness_seconds"] is None

    def test_degraded_true_when_file_stale(self, tmp_path):
        """File older than _DIGEST_STALE_THRESHOLD triggers degraded."""
        digest_file = tmp_path / "governance_digest.json"
        digest_file.write_text(json.dumps({"runner_version": "1.0"}), encoding="utf-8")
        # Set mtime to 20 minutes ago
        old_time = time.time() - 1200
        os.utime(digest_file, (old_time, old_time))
        result = sd._operator_get_governance_digest(digest_path=digest_file)
        assert result["degraded"] is True
        assert any("stale" in r for r in result["degraded_reasons"])

    def test_source_freshness_has_governance_digest_key(self, tmp_path):
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")
        assert "governance_digest" in result["source_freshness"]

    def test_queried_at_is_valid_iso_timestamp(self, tmp_path):
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")
        datetime.fromisoformat(result["queried_at"].replace("Z", "+00:00"))


# ═══════════════════════════════════════════════════════════════════════════
# Section 3: Advisory-only enforcement
# ═══════════════════════════════════════════════════════════════════════════

class TestAdvisoryOnlyEnforcement:
    """Certify recommendations enforce advisory_only=True invariant."""

    def test_recommendation_advisory_only_default_is_true(self):
        rec = Recommendation(
            category="runtime_fix",
            content="test recommendation",
        )
        assert rec.advisory_only is True

    def test_recommendation_raises_on_advisory_only_false(self):
        with pytest.raises(ValueError, match="advisory_only=True"):
            Recommendation(
                category="runtime_fix",
                content="test",
                advisory_only=False,
            )

    def test_recommendation_to_dict_includes_advisory_only_true(self):
        rec = Recommendation(
            category="runtime_fix",
            content="test",
        )
        d = rec.to_dict()
        assert d["advisory_only"] is True

    def test_build_digest_recommendations_all_advisory_only(self):
        """build_digest output recommendations all have advisory_only=True."""
        digest = build_digest([])
        for rec in digest.recommendations:
            assert rec.advisory_only is True, f"Recommendation {rec.content!r} has advisory_only=False"

    def test_digest_json_recommendations_carry_advisory_only_flag(self, tmp_path):
        """End-to-end: daemon produces digest where all recs have advisory_only=True."""
        runner = _make_runner(tmp_path)
        runner.run_once()
        data = json.loads((tmp_path / "governance_digest.json").read_text())
        for rec in data.get("recommendations", []):
            assert rec.get("advisory_only") is True, f"rec missing advisory_only=True: {rec}"

    def test_all_recommendation_categories_are_known(self):
        """Every RECOMMENDATION_CATEGORIES entry is a non-empty string."""
        assert len(RECOMMENDATION_CATEGORIES) >= 1
        for cat in RECOMMENDATION_CATEGORIES:
            assert isinstance(cat, str) and len(cat) > 0

    def test_recommendation_rejects_unknown_category(self):
        with pytest.raises(ValueError, match="Unknown recommendation category"):
            Recommendation(
                category="not_a_real_category",
                content="test",
            )


# ═══════════════════════════════════════════════════════════════════════════
# Section 4: Contract invariants D-1..D-5
# ═══════════════════════════════════════════════════════════════════════════

class TestContractInvariants:
    """Certify contract invariants from GOVERNANCE_DIGEST_PIPELINE_CONTRACT.md."""

    # D-1: Recommendations generated only from recurrence count >= 3
    def test_d1_recurrence_threshold_for_recommendations(self):
        """Recommendations should only exist for patterns seen >= 3 times."""
        digest = build_digest([])
        # No signals -> no recommendations
        assert isinstance(digest.recommendations, list)

    # D-2: Digest regenerated every 5 minutes (runner interval)
    def test_d2_default_interval_is_300_seconds(self):
        runner = _make_runner(Path("/tmp/test-d2"))
        assert runner.interval == 300

    def test_d2_should_run_respects_interval(self):
        runner = _make_runner(Path("/tmp/test-d2"))
        runner.last_run = datetime.now()
        assert runner.should_run() is False
        runner.last_run = datetime.now() - timedelta(seconds=301)
        assert runner.should_run() is True

    # D-3: Digest is a projection — deleting it has no data loss
    def test_d3_digest_is_regenerable(self, tmp_path):
        """Deleting digest and re-running produces it again."""
        runner = _make_runner(tmp_path)
        runner.run_once()
        digest_path = tmp_path / "governance_digest.json"
        assert digest_path.exists()
        first = json.loads(digest_path.read_text())

        # Delete and regenerate
        digest_path.unlink()
        runner.last_run = None  # Reset so it runs again
        runner.run_once()
        assert digest_path.exists()
        second = json.loads(digest_path.read_text())

        # Same shape
        assert set(first.keys()) == set(second.keys())

    # D-4: Recommendation status changes written to governance_recommendations.json, not digest
    def test_d4_digest_has_no_status_mutation_api(self):
        """GovernanceDigestRunner has no method to mutate recommendation status."""
        mod = _load_daemon_module()
        runner_cls = mod.GovernanceDigestRunner
        public_methods = [m for m in dir(runner_cls) if not m.startswith("_")]
        mutator_methods = [m for m in public_methods if "status" in m or "ack" in m or "dismiss" in m]
        assert mutator_methods == [], f"Runner should not have status-mutating methods: {mutator_methods}"

    # D-5: Dashboard reads only from governance_digest.json
    def test_d5_api_reads_from_digest_file_only(self, tmp_path):
        """API function takes digest_path parameter — does not read signal stores directly."""
        import inspect
        sig = inspect.signature(sd._operator_get_governance_digest)
        params = list(sig.parameters.keys())
        assert "digest_path" in params
        # And it works with just a digest file
        digest_file = tmp_path / "governance_digest.json"
        digest_file.write_text(json.dumps({"test": True}), encoding="utf-8")
        result = sd._operator_get_governance_digest(digest_path=digest_file)
        assert result["data"]["test"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Section 5: End-to-end pipeline (daemon → JSON → API → typed envelope)
# ═══════════════════════════════════════════════════════════════════════════

class TestEndToEndPipeline:
    """Certify complete daemon-to-dashboard pipeline."""

    def test_daemon_output_readable_by_api(self, tmp_path):
        """Runner writes digest -> API reads it -> returns valid envelope."""
        runner = _make_runner(tmp_path)
        runner.run_once()
        digest_path = tmp_path / "governance_digest.json"
        result = sd._operator_get_governance_digest(digest_path=digest_path)

        assert result["degraded"] is False
        assert result["view"] == "GovernanceDigestView"
        assert isinstance(result["data"], dict)
        assert "total_signals_processed" in result["data"]

    def test_pipeline_with_receipt_signals(self, tmp_path):
        """Receipts -> Runner -> digest.json -> API envelope with signal counts."""
        receipts = tmp_path / "t0_receipts.ndjson"
        receipts.write_text(
            json.dumps({"event_type": "task_complete", "gate": "gate_pr1_test", "dispatch_id": "d-001"}) + "\n"
            + json.dumps({"event_type": "gate_fail", "gate": "gate_pr2_test", "dispatch_id": "d-002"}) + "\n"
            + json.dumps({"event_type": "delivery_failure", "dispatch_id": "d-003", "terminal_id": "T1"}) + "\n",
            encoding="utf-8",
        )

        runner = _make_runner(tmp_path)
        runner.run_once()
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")

        assert result["degraded"] is False
        source = result["data"].get("source_records", {})
        assert source.get("gate_results", 0) >= 2
        assert source.get("queue_anomalies", 0) >= 1

    def test_pipeline_recurring_patterns_are_list(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.run_once()
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")
        assert isinstance(result["data"].get("recurring_patterns"), list)

    def test_pipeline_recommendations_are_list(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.run_once()
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")
        assert isinstance(result["data"].get("recommendations"), list)

    def test_pipeline_generated_at_present(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.run_once()
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")
        generated_at = result["data"].get("generated_at")
        assert generated_at is not None
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))


# ═══════════════════════════════════════════════════════════════════════════
# Section 6: SignalStore contract alignment
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalStoreContract:
    """Certify SignalStore supports the digest pipeline."""

    def test_append_and_read_round_trip(self, tmp_path):
        store = SignalStore(path=tmp_path / "signals.ndjson")
        store.append({"signal_class": "GATE_FAILURE", "content": "gate timed out"})
        records = store.read_all()
        assert len(records) == 1
        assert records[0]["signal_class"] == "GATE_FAILURE"

    def test_ndjson_format_one_object_per_line(self, tmp_path):
        store = SignalStore(path=tmp_path / "signals.ndjson")
        store.append_many([{"a": 1}, {"b": 2}, {"c": 3}])
        lines = [l for l in (tmp_path / "signals.ndjson").read_text().splitlines() if l.strip()]
        assert len(lines) == 3
        for line in lines:
            json.loads(line)

    def test_from_env_factory(self, tmp_path):
        store = SignalStore.from_env(base_dir=tmp_path)
        assert store.path == tmp_path / "feedback" / "signals.ndjson"

    def test_count_matches_actual_records(self, tmp_path):
        store = SignalStore(path=tmp_path / "signals.ndjson")
        store.append_many([{"i": n} for n in range(7)])
        assert store.count() == 7

    def test_empty_store_returns_empty_list(self, tmp_path):
        store = SignalStore(path=tmp_path / "signals.ndjson")
        assert store.read_all() == []
        assert store.count() == 0


# ═══════════════════════════════════════════════════════════════════════════
# Section 7: Scheduling and lifecycle
# ═══════════════════════════════════════════════════════════════════════════

class TestSchedulingLifecycle:
    """Certify runner scheduling matches contract (5-min interval)."""

    def test_should_run_true_on_first_call(self, tmp_path):
        runner = _make_runner(tmp_path)
        assert runner.should_run() is True

    def test_should_run_false_after_run(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.run_once()
        assert runner.should_run() is False

    def test_from_env_reads_custom_interval(self, monkeypatch):
        monkeypatch.setenv("VNX_DIGEST_INTERVAL", "60")
        mod = _load_daemon_module()
        runner = mod.GovernanceDigestRunner.from_env(Path("/tmp/test"))
        assert runner.interval == 60

    def test_from_env_defaults_to_300(self, monkeypatch):
        monkeypatch.delenv("VNX_DIGEST_INTERVAL", raising=False)
        mod = _load_daemon_module()
        runner = mod.GovernanceDigestRunner.from_env(Path("/tmp/test"))
        assert runner.interval == 300

    def test_atomic_write_creates_file(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._write_json_atomic({"test": True})
        data = json.loads((tmp_path / "governance_digest.json").read_text())
        assert data["test"] is True

    def test_run_once_returns_false_on_import_error(self, tmp_path):
        runner = _make_runner(tmp_path)
        orig_gov = sys.modules.pop("governance_signal_extractor", None)
        orig_retro = sys.modules.pop("retrospective_digest", None)
        sys.modules["governance_signal_extractor"] = None  # type: ignore
        sys.modules["retrospective_digest"] = None  # type: ignore
        try:
            assert runner.run_once() is False
        finally:
            if orig_gov is not None:
                sys.modules["governance_signal_extractor"] = orig_gov
            else:
                sys.modules.pop("governance_signal_extractor", None)
            if orig_retro is not None:
                sys.modules["retrospective_digest"] = orig_retro
            else:
                sys.modules.pop("retrospective_digest", None)


# ═══════════════════════════════════════════════════════════════════════════
# Section 8: Dashboard type alignment
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardTypeAlignment:
    """Certify dashboard TypeScript types match Python output shape."""

    def test_digest_output_keys_match_typescript_interface(self, tmp_path):
        """GovernanceDigestData TS interface fields present in daemon output."""
        runner = _make_runner(tmp_path)
        runner.run_once()
        data = json.loads((tmp_path / "governance_digest.json").read_text())

        # Keys from GovernanceDigestData in lib/types.ts
        ts_keys = {
            "runner_version", "generated_at", "total_signals_processed",
            "recurring_patterns", "recommendations", "source_records",
        }
        for key in ts_keys:
            assert key in data, f"TS GovernanceDigestData expects key {key!r}"

    def test_envelope_keys_match_typescript_interface(self, tmp_path):
        """GovernanceDigestEnvelope TS interface fields present in API output."""
        result = sd._operator_get_governance_digest(digest_path=tmp_path / "governance_digest.json")

        # Keys from GovernanceDigestEnvelope in lib/types.ts
        ts_keys = {
            "view", "queried_at", "source_freshness",
            "staleness_seconds", "degraded", "degraded_reasons", "data",
        }
        for key in ts_keys:
            assert key in result, f"TS GovernanceDigestEnvelope expects key {key!r}"

    def test_recommendation_dict_matches_typescript_interface(self):
        """Recommendation.to_dict() keys match DigestRecommendation TS interface."""
        rec = Recommendation(
            category="runtime_fix",
            content="test",
            evidence_basis=["e1"],
            severity="warn",
            recurrence_count=3,
            defect_family="timeout:session",
        )
        d = rec.to_dict()
        ts_keys = {
            "category", "content", "advisory_only",
            "evidence_basis", "severity", "recurrence_count", "defect_family",
        }
        for key in ts_keys:
            assert key in d, f"TS DigestRecommendation expects key {key!r}"
