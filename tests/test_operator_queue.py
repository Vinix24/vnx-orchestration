"""operator_queue: ADR-076 decisions 1, 2, 3, 5, 9 on tmp_path stores (all secret values invented)."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import operator_queue as oq
import secret_patterns as sp

P = "vnx-test"
PEM = "-----BEGIN RSA" + " PRIV" + "ATE KEY-----\nMIIfake\n-----END RSA" + " PRIV" + "ATE KEY-----"
SECRETS = {  # refused pattern name -> (text, invented value that must vanish)
    "sk_key": ("use sk-test0123456789abcdefXYZ", "sk-test0123456789abcdefXYZ"),
    "github_token": ("push ghp_test0123456789abcdefghij0", "ghp_test0123456789abcdefghij0"),
    "aws_key_id": ("id AKIATESTFAKE0123456X ok", "AKIATESTFAKE0123456X"),
    "slack_token": ("post xoxb-test-0123456789-fake", "xoxb-test-0123456789-fake"),
    "bearer": ("send Bearer faketoken0123456789", "faketoken0123456789"),
    "secret_assignment": ("MY_API_KEY=fakevalue77 run", "fakevalue77"),
    "authorization_header": ("curl 'Authorization: Basic fakecred99'", "fakecred99"),
    "pem_block": (PEM, "MIIfake"),
    "jwt": ("t eyJfakehdr0.fakebody1.fakesig22 x", "fakebody1"),
}


def _open(d, ref="oi:OI-1", revisie=1, **kw):
    args = dict(bron=1, ref=ref, revisie=revisie, vraag="Akkoord met OI-1?", sluit_bij="ja of nee",
                opened_by="T0")
    args.update(kw)
    return oq.open_point(P, state_dir=d, **args)


def _ledger(d):
    path = d / oq.LEDGER_FILENAME
    return path.read_text() if path.exists() else ""


def test_same_question_twice_is_one_opened(tmp_path):
    assert _open(tmp_path) is not None
    assert _open(tmp_path) is None
    assert _ledger(tmp_path).count('"kind": "opened"') == 1


def test_close_one_point_leaves_the_other_open(tmp_path):
    _open(tmp_path, "oi:OI-1")
    _open(tmp_path, "oi:OI-2")
    one, two = oq.point_id(P, 1, "oi:OI-1", 1), oq.point_id(P, 1, "oi:OI-2", 1)
    oq.close_point(P, one, uitkomst="beantwoord", bewijs="status=closed", state_dir=tmp_path)
    points = oq.read_state(P, state_dir=tmp_path)["points"]
    assert points[one]["state"] == "beantwoord" and points[one]["bewijs"] == "status=closed"
    assert points[two]["state"] == "open"


def test_new_revision_is_a_new_point_old_stays_closed(tmp_path):
    _open(tmp_path)
    old = oq.point_id(P, 1, "oi:OI-1", 1)
    oq.close_point(P, old, uitkomst="vervallen", bewijs="row gone", state_dir=tmp_path)
    assert _open(tmp_path, revisie=2) is not None
    points = oq.read_state(P, state_dir=tmp_path)["points"]
    assert points[old]["state"] == "vervallen"
    assert points[oq.point_id(P, 1, "oi:OI-1", 2)]["state"] == "open"


def test_presented_is_kept_as_last(tmp_path):
    _open(tmp_path)
    pid = oq.point_id(P, 1, "oi:OI-1", 1)
    oq.append_line(P, "presented", {"point_id": pid, "kanaal": "kickoff", "sessie": "s1"}, state_dir=tmp_path)
    oq.append_line(P, "presented", {"point_id": pid, "kanaal": "push", "sessie": "s2"}, state_dir=tmp_path)
    assert oq.read_state(P, state_dir=tmp_path)["points"][pid]["presented"]["kanaal"] == "push"


@pytest.mark.parametrize("name", sorted(SECRETS))
@pytest.mark.parametrize("field", ["vraag", "kopieerblok", "citaat"])
def test_secret_refused_with_name_not_value(tmp_path, name, field):
    text, value = SECRETS[name]
    assert name in sp.find(text)
    if field == "citaat":
        _open(tmp_path)
        pid = oq.point_id(P, 1, "oi:OI-1", 1)
        before = _ledger(tmp_path)
        with pytest.raises(oq.QueueRefused) as err:
            oq.append_line(P, "answer", {"point_id": pid, "citaat": text, "vastgelegd_door": "T0"}, state_dir=tmp_path)
    else:
        before = ""
        with pytest.raises(oq.QueueRefused) as err:
            _open(tmp_path, **{field: text})
    assert name in str(err.value) and value not in str(err.value)
    assert _ledger(tmp_path) == before
    for path in tmp_path.glob("*"):  # a conftest may add a data directory beside the store files
        if path.is_file():
            assert value not in path.read_text()


def _refused(tmp_path, kind, fields):
    with pytest.raises(oq.QueueRefused):
        oq.append_line(P, kind, fields, state_dir=tmp_path)
    assert _ledger(tmp_path) == ""


def test_unknown_kind_and_missing_field_refused(tmp_path):
    _refused(tmp_path, "deleted", {"point_id": "x"})
    _refused(tmp_path, "presented", {"point_id": "x", "kanaal": "push"})


def test_foreign_project_id_refused(tmp_path):
    _refused(tmp_path, "unknown", {"point_id": "x", "reden": "r", "project_id": "other"})


def test_bad_uitkomst_and_no_bewijs_refused(tmp_path):
    _open(tmp_path)
    pid, before = oq.point_id(P, 1, "oi:OI-1", 1), _ledger(tmp_path)
    for kw in ({"uitkomst": "klaar", "bewijs": "x"}, {"uitkomst": "beantwoord", "bewijs": ""},
               {"uitkomst": "beantwoord", "bewijs": None}, {"uitkomst": None, "bewijs": "x"}):
        with pytest.raises(oq.QueueRefused):
            oq.close_point(P, pid, state_dir=tmp_path, **kw)
    with pytest.raises(oq.QueueRefused):
        oq.append_line(P, "closed", {"point_id": pid, "uitkomst": "beantwoord", "beantwoord_door": "x"},
                       state_dir=tmp_path)
    assert _ledger(tmp_path) == before


def test_closed_unknown_point_or_twice_refused(tmp_path):
    with pytest.raises(oq.QueueRefused):
        oq.close_point(P, "nope", uitkomst="beantwoord", bewijs="x", state_dir=tmp_path)
    _open(tmp_path)
    pid = oq.point_id(P, 1, "oi:OI-1", 1)
    oq.close_point(P, pid, uitkomst="beantwoord", bewijs="x", state_dir=tmp_path)
    with pytest.raises(oq.QueueRefused):
        oq.close_point(P, pid, uitkomst="vervallen", bewijs="y", state_dir=tmp_path)


def test_opened_with_foreign_point_id_or_bad_bron_refused(tmp_path):
    base = dict(bron=1, ref="r", revisie=1, vraag="q", sluit_bij="s", kopieerblok="", asked_at="2026-11-14T10:00:00Z",
                opened_by="T0")
    with pytest.raises(oq.QueueRefused):
        oq.append_line(P, "opened", {**base, "point_id": "0" * 16}, state_dir=tmp_path)
    with pytest.raises(oq.QueueRefused):
        _open(tmp_path, bron=9)
    assert _ledger(tmp_path) == ""


def test_corrupt_line_makes_state_unknown_with_line_number(tmp_path):
    _open(tmp_path)
    _open(tmp_path, "oi:OI-2")
    lines = _ledger(tmp_path).splitlines()
    (tmp_path / oq.LEDGER_FILENAME).write_text("\n".join([lines[0], "{not json", lines[1]]) + "\n")
    state = oq.read_state(P, state_dir=tmp_path)
    assert state["status"] == "unknown" and state["line"] == 2 and state["points"] == {}
    with pytest.raises(oq.QueueUnreadable):
        _open(tmp_path, "oi:OI-3")


def test_state_is_rebuilt_from_the_ledger(tmp_path):
    _open(tmp_path)
    pid = oq.point_id(P, 1, "oi:OI-1", 1)
    oq.append_line(P, "presented", {"point_id": pid, "kanaal": "cli", "sessie": "s"}, state_dir=tmp_path)
    oq.append_line(P, "push_reserved", {"point_ids": [pid], "sessie": "s"}, state_dir=tmp_path)
    oq.append_line(P, "unknown", {"point_id": pid, "reden": "bron onleesbaar"}, state_dir=tmp_path)
    path = tmp_path / oq.STATE_FILENAME
    before = path.read_text()
    path.unlink()
    assert oq.rebuild_state(P, state_dir=tmp_path) == json.loads(before)
    assert path.read_text() == before
    assert json.loads(before)["points"][pid]["state"] == "open"


# -- the inbox summary the writer keeps (ADR-076 amendment 2026-11-22) -------------------------------------------

def _summary(d):
    return json.loads((d / oq.SUMMARY_FILENAME).read_text())


def _is_current(d):
    got = oq.read_inbox_summary(P, state_dir=d)
    assert got["freshness"] == "current", got
    return got["summary"]


def test_summary_follows_every_ledger_write_path(tmp_path):
    d, pid = tmp_path, oq.point_id(P, 1, "oi:OI-1", 1)
    _open(d)
    assert _is_current(d)["open_total"] == 1 and _is_current(d)["points"][0]["point_id"] == pid  # opened
    oq.append_line(P, "answer", {"point_id": pid, "citaat": "ja", "vastgelegd_door": "T0"}, state_dir=d)
    _is_current(d)  # answer
    oq.append_line(P, "presented", {"point_id": pid, "kanaal": "cli", "sessie": "s"}, state_dir=d)
    assert _is_current(d)["points"][0]["presented"] == 1  # presented
    oq.reserve(P, lambda st: [pid], sessie="s", state_dir=d)
    _is_current(d)  # reserve
    (d / oq.SUMMARY_FILENAME).unlink()
    oq.rebuild_state(P, state_dir=d)
    assert _is_current(d)["open_total"] == 1  # rebuild_state
    oq.close_point(P, pid, uitkomst="beantwoord", bewijs="src", state_dir=d)
    assert _is_current(d)["open_total"] == 0 and _summary(d)["points"] == []  # closed
    (d / oq.SUMMARY_FILENAME).unlink()
    res = oq.compact(P, now=datetime(2030, 1, 1, tzinfo=timezone.utc), state_dir=d)
    assert res["lines"] > 0 and _is_current(d)["open_total"] == 0  # compact rewrote the ledger and the summary


def test_summary_ledger_bytes_equals_the_ledger_size(tmp_path):
    _open(tmp_path)
    _open(tmp_path, "oi:OI-2")
    assert _summary(tmp_path)["ledger_bytes"] == (tmp_path / oq.LEDGER_FILENAME).stat().st_size


def test_summary_points_follow_decision_10(tmp_path):
    d = tmp_path
    old, new = (_open(d, f"oi:{n}", asked_at=at, now=at)["point_id"] for n, at in
                (("old", "2026-11-01T00:00:00Z"), ("new", "2026-11-10T00:00:00Z")))
    oq.append_line(P, "presented", {"point_id": old, "kanaal": "cli", "sessie": "s"}, state_dir=d)
    assert [p["point_id"] for p in _summary(d)["points"]] == [new, old]  # never presented outranks an older presented one
    for n in range(6):
        _open(d, f"oi:x{n}")
    s = _summary(d)
    assert len(s["points"]) == 5 and s["open_total"] == 8 and s["more"] == 3


def test_read_inbox_summary_is_stale_after_a_raw_append_and_missing_without_a_file(tmp_path):
    assert oq.read_inbox_summary(P, state_dir=tmp_path)["freshness"] == "missing"
    _open(tmp_path)
    _is_current(tmp_path)
    with open(tmp_path / oq.LEDGER_FILENAME, "a") as raw:
        raw.write("\n")
    got = oq.read_inbox_summary(P, state_dir=tmp_path)
    assert got["freshness"] == "stale" and "ledger changed" in got["reason"]


@pytest.mark.parametrize("text", ["not json", "{}", json.dumps({"schema": "operator_inbox_summary/1", "project_id": "other"}),
                                  json.dumps({"schema": "x", "project_id": P, "ledger_bytes": 0})])
def test_unreadable_or_foreign_summary_is_stale_never_current(tmp_path, text):
    (tmp_path / oq.SUMMARY_FILENAME).write_text(text)
    got = oq.read_inbox_summary(P, state_dir=tmp_path)
    assert got["freshness"] == "stale" and got["summary"] is None


def test_unreadable_ledger_line_gives_status_unknown_not_a_count(tmp_path):
    _open(tmp_path)
    with open(tmp_path / oq.LEDGER_FILENAME, "a") as raw:
        raw.write("{broken\n")
    oq.rebuild_state(P, state_dir=tmp_path)
    s = _summary(tmp_path)
    assert s["status"] == "unknown" and s["line"] == 2 and s["open_total"] is None and s["points"] == []
    assert oq.read_inbox_summary(P, state_dir=tmp_path)["freshness"] == "current"
