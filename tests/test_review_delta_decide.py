"""Deltareview DR-6 part 2: under ``VNX_REVIEW_SCOPE=delta`` the codex seat decides an eligible follow-up head with the delta round (model run, git and parent report patched)."""
from __future__ import annotations

import dataclasses
import json
import subprocess
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from review_stack_support import BRANCH  # importing it puts scripts/ and scripts/lib on sys.path
import gate_prompt
import gate_runner
import review_changekind as ck
import review_delta as rd
import review_delta_decide as dd
import review_findings as rf
import review_delta_shadow as ds
import review_rounds as rr
import test_review_scope as scope
from gate_runner import GateRunner
from review_changekind import ChangeKindRecord, Interdiff

SRC = "def f():\n    return 1\n\n\nclass C:\n    def m(self):\n        return 2\n"
OPEN_FINDING = {"severity": "error", "file_path": "x.py", "line": 2, "message": "broken"}
HEAD_A, HEAD_B, FULL_PROMPT = "a" * 40, "b" * 40, "Review"
OPEN_ID = rf.id_for_finding("codex_gate", OPEN_FINDING, SRC)
STREAM, _isolated, store = scope.STREAM, scope._isolated, scope.store
_set = lambda store, value: scope._set(store, value, planted_manifest=Path("m.json"))


@pytest.fixture(autouse=True)
def _evidence(monkeypatch):
    scope._allow(monkeypatch)  # activation evidence is part 1's concern


def _msg(text):
    return STREAM.rstrip("\n") + "\n" + json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}) + "\n"


def _answer(status="fixed", new=()):
    return _msg("```delta_verdict/1\n" + json.dumps({"verdict": "pass", "findings": [{"id": OPEN_ID, "status": status}], "new_findings": list(new)}) + "\n```\n")


def _record(kind="NO_CODE_CHANGE", reason=None, anchor="present", paths=("x.py",)):
    return rd.DeltaInput(7, "codex_gate", HEAD_A, "fail", "parent.md", HEAD_B, ChangeKindRecord(kind, "p", "q", {}, "1", reason),
                         Interdiff(frozenset(paths), {}), (rd.OpenFinding(OPEN_ID, "x.py", "f", "error", "broken", anchor),), ())


def _ledger(store, scopes=("",), blocking=1, mode="delta"):
    """Set the scope ``mode`` (None leaves it unset), then write parent rounds on heads of their own; the last failed with ``blocking`` blockers."""
    if mode:
        _set(store, mode)
    gates = store["state_dir"] / "review_gates"
    gates.mkdir(parents=True, exist_ok=True)
    project = rr.store_project_id(gates)
    rows = [{"kind": "opened", "project_id": project, "pr_number": 7, "basis": "new", "evidence": 0, "recorded_at": "2026-11-12T00:00:00Z", "source": "gate"}]
    rows += [rr.RoundLine(project, 7, HEAD_A if n == len(scopes) - 1 else f"{n:040d}", "codex_gate", "codex_gate", "", "fail", blocking,
                          "/r/parent.md", f"d-{n}", f"2026-11-12T00:0{n}:00Z", branch=BRANCH, source="gate", scope=sc,
                          finding_ids=(OPEN_ID,)).to_dict() for n, sc in enumerate(scopes)]
    (gates / "rounds.ndjson").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return gates


