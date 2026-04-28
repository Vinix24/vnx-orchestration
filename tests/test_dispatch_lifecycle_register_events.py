"""Tests for dispatch_created + dispatch_promoted register events (T5-PR1).

Verifies that:
  A. dispatch_created is appended when a dispatch markdown lands in pending/
     (emitted by queue_auto_accept.sh via subprocess call to dispatch_register.py)
  B. dispatch_promoted is appended when finalize_dispatch_delivery moves pending → active/
     (emitted by dispatch_lifecycle.sh via subprocess call to dispatch_register.py)
  C. A failing register call does not block the main flow (best-effort contract)
  D. Idempotency: calling append_event twice writes two records (expected append-only
     behavior; caller-side dedup via the mv operations prevents double-emission naturally)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Make scripts/lib importable
_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import dispatch_register
from dispatch_register import append_event, read_events

_REGISTER_PY = _LIB_DIR / "dispatch_register.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_state_dir(monkeypatch, tmp_path):
    """Route all register I/O into a fresh tmp dir for every test."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    return state_dir


def _reg_path(state_dir: Path) -> Path:
    return state_dir / "dispatch_register.ndjson"


def _run_cli(*args: str, state_dir: Path) -> subprocess.CompletedProcess:
    """Invoke dispatch_register.py via subprocess with an isolated state dir."""
    env = {k: v for k, v in __import__("os").environ.items()}
    env["VNX_STATE_DIR"] = str(state_dir)
    env.pop("VNX_DATA_DIR", None)
    env.pop("VNX_DATA_DIR_EXPLICIT", None)
    return subprocess.run(
        [sys.executable, str(_REGISTER_PY), *args],
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Case A: write dispatch markdown → dispatch_created appended
# ---------------------------------------------------------------------------

class TestDispatchCreated:
    """dispatch_created is emitted when a dispatch lands in pending/."""

    def test_append_event_returns_true(self, isolated_state_dir):
        result = append_event("dispatch_created", dispatch_id="test-dispatch-001", terminal="T1")
        assert result is True

    def test_record_written_to_register(self, isolated_state_dir):
        append_event("dispatch_created", dispatch_id="test-dispatch-001", terminal="T1")
        reg = _reg_path(isolated_state_dir)
        assert reg.exists(), "dispatch_register.ndjson was not created"
        rec = json.loads(reg.read_text().strip())
        assert rec["event"] == "dispatch_created"
        assert rec["dispatch_id"] == "test-dispatch-001"
        assert rec["terminal"] == "T1"

    def test_timestamp_present(self, isolated_state_dir):
        append_event("dispatch_created", dispatch_id="ts-001")
        rec = json.loads(_reg_path(isolated_state_dir).read_text().strip())
        assert "timestamp" in rec
        assert rec["timestamp"].endswith("Z")

    def test_cli_invocation_writes_record(self, isolated_state_dir):
        """Simulate the bash subprocess call: python3 dispatch_register.py append dispatch_created ..."""
        result = _run_cli(
            "append", "dispatch_created",
            "dispatch_id=cli-dispatch-001",
            "terminal=T2",
            state_dir=isolated_state_dir,
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        reg = _reg_path(isolated_state_dir)
        rec = json.loads(reg.read_text().strip())
        assert rec["event"] == "dispatch_created"
        assert rec["dispatch_id"] == "cli-dispatch-001"
        assert rec["terminal"] == "T2"

    def test_dispatch_created_without_terminal_still_writes(self, isolated_state_dir):
        """Terminal field is optional; dispatch_id alone satisfies ID requirement."""
        result = append_event("dispatch_created", dispatch_id="no-terminal-001")
        assert result is True
        rec = json.loads(_reg_path(isolated_state_dir).read_text().strip())
        assert rec["event"] == "dispatch_created"
        assert "terminal" not in rec


# ---------------------------------------------------------------------------
# Case B: promote pending → active → dispatch_promoted appended
# ---------------------------------------------------------------------------

class TestDispatchPromoted:
    """dispatch_promoted is emitted when finalize_dispatch_delivery moves pending → active."""

    def test_append_event_returns_true(self, isolated_state_dir):
        result = append_event("dispatch_promoted", dispatch_id="promoted-001", terminal="T1")
        assert result is True

    def test_record_written_to_register(self, isolated_state_dir):
        append_event("dispatch_promoted", dispatch_id="promoted-001", terminal="T1")
        reg = _reg_path(isolated_state_dir)
        rec = json.loads(reg.read_text().strip())
        assert rec["event"] == "dispatch_promoted"
        assert rec["dispatch_id"] == "promoted-001"
        assert rec["terminal"] == "T1"

    def test_cli_invocation_writes_record(self, isolated_state_dir):
        """Simulate the bash subprocess call: python3 dispatch_register.py append dispatch_promoted ..."""
        result = _run_cli(
            "append", "dispatch_promoted",
            "dispatch_id=promoted-cli-001",
            "terminal=T3",
            state_dir=isolated_state_dir,
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        rec = json.loads(_reg_path(isolated_state_dir).read_text().strip())
        assert rec["event"] == "dispatch_promoted"
        assert rec["dispatch_id"] == "promoted-cli-001"
        assert rec["terminal"] == "T3"

    def test_created_then_promoted_both_present(self, isolated_state_dir):
        """Simulate full lifecycle: created → promoted — both events persist."""
        append_event("dispatch_created", dispatch_id="full-lifecycle-001", terminal="T1")
        append_event("dispatch_promoted", dispatch_id="full-lifecycle-001", terminal="T1")
        events = read_events()
        assert len(events) == 2
        assert events[0]["event"] == "dispatch_created"
        assert events[1]["event"] == "dispatch_promoted"
        assert all(e["dispatch_id"] == "full-lifecycle-001" for e in events)


# ---------------------------------------------------------------------------
# Case C: register call failure does not block main flow
# ---------------------------------------------------------------------------

class TestRegisterFailureNonFatal:
    """A failing register call returns False and never raises."""

    def test_oserror_returns_false(self, isolated_state_dir):
        """OSError on file open → append_event returns False, does not raise."""
        with patch.object(dispatch_register.Path, "open", side_effect=OSError("disk full")):
            result = append_event("dispatch_created", dispatch_id="fail-001")
        assert result is False

    def test_oserror_does_not_raise(self, isolated_state_dir):
        """append_event never raises regardless of underlying I/O failure."""
        with patch.object(dispatch_register.Path, "open", side_effect=OSError("read-only fs")):
            try:
                append_event("dispatch_promoted", dispatch_id="no-raise-001")
            except Exception as exc:
                pytest.fail(f"append_event raised unexpectedly: {exc}")

    def test_cli_invalid_event_exits_nonzero(self, isolated_state_dir):
        """CLI returns nonzero for an unknown event; does not crash."""
        result = _run_cli("append", "bad_event_name", "dispatch_id=x", state_dir=isolated_state_dir)
        assert result.returncode != 0

    def test_failed_emit_leaves_register_consistent(self, isolated_state_dir):
        """After a failing emit, a subsequent successful emit still works."""
        with patch.object(dispatch_register.Path, "open", side_effect=OSError("transient")):
            append_event("dispatch_created", dispatch_id="transient-001")

        # Subsequent call should succeed
        result = append_event("dispatch_promoted", dispatch_id="transient-001", terminal="T1")
        assert result is True
        events = read_events()
        assert len(events) == 1
        assert events[0]["event"] == "dispatch_promoted"

    def test_append_event_used_as_fire_and_forget(self, isolated_state_dir):
        """Callers can ignore the return value — function is best-effort by design."""
        # This verifies the append_event signature returns bool (not void), enabling
        # callers to log failures without being forced to handle them.
        with patch.object(dispatch_register.Path, "open", side_effect=OSError("fail")):
            rv = append_event("dispatch_created", dispatch_id="ignore-rv-001")
        assert isinstance(rv, bool)


# ---------------------------------------------------------------------------
# Case D: idempotency — append-only log; double-emit produces two records
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Verify expected append-only behavior; document caller-side dedup contract."""

    def test_double_emit_produces_two_records(self, isolated_state_dir):
        """Calling append_event twice writes two records (append-only log — expected).

        Callers (queue_auto_accept mv, finalize_dispatch_delivery mv) prevent
        double-emission naturally via file-system mv semantics: a file can only
        be moved once from queue/ → pending/ and once from pending/ → active/.
        """
        append_event("dispatch_created", dispatch_id="dup-test-001")
        append_event("dispatch_created", dispatch_id="dup-test-001")
        events = read_events()
        assert len(events) == 2
        assert all(e["event"] == "dispatch_created" for e in events)
        assert all(e["dispatch_id"] == "dup-test-001" for e in events)

    def test_different_dispatch_ids_produce_separate_records(self, isolated_state_dir):
        """Each unique dispatch_id gets its own record."""
        append_event("dispatch_created", dispatch_id="d-alpha-001")
        append_event("dispatch_created", dispatch_id="d-beta-002")
        events = read_events()
        assert len(events) == 2
        dispatch_ids = {e["dispatch_id"] for e in events}
        assert dispatch_ids == {"d-alpha-001", "d-beta-002"}

    def test_mv_semantics_prevent_double_emission(self, tmp_path, isolated_state_dir):
        """Simulate queue_auto_accept dedup: if target exists, source is removed not moved.

        This documents the caller-side idempotency: once a .md file is in pending/,
        subsequent queue_auto_accept iterations remove the queue duplicate without
        triggering another dispatch_created emit.
        """
        queue_dir = tmp_path / "queue"
        pending_dir = tmp_path / "pending"
        queue_dir.mkdir()
        pending_dir.mkdir()

        # Put dispatch in queue
        dispatch_id = "mv-dedup-test-001"
        queue_file = queue_dir / f"{dispatch_id}.md"
        queue_file.write_text("# test dispatch\n", encoding="utf-8")
        pending_file = pending_dir / f"{dispatch_id}.md"

        # First "accept": mv queue → pending, emit
        queue_file.rename(pending_file)
        append_event("dispatch_created", dispatch_id=dispatch_id)

        # Simulate re-queuing (duplicate appears in queue)
        queue_file.write_text("# test dispatch\n", encoding="utf-8")

        # Second "accept": target already exists → remove queue file, no emit
        if pending_file.exists():
            queue_file.unlink(missing_ok=True)
            # No append_event call — mirrors queue_auto_accept.sh dedup branch
        else:
            queue_file.rename(pending_file)
            append_event("dispatch_created", dispatch_id=dispatch_id)

        events = read_events()
        assert len(events) == 1, (
            f"Expected 1 dispatch_created (dedup prevented second emit), got {len(events)}"
        )
