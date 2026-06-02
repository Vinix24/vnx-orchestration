#!/usr/bin/env python3
"""Tests for the worker-permission relay (scripts/lib/worker_permission_relay.py).

Covers: PermissionWindow open/close/status + expiry; is_catastrophic hard-list;
parse_pending_command extraction; decide policy; relay_tick auto-approve /
escalate / idempotency with a fake tmux runner asserting the SEPARATE-Enter +
EXPLICIT-session send-keys contract.
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import worker_permission_relay as relay  # noqa: E402
from worker_permission_relay import (  # noqa: E402
    PermissionWindow,
    _safe_dispatch_id,
    decide,
    is_catastrophic,
    list_escalations,
    parse_pending_command,
    read_escalation,
    relay_tick,
    resolve_escalation,
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
    """Records every tmux invocation; returns a scripted pane for capture-pane.

    ``send_fail`` makes every ``send-keys`` invocation return a non-zero rc so the
    relay's send-keys-success gating can be exercised. ``capture-pane`` still
    succeeds so the prompt is parsed normally.
    """

    def __init__(self, pane_text="", *, send_fail=False, tmux_available=True):
        self.pane_text = pane_text
        self.send_fail = send_fail
        self._tmux_available = tmux_available
        self.calls = []  # list[list[str]]

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:1] == ["capture-pane"]:
            return _FakeResult(returncode=0, stdout=self.pane_text)
        if args[:1] == ["send-keys"] and self.send_fail:
            return _FakeResult(returncode=1)
        return _FakeResult(returncode=0)

    def available(self):
        return self._tmux_available

    def send_key_calls(self):
        return [c for c in self.calls if c[:1] == ["send-keys"]]


# ---------------------------------------------------------------------------
# Sample pane buffers
# ---------------------------------------------------------------------------
PROMPT_PANE = """\
● I'll remove the temp directory.

╭───────────────────────────────────────────────╮
│ Bash command                                   │
│                                                 │
│   rm -rf /tmp/scratch                           │
│   Delete the scratch directory                  │
│                                                 │
│ Do you want to proceed?                         │
│ ❯ 1. Yes                                        │
│   2. Yes, and don't ask again this session      │
│   3. No, and tell Claude what to do differently │
╰───────────────────────────────────────────────╯
"""

ROUTINE_PROMPT_PANE = """\
╭───────────────────────────────────────────────╮
│ Bash command                                   │
│                                                 │
│   pytest tests/test_foo.py -q                   │
│   Run the focused test                          │
│                                                 │
│ Do you want to proceed?                         │
│ ❯ 1. Yes                                        │
│   2. No                                         │
╰───────────────────────────────────────────────╯
"""

TOOL_TOKEN_PANE = """\
● Running a command

  Bash(chmod +x scripts/deploy.sh)

  Do you want to proceed?
  ❯ 1. Yes
    2. No
"""

IDLE_PANE = """\
● Done. All tests pass.