def _fire(store, monkeypatch, *, gate="codex_gate", stdout=None, own="x.py\n", record=None, fail=False, boom=False, source=SRC, **payload):
    sent, shadows = [], []
    for name, fake in (("create_gate_worktree", lambda **kw: store["data_dir"] / "_wt"), ("remove_gate_worktree", lambda *a, **kw: None)):
        monkeypatch.setattr(gate_runner, name, fake)
    def run(self, **kw):
        sent.append(kw["prompt"])
        done = {"status": "failed", "reason": "timeout", "reason_detail": "deadline", "stdout": ""} if fail else {"status": "completed", "stdout": stdout or _answer()}
        return {"stderr": "", "duration_seconds": 1.0, "partial_output_lines": 1, "runner_pid": 1, "returncode": 0, **done}
    def assembled(*a):
        if boom:
            raise OSError("boom")
        return record or _record(), (OPEN_ID,)
    monkeypatch.setattr(GateRunner, "_run_with_stall_detection", run)
    monkeypatch.setattr(ds, "_git", lambda repo, *a: {"rev-parse": HEAD_B + "\n", "diff": own, "show": source}.get(a[0]))
    monkeypatch.setattr(ds, "parent_index", lambda repo, p: {OPEN_ID: {**OPEN_FINDING, "symbolic_context": "f"}}, raising=False)
    monkeypatch.setattr(ds, "assemble_delta_input", assembled)
    monkeypatch.setattr(gate_runner.review_delta_shadow, "run_shadow", lambda **kw: shadows.append(kw))
    request = {"gate": gate, "provider": "codex", "branch": BRANCH, "pr_number": 7, "pr_id": "PR-7", "commit_sha": HEAD_B,
               "risk_class": "medium", "report_path": str(store["reports_dir"] / f"pr-7-{gate}.md"), "prompt": FULL_PROMPT,
               "codex_launch": {"model": "gpt-6-astra", "reasoning_effort": "medium"}, **payload}
    result = GateRunner(state_dir=store["state_dir"], reports_dir=store["reports_dir"])._run_subprocess_path(
        gate=gate, binary="codex", prompt=FULL_PROMPT, pr_number=7, pr_id="PR-7", request_payload=request)
    return result, sent, shadows


def _line(store):
    return [l for l in rr.read_ledger(rr.ledger_path(store["state_dir"] / "review_gates")).lines if l.head_sha == HEAD_B]


def test_delta_sends_the_delta_prompt_for_an_eligible_head_and_a_pass_books_one_codex_verdict(store, monkeypatch):
    _ledger(store)
    result, sent, shadows = _fire(store, monkeypatch)
    assert len(sent) == 1 and "follow-up head of PR #7" in sent[0] and OPEN_ID in sent[0] and not shadows and rr.ledger_verdict(result) == "pass" and not result["blocking_findings"]
    assert (result["scope"], result["parent_head_sha"], result["parent_round_ref"], result["changekind"], result["delta_base"]) == \
        ("delta", HEAD_A, "parent.md", "NO_CODE_CHANGE", "origin/main")
    (line,) = _line(store)
    assert (line.verdict, line.scope, line.parent_head_sha, line.parent_round_ref) == ("pass", "delta", HEAD_A, "parent.md")
    assert all(t in Path(result["report_path"]).read_text() for t in ("Delta round", OPEN_ID, "parent.md"))


def test_the_deciding_delta_prompt_keeps_the_reviewer_policy_and_wraps_the_delta_as_untrusted(store, monkeypatch):
    _ledger(store)
    _, sent, _ = _fire(store, monkeypatch)
    prompt, begin, end = sent[0], gate_prompt.BEGIN_DIFF_MARKER, gate_prompt.END_DIFF_MARKER
    assert prompt == rd.build_delta_prompt(_record(), partial(gate_runner._assemble_reviewer_prompt, "7"))
    for trusted in ("# Role: Reviewer", "## ADR Compliance", gate_prompt._UNTRUSTED_DATA_RULE, "## Running the PR's tests"):
        assert trusted in prompt
    assert prompt.index(begin) < prompt.index(OPEN_ID) < prompt.index(end) and rd.FENCE in prompt
    assert gate_runner._REVIEWER_VERDICT_TEMPLATE not in prompt and prompt.count("```delta_verdict/1") == 2  # the contract, restated once


