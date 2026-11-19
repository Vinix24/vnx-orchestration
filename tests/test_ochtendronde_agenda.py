"""ochtendronde D2a: day snapshot, derived decision status, event ledger."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import operator_queue as oq
import ochtendronde_agenda as ag

P, DAY, NOW = "proj-a", "2026-11-19", "2026-11-19T05:00:00Z"


def _open(sd, ref, project=P):
    oq.open_point(project, bron=1, ref=ref, vraag=f"q {ref}", sluit_bij="x", opened_by="t", state_dir=sd)
    return oq.point_id(project, 1, ref, 1)


def _close(sd, pid, outcome="beantwoord"):
    oq.close_point(P, pid, uitkomst=outcome, bewijs="e", state_dir=sd)


def _garbage(sd):
    with open(sd / oq.LEDGER_FILENAME, "a") as handle:
        handle.write("garbage\n")


def _snap(sd):
    return json.loads(ag.write_snapshot(P, DAY, state_dir=sd, now=NOW).read_text())


def test_snapshot_holds_open_points_only_and_is_written_once(tmp_path):
    a, b = _open(tmp_path, "a"), _open(tmp_path, "b")
    _close(tmp_path, b)
    path = ag.write_snapshot(P, DAY, state_dir=tmp_path, now=NOW)
    data = json.loads(path.read_text())
    assert data["schema"] == "ochtendronde_snapshot/1" and data["taken_at"] == NOW
    assert [p["point_id"] for p in data["points"]] == [a]
    assert data["points"][0]["advies"] == "" and data["points"][0]["klasse"] == ""
    before, mtime = path.read_bytes(), os.stat(path).st_mtime_ns
    _open(tmp_path, "c")
    assert ag.write_snapshot(P, DAY, state_dir=tmp_path, now="2026-11-19T06:00:00Z") == path
    assert path.read_bytes() == before and os.stat(path).st_mtime_ns == mtime


def test_status_follows_closing_and_survives_restart(tmp_path):
    a, b = _open(tmp_path, "a"), _open(tmp_path, "b")
    _snap(tmp_path)
    _close(tmp_path, a, "vervallen")
    first = ag.decision_status(P, DAY, state_dir=tmp_path)
    assert first.points == {a: "decided", b: "open"} and first.all_decided is False
    del first  # restart: nothing in memory, the status is rebuilt from disk
    assert ag.decision_status(P, DAY, state_dir=Path(str(tmp_path))).points == {a: "decided", b: "open"}
    _close(tmp_path, b)
    assert ag.decision_status(P, DAY, state_dir=tmp_path).all_decided is True


def test_missing_snapshot_is_unknown(tmp_path):
    st = ag.decision_status(P, DAY, state_dir=tmp_path)
    assert st.all_decided is None and st.snapshot == "missing"


@pytest.mark.parametrize("content", ["not json", "[]", '{"schema": "x"}'])
def test_unreadable_snapshot_is_unknown(tmp_path, content):
    (tmp_path / "ochtendronde").mkdir()
    (tmp_path / "ochtendronde" / f"{DAY}.json").write_text(content)
    st = ag.decision_status(P, DAY, state_dir=tmp_path)
    assert st.all_decided is None and st.snapshot == "unreadable"


def test_unreadable_ledger_gives_unknown_points(tmp_path):
    a = _open(tmp_path, "a")
    _snap(tmp_path)
    _garbage(tmp_path)
    st = ag.decision_status(P, DAY, state_dir=tmp_path)
    assert st.points == {a: "unknown"} and st.all_decided is False and st.ledger == "unknown"
    (tmp_path / oq.LEDGER_FILENAME).write_text("")  # point gone from the ledger: also unknown
    st = ag.decision_status(P, DAY, state_dir=tmp_path)  # a point missing from a readable ledger is not decided
    assert st.points == {a: "unknown"} and st.ledger == "ok" and st.all_decided is False


def test_unreadable_ledger_with_empty_snapshot_is_not_all_decided(tmp_path):
    assert _snap(tmp_path)["points"] == []
    _garbage(tmp_path)
    st = ag.decision_status(P, DAY, state_dir=tmp_path)
    assert st.snapshot == "ok" and st.ledger == "unknown" and st.points == {}
    assert st.all_decided is False


def test_snapshot_refuses_unreadable_ledger(tmp_path):
    _open(tmp_path, "a")
    _garbage(tmp_path)
    with pytest.raises(oq.QueueUnreadable):
        ag.write_snapshot(P, DAY, state_dir=tmp_path, now=NOW)
    assert not (tmp_path / "ochtendronde" / f"{DAY}.json").exists()


def test_empty_snapshot_is_all_decided(tmp_path):
    assert _snap(tmp_path)["points"] == [] and ag.decision_status(P, DAY, state_dir=tmp_path).all_decided is True


@pytest.mark.parametrize("bad", ["../x", "2026-13-01", "/etc/passwd", "2026-1-1", ""])
def test_bad_day_refused(tmp_path, bad):
    for call in (lambda: ag.write_snapshot(P, bad, state_dir=tmp_path, now=NOW),
                 lambda: ag.decision_status(P, bad, state_dir=tmp_path),
                 lambda: ag.append_event(P, bad, "pause_start", {}, state_dir=tmp_path, now=NOW)):
        with pytest.raises(ValueError):
            call()


def test_event_seq_per_project_and_day(tmp_path):
    seqs = [ag.append_event(p, d, "pause_start", {}, state_dir=tmp_path, now=NOW)["seq"]
            for p, d in [(P, DAY), (P, DAY), ("proj-b", DAY), (P, "2026-11-20"), (P, DAY)]]
    assert seqs == [1, 2, 1, 1, 3]
    ev = ag.append_event(P, DAY, "pause_end", {"reason": "decided"}, state_dir=tmp_path, now=NOW)
    assert ev["seq"] == 4 and ev["reason"] == "decided"
    lines = (tmp_path / "ochtendronde.ndjson").read_text().splitlines()
    assert len(lines) == 6 and json.loads(lines[-1])["kind"] == "pause_end"


@pytest.mark.parametrize("kind,fields", [("bogus", {}), ("pause_end", {}), ("pause_end", {"reason": "x"}),
                                         ("pause_end", {"reason": None}), ("refused", {"seq": 9})])
def test_event_refusals_write_nothing(tmp_path, kind, fields):
    with pytest.raises(ValueError):
        ag.append_event(P, DAY, kind, fields, state_dir=tmp_path, now=NOW)
    assert not (tmp_path / "ochtendronde.ndjson").exists()


def test_corrupt_event_ledger_raises_and_is_not_appended(tmp_path):
    ag.append_event(P, DAY, "pause_start", {}, state_dir=tmp_path, now=NOW)
    path = tmp_path / "ochtendronde.ndjson"
    with open(path, "a") as handle:
        handle.write("garbage\n")
    size = path.stat().st_size
    with pytest.raises(ValueError):
        ag.append_event(P, DAY, "override", {}, state_dir=tmp_path, now=NOW)
    assert path.stat().st_size == size


def test_event_refuses_non_finite(tmp_path):
    with pytest.raises(ValueError):
        ag.append_event(P, DAY, "refused", {"x": float("nan")}, state_dir=tmp_path, now=NOW)
    assert not (tmp_path / "ochtendronde.ndjson").exists()
