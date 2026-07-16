"""Tests for the reason-aware injection-effectiveness evaluator + measure-only
tuning proposals (dispatch: injection-effectiveness-eval-loop PR-B).

Covers:
1. Bucket mapping: pattern_injection_outcome.reason -> generation/ranking/
   presentation counts + proportions, across all six reasons.
2. Proposal generation: generation-heavy / ranking-heavy / presentation-heavy
   distributions each emit the expected proposal shape; mixed distributions
   emit one proposal per non-zero bucket.
3. Proposal persistence: atomic write to the operator-gated
   pending_injection_tuning.json queue; id-dedup preserves an operator's
   status edit across re-runs.
4. Gate: VNX_INJECTION_WHY_ENABLED + VNX_INJECTION_FEEDBACK_ENABLED must BOTH
   be on, or run_reason_evaluator_and_propose is a byte-for-byte no-op.
5. Never mutates outcome/usage tables or flags.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"
for _p in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from effectiveness_probe import EFFECTIVENESS_PROBES  # noqa: E402
import injection_effectiveness_probe as iep  # noqa: E402
from injection_effectiveness_probe import (  # noqa: E402
    BUCKETS,
    REASON_TO_BUCKET,
    InjectionReasonEvaluator,
    generate_tuning_proposals,
    run_reason_evaluator_and_propose,
    write_tuning_proposals,
)
from gather_intelligence import NON_ADOPTION_REASONS  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_outcome_db(tmp_path: Path, rows) -> Path:
    """rows: iterable of (pattern_id, used, reason) tuples."""
    db_path = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE pattern_injection_outcome (
                id          INTEGER PRIMARY KEY,
                dispatch_id TEXT    NOT NULL,
                pattern_id  TEXT    NOT NULL,
                pattern_hash TEXT,
                used        INTEGER NOT NULL DEFAULT 0,
                reason      TEXT,
                evidence    TEXT,
                project_id  TEXT    NOT NULL DEFAULT 'vnx-dev',
                created_at  TEXT    NOT NULL
            )
            """
        )
        for i, (pattern_id, used, reason) in enumerate(rows):
            conn.execute(
                "INSERT INTO pattern_injection_outcome "
                "(dispatch_id, pattern_id, used, reason, project_id, created_at) "
                "VALUES (?, ?, ?, ?, 'vnx-dev', '2026-07-13T00:00:00Z')",
                (f"d-{i}", pattern_id, used, reason),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# 1. Bucket mapping
# ---------------------------------------------------------------------------

class TestReasonToBucketDriftGuard:
    def test_reason_to_bucket_covers_exactly_the_six_non_adoption_reasons(self):
        assert set(REASON_TO_BUCKET) == set(NON_ADOPTION_REASONS)

    def test_every_bucket_value_is_one_of_the_three_buckets(self):
        assert set(REASON_TO_BUCKET.values()) <= set(BUCKETS)
        assert set(REASON_TO_BUCKET.values()) == set(BUCKETS)


class TestBucketDistribution:
    def test_all_six_reasons_map_to_correct_buckets_with_counts_and_proportions(self, tmp_path):
        rows = [(f"pat-{r}", 0, r) for r in NON_ADOPTION_REASONS]
        _make_outcome_db(tmp_path, rows)

        distribution = InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        assert distribution["bucket_counts"] == {"generation": 4, "ranking": 1, "presentation": 1}
        assert distribution["total_ignored"] == 6
        assert distribution["bucket_proportions"]["generation"] == pytest.approx(4 / 6)
        assert distribution["bucket_proportions"]["ranking"] == pytest.approx(1 / 6)
        assert distribution["bucket_proportions"]["presentation"] == pytest.approx(1 / 6)
        assert distribution["reason_counts"] == {r: 1 for r in NON_ADOPTION_REASONS}

    def test_used_rows_are_excluded_from_the_distribution(self, tmp_path):
        rows = [("pat-used", 1, None), ("pat-ignored", 0, "wrong-file-affinity")]
        _make_outcome_db(tmp_path, rows)

        distribution = InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        assert distribution["total_ignored"] == 1
        assert distribution["bucket_counts"] == {"generation": 0, "ranking": 1, "presentation": 0}

    def test_no_db_at_all_returns_zeroed_distribution_not_a_crash(self, tmp_path):
        distribution = InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        assert distribution["total_ignored"] == 0
        assert distribution["bucket_counts"] == {"generation": 0, "ranking": 0, "presentation": 0}
        assert distribution["bucket_proportions"] == {"generation": 0.0, "ranking": 0.0, "presentation": 0.0}

    def test_missing_pattern_injection_outcome_table_is_treated_as_no_data(self, tmp_path):
        db_path = tmp_path / "quality_intelligence.db"
        sqlite3.connect(str(db_path)).close()

        distribution = InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        assert distribution["total_ignored"] == 0

    def test_missing_table_sqlite_error_is_logged_not_suppressed_silently(self, tmp_path, caplog):
        # OI-623: the sqlite3.Error on this read path must be logged loudly,
        # not returned as empty without a trace.
        db_path = tmp_path / "quality_intelligence.db"
        sqlite3.connect(str(db_path)).close()

        with caplog.at_level("WARNING", logger="injection_effectiveness_probe"):
            result = iep._read_reason_counts(db_path)

        assert result == {}
        assert "_read_reason_counts" in caplog.text
        assert "no such table" in caplog.text

    def test_evaluator_never_writes_to_the_database_it_reads(self, tmp_path):
        rows = [(f"pat-{r}", 0, r) for r in NON_ADOPTION_REASONS]
        db_path = _make_outcome_db(tmp_path, rows)
        before = db_path.read_bytes()

        InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        assert db_path.read_bytes() == before

    def test_evaluator_not_registered_as_a_cockpit_effectiveness_probe(self):
        """The evaluator is a diagnostic breakdown, not a health probe — it must
        not clobber InjectionEffectivenessProbe's registration for the same
        subsystem key."""
        assert InjectionReasonEvaluator not in EFFECTIVENESS_PROBES.values()
        assert EFFECTIVENESS_PROBES["intelligence-self-learning-loop"].__name__ == (
            "InjectionEffectivenessProbe"
        )


# ---------------------------------------------------------------------------
# 2. Proposal generation — pure function of a distribution
# ---------------------------------------------------------------------------

_GENERATED_AT = "2026-07-13T12:00:00Z"


class TestProposalShapes:
    def test_generation_heavy_distribution_emits_only_a_generation_proposal(self, tmp_path):
        rows = [("p1", 0, "irrelevant-to-task"), ("p2", 0, "low-signal"), ("p3", 0, "low-signal")]
        _make_outcome_db(tmp_path, rows)
        distribution = InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        proposals = generate_tuning_proposals(distribution, generated_at=_GENERATED_AT)

        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal["bucket"] == "generation"
        assert proposal["count"] == 3
        assert "down-rank" in proposal["title"]
        assert proposal["reasons"] == {"irrelevant-to-task": 1, "low-signal": 2}
        assert proposal["status"] == "pending"

    def test_ranking_heavy_distribution_emits_only_a_ranking_proposal(self, tmp_path):
        rows = [("p1", 0, "wrong-file-affinity"), ("p2", 0, "wrong-file-affinity")]
        _make_outcome_db(tmp_path, rows)
        distribution = InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        proposals = generate_tuning_proposals(distribution, generated_at=_GENERATED_AT)

        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal["bucket"] == "ranking"
        assert proposal["count"] == 2
        assert "file-affinity ranking" in proposal["title"]
        assert proposal["reasons"] == {"wrong-file-affinity": 2}

    def test_presentation_heavy_distribution_emits_only_a_presentation_proposal(self, tmp_path):
        rows = [("p1", 0, "bad-timing")]
        _make_outcome_db(tmp_path, rows)
        distribution = InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        proposals = generate_tuning_proposals(distribution, generated_at=_GENERATED_AT)

        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal["bucket"] == "presentation"
        assert proposal["count"] == 1
        assert "timing" in proposal["title"]
        assert proposal["reasons"] == {"bad-timing": 1}

    def test_mixed_distribution_emits_one_proposal_per_nonzero_bucket_in_order(self, tmp_path):
        rows = [
            ("p1", 0, "stale"),
            ("p2", 0, "wrong-file-affinity"),
            ("p3", 0, "bad-timing"),
        ]
        _make_outcome_db(tmp_path, rows)
        distribution = InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        proposals = generate_tuning_proposals(distribution, generated_at=_GENERATED_AT)

        assert [p["bucket"] for p in proposals] == ["generation", "ranking", "presentation"]

    def test_empty_distribution_emits_no_proposals(self, tmp_path):
        distribution = InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        proposals = generate_tuning_proposals(distribution, generated_at=_GENERATED_AT)

        assert proposals == []

    def test_generate_tuning_proposals_is_pure_no_files_written(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rows = [("p1", 0, "bad-timing")]
        _make_outcome_db(tmp_path, rows)
        distribution = InjectionReasonEvaluator(state_dir=tmp_path).evaluate()

        generate_tuning_proposals(distribution, generated_at=_GENERATED_AT)

        assert not (tmp_path / "pending_injection_tuning.json").exists()


# ---------------------------------------------------------------------------
# 3. Proposal persistence — operator-gated surface
# ---------------------------------------------------------------------------

class TestWriteTuningProposals:
    def test_writes_atomically_to_the_operator_gated_queue(self, tmp_path):
        proposals = [{
            "id": "injtune-ranking-20260713",
            "bucket": "ranking",
            "count": 2,
            "status": "pending",
            "generated_at": _GENERATED_AT,
        }]
        out = tmp_path / "pending_injection_tuning.json"

        added = write_tuning_proposals(proposals, out, generated_at=_GENERATED_AT)

        assert added == 1
        assert out.exists()
        assert not out.with_suffix(out.suffix + ".tmp").exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["proposals"][0]["status"] == "pending"
        assert data["proposals"][0]["id"] == "injtune-ranking-20260713"

    def test_dedup_by_id_preserves_operator_status_edit(self, tmp_path):
        out = tmp_path / "pending_injection_tuning.json"
        proposal = {
            "id": "injtune-generation-20260713",
            "bucket": "generation",
            "count": 5,
            "status": "pending",
            "generated_at": _GENERATED_AT,
        }
        write_tuning_proposals([proposal], out, generated_at=_GENERATED_AT)

        # Operator approves it out-of-band.
        data = json.loads(out.read_text(encoding="utf-8"))
        data["proposals"][0]["status"] = "approved"
        out.write_text(json.dumps(data), encoding="utf-8")

        # Evaluator re-runs the same day and regenerates the identical proposal.
        added = write_tuning_proposals([proposal], out, generated_at=_GENERATED_AT)

        assert added == 0
        data_after = json.loads(out.read_text(encoding="utf-8"))
        assert len(data_after["proposals"]) == 1
        assert data_after["proposals"][0]["status"] == "approved"

    def test_corrupt_existing_queue_is_quarantined_not_silently_replaced(self, tmp_path, caplog):
        out = tmp_path / "pending_injection_tuning.json"
        corrupt_bytes = "{not valid json"
        out.write_text(corrupt_bytes, encoding="utf-8")
        proposal = {"id": "injtune-ranking-20260713", "bucket": "ranking", "status": "pending"}

        with caplog.at_level("WARNING"):
            added = write_tuning_proposals([proposal], out, generated_at=_GENERATED_AT)

        # The write still proceeds (the loop must not seize up on a corrupt
        # queue) but the fresh file only ever holds the newly generated
        # proposal — nothing "recovered" from the corrupt bytes.
        assert added == 1
        data = json.loads(out.read_text(encoding="utf-8"))
        assert len(data["proposals"]) == 1

        # The corrupt original is preserved for operator inspection, not lost.
        quarantined = list(tmp_path.glob("pending_injection_tuning.json.corrupt.*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == corrupt_bytes

        assert any(
            "unreadable/corrupt" in record.message and str(out) in record.message
            for record in caplog.records
        )

    def test_non_object_existing_queue_is_quarantined_not_silently_dropped(self, tmp_path, caplog):
        out = tmp_path / "pending_injection_tuning.json"
        out.write_text("[]", encoding="utf-8")
        proposal = {"id": "injtune-ranking-20260713", "bucket": "ranking", "status": "pending"}

        with caplog.at_level("WARNING"):
            added = write_tuning_proposals([proposal], out, generated_at=_GENERATED_AT)

        assert added == 1
        quarantined = list(tmp_path.glob("pending_injection_tuning.json.corrupt.*"))
        assert len(quarantined) == 1
        assert any("unreadable/corrupt" in record.message for record in caplog.records)

    def test_first_run_no_existing_file_is_quiet(self, tmp_path, caplog):
        out = tmp_path / "pending_injection_tuning.json"
        proposal = {"id": "injtune-ranking-20260713", "bucket": "ranking", "status": "pending"}

        with caplog.at_level("WARNING"):
            added = write_tuning_proposals([proposal], out, generated_at=_GENERATED_AT)

        assert added == 1
        assert not list(tmp_path.glob("*.corrupt.*"))
        assert caplog.records == []

    def test_write_emits_adr005_audit_event(self, tmp_path, monkeypatch):
        events_path = tmp_path / "events" / "pending_injection_tuning.ndjson"
        monkeypatch.setattr(iep, "_tuning_proposals_events_path", lambda: events_path)
        out = tmp_path / "pending_injection_tuning.json"
        proposal = {"id": "injtune-ranking-20260713", "bucket": "ranking", "status": "pending"}

        added = write_tuning_proposals([proposal], out, generated_at=_GENERATED_AT)

        assert added == 1
        assert events_path.exists()
        lines = events_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["event_type"] == "injection_tuning_proposals_written"
        assert event["path"] == str(out)
        assert event["proposals_added"] == 1
        assert event["proposals_total"] == 1
        assert event["generated_at"] == _GENERATED_AT
        assert "record_id" in event and "project_id" in event and "timestamp" in event

    def test_write_emits_one_audit_event_per_call(self, tmp_path, monkeypatch):
        events_path = tmp_path / "events" / "pending_injection_tuning.ndjson"
        monkeypatch.setattr(iep, "_tuning_proposals_events_path", lambda: events_path)
        out = tmp_path / "pending_injection_tuning.json"
        proposal_a = {"id": "injtune-ranking-a", "bucket": "ranking", "status": "pending"}
        proposal_b = {"id": "injtune-ranking-b", "bucket": "ranking", "status": "pending"}

        write_tuning_proposals([proposal_a], out, generated_at=_GENERATED_AT)
        write_tuning_proposals([proposal_b], out, generated_at=_GENERATED_AT)

        lines = events_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# 4. Gate — both opt-in flags required; no new flag introduced
# ---------------------------------------------------------------------------

class TestGate:
    def test_both_flags_off_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_INJECTION_WHY_ENABLED", raising=False)
        monkeypatch.delenv("VNX_INJECTION_FEEDBACK_ENABLED", raising=False)
        rows = [("p1", 0, "bad-timing")]
        _make_outcome_db(tmp_path, rows)

        result = run_reason_evaluator_and_propose(state_dir=tmp_path)

        assert result == {"ran": False, "reason": "flags_off", "proposals_written": 0, "distribution": None}
        assert not (tmp_path / "pending_injection_tuning.json").exists()

    def test_only_why_flag_on_is_still_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        monkeypatch.delenv("VNX_INJECTION_FEEDBACK_ENABLED", raising=False)
        rows = [("p1", 0, "bad-timing")]
        _make_outcome_db(tmp_path, rows)

        result = run_reason_evaluator_and_propose(state_dir=tmp_path)

        assert result["ran"] is False
        assert not (tmp_path / "pending_injection_tuning.json").exists()

    def test_only_feedback_flag_on_is_still_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_INJECTION_WHY_ENABLED", raising=False)
        monkeypatch.setenv("VNX_INJECTION_FEEDBACK_ENABLED", "1")
        rows = [("p1", 0, "bad-timing")]
        _make_outcome_db(tmp_path, rows)

        result = run_reason_evaluator_and_propose(state_dir=tmp_path)

        assert result["ran"] is False
        assert not (tmp_path / "pending_injection_tuning.json").exists()

    def test_flags_off_never_touches_the_filesystem_evaluator(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_INJECTION_WHY_ENABLED", raising=False)
        monkeypatch.delenv("VNX_INJECTION_FEEDBACK_ENABLED", raising=False)
        calls = []
        monkeypatch.setattr(
            iep, "InjectionReasonEvaluator",
            lambda **kw: calls.append(kw) or InjectionReasonEvaluator(**kw),
        )

        run_reason_evaluator_and_propose(state_dir=tmp_path)

        assert calls == []

    def test_both_flags_on_runs_and_writes_proposals(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        monkeypatch.setenv("VNX_INJECTION_FEEDBACK_ENABLED", "1")
        rows = [("p1", 0, "wrong-file-affinity"), ("p2", 0, "wrong-file-affinity")]
        _make_outcome_db(tmp_path, rows)

        result = run_reason_evaluator_and_propose(state_dir=tmp_path)

        assert result["ran"] is True
        assert result["proposals_written"] == 1
        assert result["distribution"]["bucket_counts"]["ranking"] == 2

        out = tmp_path / "pending_injection_tuning.json"
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["proposals"][0]["bucket"] == "ranking"

    def test_both_flags_on_but_no_ignored_rows_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        monkeypatch.setenv("VNX_INJECTION_FEEDBACK_ENABLED", "1")

        result = run_reason_evaluator_and_propose(state_dir=tmp_path)

        assert result["ran"] is True
        assert result["proposals_written"] == 0
        assert not (tmp_path / "pending_injection_tuning.json").exists()


# ---------------------------------------------------------------------------
# 5. Never mutates outcome/usage tables or flags
# ---------------------------------------------------------------------------

class TestNeverMutatesStateItReads:
    def test_run_never_mutates_the_outcome_db_or_leaves_flags_changed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        monkeypatch.setenv("VNX_INJECTION_FEEDBACK_ENABLED", "1")
        rows = [(f"pat-{r}", 0, r) for r in NON_ADOPTION_REASONS]
        db_path = _make_outcome_db(tmp_path, rows)
        before = db_path.read_bytes()

        run_reason_evaluator_and_propose(state_dir=tmp_path)

        assert db_path.read_bytes() == before
        assert os.environ.get("VNX_INJECTION_WHY_ENABLED") == "1"
        assert os.environ.get("VNX_INJECTION_FEEDBACK_ENABLED") == "1"
        assert os.environ.get("VNX_LEARNING_LOOP_ENABLED", "0") == "0"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
