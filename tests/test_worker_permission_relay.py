#!/usr/bin/env python3
"""Tests for the worker-permission relay (scripts/lib/worker_permission_relay.py).

Covers: the escalation store (write/list/resolve/resolve-stale), the
path-traversal guard on dispatch ids, and the manual ``approve`` relay with a
fake tmux runner asserting the SEPARATE-Enter + EXPLICIT-session send-keys
contract.
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from worker_permission_relay import (  # noqa: E402
    _safe_dispatch_id,
    list_escalations,
    read_escalation,
    resolve_escalation,
    resolve_stale_escalation,
    write_escalation,
)

import permission_relay_cli as cli  # noqa: E402


# ---------------------------------------------------------------------------
# Fake tmux runner
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Records every tmux invocation and can script send-keys failures.

    ``send_fail`` makes every ``send-keys`` invocation return a non-zero rc so the
    approve relay's send-keys-success gating can be exercised.
    """

    def __init__(self, *, send_fail=False, tmux_available=True):
        self.send_fail = send_fail
        self._tmux_available = tmux_available
        self.calls = []  # list[list[str]]

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:1] == ["send-keys"] and self.send_fail:
            return _FakeResult(returncode=1)
        return _FakeResult(returncode=0)

    def available(self):
        return self._tmux_available

    def send_key_calls(self):
        return [c for c in self.calls if c[:1] == ["send-keys"]]


# ---------------------------------------------------------------------------
# Escalation lifecycle
# ---------------------------------------------------------------------------
class TestEscalationLifecycle:
    def test_write_list_resolve(self, tmp_path):
        write_escalation("d1", "rm -rf /x", "catastrophic", state_dir=tmp_path)
        write_escalation("d2", "pytest -q", "window_closed", state_dir=tmp_path)
        pending = list_escalations(state_dir=tmp_path)
        assert {r["dispatch_id"] for r in pending} == {"d1", "d2"}

        resolve_escalation("d1", approved=True, state_dir=tmp_path)
        rec = read_escalation("d1", state_dir=tmp_path)
        assert rec["status"] == "approved"
        assert rec["resolved_at"] is not None
        # Only d2 remains pending.
        pending = list_escalations(state_dir=tmp_path)
        assert {r["dispatch_id"] for r in pending} == {"d2"}

    def test_resolve_missing_returns_none(self, tmp_path):
        assert resolve_escalation("nope", approved=True, state_dir=tmp_path) is None

    def test_write_is_idempotent_for_same_pending(self, tmp_path):
        p1 = write_escalation("d3", "rm -rf /x", "catastrophic", state_dir=tmp_path, now=1000.0)
        rec1 = read_escalation("d3", state_dir=tmp_path)
        # Second write with same command + later time keeps the original captured_at.
        write_escalation("d3", "rm -rf /x", "catastrophic", state_dir=tmp_path, now=2000.0)
        rec2 = read_escalation("d3", state_dir=tmp_path)
        assert rec1["captured_at"] == rec2["captured_at"]
        assert p1.exists()


# ---------------------------------------------------------------------------
# resolve_stale_escalation (OI-1414) — resolve path for dead-dispatch escalations
# ---------------------------------------------------------------------------
class TestResolveStaleEscalation:
    def test_resolves_pending_without_live_session(self, tmp_path):
        write_escalation("d-old", "rm -rf:*", "catastrophic", state_dir=tmp_path)
        rec = resolve_stale_escalation(
            "d-old", "dispatch completed 19 days ago, no live tmux session", state_dir=tmp_path
        )
        assert rec["status"] == "stale"
        assert rec["resolved_at"] is not None
        assert rec["resolution_note"] == "dispatch completed 19 days ago, no live tmux session"
        # Persisted, and no longer pending.
        on_disk = read_escalation("d-old", state_dir=tmp_path)
        assert on_disk["status"] == "stale"
        assert list_escalations(state_dir=tmp_path) == []

    def test_missing_escalation_returns_none(self, tmp_path):
        assert resolve_stale_escalation("ghost", "note", state_dir=tmp_path) is None

    def test_empty_note_rejected(self, tmp_path):
        write_escalation("d-note", "pytest -q", "window_closed", state_dir=tmp_path)
        with pytest.raises(ValueError):
            resolve_stale_escalation("d-note", "", state_dir=tmp_path)
        with pytest.raises(ValueError):
            resolve_stale_escalation("d-note", "   ", state_dir=tmp_path)
        # Left untouched — still pending.
        assert read_escalation("d-note", state_dir=tmp_path)["status"] == "pending"

    def test_traversal_dispatch_id_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_stale_escalation("../../etc/x", "note", state_dir=tmp_path)


