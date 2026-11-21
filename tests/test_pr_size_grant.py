"""The door reads the PR-size grant (ADR-088 decision 5, pr-grootte-afgedwongen PG-4) on tmp_path inbox ledgers."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
import operator_queue as oq
import pr_size_policy as psp
from test_pr_size_policy import _REFUSE, _clean, _rows  # _clean: the autouse fixture that clears the size keys

P = "vnx-test"
_WARN = _rows(VNX_PR_SIZE_ENFORCE="warn")


def grant_line(d, pr=12, ceiling=650, project=P, revisie=1):
    """One ``pr:<pr>`` point answered with a grant, written through the queue's own validation."""
    oq.open_point(project, bron=2, ref=f"pr:{pr}", revisie=revisie, vraag=f"Exception PR {pr}?", sluit_bij="answer",
                  opened_by="T0", state_dir=d, extra={"ceiling": ceiling, "reason": "one_working_unit"})
    pid = oq.point_id(project, 2, f"pr:{pr}", revisie)
    grant = {"ceiling": ceiling, "reason": "one_working_unit", "head_sha": "a" * 40, "base_ref": "origin/main",
             "lines": 600, "measured_at": "2026-11-19T10:00:00Z"}
    oq.append_line(project, "answer", {"point_id": pid, "citaat": f"ok {ceiling}", "vastgelegd_door": "T0",
                                       "grant": grant}, state_dir=d)
    return pid


def judge(d, lines=700, resolver=_REFUSE, project=P, pr=12):
    return psp.verdict(pr, "", "main", None, project_id=project, resolver=resolver, state_dir=d,
                       measurer=lambda *a: psp.Measurement(lines, ("a.py",), "origin/main", "0" * 40))


def test_700_lines_above_a_ceiling_of_650_is_no_go_above_exception(tmp_path):
    pid = grant_line(tmp_path, ceiling=650)
    v = judge(tmp_path)
    assert (v.outcome, v.reason, v.lines, v.limit, v.grant_ceiling, v.grant_point_id) == (
        "NO-GO", "pr_size_above_exception", 700, 450, 650, pid)
    assert "700 lines above the granted ceiling 650" in v.detail and "--ref pr:12 --ceiling 700" in v.detail


def test_700_lines_within_a_ceiling_of_750_is_go_with_the_grant_identity(tmp_path):
    pid = grant_line(tmp_path, ceiling=750)
    v = judge(tmp_path)
    ts = json.loads((tmp_path / oq.LEDGER_FILENAME).read_text().splitlines()[-1])["ts"]
    assert (v.outcome, v.reason, v.grant_ceiling, v.grant_point_id) == ("GO", "pr_size_within_exception", 750, pid)
    assert (v.grant_quote, v.grant_by, v.grant_at) == ("ok 750", "T0", ts)


def test_a_later_head_above_the_ceiling_is_refused_again(tmp_path):
    grant_line(tmp_path, ceiling=650)
    assert judge(tmp_path, lines=600).reason == "pr_size_within_exception"
    assert (judge(tmp_path, lines=700).outcome, judge(tmp_path, lines=700).reason) == (
        "NO-GO", "pr_size_above_exception")


@pytest.mark.parametrize("resolver,outcome", [(_REFUSE, "NO-GO"), (_WARN, "WARN")])
def test_no_grant_keeps_the_above_limit_verdict(tmp_path, resolver, outcome):
    v = judge(tmp_path, resolver=resolver)
    assert (v.outcome, v.reason, v.grant_point_id, v.grant_ceiling) == (outcome, "pr_size_above_limit", "", None)


@pytest.mark.parametrize("resolver,outcome", [(_REFUSE, "NO-GO"), (_WARN, "WARN")])
def test_an_unreadable_ledger_is_exception_unknown(tmp_path, resolver, outcome):
    grant_line(tmp_path, ceiling=750)
    with open(tmp_path / oq.LEDGER_FILENAME, "a") as fh:
        fh.write("{not json\n")
    v = judge(tmp_path, resolver=resolver)
    assert (v.outcome, v.reason, v.grant_ceiling) == (outcome, "pr_size_exception_unknown", None)
    assert "QueueUnreadable" in v.detail


def test_a_ledger_that_is_a_directory_is_exception_unknown(tmp_path):
    (tmp_path / oq.LEDGER_FILENAME).mkdir()
    assert judge(tmp_path).reason == "pr_size_exception_unknown"


@pytest.mark.parametrize("change", [lambda g: g.pop("ceiling"), lambda g: g.update(ceiling=None),
                                    lambda g: g.update(ceiling="750"), lambda g: g.update(ceiling=750.0)])
def test_a_malformed_grant_row_is_exception_unknown_never_granted(tmp_path, change):
    grant_line(tmp_path, ceiling=750)
    path = tmp_path / oq.LEDGER_FILENAME
    rows = [json.loads(r) for r in path.read_text().splitlines()]
    change(rows[-1]["grant"])
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    v = judge(tmp_path)
    assert (v.outcome, v.reason, v.grant_ceiling) == ("NO-GO", "pr_size_exception_unknown", None)


@pytest.mark.parametrize("ceiling", [None, "750", True, 0, 750.0])
def test_a_reader_result_without_an_integer_ceiling_is_unknown(tmp_path, monkeypatch, ceiling):
    monkeypatch.setattr(oq, "latest_grant", lambda *a, **k: {"grant": {"ceiling": ceiling}, "point_id": "x"})
    assert psp.read_grant(P, 12, tmp_path)[0] == "unknown"
    assert judge(tmp_path).reason == "pr_size_exception_unknown"


def test_a_failing_reader_and_a_missing_project_are_unknown(tmp_path, monkeypatch):
    assert psp.read_grant(None, 12, tmp_path) == ("unknown", None, "no project id to read the inbox ledger of")
    assert judge(tmp_path, project=None).reason == "pr_size_exception_unknown"
    monkeypatch.setattr(oq, "latest_grant", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert psp.read_grant(P, 12, tmp_path) == ("unknown", None, "inbox ledger unreadable: RuntimeError: boom")


def test_a_grant_for_another_pr_is_no_grant(tmp_path):
    for pr in (1, 13, 123):
        grant_line(tmp_path, pr=pr, ceiling=999)
    assert judge(tmp_path).reason == "pr_size_above_limit"


def test_a_grant_for_another_project_is_never_a_grant(tmp_path):
    other = tmp_path / "other"
    grant_line(other, ceiling=999, project="other-project")
    assert judge(tmp_path).reason == "pr_size_above_limit"  # this project's own store holds none
    assert judge(other).reason == "pr_size_exception_unknown"  # another project's lines in the store read: unknown


def test_within_the_limit_the_ledger_is_never_read(tmp_path):
    v = psp.verdict(12, "", "main", None, project_id=P, resolver=_REFUSE, state_dir=tmp_path,
                    grant_reader=lambda *a: pytest.fail("ledger read within the limit"),
                    measurer=lambda *a: psp.Measurement(450, ("a.py",), "origin/main", "0" * 40))
    assert (v.outcome, v.reason, v.grant_point_id) == ("GO", "", "")
