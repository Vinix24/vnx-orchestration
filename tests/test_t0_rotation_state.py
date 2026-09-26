"""tests/test_t0_rotation_state.py — the pending marker and latch (dispatch 20260926-ff-t0-rotation-ci).

Pins the three properties the review of the rotation PR asked for:

  - the marker changes ONLY on a transition (no rewrite, no fresh timestamp on a repeat);
  - the ledger is written BEFORE the state file it describes (ADR-005), and a ledger that
    refuses the transition leaves the marker alone so the next hook call retries it;
  - the read-decide-write of the marker is one critical section (OI-1486): parallel hook
    processes cannot both see "new" and both record it.

Everything runs against tmp paths; nothing touches a real store.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import t0_rotation_state as rotation_state

PANE = "%9"
SESSION = "sess-state-1"


def _pending(sdir: Path, **overrides) -> bool:
    kwargs = dict(pane=PANE, session_id=SESSION, tokens=510_000, level="force",
                  transcript_path="/t.jsonl")
    kwargs.update(overrides)
    return rotation_state.write_pending(sdir, **kwargs)


def _marker(sdir: Path) -> dict:
    return json.loads(rotation_state.pending_path(sdir, PANE).read_text(encoding="utf-8"))


def _scratch_files(sdir: Path) -> List[str]:
    return sorted(p.name for p in sdir.iterdir() if p.name.endswith(".tmp"))


# ── the marker changes only on a transition ──────────────────────────────────


def test_first_crossing_writes_the_marker_and_reports_a_transition(tmp_path):
    assert _pending(tmp_path) is True
    marker = _marker(tmp_path)
    assert marker["session_id"] == SESSION and marker["level"] == "force"
    assert marker["first_seen"] and marker["transitioned"]
    assert rotation_state.pending_session_id(tmp_path, PANE) == SESSION


def test_a_repeat_in_the_same_band_leaves_the_marker_byte_for_byte_untouched(tmp_path):
    assert _pending(tmp_path) is True
    path = rotation_state.pending_path(tmp_path, PANE)
    before = path.read_bytes()
    os.utime(path, (1_000_000_000, 1_000_000_000))
    inode = path.stat().st_ino

    assert _pending(tmp_path, tokens=560_000, transcript_path="/other.jsonl") is False

    assert path.read_bytes() == before
    assert path.stat().st_mtime == 1_000_000_000
    assert path.stat().st_ino == inode, "the marker was replaced although nothing transitioned"


def test_a_band_change_is_a_transition_and_keeps_first_seen(tmp_path):
    _pending(tmp_path)
    first = _marker(tmp_path)

    assert _pending(tmp_path, level="hard", tokens=610_000) is True

    marker = _marker(tmp_path)
    assert marker["level"] == "hard" and marker["tokens"] == 610_000
    assert marker["first_seen"] == first["first_seen"]


def test_a_new_session_in_the_same_pane_is_a_transition(tmp_path):
    _pending(tmp_path)
    assert _pending(tmp_path, session_id="sess-successor") is True
    assert rotation_state.pending_session_id(tmp_path, PANE) == "sess-successor"


# ── ledger first (ADR-005) ───────────────────────────────────────────────────


def test_the_ledger_append_runs_before_the_marker_is_created(tmp_path):
    path = rotation_state.pending_path(tmp_path, PANE)
    seen = []

    def record() -> bool:
        seen.append(path.exists())
        return True

    assert _pending(tmp_path, record_transition=record) is True
    assert seen == [False]
    assert path.exists()


def test_the_ledger_append_runs_before_a_band_change_touches_the_marker(tmp_path):
    _pending(tmp_path)
    seen = []

    def record() -> bool:
        seen.append(_marker(tmp_path)["level"])
        return True

    assert _pending(tmp_path, level="hard", record_transition=record) is True
    assert seen == ["force"], "the marker already said hard while the ledger was being written"
    assert _marker(tmp_path)["level"] == "hard"


def test_no_ledger_append_when_nothing_transitions(tmp_path):
    _pending(tmp_path)
    calls = []
    assert _pending(tmp_path, record_transition=lambda: calls.append(1) or True) is False
    assert calls == []


def test_a_ledger_that_refuses_leaves_the_marker_alone_and_the_next_call_retries(tmp_path):
    path = rotation_state.pending_path(tmp_path, PANE)

    assert _pending(tmp_path, record_transition=lambda: False) is False
    assert not path.exists()

    recorded = []
    assert _pending(tmp_path, record_transition=lambda: recorded.append(1) or True) is True
    assert recorded == [1] and path.exists()


def test_a_refused_band_change_keeps_the_old_marker(tmp_path):
    _pending(tmp_path)
    before = rotation_state.pending_path(tmp_path, PANE).read_bytes()

    assert _pending(tmp_path, level="hard", record_transition=lambda: False) is False

    assert rotation_state.pending_path(tmp_path, PANE).read_bytes() == before


# ── one critical section (OI-1486) ───────────────────────────────────────────


def test_parallel_crossings_record_exactly_one_transition(tmp_path):
    """Parallel tool calls each fire a PreToolUse hook process. Without the lock, every one of
    them reads "no marker", decides "new", and records the transition."""
    workers = 12
    barrier = threading.Barrier(workers)
    recorded: List[int] = []
    results: List[bool] = []
    errors: List[BaseException] = []

    def record() -> bool:
        recorded.append(1)
        return True

    def cross() -> None:
        try:
            barrier.wait(timeout=30)
            results.append(_pending(tmp_path, record_transition=record))
        except BaseException as exc:  # surfaced in the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=cross) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert results.count(True) == 1 and results.count(False) == workers - 1
    assert len(recorded) == 1
    assert _marker(tmp_path)["session_id"] == SESSION


# ── scratch names: per writer, and none left behind ──────────────────────────


def test_marker_and_latch_writes_leave_no_scratch_file_behind(tmp_path):
    _pending(tmp_path)
    _pending(tmp_path, level="hard")
    rotation_state.write_latch(tmp_path, session_id=SESSION, pane=PANE, old_window="@1",
                               new_window="@2")
    assert _scratch_files(tmp_path) == []


def test_latch_payload_and_freshness(tmp_path):
    written = rotation_state.write_latch(tmp_path, session_id=SESSION, pane=PANE,
                                         old_window="@1", new_window="@2")
    assert sorted(p.name for p in written) == sorted([
        rotation_state.latch_path_for_session(tmp_path, SESSION).name,
        rotation_state.latch_path_for_pane(tmp_path, PANE).name,
    ])
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["old_window"] == "@1" and payload["new_window"] == "@2"
    assert rotation_state.is_latched(tmp_path, session_id=SESSION, pane="")
    assert rotation_state.is_latched(tmp_path, session_id="", pane=PANE)


# ── the latch command: ledger first ──────────────────────────────────────────


def test_latch_command_appends_to_the_ledger_before_the_latch_exists(tmp_path, monkeypatch):
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    session_latch = rotation_state.latch_path_for_session(tmp_path, SESSION)
    pane_latch = rotation_state.latch_path_for_pane(tmp_path, PANE)
    events = []

    def fake_emit(trigger, *, file, **fields):
        events.append({"trigger": trigger, "file": file,
                       "latches_present": [session_latch.exists(), pane_latch.exists()]})
        return True

    monkeypatch.setattr(rotation_state, "emit_event", fake_emit)

    rc = rotation_state.main(["latch", "--pane", PANE, "--session-id", SESSION,
                              "--old-window", "@1", "--new-window", "@2"])

    assert rc == 0
    assert events == [{"trigger": "t0_context_rotation_started", "file": str(session_latch),
                       "latches_present": [False, False]}]
    assert session_latch.exists() and pane_latch.exists()


def test_latch_command_still_latches_when_the_ledger_refuses(tmp_path, monkeypatch, capsys):
    """The successor is already up when the latch is written: withholding the latch would only
    make the old session nag, and nothing can retry the transition. The failure is reported."""
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(rotation_state, "emit_event", lambda trigger, *, file, **f: False)

    rc = rotation_state.main(["latch", "--pane", PANE, "--session-id", SESSION,
                              "--old-window", "@1", "--new-window", "@2"])

    assert rc == 0
    assert rotation_state.is_latched(tmp_path, session_id=SESSION, pane=PANE)
    assert "ledger" in capsys.readouterr().err


def test_latch_command_without_any_session_id_latches_by_pane_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    events = []
    monkeypatch.setattr(rotation_state, "emit_event",
                        lambda trigger, *, file, **f: events.append(file) or True)

    assert rotation_state.main(["latch", "--pane", PANE, "--old-window", "@1",
                                "--new-window", "@2"]) == 0

    assert events == [str(rotation_state.latch_path_for_pane(tmp_path, PANE))]
    assert "latching by pane only" in capsys.readouterr().err
    assert not rotation_state.latch_path_for_session(tmp_path, SESSION).exists()


@pytest.mark.parametrize("missing", ["--old-window", "--new-window"])
def test_latch_command_rejects_a_missing_window(tmp_path, monkeypatch, missing):
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    argv = ["latch", "--pane", PANE, "--old-window", "@1", "--new-window", "@2"]
    idx = argv.index(missing)
    del argv[idx:idx + 2]
    with pytest.raises(SystemExit) as exc:
        rotation_state.main(argv)
    assert exc.value.code == 2
    assert not list(tmp_path.glob("latch-*"))