# ---------------------------------------------------------------------------
# Finding 1 — path-traversal: dispatch_id must be sanitized before path use
# ---------------------------------------------------------------------------
class TestSafeDispatchId:
    @pytest.mark.parametrize(
        "bad",
        [
            "../../etc/x",      # classic traversal
            "a/b",              # path separator
            "..",               # parent-dir segment (matches the char class!)
            ".",                # current-dir segment
            "/etc/passwd",      # absolute path
            "x/../../y",        # embedded traversal
            "a b",              # space (outside the allow-list)
            "name;rm",          # shell metachar
            "",                 # empty
        ],
    )
    def test_rejects_unsafe(self, bad):
        with pytest.raises(ValueError):
            _safe_dispatch_id(bad)

    @pytest.mark.parametrize(
        "good",
        [
            "20260602-173238-relay-fix",
            "disp_1",
            "a.b.c",
            "T1-track-A",
        ],
    )
    def test_accepts_safe(self, good):
        assert _safe_dispatch_id(good) == good

    def test_write_escalation_rejects_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            write_escalation("../../etc/x", "rm -rf /x", "catastrophic", state_dir=tmp_path)
        # And nothing was written outside the escalations dir.
        assert not (tmp_path.parent / "x.json").exists()

    def test_read_escalation_rejects_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            read_escalation("a/b", state_dir=tmp_path)

    def test_resolve_escalation_rejects_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_escalation("../../etc/x", approved=True, state_dir=tmp_path)


# ---------------------------------------------------------------------------
# Finding 3 — manual `approve` must surface send failure + gate resolved=approved
# ---------------------------------------------------------------------------
class _Args:
    def __init__(self, dispatch_id, as_json=False):
        self.dispatch_id = dispatch_id
        self.json = as_json


def _write_handle(sd: Path, dispatch_id: str, session: str) -> None:
    hdir = sd / "tmux_interactive"
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / f"{dispatch_id}.json").write_text(
        json.dumps({"dispatch_id": dispatch_id, "session": session}), encoding="utf-8"
    )


