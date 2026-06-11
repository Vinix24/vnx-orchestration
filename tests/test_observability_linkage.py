"""test_observability_linkage.py — Unit tests for observability linkage fixes (H2 sweep).

Covers:
1. state-dir guard: inherited VNX_STATE_DIR without EXPLICIT flag is ignored + warning logged.
2. state-dir guard: VNX_STATE_DIR + EXPLICIT=1 is honored.
3. events-dir guard: inherited VNX_DATA_DIR without EXPLICIT flag is ignored + warning logged.
4. events-dir guard: VNX_DATA_DIR + EXPLICIT=1 resolves under that dir.
5. events-dir guard: VNX_PROJECT_ID (no EXPLICIT) resolves central path.
6. events_path in receipt: present after a successful provider-dispatch flow with EventStore.
7. events_path in receipt: null when event_store is None (claude/tmux lanes).
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch
from event_store import EventStore, _events_dir
from provider_dispatch import _resolve_state_dir


# ---------------------------------------------------------------------------
# Shared result stub
# ---------------------------------------------------------------------------

@dataclass
class _SpawnResult:
    returncode: int = 0
    completion_text: str = "OK"
    events_written: int = 1
    session_id: Optional[str] = None
    timed_out: bool = False
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    event_writer_failures: int = 0


def _make_args(provider, dispatch_id="test-h2-001"):
    args = MagicMock()
    args.provider = provider
    args.dispatch_id = dispatch_id
    args.terminal_id = "T1"
    args.instruction = "observability linkage test"
    args.model = "sonnet"
    args.pr_id = None
    args.dispatch_paths = ""
    args.no_auto_commit = True
    args.max_retries = 1
    args.gate = ""
    args.role = None
    return args


# ---------------------------------------------------------------------------
# Fix 1: _resolve_state_dir guard
# ---------------------------------------------------------------------------

class TestStateDirGuard:
    def test_no_explicit_flag_ignores_inherited_env(self, monkeypatch, tmp_path, caplog):
        """VNX_STATE_DIR without VNX_DATA_DIR_EXPLICIT=1 must be ignored and warn."""
        inherited = str(tmp_path / "wrong_state")
        monkeypatch.setenv("VNX_STATE_DIR", inherited)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        with caplog.at_level(logging.WARNING, logger="provider_dispatch"):
            result = _resolve_state_dir()

        assert str(result) != inherited
        assert "VNX_STATE_DIR" in caplog.text
        assert "VNX_DATA_DIR_EXPLICIT" in caplog.text
        # Should land in the central ledger under home
        assert "test-proj" in str(result)

    def test_with_explicit_flag_honors_env(self, monkeypatch, tmp_path):
        """VNX_STATE_DIR + VNX_DATA_DIR_EXPLICIT=1 must be honored."""
        explicit_state = str(tmp_path / "explicit_state")
        monkeypatch.setenv("VNX_STATE_DIR", explicit_state)
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        result = _resolve_state_dir()

        assert result == Path(explicit_state).resolve()

    def test_no_env_at_all_returns_central_path(self, monkeypatch):
        """Without any env vars, falls back to $HOME/.vnx-data/<project_id>/state."""
        monkeypatch.delenv("VNX_STATE_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.setenv("VNX_PROJECT_ID", "my-project")

        result = _resolve_state_dir()

        assert result == Path.home() / ".vnx-data" / "my-project" / "state"


# ---------------------------------------------------------------------------
# Fix 2: EventStore._events_dir guard
# ---------------------------------------------------------------------------

class TestEventsDirGuard:
    def test_no_explicit_flag_ignores_inherited_vnx_data_dir(self, monkeypatch, tmp_path, caplog):
        """VNX_DATA_DIR without EXPLICIT=1 is ignored; falls back to central dir."""
        wrong = str(tmp_path / "wrong_events")
        monkeypatch.setenv("VNX_DATA_DIR", wrong)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.setenv("VNX_PROJECT_ID", "my-proj")

        with caplog.at_level(logging.WARNING, logger="event_store"):
            result = _events_dir()

        assert str(result) != wrong + "/events"
        assert "VNX_DATA_DIR" in caplog.text
        assert "VNX_DATA_DIR_EXPLICIT" in caplog.text
        # Central path via VNX_PROJECT_ID
        assert result == Path.home() / ".vnx-data" / "my-proj" / "events"

    def test_with_explicit_flag_honors_vnx_data_dir(self, monkeypatch, tmp_path):
        """VNX_DATA_DIR + VNX_DATA_DIR_EXPLICIT=1 resolves to that dir/events."""
        data_dir = str(tmp_path / "explicit_data")
        monkeypatch.setenv("VNX_DATA_DIR", data_dir)
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        result = _events_dir()

        assert result == Path(data_dir).expanduser().resolve() / "events"

    def test_project_id_without_explicit_resolves_central(self, monkeypatch):
        """VNX_PROJECT_ID (no EXPLICIT) routes to home/.vnx-data/<project>/events."""
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

        result = _events_dir()

        assert result == Path.home() / ".vnx-data" / "vnx-dev" / "events"

    def test_no_project_id_fallback_to_repo_relative(self, monkeypatch):
        """Without VNX_PROJECT_ID or env, falls back to .vnx-data/events relative to repo."""
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

        result = _events_dir()

        assert result.name == "events"
        assert result.parts[-2] == ".vnx-data"

    def test_event_store_uses_consistent_dir_with_explicit(self, monkeypatch, tmp_path):
        """EventStore() constructed with EXPLICIT=1 writes events under data_dir."""
        data_dir = tmp_path / "explicit_data"
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        store = EventStore()
        assert store._events_dir == data_dir / "events"


# ---------------------------------------------------------------------------
# Fix 3: events_path in receipt
# ---------------------------------------------------------------------------

class TestEventsPathInReceipt:
    """Verifies that events_path is present in receipts after provider dispatch."""

    @pytest.fixture(autouse=True)
    def _set_dirs(self, tmp_path, monkeypatch):
        self.state_dir = tmp_path / "state"
        self.data_dir = tmp_path / "data"
        self.events_dir = self.data_dir / "events"
        self.state_dir.mkdir()
        self.data_dir.mkdir()
        monkeypatch.setenv("VNX_STATE_DIR", str(self.state_dir))
        monkeypatch.setenv("VNX_DATA_DIR", str(self.data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

    def _last_receipt(self):
        receipt_path = self.state_dir / "t0_receipts.ndjson"
        if not receipt_path.exists():
            return None
        lines = [l for l in receipt_path.read_text().splitlines() if l.strip()]
        return json.loads(lines[-1]) if lines else None

    def test_events_path_present_after_codex_dispatch(self, tmp_path):
        """Codex dispatch with EventStore wired produces events_path in receipt."""
        args = _make_args("codex", dispatch_id="test-evpath-codex-001")

        # Create real EventStore and write a fake event so archive has content
        store = EventStore(events_dir=self.events_dir)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        store.append("T1", {"type": "text", "data": {"text": "hello"}}, dispatch_id="test-evpath-codex-001")

        result = _SpawnResult(returncode=0, event_writer_failures=0)

        with patch("provider_spawns.codex_spawn.spawn_codex", return_value=result), \
             patch("event_store.EventStore", return_value=store), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"):
            rc = provider_dispatch._dispatch_codex(args)

        assert rc == 0
        receipt = self._last_receipt()
        assert receipt is not None
        assert "events_path" in receipt
        assert receipt["events_path"] is not None
        # Must point to archive path with correct dispatch_id in filename
        assert "test-evpath-codex-001" in receipt["events_path"]
        assert receipt["events_path"].endswith(".ndjson")

    def test_events_path_null_for_claude_dispatch(self, tmp_path):
        """Claude dispatch (no EventStore wired) produces events_path=null in receipt."""
        args = _make_args("claude", dispatch_id="test-evpath-claude-001")

        with patch("subprocess_dispatch.deliver_with_recovery", return_value=True), \
             patch("subprocess_dispatch._extract_role_from_instruction", return_value=None), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"):
            rc = provider_dispatch._dispatch_claude(args)

        assert rc == 0
        receipt = self._last_receipt()
        assert receipt is not None
        assert "events_path" in receipt
        assert receipt["events_path"] is None

    def test_events_path_null_when_no_events_written(self, tmp_path):
        """Codex dispatch with empty EventStore produces events_path=null (nothing to archive)."""
        args = _make_args("codex", dispatch_id="test-evpath-empty-001")

        # EventStore with empty file — archive returns None
        store = EventStore(events_dir=self.events_dir)
        self.events_dir.mkdir(parents=True, exist_ok=True)

        result = _SpawnResult(returncode=0, event_writer_failures=0)

        with patch("provider_spawns.codex_spawn.spawn_codex", return_value=result), \
             patch("event_store.EventStore", return_value=store), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"):
            rc = provider_dispatch._dispatch_codex(args)

        assert rc == 0
        receipt = self._last_receipt()
        assert receipt is not None
        assert "events_path" in receipt
        assert receipt["events_path"] is None


# ---------------------------------------------------------------------------
# Fix 3: emit_dispatch_receipt directly
# ---------------------------------------------------------------------------

class TestEmitDispatchReceiptEventsPath:
    """Direct unit tests for the events_path field in governance_emit."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir()

    def test_events_path_written_to_receipt(self):
        from governance_emit import emit_dispatch_receipt

        events_archive = "/data/.vnx-data/vnx-dev/events/archive/T1/test-ep-001.ndjson"
        emit_dispatch_receipt(
            dispatch_id="test-ep-001",
            terminal_id="T1",
            provider="codex",
            model="gpt-5.2-codex",
            pr_id=None,
            status="success",
            completion_pct=100,
            risk=0.0,
            findings=[],
            duration_seconds=1.5,
            token_usage={"input": 100, "output": 50, "cache_hit": 0},
            cost_usd=0.001,
            state_dir=self.state_dir,
            report_path="/data/unified_reports/test-ep-001.md",
            events_path=events_archive,
        )

        receipt_path = self.state_dir / "t0_receipts.ndjson"
        assert receipt_path.exists()
        line = receipt_path.read_text().strip()
        receipt = json.loads(line)
        assert receipt["events_path"] == events_archive

    def test_events_path_null_written_to_receipt(self):
        from governance_emit import emit_dispatch_receipt

        emit_dispatch_receipt(
            dispatch_id="test-ep-null-001",
            terminal_id="T1",
            provider="claude",
            model="claude-sonnet-4-6",
            pr_id=None,
            status="success",
            completion_pct=100,
            risk=0.0,
            findings=[],
            duration_seconds=2.0,
            token_usage={"input": 200, "output": 100, "cache_hit": 0},
            cost_usd=None,
            state_dir=self.state_dir,
            report_path="/data/unified_reports/test-ep-null-001.md",
            events_path=None,
        )

        receipt_path = self.state_dir / "t0_receipts.ndjson"
        receipt = json.loads(receipt_path.read_text().strip())
        assert "events_path" in receipt
        assert receipt["events_path"] is None

    def test_backwards_compat_events_path_defaults_to_none(self):
        """Callers that don't pass events_path get null — no KeyError."""
        from governance_emit import emit_dispatch_receipt

        emit_dispatch_receipt(
            dispatch_id="test-ep-compat-001",
            terminal_id="T2",
            provider="gemini",
            model="gemini-2.5-pro",
            pr_id=None,
            status="success",
            completion_pct=100,
            risk=0.0,
            findings=[],
            duration_seconds=3.0,
            token_usage={"input": 300, "output": 150, "cache_hit": 0},
            cost_usd=0.005,
            state_dir=self.state_dir,
            # events_path not passed — default None
        )

        receipt_path = self.state_dir / "t0_receipts.ndjson"
        receipt = json.loads(receipt_path.read_text().strip())
        assert receipt.get("events_path") is None


