"""operator_queue grant field and latest_grant (ADR-088 decision 5, plan pr-grootte-afgedwongen V2.4) on tmp_path stores."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import operator_queue as oq

P = "vnx-test"
SHA = "a" * 40


def _grant(**kw):
    grant = {"ceiling": 650, "reason": "one_working_unit", "head_sha": SHA, "base_ref": "origin/main",
             "lines": 600, "measured_at": "2026-11-19T10:00:00Z"}
    grant.update(kw)
    return grant


def _open(d, pr=12, revisie=1, project=P):
    oq.open_point(project, bron=2, ref=f"pr:{pr}", revisie=revisie, vraag=f"Exception PR {pr}?",
                  sluit_bij="answer", opened_by="T0", state_dir=d,
                  extra={"ceiling": 650, "reason": "one_working_unit"})
    return oq.point_id(project, 2, f"pr:{pr}", revisie)


def _answer(d, pid, grant=None, project=P, citaat="exception 12 approved, 650 lines"):
    fields = {"point_id": pid, "citaat": citaat, "vastgelegd_door": "T0"}
    if grant is not None:
        fields["grant"] = grant
    return oq.append_line(project, "answer", fields, state_dir=d)


def _ledger(d):
    path = d / oq.LEDGER_FILENAME
    return path.read_text() if path.exists() else ""


def test_reason_other_is_refused_and_nothing_is_appended(tmp_path):
    pid = _open(tmp_path)
    before = _ledger(tmp_path)
    with pytest.raises(oq.QueueRefused, match="reason"):
        _answer(tmp_path, pid, _grant(reason="other"))
    assert _ledger(tmp_path) == before


@pytest.mark.parametrize("grant, why", [
    (_grant(ceiling=599), "ceiling"),
    (_grant(head_sha="abc123"), "head_sha"),
    (_grant(head_sha="A" * 40), "head_sha"),
    (_grant(ceiling="650"), "ceiling"),
    (_grant(ceiling=650.0), "ceiling"),
    (_grant(ceiling=True), "ceiling"),
    (_grant(ceiling=0, lines=0), "ceiling"),
    (_grant(lines=-1), "lines"),
    (_grant(base_ref="  "), "base_ref"),
    (_grant(measured_at="2026-11-19T10:00:00"), "measured_at"),
    (_grant(measured_at="2026-11-19T10:00:00+01:00"), "measured_at"),
    (_grant(measured_at=None), "measured_at"),
    ({k: v for k, v in _grant().items() if k != "head_sha"}, "fields"),
    ({**_grant(), "extra": 1}, "fields"),
    ("650", "object"),
    (None, "object"),
])
def test_invalid_grant_is_refused(tmp_path, grant, why):
    pid = _open(tmp_path)
    before = _ledger(tmp_path)
    with pytest.raises(oq.QueueRefused, match=why):
        oq.append_line(P, "answer", {"point_id": pid, "citaat": "ja", "vastgelegd_door": "T0", "grant": grant},
                       state_dir=tmp_path)
    assert _ledger(tmp_path) == before


def test_grant_on_another_kind_is_refused(tmp_path):
    pid = _open(tmp_path)
    with pytest.raises(oq.QueueRefused, match="grant"):
        oq.append_line(P, "presented", {"point_id": pid, "kanaal": "status", "sessie": "s", "grant": _grant()},
                       state_dir=tmp_path)


def test_valid_grant_is_stored_and_read(tmp_path):
    pid = _open(tmp_path)
    _answer(tmp_path, pid, _grant())
    assert json.loads(_ledger(tmp_path).splitlines()[-1])["grant"] == _grant()
    got = oq.latest_grant(P, 12, state_dir=tmp_path)
    assert got == {"grant": _grant(), "point_id": pid, "revision": 1}


def test_answer_without_grant_is_valid_and_means_no(tmp_path):
    pid = _open(tmp_path)
    assert _answer(tmp_path, pid) is not None
    assert oq.latest_grant(P, 12, state_dir=tmp_path) is None


def test_no_ledger_is_no_grant(tmp_path):
    assert oq.latest_grant(P, 12, state_dir=tmp_path) is None


def test_highest_revision_with_a_grant_wins(tmp_path):
    one, two = _open(tmp_path, revisie=1), _open(tmp_path, revisie=2)
    _answer(tmp_path, two, _grant(ceiling=900))
    _answer(tmp_path, one, _grant(ceiling=650))  # file order must not decide
    assert oq.latest_grant(P, 12, state_dir=tmp_path) == {"grant": _grant(ceiling=900), "point_id": two, "revision": 2}


def test_higher_revision_answered_no_keeps_the_lower_grant(tmp_path):
    one = _open(tmp_path, revisie=1)
    _answer(tmp_path, one, _grant(ceiling=650))
    two = _open(tmp_path, revisie=2)
    _answer(tmp_path, two)
    assert oq.latest_grant(P, 12, state_dir=tmp_path) == {"grant": _grant(ceiling=650), "point_id": one, "revision": 1}


def test_answer_without_citation_never_counts(tmp_path):
    pid = _open(tmp_path)
    with pytest.raises(oq.QueueRefused, match="citaat"):
        _answer(tmp_path, pid, _grant(), citaat="  ")
    assert oq.latest_grant(P, 12, state_dir=tmp_path) is None
    line = {"kind": "answer", "project_id": P, "ts": "2026-11-19T10:00:00Z", "point_id": pid,
            "vastgelegd_door": "T0", "grant": _grant()}  # written past the writer: no citaat
    with open(tmp_path / oq.LEDGER_FILENAME, "a") as handle:
        handle.write(json.dumps(line) + "\n")
    with pytest.raises(oq.QueueUnreadable):
        oq.latest_grant(P, 12, state_dir=tmp_path)


def test_other_pr_and_other_project_are_ignored(tmp_path):
    for pr in (1, 123, 13):  # substring neighbours of 12
        _answer(tmp_path, _open(tmp_path, pr=pr), _grant(ceiling=999))
    oq.open_point(P, bron=2, ref="oi:pr:12", vraag="v", sluit_bij="s", opened_by="T0", state_dir=tmp_path)
    _answer(tmp_path, oq.point_id(P, 2, "oi:pr:12", 1), _grant())  # ref ends in pr:12, is not pr:12
    other = tmp_path / "other"
    _answer(other, _open(other, project="other-project"), _grant(ceiling=777), project="other-project")
    assert oq.latest_grant(P, 12, state_dir=tmp_path) is None
    mine = _open(tmp_path)
    _answer(tmp_path, mine, _grant())
    assert oq.latest_grant(P, 12, state_dir=tmp_path)["point_id"] == mine
    assert oq.latest_grant("other-project", 12, state_dir=other)["grant"]["ceiling"] == 777


@pytest.mark.parametrize("pr", [0, -1, "12", True, None])
def test_invalid_pr_is_refused(tmp_path, pr):
    with pytest.raises(ValueError):
        oq.latest_grant(P, pr, state_dir=tmp_path)


def test_corrupt_ledger_is_typed_unknown_not_none(tmp_path):
    pid = _open(tmp_path)
    _answer(tmp_path, pid, _grant())
    with open(tmp_path / oq.LEDGER_FILENAME, "a") as handle:
        handle.write("{not json\n")
    with pytest.raises(oq.QueueUnreadable) as exc:
        oq.latest_grant(P, 12, state_dir=tmp_path)
    assert exc.value.line == 3


def test_invalid_grant_written_past_the_writer_makes_the_ledger_unknown(tmp_path):
    pid = _open(tmp_path)
    line = {"kind": "answer", "project_id": P, "ts": "2026-11-19T10:00:00Z", "point_id": pid, "citaat": "ja",
            "vastgelegd_door": "T0", "grant": _grant(reason="other")}
    with open(tmp_path / oq.LEDGER_FILENAME, "a") as handle:
        handle.write(json.dumps(line) + "\n")
    with pytest.raises(oq.QueueUnreadable, match="reason"):
        oq.latest_grant(P, 12, state_dir=tmp_path)
    assert oq.read_state(P, state_dir=tmp_path)["status"] == "unknown"
