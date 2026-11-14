"""Characterisation tests for golf-schoon R12 (dlv-41450243c633).

Pin the observable behaviour of ``planning_cli.cmd_objective_close`` before the
extract-method refactor: exit codes, the stdout lines an operator reads, the
JSON payload, and the phase it writes. Real temp dirs and SQLite files; only the
gh-backed evidence gather and, for the race branches the library can return,
``close_track_if_done`` are patched.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
for p in (_TESTS, _TESTS.parent / "scripts" / "lib", _TESTS.parent / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import objective_reconcile  # noqa: E402
import planning_cli  # noqa: E402
import track_reconciler  # noqa: E402
import tracks as tracks_lib  # noqa: E402
from test_objective_close import PROJECT_ID, _build_db, _phase, _seed_done_track  # noqa: E402


def _close_args(sd, track_id, *, apply=False, approval_id="", as_json=False):
    return argparse.Namespace(
        state_dir=str(sd), project_id=PROJECT_ID, track_id=track_id, apply=apply,
        approval_id=approval_id, json=as_json, include_parked=False,
    )


def test_close_dry_run_text_is_pinned(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-a", phase="active")
    assert planning_cli.cmd_objective_close(_close_args(sd, "T-a")) == 0
    assert capsys.readouterr().out == (
        f"\nvnx objective close — T-a (project '{PROJECT_ID}')\n\n"
        "  declared=active  derived=done\n"
        "  evidence: completed=1 failed_terminal=0 in_flight=0 pr_ref=- pr_merged=False\n"
        "  [dry-run] would transition active -> done. "
        "Re-run with --apply --approval-id <id> to write.\n\n"
    )
    assert _phase(sd, "T-a") == "active"


def test_close_json_closed_payload_is_pinned(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-q", phase="queued")
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T-q", apply=True, approval_id="APR-9", as_json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload) == ["track_id", "project_id", "declared_phase", "derived_status",
                             "action", "applied", "evidence", "path"]
    assert payload["action"] == "closed" and payload["applied"] is True
    assert payload["path"] == ["queued", "active", "done"]
    assert payload["declared_phase"] == "done"
    assert _phase(sd, "T-q") == "done"


def test_close_gh_unavailable_note_and_payload(tmp_path, capsys, monkeypatch):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-gh", PROJECT_ID, title="x", goal_state="y", phase="queued")
    monkeypatch.setattr(objective_reconcile, "gather_close_evidence", lambda *a, **k: {
        "merged_pr_numbers": set(), "consulted_gh": False, "gh_health": "absent",
        "pr_numbers": [77],
    })
    assert planning_cli.cmd_objective_close(_close_args(sd, "T-gh")) == 0
    out = capsys.readouterr().out
    assert out.endswith(
        "  nothing to close: derived_status=queued is not terminal ('done').\n\n"
        "  ! NOTE: could not consult GitHub (gh=absent); derived_status used local "
        "evidence only. If the track's PRs merged via a bare `gh pr merge`, re-run "
        "when gh is reachable.\n"
    )
    assert planning_cli.cmd_objective_close(_close_args(sd, "T-gh", as_json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["gh_health"] == "absent" and payload["action"] == "noop_not_terminal"


def test_close_blocked_prints_blocking_hint(tmp_path, capsys, monkeypatch):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-b", PROJECT_ID, title="x", goal_state="y", phase="active")
    monkeypatch.setattr(track_reconciler, "peek_derived_status", lambda *a, **k: {
        "derived_status": "blocked", "declared_phase": "active", "blocking_detail": {"k": 1},
    })
    monkeypatch.setattr(track_reconciler, "format_blocking_hint",
                        lambda d: f"HINT {d['k']}", raising=False)
    assert planning_cli.cmd_objective_close(_close_args(sd, "T-b")) == 0
    assert capsys.readouterr().out.endswith("is not terminal ('done').\n\nHINT 1\n")


@pytest.mark.parametrize("lib, rc, last_line", [
    ({"action": "stale_candidate"}, 2,
     "close aborted: stale candidate (state changed after nomination). No change made."),
    ({"action": "rejected_not_found", "error": "gone"}, 2, "ERROR: gone"),
    ({"action": "rejected_walk_failed", "declared_phase": "active", "error": "boom"}, 2,
     "ERROR: transition failed at 'active': boom. Track left at 'active' — "
     "re-run `objective close` to resume."),
    ({"action": "noop_already_closed", "declared_phase": "done"}, 0,
     "already closed (phase=done)."),
    ({"action": "noop_not_terminal", "derived_status": "active"}, 0,
     "nothing to close: derived_status=active is not terminal ('done')."),
    ({"action": "rejected_parked"}, 2,
     "track is PARKED (a deliberate stop). Pass --include-parked to close it anyway, "
     "or un-park it first. No change made."),
    ({"action": "rejected_no_path", "declared_phase": "x"}, 2,
     "ERROR: no legal transition path x -> done (ALLOWED_TRANSITIONS). No change made."),
    ({}, 2, "unexpected result from library: None"),
])
def test_close_library_result_mapping(tmp_path, capsys, monkeypatch, lib, rc, last_line):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-r", phase="active")
    monkeypatch.setattr(track_reconciler, "close_track_if_done", lambda *a, **k: dict(lib))
    assert planning_cli.cmd_objective_close(
        _close_args(sd, "T-r", apply=True, approval_id="A")) == rc
    assert capsys.readouterr().out.rstrip("\n").splitlines()[-1] == f"  {last_line}"
    assert planning_cli.cmd_objective_close(
        _close_args(sd, "T-r", apply=True, approval_id="A", as_json=True)) == rc
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == (lib.get("action") or "unknown")
    assert payload["applied"] is False

