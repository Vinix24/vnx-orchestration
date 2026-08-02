"""test_link_sessions_dispatches_cleanup.py — Tests for OI-872 dispatch_id cleanup
and receipt token_usage backfill in link_sessions_dispatches.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

# Ensure vnx_paths module can resolve; link_sessions_dispatches uses it at import.
os.environ.setdefault("VNX_PROJECT_ID", "vnx-dev")


def _init_db_schema(conn: sqlite3.Connection) -> None:
    """Create minimal session_analytics and dispatch_metadata for tests."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            project_path TEXT NOT NULL,
            terminal TEXT,
            session_date DATE NOT NULL,
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            dispatch_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            terminal TEXT NOT NULL,
            track TEXT NOT NULL,
            session_id TEXT
        )
    """)
    conn.commit()


class TestCleanPollutedDispatchIds:
    """Tests for clean_polluted_dispatch_ids() — OI-872 fix 2."""

    def test_nulls_exact_literal(self, tmp_path):
        """Only the exact '<dispatch_id>' literal is NULLed."""
        from link_sessions_dispatches import clean_polluted_dispatch_ids

        db_path = tmp_path / "test_cleanup.db"
        conn = sqlite3.connect(str(db_path))
        _init_db_schema(conn)

        # Insert rows: one polluted, one with real id, one already NULL.
        conn.execute("""
            INSERT INTO session_analytics
                (session_id, project_path, session_date, dispatch_id)
            VALUES
                ('polluted', '/test', '2026-07-31', '<dispatch_id>'),
                ('real',     '/test', '2026-07-31', '20260731-123456-real-id'),
                ('nulled',   '/test', '2026-07-31', NULL)
        """)
        conn.commit()

        cleaned = clean_polluted_dispatch_ids(conn)
        assert cleaned == 1

        # Verify only the polluted row was NULLed.
        rows = conn.execute(
            "SELECT session_id, dispatch_id FROM session_analytics ORDER BY session_id"
        ).fetchall()
        assert rows == [
            ("nulled", None),
            ("polluted", None),
            ("real", "20260731-123456-real-id"),
        ]
        conn.close()

    def test_idempotent(self, tmp_path):
        """Running the cleanup twice changes nothing extra."""
        from link_sessions_dispatches import clean_polluted_dispatch_ids

        db_path = tmp_path / "test_idempotent.db"
        conn = sqlite3.connect(str(db_path))
        _init_db_schema(conn)

        conn.execute("""
            INSERT INTO session_analytics
                (session_id, project_path, session_date, dispatch_id)
            VALUES
                ('a', '/test', '2026-07-31', '<dispatch_id>'),
                ('b', '/test', '2026-07-31', '<dispatch_id>')
        """)
        conn.commit()

        first = clean_polluted_dispatch_ids(conn)
        assert first == 2

        second = clean_polluted_dispatch_ids(conn)
        assert second == 0

        # Both still NULL.
        rows = conn.execute(
            "SELECT dispatch_id FROM session_analytics"
        ).fetchall()
        assert all(r[0] is None for r in rows)
        conn.close()

    def test_leaves_real_ids_untouched(self, tmp_path):
        """Real dispatch IDs matching YYYYMMDD-* are never altered."""
        from link_sessions_dispatches import clean_polluted_dispatch_ids

        db_path = tmp_path / "test_real_ids.db"
        conn = sqlite3.connect(str(db_path))
        _init_db_schema(conn)

        real_ids = [
            "20260731-123456-my-feature",
            "20260603-abcdef-fix-A",
            "20260101-000000-bench-run",
        ]
        for i, did in enumerate(real_ids):
            conn.execute(
                "INSERT INTO session_analytics "
                "(session_id, project_path, session_date, dispatch_id) "
                "VALUES (?, '/test', '2026-07-31', ?)",
                (f"real-{i}", did),
            )
        conn.commit()

        cleaned = clean_polluted_dispatch_ids(conn)
        assert cleaned == 0

        rows = conn.execute(
            "SELECT dispatch_id FROM session_analytics ORDER BY id"
        ).fetchall()
        assert [r[0] for r in rows] == real_ids
        conn.close()


class TestBackfillReceiptTokenUsage:
    """Tests for backfill_receipt_token_usage() — OI-872 fix 3 (chain closure)."""

    def _setup_db(self, tmp_path: Path) -> sqlite3.Connection:
        db_path = tmp_path / "test_backfill.db"
        conn = sqlite3.connect(str(db_path))
        _init_db_schema(conn)
        return conn

    def _write_receipts(self, state_dir: Path, receipts: list[dict]) -> Path:
        receipt_file = state_dir / "t0_receipts.ndjson"
        receipt_file.parent.mkdir(parents=True, exist_ok=True)
        with open(receipt_file, "w", encoding="utf-8") as f:
            for r in receipts:
                f.write(json.dumps(r, sort_keys=False) + "\n")
        return receipt_file

    def test_enriches_claude_receipt_with_null_token_usage(self, tmp_path, monkeypatch):
        """A claude receipt with null token_usage gets populated from session_analytics."""
        from link_sessions_dispatches import backfill_receipt_token_usage

        conn = self._setup_db(tmp_path)
        conn.execute("""
            INSERT INTO session_analytics
                (session_id, project_path, session_date, dispatch_id,
                 total_input_tokens, total_output_tokens,
                 cache_creation_tokens, cache_read_tokens)
            VALUES ('sess-1', '/project', '2026-07-31', '20260731-123456-test',
                    5000, 3200, 400, 1200)
        """)
        conn.commit()

        state_dir = tmp_path / "state"
        original = self._write_receipts(state_dir, [
            {
                "dispatch_id": "20260731-123456-test",
                "provider": "claude",
                "token_usage": None,
                "status": "success",
                "terminal_id": "T1",
            },
            {
                "dispatch_id": "20260731-999999-other",
                "provider": "kimi",
                "token_usage": {"input": 100, "output": 50},
                "status": "success",
                "terminal_id": "T2",
            },
        ])

        monkeypatch.setattr(
            "link_sessions_dispatches.RECEIPTS_FILE", original)
        monkeypatch.setattr(
            "link_sessions_dispatches.STATE_DIR", state_dir)

        enriched = backfill_receipt_token_usage(conn)
        assert enriched == 1

        # Read back the rewritten file.
        with open(original, "r") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        assert len(lines) == 2

        claude = lines[0]
        assert claude["token_usage"] == {
            "input": 5000,
            "output": 3200,
            "cache_creation_5m": 400,
            "cache_creation_1h": 0,
            "cache_read": 1200,
        }

        # Kimi receipt untouched.
        kimi = lines[1]
        assert kimi["token_usage"] == {"input": 100, "output": 50}
        conn.close()

    def test_enriches_claude_receipt_with_empty_unavailable(self, tmp_path, monkeypatch):
        """A claude receipt with unavailable token_usage gets backfilled."""
        from link_sessions_dispatches import backfill_receipt_token_usage

        conn = self._setup_db(tmp_path)
        conn.execute("""
            INSERT INTO session_analytics
                (session_id, project_path, session_date, dispatch_id,
                 total_input_tokens, total_output_tokens)
            VALUES ('sess-2', '/project', '2026-07-31', '20260731-999999-fill',
                    8000, 4500)
        """)
        conn.commit()

        state_dir = tmp_path / "state"
        original = self._write_receipts(state_dir, [
            {
                "dispatch_id": "20260731-999999-fill",
                "provider": "claude",
                "token_usage": {"input": 0, "output": 0, "unavailable": True},
                "status": "success",
                "terminal_id": "T1",
            },
        ])

        monkeypatch.setattr(
            "link_sessions_dispatches.RECEIPTS_FILE", original)
        monkeypatch.setattr(
            "link_sessions_dispatches.STATE_DIR", state_dir)

        enriched = backfill_receipt_token_usage(conn)
        assert enriched == 1

        with open(original, "r") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert lines[0]["token_usage"] == {
            "input": 8000, "output": 4500,
            "cache_creation_5m": 0, "cache_creation_1h": 0, "cache_read": 0,
        }
        conn.close()

    def test_skips_when_no_session_data(self, tmp_path, monkeypatch):
        """A claude receipt with no match in session_analytics is left untouched."""
        from link_sessions_dispatches import backfill_receipt_token_usage

        conn = self._setup_db(tmp_path)
        # No session_analytics row for this dispatch.

        state_dir = tmp_path / "state"
        original = self._write_receipts(state_dir, [
            {
                "dispatch_id": "20260731-000000-nomatch",
                "provider": "claude",
                "token_usage": None,
                "status": "success",
                "terminal_id": "T1",
            },
        ])

        monkeypatch.setattr(
            "link_sessions_dispatches.RECEIPTS_FILE", original)
        monkeypatch.setattr(
            "link_sessions_dispatches.STATE_DIR", state_dir)

        enriched = backfill_receipt_token_usage(conn)
        assert enriched == 0

        with open(original, "r") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert lines[0]["token_usage"] is None
        conn.close()

    def test_skips_already_populated_receipts(self, tmp_path, monkeypatch):
        """A claude receipt with real token_usage is not overwritten."""
        from link_sessions_dispatches import backfill_receipt_token_usage

        conn = self._setup_db(tmp_path)
        conn.execute("""
            INSERT INTO session_analytics
                (session_id, project_path, session_date, dispatch_id,
                 total_input_tokens, total_output_tokens)
            VALUES ('sess-3', '/project', '2026-07-31', '20260731-123456-has',
                    100, 200)
        """)
        conn.commit()

        state_dir = tmp_path / "state"
        original = self._write_receipts(state_dir, [
            {
                "dispatch_id": "20260731-123456-has",
                "provider": "claude",
                "token_usage": {"input": 9999, "output": 8888},
                "status": "success",
                "terminal_id": "T1",
            },
        ])

        monkeypatch.setattr(
            "link_sessions_dispatches.RECEIPTS_FILE", original)
        monkeypatch.setattr(
            "link_sessions_dispatches.STATE_DIR", state_dir)

        enriched = backfill_receipt_token_usage(conn)
        assert enriched == 0

        with open(original, "r") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        # Original token_usage preserved.
        assert lines[0]["token_usage"] == {"input": 9999, "output": 8888}
        conn.close()

    def test_idempotent(self, tmp_path, monkeypatch):
        """Running the backfill twice does not double-count or corrupt."""
        from link_sessions_dispatches import backfill_receipt_token_usage

        conn = self._setup_db(tmp_path)
        conn.execute("""
            INSERT INTO session_analytics
                (session_id, project_path, session_date, dispatch_id,
                 total_input_tokens, total_output_tokens)
            VALUES ('sess-idem', '/project', '2026-07-31', '20260731-idempotent-test',
                    100, 200)
        """)
        conn.commit()

        state_dir = tmp_path / "state"
        original = self._write_receipts(state_dir, [
            {
                "dispatch_id": "20260731-idempotent-test",
                "provider": "claude",
                "token_usage": None,
                "status": "success",
                "terminal_id": "T1",
            },
        ])

        monkeypatch.setattr(
            "link_sessions_dispatches.RECEIPTS_FILE", original)
        monkeypatch.setattr(
            "link_sessions_dispatches.STATE_DIR", state_dir)

        first = backfill_receipt_token_usage(conn)
        assert first == 1

        second = backfill_receipt_token_usage(conn)
        assert second == 0  # Already populated — skipped.

        with open(original, "r") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert lines[0]["token_usage"] == {
            "input": 100, "output": 200,
            "cache_creation_5m": 0, "cache_creation_1h": 0, "cache_read": 0,
        }
        assert len(lines) == 1  # No duplicate appended.
        conn.close()

    def test_preserves_non_claude_lines_verbatim(self, tmp_path, monkeypatch):
        """Non-claude receipt lines pass through unchanged."""
        from link_sessions_dispatches import backfill_receipt_token_usage

        conn = self._setup_db(tmp_path)

        state_dir = tmp_path / "state"
        kimi_original = {
            "dispatch_id": "20260731-kimi-test",
            "provider": "kimi",
            "token_usage": {"input": 50, "output": 30, "other_field": "keep"},
            "status": "success",
        }
        original = self._write_receipts(state_dir, [kimi_original])

        monkeypatch.setattr(
            "link_sessions_dispatches.RECEIPTS_FILE", original)
        monkeypatch.setattr(
            "link_sessions_dispatches.STATE_DIR", state_dir)

        enriched = backfill_receipt_token_usage(conn)
        assert enriched == 0

        with open(original, "r") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert lines[0] == kimi_original
        conn.close()

    # OI-884: deepseek-harness / glm-harness run through the Claude Code
    # harness and must be backfilled by the same set as the emit path.

    def test_enriches_deepseek_harness_receipt_with_null_token_usage(self, tmp_path, monkeypatch):
        """A deepseek-harness receipt with null token_usage gets populated."""
        from link_sessions_dispatches import backfill_receipt_token_usage

        conn = self._setup_db(tmp_path)
        conn.execute("""
            INSERT INTO session_analytics
                (session_id, project_path, session_date, dispatch_id,
                 total_input_tokens, total_output_tokens,
                 cache_creation_tokens, cache_read_tokens)
            VALUES ('sess-ds', '/project', '2026-07-31', '20260731-123456-ds',
                    5000, 3200, 400, 1200)
        """)
        conn.commit()

        state_dir = tmp_path / "state"
        original = self._write_receipts(state_dir, [
            {
                "dispatch_id": "20260731-123456-ds",
                "provider": "deepseek-harness",
                "token_usage": None,
                "status": "success",
                "terminal_id": "T1",
            },
        ])

        monkeypatch.setattr(
            "link_sessions_dispatches.RECEIPTS_FILE", original)
        monkeypatch.setattr(
            "link_sessions_dispatches.STATE_DIR", state_dir)

        enriched = backfill_receipt_token_usage(conn)
        assert enriched == 1

        with open(original, "r") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert lines[0]["token_usage"] == {
            "input": 5000, "output": 3200,
            "cache_creation_5m": 400, "cache_creation_1h": 0, "cache_read": 1200,
        }
        conn.close()

    def test_enriches_glm_harness_receipt_with_null_token_usage(self, tmp_path, monkeypatch):
        """A glm-harness receipt with null token_usage gets populated."""
        from link_sessions_dispatches import backfill_receipt_token_usage

        conn = self._setup_db(tmp_path)
        conn.execute("""
            INSERT INTO session_analytics
                (session_id, project_path, session_date, dispatch_id,
                 total_input_tokens, total_output_tokens,
                 cache_creation_tokens, cache_read_tokens)
            VALUES ('sess-glm', '/project', '2026-07-31', '20260731-123456-glm',
                    7000, 2100, 300, 900)
        """)
        conn.commit()

        state_dir = tmp_path / "state"
        original = self._write_receipts(state_dir, [
            {
                "dispatch_id": "20260731-123456-glm",
                "provider": "glm-harness",
                "token_usage": None,
                "status": "success",
                "terminal_id": "T1",
            },
        ])

        monkeypatch.setattr(
            "link_sessions_dispatches.RECEIPTS_FILE", original)
        monkeypatch.setattr(
            "link_sessions_dispatches.STATE_DIR", state_dir)

        enriched = backfill_receipt_token_usage(conn)
        assert enriched == 1

        with open(original, "r") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert lines[0]["token_usage"] == {
            "input": 7000, "output": 2100,
            "cache_creation_5m": 300, "cache_creation_1h": 0, "cache_read": 900,
        }
        conn.close()
