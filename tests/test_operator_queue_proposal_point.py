"""M5a: proposal answers are inbox evidence, never daily-metrics approval."""
import ast
import json
from datetime import date
from pathlib import Path

import pytest

import operator_queue as oq
import operator_queue_ask as qa
from scripts import inbox_cli
from scripts.lib.daily_metrics import proposal_ledger as proposals
from scripts.lib.daily_metrics.proposal_rules import Candidate

PROJECT = "proposal-project"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_AGENT_ID", "t1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    q = tmp_path / "state"
    ledger = q / "daily_metrics" / PROJECT / "proposals.ndjson"
    ids = proposals.append_proposals(
        ledger, project_id=PROJECT, metric_day=date(2026, 11, 24),
        candidates=[Candidate("merges", "merges", None, 4, 12, 70, 8, 0.67)],
        rule_version="m5a", recorded_at="2026-11-25T09:00:00Z")
    return tmp_path, q, ledger, "proposal:" + ids[0]


def ask(env, ref=None):
    return qa.ask(PROJECT, bron="besluit", ref=env[3] if ref is None else ref,
                  vraag="Apply this proposal?", sluit_bij="rejected or deferred",
                  opened_by="T0", state_dir=env[1])


def read(env):
    return {r["bron"]: r for r in qa.read_asked(PROJECT, data_root=env[0], state_dir=env[1])}[2]


def test_ask_accepts_proposal_id_from_the_real_ledger(env):
    point = ask(env)
    assert point["ref"] == env[3] and point["bron"] == 2
    assert qa.parse_ref(env[3]) == ("proposal", (env[3].split(":")[1],))
    assert ask(env) is None
    assert read(env)["closed"] == []


@pytest.mark.parametrize("ref", ["proposal:", "proposal:abc", "proposal:ABC123DEF456",
                                  "proposal:abc123def4567", "proposal:../abc123def456",
                                  "proposal:/abc123def456", "proposal:abc123def456\n"])
def test_ask_refuses_malformed_proposal_and_lists_new_form(env, ref):
    with pytest.raises(qa.RefRefused, match="proposal:<id>") as exc:
        ask(env, ref)
    assert all(form in str(exc.value) for form in qa.FORMS)
    assert not (env[1] / oq.LEDGER_FILENAME).exists()


@pytest.mark.parametrize("bron", ["vraag", "promote"])
def test_proposal_requires_decision_source(env, bron):
    with pytest.raises(qa.RefRefused):
        qa.ask(PROJECT, bron=bron, ref=env[3], vraag="Apply?", sluit_bij="rejected",
               opened_by="T0", state_dir=env[1])
    assert not (env[1] / oq.LEDGER_FILENAME).exists()


def test_unreadable_answers_keep_proposal_open_with_a_reason(env, monkeypatch):
    pid = ask(env)["point_id"]

    def unreadable(*args, **kwargs):
        raise OSError("answer ledger read denied")

    monkeypatch.setattr(oq, "answers", unreadable)
    result = read(env)
    assert result["closed"] == [] and result["unknown"] == [pid]
    point = oq.read_state(PROJECT, state_dir=env[1])["points"][pid]
    assert point["state"] == "open" and "answer ledger read denied" in point["unknown_reden"]


@pytest.mark.parametrize("quote,outcome", [("ja", "open"), ("approved", "open"),
    ("afgewezen", "beantwoord"), ("later", "vervallen"), ("maybe later", "open"),
    ("not afgewezen", "open"), ("unknown", "open")])
def test_agent_answer_never_approves_and_only_explicit_dispositions_close(env, quote, outcome):
    pid = ask(env)["point_id"]
    other = ask(env, "proposal:000000000000")["point_id"]
    before = env[2].read_bytes()
    assert inbox_cli.main(["--project-id", PROJECT, "--state-dir", str(env[1]),
                          "--logdir", str(env[0] / "daily-log"), "answer", pid,
                          "--by", "vincent", "--quote", quote]) == 0
    (answer,) = oq.answers(PROJECT, state_dir=env[1])
    assert (answer["point_id"], answer["citaat"], answer["vastgelegd_door"]) == (pid, quote, "vincent")
    read(env)
    points = oq.read_state(PROJECT, state_dir=env[1])["points"]
    assert points[pid]["state"] == outcome and points[other]["state"] == "open"
    if outcome != "open":
        assert quote in points[pid]["bewijs"] and outcome in oq.OUTCOMES
    snapshot = (env[1] / oq.LEDGER_FILENAME).read_bytes()
    assert read(env)["closed"] == []
    assert (env[1] / oq.LEDGER_FILENAME).read_bytes() == snapshot
    assert env[2].read_bytes() == before
    assert not any(e["event"] == "approved" for e in proposals.read_events(env[2]))


@pytest.mark.parametrize("fault", ["missing", None, 42, "", "unknown"])
def test_missing_or_bad_quote_never_closes(env, fault):
    pid = ask(env)["point_id"]
    line = {"kind": "answer", "project_id": PROJECT, "point_id": pid,
            "ts": "2026-11-25T09:00:00Z", "vastgelegd_door": "vincent"}
    if fault != "missing":
        line["citaat"] = fault
    path = env[1] / oq.LEDGER_FILENAME
    with path.open("a") as handle:
        handle.write(json.dumps(line) + "\n")
    result = read(env)
    assert result["closed"] == []
    if fault != "unknown":
        assert result["dekking"] == "unknown" and result["reden"]
    assert not any(ln["kind"] == "closed" for ln in map(json.loads, path.read_text().splitlines()))


@pytest.mark.parametrize("filename", ["operator_queue_ask.py", "operator_queue.py"])
def test_inbox_imports_no_approval_command_or_proposal_writers(filename):
    tree = ast.parse((Path(qa.__file__).parent / filename).read_text())
    forbidden = {"append_approval", "append_proposals", "append_verdict", "_write", "*"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not a.name.endswith(("commands.approve", "proposal_ledger")) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.endswith("commands.approve")
            if module.endswith("proposal_ledger"):
                assert not forbidden.intersection(a.name for a in node.names)
            if module.endswith("daily_metrics"):
                assert not {"proposal_ledger", "*"}.intersection(a.name for a in node.names)
            if module.endswith("daily_metrics.commands"):
                assert not {"approve", "*"}.intersection(a.name for a in node.names)