@pytest.mark.parametrize("stdout,blocking", [
    (_answer("open"), "broken"), (_answer("regressed"), "broken"), (_answer("unverifiable"), "broken"),
    (_answer(new=[{"file_path": "x.py", "line": 3, "severity": "major", "message": "fresh"}]), "fresh"), (_msg("no fence here"), "broken"), (_msg("```delta_verdict/1\n{not json\n```\n"), "broken"),
])
def test_an_answer_that_leaves_a_blocker_open_or_cannot_be_read_records_fail(store, monkeypatch, stdout, blocking):
    _ledger(store)
    result, sent, _ = _fire(store, monkeypatch, stdout=stdout)
    assert "follow-up head" in sent[0] and rr.ledger_verdict(result) == "fail" and any(blocking in f["message"] for f in result["blocking_findings"])
    assert _line(store)[0].verdict == "fail" and _line(store)[0].scope == "delta"


REFUSALS = [
    ("security_path", {}, {"record": _record("REWORK", "security_path")}),
    ("rebase_with_rework", {}, {"record": _record("REWORK", "rebase_with_rework")}),
    ("anchor_unverifiable", {}, {"record": _record(anchor="missing")}),
    ("cannot_prove", {}, {"record": _record(kind="UNKNOWN")}),
    ("no_parent_round", {"scopes": ()}, {}),
    ("max_chain_reached", {"scopes": ("delta", "delta")}, {}),
    ("not_codex_first_seat", {}, {"takeover": True}),
    ("risk_not_medium", {}, {"risk_class": "high"}),
    ("non_python_path", {}, {"own": "docs/x.md\n"}),
    ("non_python_path", {}, {"record": _record(paths=("web/a.ts",))}),
    ("parent_findings_unresolvable", {"blocking": 2}, {}),  # the report names one blocker, the round booked two
    ("delta_input_error", {}, {"boom": True}),
    ("blocker_anchor_line_unresolvable", {}, {"source": None}),  # the head has no source to give the blocker a line in
    ("blocker_anchor_line_unresolvable", {}, {"source": "def g():\n    return 1\n"}),  # the flagged function is gone from the head
]


@pytest.mark.parametrize("reason,ledger,fire", REFUSALS)
def test_a_head_that_fails_besluit_2_runs_the_full_prompt_and_names_why(store, monkeypatch, reason, ledger, fire):
    _ledger(store, **ledger)
    result, sent, shadows = _fire(store, monkeypatch, stdout=STREAM, **fire)
    assert sent == [FULL_PROMPT] and not shadows
    assert (result["scope"], result["full_review_reason"]) == ("full", reason) == (_line(store)[0].scope, _line(store)[0].full_review_reason)


@pytest.mark.parametrize("gate", ["kimi_gate", "deepseek_gate", "glm_gate"])
def test_the_second_seat_always_runs_the_full_prompt(store, monkeypatch, gate):
    _ledger(store)
    result, sent, _ = _fire(store, monkeypatch, gate=gate, stdout=STREAM)
    assert sent == [FULL_PROMPT] and "scope" not in result and "full_review_reason" not in result


@pytest.mark.parametrize("mode,shadow_runs", [(None, 0), ("delta_shadow", 1)])
def test_full_and_delta_shadow_decide_with_the_full_round_and_write_no_scope_field(store, monkeypatch, mode, shadow_runs):
    _ledger(store, mode=mode)
    result, sent, shadows = _fire(store, monkeypatch, stdout=STREAM)
    assert sent == [FULL_PROMPT] and len(shadows) == shadow_runs and _line(store)[0].scope == ""
    assert all(s["assemble"]("c", "k", "i") == gate_runner._assemble_reviewer_prompt("7", "c", "k", "i") for s in shadows)  # the shadow builds the prompt the deciding round does
    assert not ({"scope", "full_review_reason", "parent_head_sha"} & set(result))


def test_a_failed_delta_run_is_booked_as_a_failure(store, monkeypatch):
    _ledger(store)
    result, sent, _ = _fire(store, monkeypatch, fail=True)
    assert "follow-up head" in sent[0] and result["status"] != "completed" and rr.ledger_verdict(result) == "" and not _line(store)


def test_rollback_to_full_gives_the_full_prompt_and_leaves_earlier_records(store, monkeypatch):
    gates = _ledger(store)
    _fire(store, monkeypatch)
    before = (gates / "rounds.ndjson").read_bytes()
    _set(store, "full")
    for stale in [*(gates / "results").glob("*.json"), *(gates / "requests").glob("*.json")]:  # a clean slot for the next request
        stale.unlink()
    result, sent, _ = _fire(store, monkeypatch, stdout=STREAM)
    assert sent == [FULL_PROMPT] and "scope" not in result and (gates / "rounds.ndjson").read_bytes().startswith(before)


