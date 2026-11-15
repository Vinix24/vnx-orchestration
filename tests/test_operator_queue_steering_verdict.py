"""operator_queue_steering_verdict: the readers for sources 5 and 6 (ADR-076 decisions 2, 3, 5, 7) on tmp_path stores."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import operator_queue as oq
import operator_queue_steering_verdict as rd
import steering_proposal_store as sps

P = "proj-q2b"


@pytest.fixture
def env(tmp_path):
    root, queue = tmp_path / "root", tmp_path / "queue"
    (root / "state").mkdir(parents=True)
    return root, root / "state", queue


def _read(fn, env, **kw):
    root, _, queue = env
    return fn(P, data_root=root, state_dir=queue, now="2026-11-15T10:00:00Z", **kw)


def _bind(state):
    (state / "steering_sources.json").write_text(json.dumps(
        {"schema": "steering_sources/1", "project_id": P, "transcript_dirs": ["slug"], "memory_dir": "/m",
         "bound_at": "2026-11-14T00:00:00Z"}), encoding="utf-8")


def _propose(state, kind="regel", text="v1", now="2026-11-15T08:00:00Z", **kw):
    rec = sps.add_revision(state, P, {"oorzaak": "o", "artefact_soort": kind, "doelpad": "d.md", "voorstel": text},
                           now=now, **kw)
    return rec["proposal_id"]


def _lines(queue, kind=None):
    path = queue / oq.LEDGER_FILENAME
    rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if kind in (None, r["kind"])]


def _points(queue):
    return oq.read_state(P, state_dir=queue)["points"]


def test_no_binding_is_niet_gebonden_never_zero_read(env):
    _, state, queue = env
    _propose(state)
    five, six = _read(rd.read_steering, env)
    for res, bron in ((five, 5), (six, 6)):
        assert (res["bron"], res["dekking"]) == (bron, "niet_gebonden") and res["reden"]
        assert res["dekking"] != "gelezen" and res["opened"] == res["closed"] == []
    assert _lines(queue) == []


def test_pending_proposal_opens_once_and_a_rejection_closes_it(env):
    _, state, queue = env
    _bind(state)
    pid = _propose(state)
    first = _read(rd.read_steering, env)
    assert [r["dekking"] for r in first] == ["gelezen", "gelezen"] and len(first[0]["opened"]) == 1
    assert _read(rd.read_steering, env)[0]["opened"] == [] and len(_lines(queue, "opened")) == 1
    point = _points(queue)[first[0]["opened"][0]]
    assert (point["ref"], point["revisie"], point["bron"]) == (f"steering:{pid}", 1, 5)
    sps.decide(state, P, pid, "rejected", reason="nee", decided_by="vincent", now="2026-11-15T09:00:00Z")
    res = _read(rd.read_steering, env)[0]
    closed = _lines(queue, "closed")
    assert res["closed"] == first[0]["opened"] and len(closed) == 1
    assert (closed[0]["uitkomst"], closed[0]["beantwoord_door"]) == ("beantwoord", "vincent")
    assert "rejected" in closed[0]["bewijs"] and "2026-11-15T09:00:00Z" in closed[0]["bewijs"]
    _read(rd.read_steering, env)
    assert len(_lines(queue)) == 2


def test_reopened_proposal_is_a_new_point_and_the_old_one_stays_closed(env):
    _, state, queue = env
    _bind(state)
    pid = _propose(state)
    _read(rd.read_steering, env)
    sps.decide(state, P, pid, "rejected", reason="nee", decided_by="vincent", now="2026-11-15T09:00:00Z")
    _read(rd.read_steering, env)
    _propose(state, text="v2", reopen=True, now="2026-11-15T11:00:00Z")
    res = _read(rd.read_steering, env)[0]
    points = _points(queue)
    assert len(res["opened"]) == 1 and len(points) == 2
    assert sorted((p["revisie"], p["state"]) for p in points.values()) == [(1, "beantwoord"), (2, "open")]


def test_a_new_revision_of_a_pending_proposal_supersedes_the_old_point(env):
    _, state, queue = env
    _bind(state)
    _propose(state)
    old = _read(rd.read_steering, env)[0]["opened"][0]
    _propose(state, text="v2", now="2026-11-15T09:00:00Z")
    res = _read(rd.read_steering, env)[0]
    assert res["closed"] == [old] and len(res["opened"]) == 1
    assert _points(queue)[old]["state"] == "vervallen"


def test_kind_decides_the_source(env):
    _, state, queue = env
    _bind(state)
    _propose(state, kind="permissie")
    _propose(state, kind="skill_regel")
    five, six = _read(rd.read_steering, env)
    assert (len(five["opened"]), len(six["opened"])) == (1, 1)
    assert _points(queue)[six["opened"][0]]["bron"] == 6 and _points(queue)[five["opened"][0]]["bron"] == 5
    for kind in ("bouwer_allowlist", "bouwgewoonte", "alleen_melding"):
        assert kind in rd.PERMISSION_KINDS


def test_an_unreadable_store_is_unknown_not_zero(env):
    _, state, queue = env
    _bind(state)
    (state / sps.DECISIONS_FILENAME).write_text("{not json\n", encoding="utf-8")
    for res in _read(rd.read_steering, env):
        assert res["dekking"] == "unknown" and res["reden"] and res["opened"] == []
    assert _lines(queue) == []


def test_readers_registry():
    assert [name for name, _ in rd.READERS] == ["steering"]