# ---------------------------------------------------------------------------
# Safety net: unexpected spawn exception must still archive + clear (gate F1/F3)
# ---------------------------------------------------------------------------

class TestEventStoreSafetyNet:
    """An UNEXPECTED exception in the spawn path bypasses _emit_governance;
    the finally-block safety net must archive + truncate the live stream."""

    def _store_with_live_events(self, monkeypatch, tmp_path) -> EventStore:
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        store = EventStore()
        store.append("T9", {"type": "worker_stdout", "dispatch_id": "d-net-1", "text": "x"})
        return store

    def test_safety_net_archives_and_clears(self, monkeypatch, tmp_path):
        store = self._store_with_live_events(monkeypatch, tmp_path)
        live = tmp_path / "events" / "T9.ndjson"
        assert live.stat().st_size > 0

        args = MagicMock()
        args.terminal_id = "T9"
        args.dispatch_id = "d-net-1"
        provider_dispatch._event_store_safety_net(store, args)

        assert live.stat().st_size == 0, "live stream must be truncated"
        archived = tmp_path / "events" / "archive" / "T9" / "d-net-1.ndjson"
        assert archived.exists(), "events must be archived before truncation"

    def test_safety_net_idempotent_after_normal_emit(self, monkeypatch, tmp_path):
        store = self._store_with_live_events(monkeypatch, tmp_path)
        args = MagicMock()
        args.terminal_id = "T9"
        args.dispatch_id = "d-net-2"
        # Simulate _emit_governance's authoritative archive+clear:
        store.clear("T9", archive_dispatch_id="d-net-2")
        first = (tmp_path / "events" / "archive" / "T9" / "d-net-2.ndjson").read_text()
        # Finally-path safety net runs afterwards — must not re-archive or fail.
        provider_dispatch._event_store_safety_net(store, args)
        second = (tmp_path / "events" / "archive" / "T9" / "d-net-2.ndjson").read_text()
        assert first == second, "second clear must not overwrite the archive"

    def test_safety_net_never_raises(self, monkeypatch):
        args = MagicMock()
        args.terminal_id = "T9"
        args.dispatch_id = "d-net-3"
        broken = MagicMock()
        broken.clear.side_effect = OSError("disk gone")
        provider_dispatch._event_store_safety_net(broken, args)  # must not raise
        provider_dispatch._event_store_safety_net(None, args)    # None is a no-op
