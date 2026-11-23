"""operator_queue_steering_verdict: the readers for sources 5, 6 and 7 (ADR-076 decisions 2, 3, 5, 7) on tmp_path stores."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import operator_queue as oq
import operator_queue_steering_verdict as rd
import review_cap_verdicts as rcv
import steering_proposal_store as sps

P = "proj-q2b"
HEAD, MOVED = "a" * 40, "b" * 40
DDL = """CREATE TABLE review_cap_verdicts (
    project_id TEXT NOT NULL, pr_number INTEGER NOT NULL, verdict_id TEXT NOT NULL, head_sha TEXT NOT NULL,
    cap_rule TEXT NOT NULL, outcome TEXT NOT NULL, chosen_outcome TEXT NOT NULL, intervention TEXT NOT NULL DEFAULT '',
    oi_ids TEXT NOT NULL DEFAULT '[]', granted_dispatch_id TEXT NOT NULL DEFAULT '', tiebreaker_model TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY (project_id, pr_number, verdict_id))"""


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
    klasse = {"handelingsklasse": "K1"} if kind == "permissie" else {}  # ADR-084: permissie carries its class
    rec = sps.add_revision(state, P, {"oorzaak": "o", "artefact_soort": kind, "doelpad": "d.md", "voorstel": text,
                                      **klasse}, now=now, **kw)
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


def _verdict(state, vid, pr=7, head=HEAD, created="2026-11-15T07:00:00Z", outcome="mergen-met-OI"):
    conn = sqlite3.connect(str(state / "runtime_coordination.db"))
    try:
        conn.execute(DDL) if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='review_cap_verdicts'").fetchone() else None
        conn.execute("INSERT INTO review_cap_verdicts (project_id, pr_number, verdict_id, head_sha, cap_rule, outcome, "
                     "chosen_outcome, created_at) VALUES (?,?,?,?,'B3.2',?,?,?)", (P, pr, vid, head, outcome, outcome, created))
        conn.commit()
    finally:
        conn.close()
    return rcv.CapVerdict(P, pr, vid, head, "B3.2", outcome, outcome, created_at=created)


def _heads(head=HEAD, state="open"):
    return {"head_of": lambda pr: head, "pr_state_of": lambda pr: state}


def test_verdict_without_approval_opens_and_an_approval_closes_it(env):
    _, state, queue = env
    v = _verdict(state, "cap-pr7-a")
    res = _read(rd.read_cap_verdict, env, **_heads())
    assert res["bron"] == 7 and res["dekking"] == "gelezen" and len(res["opened"]) == 1
    assert _points(queue)[res["opened"][0]]["ref"] == "verdict:cap-pr7-a"
    assert _read(rd.read_cap_verdict, env, **_heads())["opened"] == [] and len(_lines(queue)) == 1
    rcv.record_merge_approval(state, v, "vincent")
    res = _read(rd.read_cap_verdict, env, **_heads())
    closed = _lines(queue, "closed")
    assert res["closed"] == [closed[0]["point_id"]] and closed[0]["uitkomst"] == "beantwoord"
    assert closed[0]["beantwoord_door"] == "onbekend" and "cap-pr7-a" in closed[0]["bewijs"]


def test_head_moved_keeps_the_point_open_even_with_an_approval_on_the_old_head(env):
    _, state, queue = env
    v = _verdict(state, "cap-pr7-a")
    rcv.record_merge_approval(state, v, "vincent")
    res = _read(rd.read_cap_verdict, env, **_heads(head=MOVED))
    assert len(res["opened"]) == 1 and res["unknown"] == res["opened"] and res["closed"] == []
    assert [u["reden"] for u in _lines(queue, "unknown")] == ["kop_verschoven"]
    assert _points(queue)[res["opened"][0]]["state"] == "open"
    _read(rd.read_cap_verdict, env, **_heads(head=MOVED))
    assert len(_lines(queue, "unknown")) == 1 and _lines(queue, "closed") == []


def test_a_newer_verdict_on_the_same_pr_makes_the_point_vervallen(env):
    _, state, queue = env
    _verdict(state, "cap-pr7-a")
    _read(rd.read_cap_verdict, env, **_heads())
    _verdict(state, "cap-pr7-b", head=MOVED, created="2026-11-15T09:00:00Z")
    res = _read(rd.read_cap_verdict, env, **_heads(head=MOVED))
    closed = _lines(queue, "closed")
    assert len(closed) == 1 and closed[0]["uitkomst"] == "vervallen" and "cap-pr7-b" in closed[0]["bewijs"]
    assert len(res["closed"]) == 1 and len(_lines(queue, "opened")) == 2  # the newer verdict is its own open point


@pytest.mark.parametrize("pr_state", ["merged", "closed"])
def test_a_merged_or_closed_pr_makes_the_point_vervallen_never_beantwoord(env, pr_state):
    _, state, queue = env
    v = _verdict(state, "cap-pr7-a")
    _read(rd.read_cap_verdict, env, **_heads())
    rcv.record_merge_approval(state, v, "vincent")
    _verdict(state, "cap-pr8-z", pr=8)
    read = _read(rd.read_cap_verdict, env, head_of=lambda pr: HEAD, pr_state_of=lambda pr: pr_state if pr == 7 else "open")
    closed = _lines(queue, "closed")
    assert [c["uitkomst"] for c in closed] == ["vervallen"] and len(read["closed"]) == 1


def test_a_merged_pr_without_a_point_opens_nothing(env):
    _, state, queue = env
    _verdict(state, "cap-pr7-a")
    res = _read(rd.read_cap_verdict, env, **_heads(state="merged"))
    assert res["opened"] == [] and _lines(queue) == []


@pytest.mark.parametrize("head,state,reden", [(None, "open", "kop_onleesbaar"), (HEAD, None, "pr_staat_onleesbaar"),
                                              (HEAD, "weird", "pr_staat_onbekend")])
def test_an_unreadable_head_or_state_closes_nothing(env, head, state, reden):
    _, state_dir, queue = env
    v = _verdict(state_dir, "cap-pr7-a")
    rcv.record_merge_approval(state_dir, v, "vincent")
    res = _read(rd.read_cap_verdict, env, head_of=lambda pr: head, pr_state_of=lambda pr: state)
    assert res["closed"] == [] and len(res["unknown"]) == 1 and _lines(queue, "closed") == []
    assert _lines(queue, "unknown")[0]["reden"].startswith(reden)


def test_no_runtime_store_is_unknown_not_zero(env):
    res = _read(rd.read_cap_verdict, env, **_heads())
    assert res["dekking"] == "unknown" and res["reden"] and res["opened"] == []


def test_only_mergen_met_oi_opens_a_point(env):
    _, state, queue = env
    _verdict(state, "cap-pr7-a", outcome="verkleinen")
    assert _read(rd.read_cap_verdict, env, **_heads())["opened"] == []


def test_default_pr_state_reads_the_merge_receipt_and_never_the_network(env):
    root, state, queue = env
    assert rd.default_pr_state_of(state)(7) is None  # no receipt ledger: unreadable, not open
    ledger = state / "t0_receipts.ndjson"
    ledger.write_text(json.dumps({"event_type": "pr_merged", "pr_number": 8}) + "\n", encoding="utf-8")
    assert rd.default_pr_state_of(state)(7) == "open"
    ledger.write_text(ledger.read_text() + json.dumps({"event_type": "pr_merged", "pr_number": 7}) + "\n", encoding="utf-8")
    assert rd.default_pr_state_of(state)(7) == "merged"


def test_default_head_reads_the_local_remote_tracking_ref(env, tmp_path):
    root, state, queue = env
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("-c", "user.email=t@e.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x")
    sha = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/feat-x", sha)
    rounds = state / "review_gates"
    rounds.mkdir()
    (rounds / "rounds.ndjson").write_text(json.dumps({"pr_number": 7, "branch": "feat-x"}) + "\n", encoding="utf-8")
    head_of = rd.default_head_of(state, repo)
    assert head_of(7) == sha and head_of(9) is None
    (rounds / "rounds.ndjson").write_text(json.dumps({"pr_number": 7, "branch": "nope"}) + "\n", encoding="utf-8")
    assert head_of(7) is None


def test_readers_registry():
    assert [name for name, _ in rd.READERS] == ["steering", "cap_verdict"]


def test_read_steering_leaves_a_stale_or_absent_projection_and_the_lock_untouched(env):
    _, state, queue = env
    _bind(state)
    _propose(state)
    projection, lock = state / sps.PROJECTION_FILENAME, state / sps.LOCK_FILENAME
    projection.write_text("{}", encoding="utf-8")  # stale: differs from the replay
    lock.unlink()
    _read(rd.read_steering, env)
    assert projection.read_bytes() == b"{}" and not lock.exists()
    projection.unlink()
    assert len(_read(rd.read_steering, env)[0]["opened"]) == 0 and not projection.exists() and not lock.exists()


def test_without_a_registered_repo_the_default_head_is_none_and_the_point_stays_open(env, monkeypatch):
    _, state, queue = env
    v = _verdict(state, "cap-pr7-a")
    rcv.record_merge_approval(state, v, "vincent")
    monkeypatch.setattr(rd, "project_checkout_path", lambda project_id: None)
    res = _read(rd.read_cap_verdict, env, pr_state_of=lambda pr: "open")
    assert res["closed"] == [] and len(res["unknown"]) == 1
    assert _lines(queue, "unknown")[0]["reden"] == "kop_onleesbaar"


@pytest.mark.parametrize("bad", ["{torn", "[1, 2]", "42"])
def test_a_malformed_receipt_line_makes_the_pr_state_unknown_never_open(env, bad):
    _, state, queue = env
    v = _verdict(state, "cap-pr7-a")
    rcv.record_merge_approval(state, v, "vincent")
    (state / "t0_receipts.ndjson").write_text(json.dumps({"event_type": "x"}) + "\n" + bad + "\n", encoding="utf-8")
    assert rd.default_pr_state_of(state)(7) is None
    res = _read(rd.read_cap_verdict, env, head_of=lambda pr: HEAD)
    assert res["closed"] == [] and _lines(queue, "closed") == [] and len(res["unknown"]) == 1
    assert _lines(queue, "unknown")[0]["reden"] == "pr_staat_onleesbaar"
    assert _points(queue)[res["unknown"][0]]["state"] == "open"


def test_a_torn_newest_rounds_line_is_unreadable_never_matched_through_an_older_branch(env, tmp_path):
    _, state, queue = env
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("-c", "user.email=t@e.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x")
    git("update-ref", "refs/remotes/origin/old-branch", git("rev-parse", "HEAD"))
    (state / "review_gates").mkdir()
    rounds = state / "review_gates" / "rounds.ndjson"
    rounds.write_text(json.dumps({"pr_number": 7, "branch": "old-branch"}) + "\n{torn", encoding="utf-8")
    v = _verdict(state, "cap-pr7-a", head=git("rev-parse", "HEAD"))
    rcv.record_merge_approval(state, v, "vincent")
    assert rd.default_head_of(state, repo)(7) is None
    res = _read(rd.read_cap_verdict, env, repo=repo, pr_state_of=lambda pr: "open")
    assert res["closed"] == [] and _lines(queue, "closed") == []
    assert _lines(queue, "unknown")[0]["reden"] == "kop_onleesbaar"


def test_an_empty_ledger_is_an_empty_history_and_a_clean_one_still_closes(env):
    _, state, queue = env
    (state / "t0_receipts.ndjson").write_text("", encoding="utf-8")
    assert rd.default_pr_state_of(state)(7) == "open"
    v = _verdict(state, "cap-pr7-a")
    rcv.record_merge_approval(state, v, "vincent")
    res = _read(rd.read_cap_verdict, env, head_of=lambda pr: HEAD)
    assert len(res["closed"]) == 1 and _lines(queue, "closed")[0]["uitkomst"] == "beantwoord"


def test_a_suppressed_proposal_closes_and_a_notification_asks_to_see_never_to_approve(env):
    _, state, queue = env
    _bind(state)
    _propose(state, kind="permissie")
    opened = _read(rd.read_steering, env)[1]["opened"]
    sps.reconcile(state, P, [{"oorzaak": "o", "artefact_soort": sps.MELDING_KIND, "doelpad": "d.md",
                              "voorstel": "harde stop"}], now="2026-11-15T09:00:00Z")
    six = _read(rd.read_steering, env)[1]
    assert six["closed"] == opened and _points(queue)[opened[0]]["state"] == "vervallen"
    vraag = _points(queue)[six["opened"][0]]["vraag"]
    assert "gezien" in vraag and "goedkeuren" not in vraag
