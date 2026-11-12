"""OB-5b: `vnx learning steering proposals` and `vnx learning decide-steering` (ADR-072 decisions 9, 10, 11, 14).

Every proposal below is invented. State lives under tmp_path; no live store is opened.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts"))

import steering_proposal_store as store
from vnx_cli.commands.learning import vnx_learning
from vnx_cli.main import _register_learning_subparser

SECRET = "geheime-prompttekst-xyz"
PID = "proj-a"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr("vnx_cli.commands.learning._resolve_state_dir", lambda project_dir: state)
    monkeypatch.setattr("vnx_cli._engine.derive_project_id", lambda project_dir, explicit=None: PID)
    return state


def _seed(state, n, voorstel="voeg een regel toe"):
    proposal = {"oorzaak": f"oorzaak-{n}", "artefact_soort": "skill", "doelpad": f"skills/s{n}.md",
                "voorstel": voorstel, "evidence": [{"prompt_id": f"p{n}", "ts": "2026-11-01T10:00:00Z"}]}
    return store.add_revision(state, PID, proposal, now="2026-11-02T10:00:00Z")["proposal_id"]


def _run(argv, capsys):
    parser = argparse.ArgumentParser()
    _register_learning_subparser(parser.add_subparsers(dest="command"))
    code = vnx_learning(parser.parse_args(["learning", *argv]))
    out = capsys.readouterr()
    return code, out.out, out.err


def _decide(pid, action, capsys, *extra, reason="akkoord"):
    return _run(["decide-steering", pid, action, "--approval-id", "ap-1", "--reason", reason, *extra], capsys)


def test_proposals_json_lists_doelpad_and_bewijs(env, capsys):
    a, b = _seed(env, 1, SECRET), _seed(env, 2)
    code, out, _ = _run(["steering", "proposals", "--json"], capsys)
    doc = json.loads(out)
    assert code == 0 and {p["proposal_id"] for p in doc["proposals"]} == {a, b}
    first = next(p for p in doc["proposals"] if p["proposal_id"] == a)
    assert first["doelpad"] == "skills/s1.md" and first["bewijs"] == ["p1"] and first["revision"] == 1
    assert SECRET not in out


def test_proposals_status_filter_and_pending_first(env, capsys):
    a, b = _seed(env, 1), _seed(env, 2)
    assert _decide(a, "reject", capsys)[0] == 0
    _, out, _ = _run(["steering", "proposals", "--json"], capsys)
    assert [p["status"] for p in json.loads(out)["proposals"]] == ["pending", "rejected"]
    _, out, _ = _run(["steering", "proposals", "--status", "rejected", "--json"], capsys)
    assert [p["proposal_id"] for p in json.loads(out)["proposals"]] == [a]
    _, text, _ = _run(["steering", "proposals"], capsys)
    assert text.splitlines()[0].startswith(b) and "doelpad=skills/s2.md" in text and "bewijs=p2" in text


def test_proposals_empty_and_unreadable_store(env, capsys):
    assert _run(["steering", "proposals"], capsys)[1].strip() == "no steering proposals"
    env.mkdir(parents=True, exist_ok=True)
    (env / store.DECISIONS_FILENAME).write_text("{kapot\n")
    code, _, err = _run(["steering", "proposals"], capsys)
    assert code == 3 and "StoreUnreadable" in err


def test_approve_then_apply_ends_applied_with_ref(env, capsys):
    pid = _seed(env, 1)
    code, out, _ = _decide(pid, "approve", capsys)
    assert code == 0 and "changes nothing by itself" in out
    assert store.get(env, PID, pid)["status"] == "approved"
    assert _decide(pid, "apply", capsys, "--applied-ref", "1234")[0] == 0
    entry = store.get(env, PID, pid)
    assert entry["status"] == "applied" and entry["applied_ref"] == "1234"
    last = store.decisions(env, PID)[-1]
    assert last["decided_by"] == "operator" and "ap-1" in last["reason"]
    _, out, _ = _run(["steering", "proposals", "--json"], capsys)
    assert json.loads(out)["proposals"][0]["applied_ref"] == "1234"


def test_apply_without_ref_exits_2_and_changes_nothing(env, capsys):
    pid = _seed(env, 1)
    _decide(pid, "approve", capsys)
    before = store.decisions(env, PID)
    code, _, err = _decide(pid, "apply", capsys)
    assert code == 2 and err.count("\n") == 1 and "--applied-ref" in err
    assert store.decisions(env, PID) == before and store.get(env, PID, pid)["status"] == "approved"


def test_unknown_id_exits_1(env, capsys):
    _seed(env, 1)
    code, _, err = _decide("0" * 16, "approve", capsys)
    assert code == 1 and "unknown proposal" in err


def test_refused_decision_exits_2_with_store_reason(env, capsys):
    pid = _seed(env, 1)
    code, _, err = _decide(pid, "apply", capsys, "--applied-ref", "99")
    assert code == 2 and "applied needs an approved proposal" in err
    _decide(pid, "reject", capsys)
    code, _, err = _decide(pid, "approve", capsys)
    assert code == 2 and "cannot approved a rejected proposal" in err
    assert store.get(env, PID, pid)["status"] == "rejected"


@pytest.mark.parametrize("extra,reason", [(("--applied-ref", "7"), "akkoord"), ((), "   ")])
def test_usage_errors_exit_2_and_write_nothing(env, capsys, extra, reason):
    pid = _seed(env, 1)
    before = store.decisions(env, PID)
    assert _decide(pid, "approve", capsys, *extra, reason=reason)[0] == 2
    assert store.decisions(env, PID) == before


def test_decide_on_unreadable_store_exits_3(env, capsys):
    env.mkdir(parents=True, exist_ok=True)
    (env / store.DECISIONS_FILENAME).write_text("{kapot\n")
    assert _decide("0" * 16, "approve", capsys)[0] == 3


def test_steering_report_still_forwards(env, capsys, monkeypatch):
    import operator_steering
    seen = []
    monkeypatch.setattr(operator_steering, "main", lambda argv: seen.append(argv) or 0)
    code, _, _ = _run(["steering", "report", "--json"], capsys)
    assert code == 0 and seen == [["--state-dir", str(env), "report", "--json"]]
    assert _run(["steering"], capsys)[0] == 2