› ❯
"""


# ---------------------------------------------------------------------------
# PermissionWindow
# ---------------------------------------------------------------------------
class TestPermissionWindow:
    def test_default_closed(self, tmp_path):
        win = PermissionWindow(tmp_path)
        assert win.is_open() is False
        assert win.status()["open"] is False
        assert win.status()["remaining_seconds"] == 0

    def test_open_then_status(self, tmp_path):
        win = PermissionWindow(tmp_path)
        win.open(10, "operator at keyboard", now=1000.0)
        # 5 minutes later still open.
        assert win.is_open(now=1000.0 + 300) is True
        status = win.status(now=1000.0 + 300)
        assert status["open"] is True
        assert status["remaining_seconds"] == 300  # 10min - 5min
        assert status["reason"] == "operator at keyboard"

    def test_close(self, tmp_path):
        win = PermissionWindow(tmp_path)
        win.open(10, now=1000.0)
        win.close(now=1000.0 + 60)
        assert win.is_open(now=1000.0 + 60) is False
        assert win.status(now=1000.0 + 60)["open"] is False

    def test_expiry_reads_closed(self, tmp_path):
        win = PermissionWindow(tmp_path)
        win.open(5, now=1000.0)
        # 6 minutes later: expired even though open flag is true on disk.
        assert win.is_open(now=1000.0 + 360) is False
        assert win.status(now=1000.0 + 360)["open"] is False
        assert win.status(now=1000.0 + 360)["remaining_seconds"] == 0

    def test_open_rejects_nonpositive(self, tmp_path):
        win = PermissionWindow(tmp_path)
        with pytest.raises(ValueError):
            win.open(0)


# ---------------------------------------------------------------------------
# is_catastrophic
# ---------------------------------------------------------------------------
class TestIsCatastrophic:
    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf /x",
            "rm -fr /tmp/data",
            "rm -r -f build",
            "sudo rm -rf /",
            "DROP TABLE foo",
            "drop database prod",
            "TRUNCATE TABLE events",
            "mkfs.ext4 /dev/sda1",
            "mkfs /dev/sdb",
            "dd if=/dev/zero of=/dev/sda bs=1M",
            "echo boom > /dev/sda",
            "git reset --hard origin/main",
            "find . -name '*.log' -delete",
            "find /tmp -exec rm {} \\;",
            "git clean -fdx",
        ],
    )
    def test_catastrophic_true(self, cmd):
        assert is_catastrophic(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "git push --force-with-lease",
            "git push --force",
            "git push -f origin feature",
            "chmod +x scripts/x.sh",
            "git commit -m 'feat: thing'",
            "pytest tests/ -q",
            "rm file.txt",          # non-recursive single file
            "rm -f stale.lock",     # force but not recursive
            "git reset --hard HEAD~1",  # local reset, not onto a remote
            "ls -la",
            "",
        ],
    )
    def test_catastrophic_false(self, cmd):
        assert is_catastrophic(cmd) is False


# ---------------------------------------------------------------------------
# parse_pending_command
# ---------------------------------------------------------------------------
class TestParsePendingCommand:
    def test_extracts_from_box(self):
        assert parse_pending_command(PROMPT_PANE) == "rm -rf /tmp/scratch"

    def test_extracts_routine(self):
        assert parse_pending_command(ROUTINE_PROMPT_PANE) == "pytest tests/test_foo.py -q"

    def test_extracts_bash_token(self):
        assert parse_pending_command(TOOL_TOKEN_PANE) == "chmod +x scripts/deploy.sh"

    def test_idle_returns_none(self):
        assert parse_pending_command(IDLE_PANE) is None

    def test_empty_returns_none(self):
        assert parse_pending_command("") is None


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------
class TestDecide:
    def test_open_routine_auto_approve(self, tmp_path):
        win = PermissionWindow(tmp_path)
        win.open(10, now=1000.0)
        # window.is_open() uses real time; open for 10 min from now() so it's open.
        assert decide("pytest -q", True) == "auto_approve"

    def test_open_catastrophic_escalates(self):
        assert decide("rm -rf /tmp/x", True) == "escalate"

    def test_closed_escalates(self):
        assert decide("pytest -q", False) == "escalate"

    def test_window_object_open(self, tmp_path):
        win = PermissionWindow(tmp_path)
        win.open(10)
        assert decide("pytest -q", win) == "auto_approve"

    def test_window_object_closed(self, tmp_path):
        win = PermissionWindow(tmp_path)
        assert decide("pytest -q", win) == "escalate"


# ---------------------------------------------------------------------------
# relay_tick
# ---------------------------------------------------------------------------
class TestRelayTick:
    def test_auto_approve_sends_one_then_enter_separate(self, tmp_path):
        win = PermissionWindow(tmp_path)
        win.open(10)  # open now
        runner = FakeRunner(pane_text=ROUTINE_PROMPT_PANE)
        action = relay_tick("vnx-disp-1", "disp-1", runner, state_dir=tmp_path, window=win)
        assert action == "auto_approve"

        sends = runner.send_key_calls()
        assert len(sends) == 2, sends
        # First keystroke: literal "1" to the EXPLICIT session, never empty target.
        assert sends[0] == ["send-keys", "-t", "vnx-disp-1", "1"]
        # Second keystroke: Enter as its OWN separate call.
        assert sends[1] == ["send-keys", "-t", "vnx-disp-1", "Enter"]
        # No send-keys ever targets an empty/missing session.
        for c in sends:
            assert "-t" in c
            tgt = c[c.index("-t") + 1]
            assert tgt == "vnx-disp-1" and tgt != ""

    def test_escalate_writes_record_and_sends_no_keys(self, tmp_path):
        win = PermissionWindow(tmp_path)
        win.open(10)  # window OPEN — but command is catastrophic, must escalate
        runner = FakeRunner(pane_text=PROMPT_PANE)  # rm -rf
        action = relay_tick("vnx-disp-2", "disp-2", runner, state_dir=tmp_path, window=win)
        assert action == "escalate"
        # NO send-keys issued.
        assert runner.send_key_calls() == []
        # Escalation record written, pending, reason=catastrophic.
        rec = read_escalation("disp-2", state_dir=tmp_path)
        assert rec is not None
        assert rec["status"] == "pending"
        assert rec["reason"] == "catastrophic"
        assert rec["command"] == "rm -rf /tmp/scratch"

    def test_window_closed_escalates_routine(self, tmp_path):
        # No window opened => closed => even a routine prompt escalates.
        runner = FakeRunner(pane_text=ROUTINE_PROMPT_PANE)
        action = relay_tick("vnx-disp-3", "disp-3", runner, state_dir=tmp_path)
        assert action == "escalate"
        assert runner.send_key_calls() == []
        rec = read_escalation("disp-3", state_dir=tmp_path)
        assert rec is not None
        assert rec["reason"] == "window_closed"

    def test_idle_pane_no_action(self, tmp_path):
        runner = FakeRunner(pane_text=IDLE_PANE)
        action = relay_tick("vnx-disp-4", "disp-4", runner, state_dir=tmp_path)
        assert action == "idle"
        assert runner.send_key_calls() == []

    def test_idempotent_repeated_identical_prompt(self, tmp_path):
        win = PermissionWindow(tmp_path)
        win.open(10)
        runner = FakeRunner(pane_text=ROUTINE_PROMPT_PANE)
        first = relay_tick("vnx-disp-5", "disp-5", runner, state_dir=tmp_path, window=win)
        assert first == "auto_approve"
        assert len(runner.send_key_calls()) == 2
        # Same prompt still on the pane on the next tick — must NOT re-approve.
        second = relay_tick("vnx-disp-5", "disp-5", runner, state_dir=tmp_path, window=win)
        assert second == "already_handled"
        assert len(runner.send_key_calls()) == 2  # unchanged

    def test_empty_session_refused(self, tmp_path):
        runner = FakeRunner(pane_text=ROUTINE_PROMPT_PANE)
        with pytest.raises(ValueError):
            relay_tick("", "disp-6", runner, state_dir=tmp_path)


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

    def test_relay_tick_rejects_traversal(self, tmp_path):
        runner = FakeRunner(pane_text=ROUTINE_PROMPT_PANE)
        with pytest.raises(ValueError):
            relay_tick("vnx-sess", "../../etc/x", runner, state_dir=tmp_path)


# ---------------------------------------------------------------------------
# Finding 2 — auto-approve must NOT mark handled when the send-keys fails
# ---------------------------------------------------------------------------
class TestAutoApproveGatesOnSendResult:
    def test_failed_send_not_marked_handled(self, tmp_path):
        win = PermissionWindow(tmp_path)
        win.open(10)  # window OPEN — routine prompt would auto-approve
        runner = FakeRunner(pane_text=ROUTINE_PROMPT_PANE, send_fail=True)
        action = relay_tick("vnx-disp-f", "disp-f", runner, state_dir=tmp_path, window=win)
        # Send failed -> explicit failure status, NOT "auto_approve".
        assert action == "auto_approve_failed"
        # Fingerprint NOT persisted -> a retry next tick is still possible.
        assert relay._read_fingerprint(tmp_path, "disp-f") is None
        # The "1" keystroke was attempted (returned rc!=0), so the first send-keys
        # is present; Enter is short-circuited because rc1 failed.
        sends = runner.send_key_calls()
        assert sends[0] == ["send-keys", "-t", "vnx-disp-f", "1"]

    def test_retry_after_failed_send_then_success(self, tmp_path):
        win = PermissionWindow(tmp_path)
        win.open(10)
        # First tick: send fails -> not handled.
        failing = FakeRunner(pane_text=ROUTINE_PROMPT_PANE, send_fail=True)
        assert relay_tick("vnx-r", "disp-r", failing, state_dir=tmp_path, window=win) == "auto_approve_failed"
        assert relay._read_fingerprint(tmp_path, "disp-r") is None
        # Second tick (same prompt still on pane): send succeeds -> handled now.
        ok = FakeRunner(pane_text=ROUTINE_PROMPT_PANE, send_fail=False)
        assert relay_tick("vnx-r", "disp-r", ok, state_dir=tmp_path, window=win) == "auto_approve"
        assert relay._read_fingerprint(tmp_path, "disp-r") is not None
        sends = ok.send_key_calls()
        assert sends == [
            ["send-keys", "-t", "vnx-r", "1"],
            ["send-keys", "-t", "vnx-r", "Enter"],
        ]


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
