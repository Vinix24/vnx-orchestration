"""The receipt's context_manifest reads the skill materialisation record (ADR-059 decisions 9, 10, 15; skilldisc R5).

Real writer, tmp_path stores; ``agent_skill_evidence`` loads only via ``ev`` so the red run fails on an assert.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import agent_layout
import agent_skill_materialize
import context_manifest
import governance_emit
from governance_emit import emit_dispatch_receipt

DISPATCH, SESSION, PROJECT = "d-1", "sess-1", "proj-a"
LEDGER = "context_manifests.ndjson"
SKILL = {"slug": "refactor-safe", "dest": ".claude/skills/refactor-safe", "sha256": "a" * 64, "bytes": 10,
         "ownership": "materialized"}
UNAVAILABLE = {"schema": 1, "status": "unavailable", "reason": "no_session_id"}
BAD_SKILLS_SHA = hashlib.sha256(json.dumps([SKILL, "x"], sort_keys=True).encode()).hexdigest()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setattr(agent_layout, "layout_mode", lambda: "on")
    monkeypatch.setattr(context_manifest, "capture", lambda sid, **kw: dict(UNAVAILABLE))
    (tmp_path / "state").mkdir()
    return tmp_path / "state"


@pytest.fixture
def ev():
    import agent_skill_evidence
    return agent_skill_evidence


def _record(state, status="ok", reason=None, skills=(SKILL,), provider="claude", dispatch_id=DISPATCH,
            fields=None, drop=None, content=None):
    """A record by the real writer in the folder of *dispatch_id*/*provider*, then edited in place."""
    ids = {"role": "backend-developer", "provider": provider, "dispatch_id": dispatch_id, "project_id": PROJECT,
           "worktree": "/wt"}
    result = {"status": status, "reason": reason, "skills": list(skills)}
    path = Path(agent_skill_materialize._write_record(state, ids, result))
    body = {**json.loads(path.read_text()), **(fields or {})}
    body.pop(drop, None)
    path.write_bytes(json.dumps(body).encode() if content is None else content)
    return path


def _emit(state):
    emit_dispatch_receipt(
        dispatch_id=DISPATCH, terminal_id="T1", provider="claude", model="sonnet", pr_id=None, status="success",
        completion_pct=100, risk=0.0, findings=[], duration_seconds=1.0, cost_usd=None, state_dir=state,
        token_usage={"input": 1, "output": 1, "cache_hit": 0}, receipt_kind="dispatch", session_id=SESSION)
    [line] = (state / "t0_receipts.ndjson").read_text().splitlines()
    return json.loads(line)


def _stamp(state):
    return _emit(state)["context_manifest"]


def _ledger(state):
    path = state / LEDGER
    return path.read_text().splitlines() if path.exists() else []


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _refused(reason):
    return {"schema": 1, "status": "unavailable", "reason": reason}


def _read(ev, state, dispatch_id=DISPATCH, provider="claude", **over):
    return ev.read_materialization(state, dispatch_id, provider, **{"project_id": PROJECT, "session_id": SESSION,
                                                                    **over})


@pytest.mark.parametrize("mode,status,reason", [("on", "ok", None), ("shadow", "ok", "already_native"),
                                                ("on", "partial", "not_owned:x")])
def test_a_valid_materialized_record_is_evidence_when_the_session_is_unavailable(state, monkeypatch, mode, status,
                                                                                 reason):
    monkeypatch.setattr(agent_layout, "layout_mode", lambda: mode)
    raw = _record(state, status=status, reason=reason).read_bytes()
    manifest = {"schema": 1, "status": status, "source": "materialized", "provider": "claude", "attempt": 1,
                "role": "backend-developer", "record_sha256": hashlib.sha256(raw).hexdigest(),
                "manifest_sha256": json.loads(raw)["manifest_sha256"], "skills": [SKILL],
                **({"reason": reason} if reason else {})}
    line = governance_emit._manifest_line(DISPATCH, SESSION, PROJECT, manifest)
    receipt = _emit(state)
    assert receipt.get("context_manifest") == {"schema": 1, "status": status, "sha256": _sha(line)}
    assert _ledger(state) == [line]


@pytest.mark.parametrize("status", ["ok", "partial"])
def test_a_usable_session_capture_wins_and_its_output_is_todays(state, monkeypatch, status):
    session = {"schema": 1, "status": status, "skills": {"initial": []}}
    monkeypatch.setattr(context_manifest, "capture", lambda sid, **kw: dict(session))
    _record(state)
    line = governance_emit._manifest_line(DISPATCH, SESSION, PROJECT, session)
    assert _stamp(state) == {"schema": 1, "status": status, "sha256": _sha(line)}
    assert (state / LEDGER).read_text() == line + "\n"


def test_no_record_keeps_the_session_stamp_and_writes_no_line(state, ev):
    assert _stamp(state) == UNAVAILABLE and _ledger(state) == []
    assert _read(ev, state)["reason"] == "no_record"


@pytest.mark.parametrize("spec,reason", [
    ({"content": b""}, "record_malformed"),
    ({"content": b"null"}, "record_malformed"),
    ({"content": b"{not json"}, "record_malformed"),
    ({"content": b'{"schema": 1, "status": "ok", "dispatch_id"'}, "record_malformed"),
    ({"status": "done"}, "unknown_status"),
    ({"status": None}, "unknown_status"),
    ({"status": ["ok"]}, "unknown_status"),
    ({"fields": {"dispatch_id": "d-2"}}, "identity_mismatch"),
    ({"fields": {"provider": "codex"}}, "identity_mismatch"),
    ({"fields": {"project_id": None}}, "project_mismatch"),
    ({"fields": {"project_id": "proj-b"}}, "project_mismatch"),
    ({"fields": {"session_id": "sess-other"}}, "session_mismatch"),
    ({"status": "skipped", "reason": "no_declared_set"}, "no_declared_set"),
    ({"status": "error", "reason": "copy_failed:OSError"},
     {"schema": 1, "status": "error", "reason": "copy_failed:OSError"}),
], ids=["empty", "none", "unparseable", "half_written", "unknown", "status_none", "status_list", "dispatch",
        "provider", "project_missing", "project_other", "session", "skipped", "error"])
def test_a_record_that_is_not_ok_evidence_stamps_its_reason_without_a_hash(state, spec, reason):
    _record(state, **spec)
    assert _stamp(state) == (reason if isinstance(reason, dict) else _refused(reason))
    assert _ledger(state) == []


def test_a_recorded_session_must_equal_the_receipts_and_a_missing_project_never_matches(state, monkeypatch, ev):
    _record(state, fields={"session_id": SESSION})
    assert _read(ev, state)["status"] == "ok"
    assert {_read(ev, state, session_id=s)["reason"] for s in (None, "", "sess-other")} == {"session_mismatch"}
    _record(state, fields={"project_id": None})
    assert _read(ev, state, project_id=None)["reason"] == "project_mismatch"
    monkeypatch.delenv("VNX_PROJECT_ID")
    assert _stamp(state) == _refused("project_mismatch")


@pytest.mark.parametrize("specs,expected", [
    ([{"status": "skipped", "reason": "no_declared_set"}, {"fields": {"project_id": "proj-b"}}],
     ("unavailable", "no_declared_set", 1)),
    ([{}, {"status": "error", "reason": "copy_failed:OSError"}], ("error", "copy_failed:OSError", 2)),
    ([{"fields": {"dispatch_id": "d-9"}}, {}], ("ok", None, 2)),
    ([{}, {"content": b""}], ("unavailable", "record_malformed", 2)),
    ([{"fields": {"project_id": "proj-b"}}, {"fields": {"dispatch_id": "d-9"}}],
     ("unavailable", "identity_mismatch", 2)),
], ids=["failing_newer_ok_never_counts", "newer_passing_beats_older_ok", "failing_older_hides_nothing",
        "malformed_newest_stops", "all_fail_names_the_highest"])
def test_the_highest_attempt_that_passes_the_identity_test_decides(state, ev, specs, expected):
    for spec in specs:
        _record(state, **spec)
    assert tuple(_read(ev, state)[k] for k in ("status", "reason", "attempt")) == expected


def test_a_record_of_another_dispatch_or_provider_is_never_read(state, ev):
    _record(state, dispatch_id="d-2", fields={"dispatch_id": DISPATCH})  # a sibling folder claiming this dispatch
    _record(state, provider="codex", fields={"provider": "claude"})  # another provider claiming this one
    assert _stamp(state) == UNAVAILABLE and _ledger(state) == []
    for dispatch_id, provider in (("../d-2", "claude"), ("..", "claude"), (None, "claude"),
                                  (DISPATCH, "litellm:openrouter:openai/gpt-4o"), (DISPATCH, "")):
        assert _read(ev, state, dispatch_id, provider)["reason"] == "no_record"


@pytest.mark.parametrize("spec", [
    {"fields": {"schema": 2}}, {"fields": {"schema": True}}, {"fields": {"attempt": 2}},
    {"fields": {"manifest_sha256": "0" * 64}}, {"fields": {"reason": 5}},
    {"fields": {"skills": [SKILL, "x"], "manifest_sha256": BAD_SKILLS_SHA}}, {"drop": "session_id"},
    {"skills": ()}, {"status": "skipped"}, {"status": "error"},
], ids=["schema", "bool_schema", "attempt", "hash", "reason_type", "skill_type", "no_session_key", "ok_no_skills",
        "skipped_no_reason", "error_no_reason"])
def test_a_record_not_of_the_writers_form_is_malformed(state, ev, spec):
    _record(state, **spec)
    assert _read(ev, state) == {"status": "unavailable", "reason": "record_malformed", "attempt": 1}


def test_a_link_or_a_file_in_place_of_the_record_is_malformed(state, ev):
    target = _record(state, dispatch_id="d-2", fields={"dispatch_id": DISPATCH})  # following it would read an ok
    folder = state / agent_skill_materialize.RECORD_DIR / DISPATCH
    folder.mkdir()
    (folder / "claude.attempt-1.json").symlink_to(target)
    assert _read(ev, state)["reason"] == "record_malformed"
    (folder.parent / "d-3").write_text("{}")
    assert _read(ev, state, "d-3")["reason"] == "record_malformed"


def test_a_failed_ledger_write_gives_an_error_and_no_hash(state):
    _record(state)
    (state / LEDGER).mkdir()
    assert _stamp(state) == {"schema": 1, "status": "error", "reason": "ledger_write_failed:IsADirectoryError"}


def test_off_stamps_nothing_and_reads_no_record(state, monkeypatch, ev):
    monkeypatch.setattr(agent_layout, "layout_mode", lambda: "off")
    monkeypatch.setattr(ev, "read_materialization", lambda *a, **k: 1 / 0)
    _record(state)
    assert "context_manifest" not in _emit(state) and not (state / LEDGER).exists()


def test_a_reader_that_raises_never_fails_the_receipt(state, monkeypatch, ev):
    monkeypatch.setattr(ev, "read_materialization", lambda *a, **k: 1 / 0)
    assert _stamp(state) == {"schema": 1, "status": "error", "reason": "extract_failed:ZeroDivisionError"}


def test_the_status_map_covers_the_writers_statuses_and_reads_its_public_skipped_record(state, ev):
    assert frozenset(ev.STATUS_MAP) == agent_skill_materialize.STATUSES
    agent_skill_materialize.record_skipped("not_isolated", role="backend-developer", provider="claude",
                                           worktree=None, dispatch_id=DISPATCH, state_dir=state, project_id=PROJECT)
    assert _read(ev, state) == {"status": "unavailable", "reason": "not_isolated", "attempt": 1}