class TestManualApproveSurfacesSendFailure:
    def test_approve_send_failure_leaves_pending_and_exits_nonzero(self, tmp_path, monkeypatch):
        write_escalation("d-app", "pytest -q", "window_closed", state_dir=tmp_path)
        _write_handle(tmp_path, "d-app", "vnx-d-app")
        # Inject a runner whose send-keys fails. Capture the ORIGINAL fn first so the
        # wrapper does not recurse into itself after monkeypatching.
        failing = FakeRunner(send_fail=True)
        orig = cli._relay_keystroke_to_worker
        monkeypatch.setattr(
            cli, "_relay_keystroke_to_worker",
            lambda did, sd: orig(did, sd, runner=failing),
        )
        rc = cli._cmd_approve(_Args("d-app"), tmp_path)
        assert rc == 1  # non-zero: send did not land
        # Escalation MUST remain pending (NOT flipped to approved).
        rec = read_escalation("d-app", state_dir=tmp_path)
        assert rec["status"] == "pending"

    def test_approve_no_session_leaves_pending_and_exits_nonzero(self, tmp_path):
        write_escalation("d-nos", "pytest -q", "window_closed", state_dir=tmp_path)
        # No handle written -> no live session.
        rc = cli._cmd_approve(_Args("d-nos"), tmp_path)
        assert rc == 1
        rec = read_escalation("d-nos", state_dir=tmp_path)
        assert rec["status"] == "pending"

    def test_approve_success_marks_approved_and_exits_zero(self, tmp_path, monkeypatch):
        write_escalation("d-ok", "pytest -q", "window_closed", state_dir=tmp_path)
        _write_handle(tmp_path, "d-ok", "vnx-d-ok")
        ok = FakeRunner(send_fail=False)
        orig = cli._relay_keystroke_to_worker
        monkeypatch.setattr(
            cli, "_relay_keystroke_to_worker",
            lambda did, sd: orig(did, sd, runner=ok),
        )
        rc = cli._cmd_approve(_Args("d-ok"), tmp_path)
        assert rc == 0
        rec = read_escalation("d-ok", state_dir=tmp_path)
        assert rec["status"] == "approved"

    def test_approve_missing_escalation_exits_one(self, tmp_path):
        rc = cli._cmd_approve(_Args("ghost"), tmp_path)
        assert rc == 1

    def test_approve_rejects_traversal_dispatch_id(self, tmp_path):
        rc = cli._cmd_approve(_Args("../../etc/x"), tmp_path)
        assert rc == 2  # invalid dispatch_id, rejected before any path use

    def test_deny_rejects_traversal_dispatch_id(self, tmp_path):
        rc = cli._cmd_deny(_Args("a/b"), tmp_path)
        assert rc == 2

    def test_relay_keystroke_distinguishes_statuses(self, tmp_path):
        # no handle -> no_session
        assert cli._relay_keystroke_to_worker("none", tmp_path)[0] == "no_session"
        # handle + failing send -> send_failed
        _write_handle(tmp_path, "sf", "vnx-sf")
        status, session = cli._relay_keystroke_to_worker(
            "sf", tmp_path, runner=FakeRunner(send_fail=True)
        )
        assert status == "send_failed"
        assert session == "vnx-sf"
        # handle + ok send -> sent
        _write_handle(tmp_path, "okk", "vnx-okk")
        status, session = cli._relay_keystroke_to_worker(
            "okk", tmp_path, runner=FakeRunner(send_fail=False)
        )
        assert status == "sent"
        assert session == "vnx-okk"
        # tmux unavailable -> error (operational, not "no session")
        _write_handle(tmp_path, "noux", "vnx-noux")
        status, session = cli._relay_keystroke_to_worker(
            "noux", tmp_path, runner=FakeRunner(tmux_available=False)
        )
        assert status == "error"


# ---------------------------------------------------------------------------
# `permission resolve-stale` — CLI resolve path for dead-dispatch escalations
# (OI-1414: 21 records pending up to 19 days, all denied a live tmux session)
# ---------------------------------------------------------------------------
class _StaleArgs:
    def __init__(self, dispatch_id, note, as_json=False):
        self.dispatch_id = dispatch_id
        self.note = note
        self.json = as_json


class TestResolveStaleCli:
    def test_resolve_stale_no_session_needed(self, tmp_path):
        write_escalation("d-dead", "rm -rf:*", "catastrophic", state_dir=tmp_path)
        # No tmux_interactive handle written — the worker is long gone.
        rc = cli._cmd_resolve_stale(
            _StaleArgs("d-dead", "dispatch completed 19 days ago, no live session"), tmp_path
        )
        assert rc == 0
        rec = read_escalation("d-dead", state_dir=tmp_path)
        assert rec["status"] == "stale"
        assert rec["resolved_at"] is not None
        assert rec["resolution_note"] == "dispatch completed 19 days ago, no live session"

    def test_resolve_stale_missing_escalation_exits_one(self, tmp_path):
        rc = cli._cmd_resolve_stale(_StaleArgs("ghost", "note"), tmp_path)
        assert rc == 1

    def test_resolve_stale_rejects_traversal_dispatch_id(self, tmp_path):
        rc = cli._cmd_resolve_stale(_StaleArgs("../../etc/x", "note"), tmp_path)
        assert rc == 2

    def test_resolve_stale_rejects_empty_note(self, tmp_path):
        write_escalation("d-blank", "pytest -q", "window_closed", state_dir=tmp_path)
        rc = cli._cmd_resolve_stale(_StaleArgs("d-blank", "  "), tmp_path)
        assert rc == 2
        assert read_escalation("d-blank", state_dir=tmp_path)["status"] == "pending"