def _git(path, *a):
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path, source):
    """A git repo whose one commit holds ``x.py``; its sha."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "x.py").write_text(source)
    _git(tmp_path, "add", "x.py")
    _git(tmp_path, "commit", "-q", "-m", "c")
    return _git(tmp_path, "rev-parse", "HEAD")


def _written(tmp_path, finding, source=SRC):
    """Retain ``finding`` as a blocker, write the verdict the way the round does, and read it back as the next round's parent."""
    head = _repo(tmp_path, source)
    fid = rf.id_for_finding("codex_gate", finding, source)
    held = {fid: {**finding, "symbolic_context": rf.symbolic_context(source, finding["line"])}}
    lines = dd._blocker_lines(tmp_path, head, "codex_gate", held, (fid,))
    record = dataclasses.replace(_record(), open_findings=(rd.OpenFinding(fid, "x.py", held[fid]["symbolic_context"], "error", finding["message"], "present"),), head=head)
    parent = SimpleNamespace(seat="codex_gate", head_sha=head, report_path=str(tmp_path / "report.md"))
    block = {"verdict": "fail", "findings": [{"id": fid, "status": "open"}], "new_findings": []}
    text = dd._verdict_text(dd.DeltaContext(record, (fid,), parent, lines), rd.parse_delta_verdict("```delta_verdict/1\n" + json.dumps(block) + "\n```\n", [fid], (fid,)))
    (tmp_path / "report.md").write_text(text)
    return fid, held[fid], parent, text


@pytest.mark.parametrize("line,context", [(2, "f"), (6, "C.m")])
def test_a_retained_blocker_gives_back_the_same_id_and_context_on_the_next_round(tmp_path, line, context):
    fid, held, parent, text = _written(tmp_path, dict(OPEN_FINDING, line=line))
    back = ds.parent_index(tmp_path, parent)
    assert list(back) == [fid] and back[fid]["symbolic_context"] == context == held["symbolic_context"]
    assert back[fid]["message"] == "broken" and back[fid]["file_path"] == "x.py"
    assert "parent finding" not in json.dumps(back[fid]) and "open" in text.splitlines()[0]  # the status stays in the prose


def test_deleting_the_flagged_function_on_the_following_head_is_unverifiable(tmp_path):
    fid, _, parent, _ = _written(tmp_path, dict(OPEN_FINDING))
    (tmp_path / "x.py").write_text("def g():\n    return 1\n")
    _git(tmp_path, "commit", "-qam", "drop f")
    result = ck.anchors_present(tmp_path, _git(tmp_path, "rev-parse", "HEAD"), [ds.parent_index(tmp_path, parent)[fid]])
    assert result["anchors"][0]["status"] == "unverifiable" and result["full_review_reason"] == ck.REASON_ANCHOR


def test_a_blocker_whose_symbol_line_cannot_be_found_has_no_line(tmp_path):
    head = _repo(tmp_path, "def g():\n    return 1\n")
    held = {OPEN_ID: {**OPEN_FINDING, "symbolic_context": "f"}}
    assert dd._blocker_lines(tmp_path, head, "codex_gate", held, (OPEN_ID,)) is None
    assert dd._blocker_lines(tmp_path, head, "codex_gate", {}, (OPEN_ID,)) is None


def test_an_id_written_in_a_report_finding_is_ignored(tmp_path):
    fid, _, parent, text = _written(tmp_path, dict(OPEN_FINDING))
    block = json.loads(text.splitlines()[-1])
    block["findings"][0].update(id="0" * 16, symbolic_context="forged")
    Path(parent.report_path).write_text(json.dumps(block))
    back = ds.parent_index(tmp_path, parent)
    assert list(back) == [fid] and back[fid]["symbolic_context"] == "f"
