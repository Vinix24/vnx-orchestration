"""ADR-076 Q3: the ref grammar, ``ask`` / ``answer``, the closing rules of ``read_asked`` and the promote refusal."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts", _ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import inbox_cli  # noqa: E402
import operator_queue as oq  # noqa: E402
import operator_queue_ask as qa  # noqa: E402
import schema_migration  # noqa: E402
import operator_queue_steering_verdict as steering  # noqa: E402
import review_cap_verdicts as rcv  # noqa: E402
import tracks  # noqa: E402
import config_store_db  # noqa: E402
import actor_class  # noqa: E402
from fixtures.dispatches_schema_fixture import dispatches_ddl  # noqa: E402

PID = "proj"


@pytest.fixture()
def env(tmp_path):
    sdir = tmp_path / "root" / "state"
    sdir.mkdir(parents=True)
    conn = sqlite3.connect(str(sdir / "runtime_coordination.db"))
    conn.execute(dispatches_ddl())
    conn.execute("CREATE TABLE coordination_events (event_id TEXT, event_type TEXT, entity_id TEXT, "
                 "metadata_json TEXT, occurred_at TEXT, project_id TEXT)")
    for version, name in [(22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql"),
                          (27, "0027_planning_horizon_and_deliverable_view.sql"), (28, "0028_tracks_derived_status.sql")]:
        schema_migration.apply_script_if_below(conn, version, (_ROOT / "schemas" / "migrations" / name).read_text())
    conn.commit()
    conn.close()
    return {"root": tmp_path / "root", "sdir": sdir, "q": tmp_path / "q"}


def _sql(env, sql, *params):
    conn = sqlite3.connect(str(env["sdir"] / "runtime_coordination.db"))
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def _dispatch(env, did, state="proposed", track=None):
    _sql(env, "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)", did, PID, state, track)


def _ask(env, bron="besluit", ref="dlv:d1", vraag="Promote d1?", **kw):
    return qa.ask(PID, bron=bron, ref=ref, vraag=vraag, sluit_bij="promote or not", opened_by="T0",
                  state_dir=env["q"], **kw)


def _read(env):
    return {r["bron"]: r for r in qa.read_asked(PID, data_root=env["root"], state_dir=env["q"])}


def _points(env):
    return oq.read_state(PID, state_dir=env["q"])["points"]


def _ledger(env):
    path = env["q"] / oq.LEDGER_FILENAME
    return path.read_text() if path.exists() else ""


def _track(env, *phases):
    tracks.create_track(env["sdir"], "t1", PID, "T", "goal")
    for to in phases:
        tracks.transition_phase(env["sdir"], "t1", PID, to, actor="T0")


@pytest.mark.parametrize("ref,kind", [("dlv:d1", "dlv"), ("oi:OI-12", "oi"), ("track:t1:active", "track"),
                                       ("config:review.scope", "config"), ("verdict:v-1", "verdict"),
                                       ("antwoord:0123456789ab", "antwoord")])
def test_each_form_parses(ref, kind):
    assert qa.parse_ref(ref)[0] == kind


@pytest.mark.parametrize("ref", ["dlv", "oi:", "track:x", "pr:0", "free text"])
def test_malformed_refs_are_refused_and_the_cli_exits_2_writing_nothing(env, capsys, ref):
    with pytest.raises(qa.RefRefused, match=r"dlv:<id>.*antwoord:<hash>"):
        qa.parse_ref(ref)
    code = inbox_cli.main(["--project-id", PID, "--state-dir", str(env["q"]), "ask", "--bron", "besluit",
                           "--ref", ref, "--vraag", "q?", "--sluit-bij", "x"])
    assert code == 2 and "dlv:<id>" in capsys.readouterr().err and _ledger(env) == ""


def test_cli_ask_answer_and_a_secret_is_refused_by_name_only(env, capsys):
    base = ["--project-id", PID, "--state-dir", str(env["q"])]
    ask = ["ask", "--bron", "vraag", "--ref", "antwoord", "--sluit-bij", "yes", "--vraag"]
    assert inbox_cli.main(base + ask + ["Go?"]) == 0
    assert inbox_cli.main(base + ["answer", next(iter(_points(env))), "--quote", "ja"]) == 0
    secret = "sk-" + "a" * 40
    assert inbox_cli.main(base + ask + [secret]) == 2
    err = capsys.readouterr().err
    assert "sk_key" in err and secret not in err + _ledger(env)


def test_chat_questions_differing_in_a_pr_number_are_two_points_and_spacing_is_one(env):
    _ask(env, "vraag", "antwoord", vraag="Mergen #101?")
    _ask(env, "vraag", "antwoord", vraag="Mergen #102?")
    assert _ask(env, "vraag", "antwoord", vraag="  mergen   #101?\n") is None
    first = next(p for p in _points(env).values() if "101" in p["vraag"])["point_id"]
    assert len(_points(env)) == 2  # two PR numbers, two points
    qa.answer(PID, first, quote="ja", vastgelegd_door="T0", state_dir=env["q"], now="2026-11-15T10:00:00Z")
    assert _read(env)[8]["closed"] == [first]
    assert {p["vraag"]: p["state"] for p in _points(env).values()} == {"Mergen #101?": "beantwoord", "Mergen #102?": "open"}
    closed = [json.loads(x) for x in _ledger(env).splitlines() if '"closed"' in x][0]
    assert (closed["beantwoord_door"], closed["bewijs"]) == ("t0_namens_operator", "2026-11-15T10:00:00Z")


def test_answer_refusals(env):
    _ask(env, "besluit", "dlv:d1")
    _ask(env, "vraag", "antwoord", vraag="Open vraag?")
    by_ref = {p["ref"].split(":")[0]: p["point_id"] for p in _points(env).values()}
    kw = {"vastgelegd_door": "T0", "state_dir": env["q"]}
    with pytest.raises(qa.RefRefused):
        _ask(env, "promote", "oi:OI-1")  # bron promote takes only a dlv: ref
    for pid, quote, match in ((by_ref["dlv"], "ja", "antwoord:"), ("0" * 16, "ja", "does not exist"),
                              (by_ref["antwoord"], "", "quote"), (by_ref["antwoord"], "x" * 301, "quote")):
        with pytest.raises(oq.QueueRefused, match=match):
            qa.answer(PID, pid, quote=quote, **kw)
    qa.answer(PID, by_ref["antwoord"], quote="ja", **kw)
    _read(env)
    with pytest.raises(oq.QueueRefused, match="closed"):
        qa.answer(PID, by_ref["antwoord"], quote="nogmaals", **kw)


def test_dlv_closes_when_no_longer_proposed_and_a_second_read_writes_nothing(env):
    _dispatch(env, "d1")
    _ask(env, "besluit", "dlv:d1")
    assert _read(env)[2]["closed"] == []
    _sql(env, "UPDATE dispatches SET state='ready'")
    _sql(env, "INSERT INTO coordination_events (event_id, event_type, entity_id, metadata_json, occurred_at, project_id) "
              "VALUES ('e1','deliverable_promoted','d1','{\"actor_class\":\"operator\"}','2026-11-15T10:00:00Z',?)", PID)
    assert len(_read(env)[2]["closed"]) == 1
    before = _ledger(env)
    assert _read(env)[2]["closed"] == [] and _ledger(env) == before
    assert '"beantwoord_door": "operator"' in before


def test_dlv_with_track_done_is_vervallen(env):
    _track(env, "active", "done")
    _dispatch(env, "d1", track="t1")
    _ask(env, "besluit", "dlv:d1")
    pid = _read(env)[2]["closed"][0]
    assert _points(env)[pid]["state"] == "vervallen"


def test_dlv_row_gone_is_unknown_never_vervallen_and_the_reason_is_written_once(env):
    _ask(env, "besluit", "dlv:gone")
    result = _read(env)[2]
    pid = result["unknown"][0]
    assert result["closed"] == [] and result["dekking"] == "unknown" and _points(env)[pid]["state"] == "open"
    before = _ledger(env)
    _read(env)
    assert _ledger(env) == before


def test_missing_database_and_unreadable_ledger_are_unknown_and_close_nothing(env, tmp_path):
    _ask(env, "besluit", "dlv:d1")
    result = qa.read_asked(PID, data_root=tmp_path / "none", state_dir=env["q"])
    assert result[1]["dekking"] == "unknown" and result[1]["closed"] == []
    (env["q"] / oq.LEDGER_FILENAME).write_text("not json\n")
    assert all(r["dekking"] == "unknown" for r in _read(env).values())


def test_oi_closes_on_deferred_and_stays_open_on_open_or_garbage(env):
    store = env["sdir"] / "open_items.json"
    _ask(env, "besluit", "oi:OI-7")
    for status, closes in (("open", False), (None, False), ("weird", False), ("deferred", True)):
        store.write_text(json.dumps({"items": [{"id": "OI-7", "status": status}]}))
        assert bool(_read(env)[2]["closed"]) is closes
    store.write_text(json.dumps({"items": []}))
    other = _ask(env, "besluit", "oi:OI-8")
    assert _read(env)[2]["unknown"] == [other["point_id"]]


def test_an_open_outcome_without_an_ask_gives_no_point(env):
    (env["sdir"] / "open_outcomes.json").write_text(json.dumps({"outcomes": [{"id": "o1", "status": "open"}]}))
    assert all(r["closed"] == [] and r["unknown"] == [] and r["opened"] == [] for r in _read(env).values())
    assert len(_points(env)) == 0 and qa.READERS == (("asked", qa.read_asked),)


HEAD = "a" * 40
CAP_DDL = """CREATE TABLE review_cap_verdicts (
    project_id TEXT NOT NULL, pr_number INTEGER NOT NULL, verdict_id TEXT NOT NULL, head_sha TEXT NOT NULL,
    cap_rule TEXT NOT NULL, outcome TEXT NOT NULL, chosen_outcome TEXT NOT NULL, intervention TEXT NOT NULL DEFAULT '',
    oi_ids TEXT NOT NULL DEFAULT '[]', granted_dispatch_id TEXT NOT NULL DEFAULT '', tiebreaker_model TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY (project_id, pr_number, verdict_id))"""


def _closed(env):
    return [json.loads(x) for x in _ledger(env).splitlines() if '"closed"' in x]


def test_track_phase_rule_closes_on_active_and_a_later_phase_also_counts(env):
    _track(env)
    _ask(env, "besluit", "track:t1:active")
    assert _read(env)[2]["closed"] == []
    tracks.transition_phase(env["sdir"], "t1", PID, "active", actor="T0")
    assert len(_read(env)[2]["closed"]) == 1 and _closed(env)[0]["uitkomst"] == "beantwoord"


def test_track_done_without_the_phase_is_vervallen(env):
    _track(env, "parked")
    _sql(env, "UPDATE tracks SET phase='done'")  # no legal transition reaches done without active; a forced row does
    _ask(env, "besluit", "track:t1:active")
    _ask(env, "besluit", "track:t1:parked", vraag="p?")
    _read(env)
    by_ref = {c["point_id"]: c["uitkomst"] for c in _closed(env)}
    refs = {p["ref"]: p["point_id"] for p in _points(env).values()}
    assert by_ref[refs["track:t1:active"]] == "vervallen" and by_ref[refs["track:t1:parked"]] == "beantwoord"


def test_track_parked_is_not_later_than_active_and_a_missing_track_is_unknown(env):
    _track(env, "active", "parked")
    _ask(env, "besluit", "track:t1:done")
    _ask(env, "besluit", "track:t1:parked", vraag="p?")
    ghost = _ask(env, "besluit", "track:nope:active", vraag="g?")
    res = _read(env)[2]
    assert len(res["closed"]) == 1 and res["unknown"] == [ghost["point_id"]]  # only :parked closes


def _config(env, key, at):
    conn = sqlite3.connect(str(env["sdir"] / "runtime_coordination.db"))
    config_store_db.ensure_config_tables(conn)
    conn.execute("INSERT OR IGNORE INTO project_config (project_id, config_key, config_value, updated_by) VALUES (?,?,?,?)",
                 (PID, key, "v", "op"))
    conn.execute("INSERT INTO project_config_audit (project_id, config_key, new_value, changed_by, changed_at, event_id) "
                 "VALUES (?,?,?,?,?,?)", (PID, key, "v", "op", at, f"e-{at}"))
    conn.commit()
    conn.close()


def test_config_closes_only_on_an_audit_row_after_asked_at(env):
    _ask(env, "besluit", "config:review.scope", now="2026-11-15T10:00:00Z")
    _config(env, "review.scope", "2026-11-15T09:00:00.000Z")
    assert _read(env)[2]["closed"] == []
    _config(env, "review.scope", "2026-11-15T11:00:00.000Z")
    assert len(_read(env)[2]["closed"]) == 1 and "11:00" in _closed(env)[0]["bewijs"]


def test_config_without_the_config_tables_is_unknown_never_closed(env):
    point = _ask(env, "besluit", "config:review.scope", now="2026-11-15T10:00:00Z")
    assert _read(env)[2]["unknown"] == [point["point_id"]] and _closed(env) == []


def _cap(env, monkeypatch, vid="cap-pr7-a", created="2026-11-15T07:00:00Z", head=HEAD):
    conn = sqlite3.connect(str(env["sdir"] / "runtime_coordination.db"))
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='review_cap_verdicts'").fetchone():
        conn.execute(CAP_DDL)
    conn.execute("INSERT INTO review_cap_verdicts (project_id, pr_number, verdict_id, head_sha, cap_rule, outcome, "
                 "chosen_outcome, created_at) VALUES (?,7,?,?,'B3.2','mergen-met-OI','mergen-met-OI',?)",
                 (PID, vid, head, created))
    conn.commit()
    conn.close()
    monkeypatch.setattr(steering, "default_head_of", lambda src, repo=None: (lambda pr: head))
    monkeypatch.setattr(steering, "default_pr_state_of", lambda src: (lambda pr: "open"))
    return rcv.CapVerdict(PID, 7, vid, head, "B3.2", "mergen-met-OI", "mergen-met-OI", created_at=created)


def test_verdict_closes_beantwoord_on_a_matching_approval_and_vervallen_on_a_newer_verdict(env, monkeypatch):
    v = _cap(env, monkeypatch)
    _ask(env, "besluit", "verdict:cap-pr7-a")
    assert _read(env)[2]["closed"] == [] and _closed(env) == []  # source 7 opened its own point; the ask stays open
    rcv.record_merge_approval(env["sdir"], v, "vincent")
    assert len(_read(env)[2]["closed"]) == 1
    assert [c["uitkomst"] for c in _closed(env) if "cap-pr7-a" in c["bewijs"]] == ["beantwoord", "beantwoord"]
    _ask(env, "besluit", "verdict:cap-pr7-b", vraag="b?")
    _cap(env, monkeypatch, vid="cap-pr7-b", created="2026-11-15T09:00:00Z")
    _read(env)  # source 7 opens b's point
    _cap(env, monkeypatch, vid="cap-pr7-c", created="2026-11-15T10:00:00Z")
    _read(env)
    assert _closed(env)[-1]["uitkomst"] == "vervallen" and "cap-pr7-c" in _closed(env)[-1]["bewijs"]


def test_verdict_without_a_runtime_store_table_is_unknown_never_closed(env):
    point = _ask(env, "besluit", "verdict:cap-pr7-a")
    assert _read(env)[2]["unknown"] == [point["point_id"]] and _closed(env) == []


def test_source_one_reader_carries_the_refusal_reason_of_the_promote_gate(env, monkeypatch):
    _track(env)
    _dispatch(env, "d1", track="t1")
    _dispatch(env, "d2")
    _ask(env, "promote", "dlv:d1", vraag="Promote d1?")
    _ask(env, "promote", "dlv:d2", vraag="Promote d2?")
    monkeypatch.setattr(actor_class, "derive", lambda *a, **k: actor_class.T0)
    res = _read(env)[1]
    by_ref = {p["ref"]: p["point_id"] for p in _points(env).values()}
    reasons = res["weigerreden"]
    assert set(reasons) == set(by_ref.values()) and all(isinstance(r, str) and r for r in reasons.values())
    assert "autonomous_promotion_eligible" in reasons[by_ref["dlv:d2"]]
    assert _points(env)[by_ref["dlv:d1"]]["state"] == "open"  # never promotes


def test_source_one_refusal_is_none_for_the_operator_and_absent_for_a_closed_point(env, monkeypatch):
    _dispatch(env, "d1")
    _ask(env, "promote", "dlv:d1")
    monkeypatch.setattr(actor_class, "derive", lambda *a, **k: actor_class.OPERATOR)
    pid = next(iter(_points(env)))
    assert _read(env)[1]["weigerreden"] == {pid: None}
    _sql(env, "UPDATE dispatches SET state='ready'")
    assert _read(env)[1]["weigerreden"] == {}


def test_answers_returns_the_answer_lines_in_order_and_raises_on_a_bad_ledger(env):
    for n, q in enumerate(("Eerste?", "Tweede?")):
        pid = _ask(env, "vraag", "antwoord", vraag=q)["point_id"]
        qa.answer(PID, pid, quote=f"ja{n}", vastgelegd_door="T0", state_dir=env["q"], now=f"2026-11-15T10:0{n}:00Z")
    got = oq.answers(PID, state_dir=env["q"])
    assert [a["citaat"] for a in got] == ["ja0", "ja1"] and {a["kind"] for a in got} == {"answer"}
    assert oq.answers(PID, state_dir=env["q"] / "nowhere") == []
    (env["q"] / oq.LEDGER_FILENAME).write_text("{torn")
    with pytest.raises(oq.QueueUnreadable):
        oq.answers(PID, state_dir=env["q"])
