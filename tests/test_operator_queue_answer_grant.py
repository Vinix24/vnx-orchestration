"""ADR-088 decisions 5 and 7 (PG-3c): ``vnx inbox answer --grant`` writes the PR-size exception on the answer line."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(_ROOT / "scripts" / "lib"), str(_ROOT / "scripts")]

import actor_class
import inbox_cli
import operator_queue as oq
import operator_queue_ask as qa
import operator_queue_grant as qg

PID, HEAD, QUOTE = "proj", "a" * 40, "uitzondering 12 akkoord, 500 regels"
MARKERS = actor_class.DISPATCH_MARKERS + actor_class.TERMINAL_MARKERS + (actor_class.ROLE_MARKER, "VNX_PROCESS_OWNER")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for name in MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(actor_class.ROLE_MARKER, "orchestrator")  # t0
    e = {"q": tmp_path / "q", "root": tmp_path / "root", "gh_calls": [], "m_calls": [], "rc": 0, "lines": 480,
         "data": {"headRefOid": HEAD, "baseRefName": "main", "state": "OPEN"}, "raise": None, "during": None}

    def fake_run(cmd, **kw):  # one subprocess module: the reader's state read and the grant's head read
        if "state" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="OPEN\n", stderr="")
        e["gh_calls"].append(cmd)
        return subprocess.CompletedProcess(cmd, e["rc"], stdout=json.dumps(e["data"]), stderr="")

    def fake_measure(pr, head_sha, base_branch, cwd):
        e["m_calls"].append((pr, head_sha, base_branch))
        if e["during"]:
            e["during"]()
        if e["raise"]:
            raise e["raise"]
        return qg.pr_size_policy.Measurement(e["lines"], ("scripts/x.py",), "origin/main", "b" * 40)
    for mod in (qg, qa):
        monkeypatch.setattr(mod, "project_checkout_path", lambda pid: tmp_path)
    monkeypatch.setattr(qg.subprocess, "run", fake_run)
    monkeypatch.setattr(qg.pr_size_policy, "measure", fake_measure)
    (tmp_path / "root" / "state").mkdir(parents=True)
    return e


def _point(env, ceiling=500, kanaal="kickoff", ref="pr:12", bron="besluit"):
    kw = {"ceiling": ceiling, "reason": "one_working_unit"} if ref.startswith("pr:") else {}
    pid = qa.ask(PID, bron=bron, ref=ref, vraag=f"Allow {ref} above 450?", sluit_bij="an answer line",
                 opened_by="T0", state_dir=env["q"], **kw)["point_id"]
    if kanaal:
        oq.append_line(PID, "presented", {"point_id": pid, "kanaal": kanaal, "sessie": "s1"}, state_dir=env["q"])
    return pid


def _ledger(env):
    return path.read_text() if (path := env["q"] / oq.LEDGER_FILENAME).exists() else ""


def _grant(env, pid, quote=QUOTE):
    return inbox_cli.main(["--project-id", PID, "--state-dir", str(env["q"]), "--logdir", str(env["root"]),
                           "answer", pid, "--quote", quote, "--grant"])


def test_grant_writes_one_answer_line_that_latest_grant_reads_back_and_closes_answered(env):
    pid = _point(env)
    before = _ledger(env).count("\n")
    assert _grant(env, pid) == 0
    lines = [json.loads(ln) for ln in _ledger(env).splitlines()]
    assert len(lines) == before + 1 and lines[-1]["kind"] == "answer" and lines[-1]["citaat"] == QUOTE
    grant = lines[-1]["grant"]
    assert grant == {"ceiling": 500, "reason": "one_working_unit", "head_sha": HEAD, "base_ref": "origin/main",
                     "lines": 480, "measured_at": lines[-1]["ts"]}
    assert env["gh_calls"][0][:6] == ["gh", "pr", "view", "12", "--json", "headRefOid,baseRefName,state"]
    assert env["m_calls"] == [(12, HEAD, "main")]
    assert oq.latest_grant(PID, 12, state_dir=env["q"]) == {"grant": grant, "point_id": pid, "revision": 1,
                                                            "quote": QUOTE, "by": "T0", "at": lines[-1]["ts"]}
    qa.read_asked(PID, data_root=env["root"], state_dir=env["q"])
    assert oq.read_state(PID, state_dir=env["q"])["points"][pid]["state"] == "beantwoord"


def test_answer_without_grant_is_a_no_and_measures_nothing(env):
    line = qa.answer(PID, _point(env), quote="nee", vastgelegd_door="T0", state_dir=env["q"])
    assert "grant" not in line and env["gh_calls"] == env["m_calls"] == []
    assert oq.latest_grant(PID, 12, state_dir=env["q"]) is None


@pytest.mark.parametrize("marker,tty,who", [({"VNX_PROCESS_OWNER": "daemon"}, False, "unknown"),
                                            ({}, False, "unknown"), ({}, True, "operator")])
def test_only_operator_or_t0_may_grant(env, marker, tty, who):
    pid = _point(env)
    if who == "operator":
        assert qa.answer(PID, pid, quote="ja", vastgelegd_door="T0", state_dir=env["q"], grant=True, env=marker,
                         isatty=tty)["grant"]["lines"] == 480
        return
    with pytest.raises(oq.QueueRefused, match=f"actor class {who} may not grant"):
        qa.answer(PID, pid, quote="ja", vastgelegd_door="T0", state_dir=env["q"], grant=True, env=marker, isatty=tty)


def _gh(**change):
    return lambda e, mp: (e.update(change), _point(e))[1]


def _answer_during_measure(env, mp):  # the guard under the lock sees what landed after the first check
    pid = _point(env)
    env["during"] = lambda: qa.answer(PID, pid, quote="nee", vastgelegd_door="T0", state_dir=env["q"])
    return pid


_ROLE = actor_class.ROLE_MARKER
REFUSALS = {  # case: (setup returning the point id, the refusal named on stderr[, quote])
    "worker": (lambda e, mp: (mp.delenv(_ROLE), mp.setenv("VNX_DISPATCH_ID", "dlv-x"), _point(e))[2],  # V2.7
               "actor class worker may not grant"),
    "ref_not_pr": (lambda e, mp: _point(e, ref="antwoord", bron="vraag"), "--grant needs a pr:<n> point"),
    "never_presented": (lambda e, mp: _point(e, kanaal=None), "on a channel other than cli"),
    "presented_on_cli_only": (lambda e, mp: _point(e, kanaal="cli"), "on a channel other than cli"),
    "empty_quote": (lambda e, mp: _point(e), "the quote is empty or over 300 characters", "   "),
    "long_quote": (lambda e, mp: _point(e), "the quote is empty or over 300", "x" * (qa.MAX_QUOTE + 1)),
    "pr_merged": (_gh(data={"headRefOid": HEAD, "baseRefName": "main", "state": "MERGED"}), "is MERGED, not OPEN"),
    "pr_closed": (_gh(data={"headRefOid": HEAD, "baseRefName": "main", "state": "CLOSED"}), "is CLOSED, not OPEN"),
    "gh_exit_1": (_gh(rc=1), "gh pr view 12 unreadable"),
    "gh_no_base": (_gh(data={"headRefOid": HEAD, "state": "OPEN"}), "gh pr view 12 unreadable"),
    "measure_fails": (_gh(**{"raise": qg.pr_size_policy.PrSizeUnmeasured("fetch", "network down")}),
                      "measurement failed at fetch: network down"),
    "ceiling_below": (lambda e, mp: _point(e, ceiling=470), "ceiling 470 is below the measured 480 lines"),
    "superseded": (lambda e, mp: (_point(e), _point(e, ceiling=600))[0], "the point is closed"),
    "already_answered": (lambda e, mp: qa.answer(PID, _point(e), quote="nee", vastgelegd_door="T0",
                                                 state_dir=e["q"])["point_id"], "the point is already answered"),
    "answered_during_measure": (_answer_during_measure, "the point is already answered"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_refusal_exits_2_writes_nothing_and_keeps_the_point(env, monkeypatch, capsys, case):
    setup, match, quote = (REFUSALS[case] + (QUOTE,))[:3]
    pid = setup(env, monkeypatch)
    state = oq.read_state(PID, state_dir=env["q"])["points"][pid]["state"]
    before = _ledger(env)
    assert _grant(env, pid, quote) == 2
    assert match in capsys.readouterr().err
    assert '"grant"' not in _ledger(env) and (_ledger(env) == before or case == "answered_during_measure")
    assert oq.read_state(PID, state_dir=env["q"])["points"][pid]["state"] == state
    assert oq.latest_grant(PID, 12, state_dir=env["q"]) is None
    assert env["gh_calls"] == [] or case not in ("worker", "never_presented", "superseded", "already_answered")


def test_failing_append_prints_and_leaves_the_point_open(env, monkeypatch, capsys):
    pid = _point(env)
    before = _ledger(env)

    monkeypatch.setattr(oq, "_append", lambda sdir, line: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    assert _grant(env, pid) == 1
    assert "ledger write failed" in capsys.readouterr().err and _ledger(env) == before
    assert oq.read_state(PID, state_dir=env["q"])["points"][pid]["state"] == "open"
