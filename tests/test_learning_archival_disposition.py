#!/usr/bin/env python3
"""tests/test_learning_archival_disposition.py — operator disposition verbs
(OI-1076 / OI-1038).

Covers the consumer the proposal tier was missing: `vnx learning approve`
and `vnx learning dismiss` carry out a disposition on a
pending_archival.json candidate, audit it to intelligence_usage.ndjson, and
remove the candidate from the actionable queue so `status`/`review` stop
resurfacing it (the OI-1038 complaint).

All candidates are constructed in-test. No writes to the production store:
every test runs against a tmp_path state dir and a tmp quality_intelligence.db.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(_REPO_ROOT))

# OI-1051 / OI-1076: guard against a stubbed sys.modules["learning_loop"].
# Other suites (notably test_intelligence_daemon_paths._install_stub_modules
# and test_governance_digest_certification) replace sys.modules["learning_loop"]
# with a types.ModuleType stub carrying only LearningLoop, and never restore
# it. In collection order that stub wins: a bare `import learning_loop` (and
# the lazy `import learning_loop as ll` inside vnx_cli/commands/learning.py)
# then returns the stub, which lacks apply_archival_decision /
# ArchivalDispositionError — the three TestCliVerbs cases fail.
#
# We pop any stale cache entry and re-import the genuine module via the normal
# import mechanism (NOT spec_from_file_location, which creates a second module
# object and trips isinstance/__module__ identity in sibling learning tests).
# This is the same sys.modules-pop idiom test_append_receipt_identity uses to
# force a re-import. No teardown: leaving the real module cached is the
# correct end state — the stub installers re-stub on their own next use, so we
# do not pollute them, and every other learning test wants the real module.
_cached = sys.modules.get("learning_loop")
if _cached is None or not hasattr(_cached, "apply_archival_decision"):
    sys.modules.pop("learning_loop", None)
    import learning_loop as ll  # noqa: E402
else:
    ll = _cached

from vnx_cli.commands import learning  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_real_learning_loop_module():
    """Re-import the real learning_loop if a prior test stubbed sys.modules.

    The module-level guard above covers collection time. But a sibling test in
    the same session can re-stub sys.modules["learning_loop"] between our tests
    (the stub installers have no teardown). The CLI verb does a lazy
    `import learning_loop as ll` at call time, so it would pick up a re-stub.
    This fixture guarantees the cached entry is the real module before each
    test in this file runs. It leaves the real module cached on exit — the
    correct end state for every downstream learning test.
    """
    _mod = sys.modules.get("learning_loop")
    if _mod is None or not hasattr(_mod, "apply_archival_decision"):
        sys.modules.pop("learning_loop", None)
        sys.modules["learning_loop"] = importlib.import_module("learning_loop")
    yield



# ---------------------------------------------------------------------------
# Schema + candidate helpers
# ---------------------------------------------------------------------------

def _bootstrap_schema(db_path: Path) -> None:
    """Create the minimal quality_intelligence.db schema the dispositions touch."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
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
        );
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            pattern_data TEXT,
            category TEXT,
            confidence_score REAL DEFAULT 0.5,
            valid_from DATETIME DEFAULT (datetime('now')),
            valid_until DATETIME DEFAULT NULL,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT DEFAULT '[]',
            tags TEXT DEFAULT '[]'
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
            valid_from DATETIME DEFAULT (datetime('now')),
            valid_until DATETIME DEFAULT NULL
        );
    """)
    conn.commit()
    conn.close()


def _write_pending_archival(state_dir: Path, candidates: list[dict]) -> Path:
    path = state_dir / "pending_archival.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pending_archival": candidates}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _read_pending_archival(state_dir: Path) -> list[dict]:
    path = state_dir / "pending_archival.json"
    return json.loads(path.read_text(encoding="utf-8")).get("pending_archival", [])


def _read_audit_events(state_dir: Path) -> list[dict]:
    path = state_dir / "intelligence_usage.ndjson"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _archive_candidate(pattern_id: str = "pat-archive-1", **extra) -> dict:
    base = {
        "pattern_id": pattern_id,
        "title": f"Test archive {pattern_id}",
        "last_used": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),
        "confidence": 0.2,
        "used_count": 0,
        "ignored_count": 5,
        "reason": "Unused for 30+ days with confidence < 0.3",
        "queued_at": "2026-08-09T08:00:00Z",
        "status": "pending",
    }
    base.update(extra)
    return base


def _supersede_candidate(pattern_id: str = "42", source_table: str = "success_patterns",
                         **extra) -> dict:
    base = {
        "pattern_id": pattern_id,
        "title": "Stale success pattern",
        "confidence": 0.2,
        "source_table": source_table,
        "action": "supersede",
        "reason": "confidence_score < 0.3, older than 30 days",
        "queued_at": "2026-08-09T08:30:00Z",
        "status": "pending",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 1. approve (archive): pattern_usage row removed, candidate marked, audited
# ---------------------------------------------------------------------------

class TestApproveArchive:
    def test_approve_archive_deletes_pattern_usage_row(self, tmp_path):
        state_dir = tmp_path / "state"
        db_path = state_dir / "quality_intelligence.db"
        _bootstrap_schema(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, "
            "confidence) VALUES ('pat-archive-1', 'T', 'h', 0.2)"
        )
        conn.commit()
        conn.close()

        _write_pending_archival(state_dir, [_archive_candidate()])
        before = _read_pending_archival(state_dir)
        assert before[0]["status"] == "pending"

        result = ll.apply_archival_decision(
            state_dir, "pat-archive-1", "approve",
            approval_id="appr-test-1", reason="operator reviewed and approved archival",
        )

        assert result["decision"] == "approve"
        assert result["action"] == "archive"
        assert result["db_applied"] is True
        assert result["status"] == "approved"

        # The candidate is marked decided, no longer pending.
        after = _read_pending_archival(state_dir)
        assert len(after) == 1
        assert after[0]["status"] == "approved"
        assert after[0]["approval_id"] == "appr-test-1"
        assert after[0]["decision_reason"] == "operator reviewed and approved archival"

        # DB row removed.
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*) FROM pattern_usage WHERE pattern_id = 'pat-archive-1'"
        ).fetchone()
        conn.close()
        assert row[0] == 0

        # Audit event in the governed NDJSON stream.
        events = _read_audit_events(state_dir)
        archival_events = [e for e in events if e["event_type"] == "archival_decision"]
        assert len(archival_events) == 1
        ev = archival_events[0]
        assert ev["decision"] == "approve"
        assert ev["pattern_id"] == "pat-archive-1"
        assert ev["approval_id"] == "appr-test-1"
        assert ev["approved_by"] == "operator"
        assert ev["db_applied"] is True

    def test_approve_archive_when_row_already_absent_is_idempotent(self, tmp_path):
        state_dir = tmp_path / "state"
        db_path = state_dir / "quality_intelligence.db"
        _bootstrap_schema(db_path)
        # No pattern_usage row exists for this candidate.
        _write_pending_archival(state_dir, [_archive_candidate("pat-gone")])

        result = ll.apply_archival_decision(
            state_dir, "pat-gone", "approve",
            approval_id="appr-2", reason="approved",
        )
        assert result["db_applied"] is False  # nothing to delete
        assert result["status"] == "approved"
        # Candidate still marked decided.
        assert _read_pending_archival(state_dir)[0]["status"] == "approved"


# ---------------------------------------------------------------------------
# 2. approve (supersede): valid_until set, idempotent on second call
# ---------------------------------------------------------------------------

class TestApproveSupersede:
    def _seed_success_pattern(self, db_path: Path, *, title="stale", confidence=0.1) -> int:
        conn = sqlite3.connect(str(db_path))
        old_date = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        conn.execute(
            "INSERT INTO success_patterns "
            "(title, pattern_data, category, confidence_score, valid_from, valid_until) "
            "VALUES (?, '{}', 'general', ?, ?, NULL)",
            (title, confidence, old_date),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM success_patterns WHERE title = ?", (title,)
        ).fetchone()
        conn.close()
        return row[0]

    def test_approve_supersede_sets_valid_until(self, tmp_path):
        state_dir = tmp_path / "state"
        db_path = state_dir / "quality_intelligence.db"
        _bootstrap_schema(db_path)
        pat_id = self._seed_success_pattern(db_path)

        _write_pending_archival(state_dir, [_supersede_candidate(str(pat_id))])

        result = ll.apply_archival_decision(
            state_dir, str(pat_id), "approve",
            approval_id="appr-sup-1", reason="supersede approved",
            source_table="success_patterns",
        )
        assert result["action"] == "supersede"
        assert result["db_applied"] is True

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT valid_until FROM success_patterns WHERE id = ?", (pat_id,)
        ).fetchone()
        conn.close()
        assert row[0] is not None, "valid_until must be set on approve"

        events = _read_audit_events(state_dir)
        ev = [e for e in events if e["event_type"] == "archival_decision"][0]
        assert ev["action"] == "supersede"
        assert ev["source_table"] == "success_patterns"

    def test_approve_supersede_prevention_rules(self, tmp_path):
        state_dir = tmp_path / "state"
        db_path = state_dir / "quality_intelligence.db"
        _bootstrap_schema(db_path)
        conn = sqlite3.connect(str(db_path))
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        conn.execute(
            "INSERT INTO prevention_rules "
            "(tag_combination, rule_type, description, confidence, valid_from, valid_until) "
            "VALUES ('T1', 'failure_prevention', 'rule', 0.1, ?, NULL)",
            (old,),
        )
        conn.commit()
        rule_id = conn.execute(
            "SELECT id FROM prevention_rules WHERE description = 'rule'"
        ).fetchone()[0]
        conn.close()

        _write_pending_archival(
            state_dir, [_supersede_candidate(str(rule_id), source_table="prevention_rules")]
        )

        result = ll.apply_archival_decision(
            state_dir, str(rule_id), "approve",
            approval_id="appr-pr-1", reason="approved",
            source_table="prevention_rules",
        )
        assert result["db_applied"] is True

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT valid_until FROM prevention_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        conn.close()
        assert row[0] is not None


# ---------------------------------------------------------------------------
# 3. dismiss: no DB mutation, candidate removed from queue
# ---------------------------------------------------------------------------

class TestDismiss:
    def test_dismiss_no_db_mutation(self, tmp_path):
        state_dir = tmp_path / "state"
        db_path = state_dir / "quality_intelligence.db"
        _bootstrap_schema(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, "
            "confidence) VALUES ('pat-dismiss-1', 'T', 'h', 0.2)"
        )
        conn.commit()
        conn.close()

        _write_pending_archival(state_dir, [_archive_candidate("pat-dismiss-1")])

        result = ll.apply_archival_decision(
            state_dir, "pat-dismiss-1", "dismiss",
            approval_id="appr-d-1", reason="operator rejects this proposal",
        )
        assert result["decision"] == "dismiss"
        assert result["db_applied"] is False
        assert result["status"] == "dismissed"

        # pattern_usage row must still exist — dismiss does not mutate the DB.
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*) FROM pattern_usage WHERE pattern_id = 'pat-dismiss-1'"
        ).fetchone()
        conn.close()
        assert row[0] == 1

        after = _read_pending_archival(state_dir)
        assert after[0]["status"] == "dismissed"

        ev = _read_audit_events(state_dir)
        archival = [e for e in ev if e["event_type"] == "archival_decision"][0]
        assert archival["decision"] == "dismiss"
        assert archival["db_applied"] is False

    def test_dismiss_supersede_no_valid_until_set(self, tmp_path):
        state_dir = tmp_path / "state"
        db_path = state_dir / "quality_intelligence.db"
        _bootstrap_schema(db_path)
        conn = sqlite3.connect(str(db_path))
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        conn.execute(
            "INSERT INTO success_patterns "
            "(title, pattern_data, category, confidence_score, valid_from, valid_until) "
            "VALUES ('dismiss-sup', '{}', 'g', 0.1, ?, NULL)",
            (old,),
        )
        conn.commit()
        pat_id = conn.execute(
            "SELECT id FROM success_patterns WHERE title = 'dismiss-sup'"
        ).fetchone()[0]
        conn.close()

        _write_pending_archival(
            state_dir, [_supersede_candidate(str(pat_id), source_table="success_patterns")]
        )

        ll.apply_archival_decision(
            state_dir, str(pat_id), "dismiss",
            approval_id="appr-ds-1", reason="not superseding",
            source_table="success_patterns",
        )

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT valid_until FROM success_patterns WHERE id = ?", (pat_id,)
        ).fetchone()
        conn.close()
        assert row[0] is None, "dismiss must not set valid_until"


# ---------------------------------------------------------------------------
# 4. Idempotency: second approve/dismiss on same id does not double-act
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_approve_is_rejected_not_doubled(self, tmp_path):
        state_dir = tmp_path / "state"
        db_path = state_dir / "quality_intelligence.db"
        _bootstrap_schema(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, "
            "confidence) VALUES ('pat-idem-1', 'T', 'h', 0.2)"
        )
        conn.commit()
        conn.close()
        _write_pending_archival(state_dir, [_archive_candidate("pat-idem-1")])

        first = ll.apply_archival_decision(
            state_dir, "pat-idem-1", "approve",
            approval_id="appr-i-1", reason="first approve",
        )
        assert first["db_applied"] is True

        with pytest.raises(ll.CandidateAlreadyHandledError) as exc_info:
            ll.apply_archival_decision(
                state_dir, "pat-idem-1", "approve",
                approval_id="appr-i-2", reason="second approve",
            )
        assert "already" in str(exc_info.value).lower()

        # Only ONE audit event for this pattern_id.
        events = [e for e in _read_audit_events(state_dir)
                  if e["event_type"] == "archival_decision"
                  and e["pattern_id"] == "pat-idem-1"]
        assert len(events) == 1

    def test_second_dismiss_after_approve_rejected(self, tmp_path):
        state_dir = tmp_path / "state"
        db_path = state_dir / "quality_intelligence.db"
        _bootstrap_schema(db_path)
        _write_pending_archival(state_dir, [_archive_candidate("pat-idem-2")])

        ll.apply_archival_decision(
            state_dir, "pat-idem-2", "approve",
            approval_id="appr-a", reason="approved",
        )
        with pytest.raises(ll.CandidateAlreadyHandledError):
            ll.apply_archival_decision(
                state_dir, "pat-idem-2", "dismiss",
                approval_id="appr-b", reason="try dismiss later",
            )


# ---------------------------------------------------------------------------
# 5. status & review hide handled candidates
# ---------------------------------------------------------------------------

class TestStatusAndReviewHideHandled:
    def _make_args(self, project_dir, mode="all"):
        ns = argparse.Namespace()
        ns.project_dir = str(project_dir)
        ns.mode = mode
        return ns

    def test_review_omits_handled_candidates(self, tmp_path, capsys, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        _write_pending_archival(state_dir, [
            _archive_candidate("pat-live"),
            _archive_candidate("pat-done", status="approved"),
        ])
        monkeypatch.setattr(learning, "_resolve_state_dir", lambda _pd: state_dir)

        rc = learning._cmd_review(self._make_args(tmp_path, mode="archival"))
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "pat-live" in out
        assert "pat-done" not in out

    def test_status_counts_handled_separately(self, tmp_path, capsys, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        _write_pending_archival(state_dir, [
            _archive_candidate("pat-live"),
            _archive_candidate("pat-done", status="approved"),
            _supersede_candidate("7", status="dismissed"),
        ])
        monkeypatch.setattr(learning, "_resolve_state_dir", lambda _pd: state_dir)

        rc = learning._cmd_status(self._make_args(tmp_path))
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "Pending archival candidates:                1" in out
        assert "Handled (approved/dismissed, out of queue): 2" in out


# ---------------------------------------------------------------------------
# 6. Human-gate validation
# ---------------------------------------------------------------------------

class TestHumanGateValidation:
    def test_missing_approval_id_raises(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_pending_archival(state_dir, [_archive_candidate()])
        with pytest.raises(ll.ArchivalDispositionError):
            ll.apply_archival_decision(
                state_dir, "pat-archive-1", "approve",
                approval_id="", reason="ok",
            )

    def test_missing_reason_raises(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_pending_archival(state_dir, [_archive_candidate()])
        with pytest.raises(ll.ArchivalDispositionError):
            ll.apply_archival_decision(
                state_dir, "pat-archive-1", "approve",
                approval_id="appr", reason="",
            )

    def test_invalid_approver_raises(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_pending_archival(state_dir, [_archive_candidate()])
        with pytest.raises(ll.ArchivalDispositionError):
            ll.apply_archival_decision(
                state_dir, "pat-archive-1", "approve",
                approval_id="appr", reason="ok", approved_by="worker",
            )

    def test_unknown_pattern_id_raises(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_pending_archival(state_dir, [_archive_candidate()])
        with pytest.raises(ll.CandidateNotFoundError):
            ll.apply_archival_decision(
                state_dir, "does-not-exist", "approve",
                approval_id="appr", reason="ok",
            )


# ---------------------------------------------------------------------------
# 7. CLI verbs end-to-end (argparse → library)
# ---------------------------------------------------------------------------

class TestCliVerbs:
    def _approve_args(self, project_dir, pattern_id):
        ns = argparse.Namespace()
        ns.project_dir = str(project_dir)
        ns.pattern_id = pattern_id
        ns.approval_id = "appr-cli"
        ns.reason = "cli approve"
        ns.source_table = None
        ns.learning_subcommand = "approve"
        return ns

    def _dismiss_args(self, project_dir, pattern_id):
        ns = argparse.Namespace()
        ns.project_dir = str(project_dir)
        ns.pattern_id = pattern_id
        ns.approval_id = "appr-cli"
        ns.reason = "cli dismiss"
        ns.source_table = None
        ns.learning_subcommand = "dismiss"
        return ns

    def test_cli_approve_returns_zero_and_marks_candidate(self, tmp_path, capsys, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        db_path = state_dir / "quality_intelligence.db"
        _bootstrap_schema(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, "
            "confidence) VALUES ('pat-cli-1', 'T', 'h', 0.2)"
        )
        conn.commit()
        conn.close()
        _write_pending_archival(state_dir, [_archive_candidate("pat-cli-1")])
        monkeypatch.setattr(learning, "_resolve_state_dir", lambda _pd: state_dir)

        rc = learning.vnx_learning(self._approve_args(tmp_path, "pat-cli-1"))
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "approved: pattern_id=pat-cli-1" in out
        assert _read_pending_archival(state_dir)[0]["status"] == "approved"

    def test_cli_dismiss_returns_zero_and_marks_candidate(self, tmp_path, capsys, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        _write_pending_archival(state_dir, [_archive_candidate("pat-cli-2")])
        monkeypatch.setattr(learning, "_resolve_state_dir", lambda _pd: state_dir)

        rc = learning.vnx_learning(self._dismiss_args(tmp_path, "pat-cli-2"))
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "dismissed: pattern_id=pat-cli-2" in out
        assert _read_pending_archival(state_dir)[0]["status"] == "dismissed"

    def test_cli_second_approve_returns_nonzero(self, tmp_path, capsys, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        _bootstrap_schema(state_dir / "quality_intelligence.db")
        _write_pending_archival(state_dir, [_archive_candidate("pat-cli-3")])
        monkeypatch.setattr(learning, "_resolve_state_dir", lambda _pd: state_dir)

        assert learning.vnx_learning(self._approve_args(tmp_path, "pat-cli-3")) == 0
        rc = learning.vnx_learning(self._approve_args(tmp_path, "pat-cli-3"))
        assert rc != 0
        _, err = capsys.readouterr()
        assert "already handled" in err
