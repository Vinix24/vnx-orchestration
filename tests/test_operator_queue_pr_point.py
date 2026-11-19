"""ADR-088 decision 6 (PG-3b): the inbox point ``pr:<n>``, its ceiling and reason, revisions and close rules."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import inbox_cli  # noqa: E402
import operator_queue as oq  # noqa: E402
import operator_queue_ask as qa  # noqa: E402

PID = "proj"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    (tmp_path / "root" / "state").mkdir(parents=True)
    gh = {"out": "OPEN\n", "rc": 0, "raise": None, "calls": []}

    def fake_run(cmd, **kw):
        gh["calls"].append(cmd)
        if gh["raise"]:
            raise gh["raise"]
        return subprocess.CompletedProcess(cmd, gh["rc"], stdout=gh["out"], stderr="")
    monkeypatch.setattr(qa, "project_checkout_path", lambda pid: tmp_path)
    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    return {"root": tmp_path / "root", "q": tmp_path / "q", "gh": gh}


def _ask(env, ceiling=500, reason="one_working_unit", ref="pr:12", bron="besluit"):
    return qa.ask(PID, bron=bron, ref=ref, vraag="Allow PR 12 above 450?", sluit_bij="an answer line",
                  opened_by="T0", state_dir=env["q"], ceiling=ceiling, reason=reason)


def _points(env):
    return oq.read_state(PID, state_dir=env["q"])["points"]


def _ledger(env):
    path = env["q"] / oq.LEDGER_FILENAME
    return path.read_text() if path.exists() else ""


def _read(env):
    return {r["bron"]: r for r in qa.read_asked(PID, data_root=env["root"], state_dir=env["q"])}[2]


def _cli(env, *extra):
    return inbox_cli.main(["--project-id", PID, "--state-dir", str(env["q"]), "ask", "--bron", "besluit",
                           "--ref", "pr:12", "--vraag", "Allow PR 12?", "--sluit-bij", "an answer line", *extra])


def test_cli_ask_pr_needs_a_ceiling_and_opens_with_both_fields(env):
    assert _cli(env, "--reason", "generated") == 2
    with pytest.raises(SystemExit) as exc:
        _cli(env, "--ceiling", "lots", "--reason", "generated")
    assert exc.value.code == 2 and _ledger(env) == ""
    assert _cli(env, "--ceiling", "500", "--reason", "generated") == 0
    (point,) = _points(env).values()
    assert (point["ref"], point["revisie"], point["ceiling"], point["bron"]) == ("pr:12", 1, 500, 2)
    assert '"reason": "generated"' in _ledger(env)


def test_open_pr_without_answer_stays_open(env):
    pid = _ask(env)["point_id"]
    res = _read(env)
    assert pid not in res["closed"] and pid not in res["unknown"]
    assert _points(env)[pid]["state"] == "open"
    assert env["gh"]["calls"][0][:4] == ["gh", "pr", "view", "12"]


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_merged_or_closed_pr_lapses_never_answered(env, state):
    pid = _ask(env)["point_id"]
    env["gh"]["out"] = state + "\n"
    assert pid in _read(env)["closed"]
    point = _points(env)[pid]
    assert point["state"] == "vervallen" and point["bewijs"] == f"gh pr view 12: state={state}"


def test_answer_line_closes_as_answered_even_on_a_merged_pr(env):
    pid = _ask(env)["point_id"]
    qa.answer(PID, pid, quote="yes, 500 is fine", vastgelegd_door="T0", state_dir=env["q"])
    env["gh"]["out"] = "MERGED\n"
    _read(env)
    assert _points(env)[pid]["state"] == "beantwoord"


def test_higher_ceiling_opens_revision_2_and_answer_on_revision_1_is_refused(env):
    first = _ask(env, ceiling=500)["point_id"]
    second = _ask(env, ceiling=600)
    assert second["revisie"] == 2 and second["ceiling"] == 600
    points = _points(env)
    assert points[first]["state"] == "vervallen" and points[first]["bewijs"] == "replaced by revision 2"
    assert points[second["point_id"]]["state"] == "open"
    with pytest.raises(oq.QueueRefused, match="answer: the point is closed"):
        qa.answer(PID, first, quote="yes", vastgelegd_door="T0", state_dir=env["q"])


@pytest.mark.parametrize("ceiling", [500, 400])
def test_same_or_lower_ceiling_opens_nothing_and_says_so(env, ceiling):
    _ask(env, ceiling=500)
    before = _ledger(env)
    with pytest.raises(oq.QueueRefused, match="not above ceiling 500 of revision 1"):
        _ask(env, ceiling=ceiling)
    assert _ledger(env) == before


def test_answer_on_open_older_revision_refused_and_reader_lapses_it(env):  # its close line never landed
    kw = {"bron": 2, "ref": "pr:12", "vraag": "v", "sluit_bij": "s", "opened_by": "T0", "state_dir": env["q"]}
    first = oq.open_point(PID, **kw, extra={"ceiling": 500, "reason": "generated"})["point_id"]
    oq.open_point(PID, **kw, revisie=2, extra={"ceiling": 600, "reason": "generated"})
    before = _ledger(env)
    with pytest.raises(oq.QueueRefused, match="revision 2 of pr:12 replaces this point"):
        qa.answer(PID, first, quote="yes", vastgelegd_door="T0", state_dir=env["q"])
    assert _ledger(env) == before
    _read(env)
    assert _points(env)[first]["bewijs"] == "replaced by revision 2"


@pytest.mark.parametrize("bron,ref", [("vraag", "pr:12"), ("promote", "pr:12"), ("besluit", "dlv:d1")])
def test_pr_with_other_bron_or_ceiling_on_other_ref_refused(env, bron, ref):
    with pytest.raises(qa.RefRefused):
        _ask(env, bron=bron, ref=ref)
    assert _ledger(env) == ""


@pytest.mark.parametrize("ceiling,reason", [(None, "generated"), (0, "generated"), (-5, "generated"), (True, "generated"),
                                            ("500", "generated"), (500, None), (500, "other"), (500, "")])
def test_missing_or_invalid_ceiling_or_reason_refused_writes_nothing(env, ceiling, reason):
    with pytest.raises(oq.QueueRefused, match="needs --ceiling"):
        _ask(env, ceiling=ceiling, reason=reason)
    assert _ledger(env) == ""


@pytest.mark.parametrize("ref", ["pr:0", "pr:-1", "pr:abc", "pr:", "pr:1/2", "pr:01"])
def test_malformed_pr_ref_refused(ref):
    with pytest.raises(qa.RefRefused):
        qa.parse_ref(ref)


@pytest.mark.parametrize("fault", [{"rc": 1, "out": ""}, {"out": "DRAFT\n"}, {"out": ""},
                                   {"raise": FileNotFoundError("gh")}, {"raise": subprocess.TimeoutExpired("gh", 20)}, None])
def test_unreadable_gh_keeps_the_point_open_as_unknown(env, monkeypatch, fault):
    pid = _ask(env)["point_id"]
    if fault is None:
        monkeypatch.setattr(qa, "project_checkout_path", lambda pid: None)
    else:
        env["gh"].update(fault)
    res = _read(env)
    assert pid in res["unknown"] and res["dekking"] == "unknown"
    point = _points(env)[pid]
    assert point["state"] == "open" and point["unknown_reden"].startswith("source unreadable")


@pytest.mark.parametrize("fields,why", [({"ceiling": 500}, "needs a ceiling"), ({"reason": "generated"}, "needs a ceiling"),
                                        ({"ceiling": 5, "reason": "generated", "bron": 8}, "only with bron 2"),
                                        ({"ceiling": 5, "ref": "dlv:d1"}, "only on a pr: point")])
def test_ledger_rejects_wrong_ceiling_and_reason_on_an_opened_line(fields, why):
    bron, ref = fields.pop("bron", 2), fields.pop("ref", "pr:3")
    line = {"kind": "opened", "project_id": PID, "ts": "2026-11-19T10:00:00Z", "point_id": oq.point_id(PID, bron, ref, 1),
            "bron": bron, "ref": ref, "revisie": 1, "vraag": "v", "sluit_bij": "s", "asked_at": "2026-11-19T10:00:00Z",
            "opened_by": "T0", **fields}
    with pytest.raises(ValueError, match=why):
        oq.check_line(PID, line)
