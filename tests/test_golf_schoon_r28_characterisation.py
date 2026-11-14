"""Golf-schoon R28 characterisation: `fulfill_obligation` (attempt-gate path), `_stale_terminal_evidence`
and `gate_reanchor_cli.main`, pinned before the extract-method cut. Real temp stores and a real git
repo; only `gh` and the gate manager (the external model run) are replaced."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts" / "lib", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("_r28_gate_obligation_runner", "scripts/gate_obligation_runner.py")
cli = _load("_r28_gate_reanchor_cli", "scripts/gate_reanchor_cli.py")
import gate_run_guard  # noqa: E402
from gate_obligations import obligation_path, register_obligation  # noqa: E402

PR, GATE, A, B = 62828, "codex_gate", "a1" * 20, "b2" * 20


class _Manager:
    def __init__(self, state_dir, text, boom=False):
        self.state_dir, self.text, self.boom, self.calls = state_dir, text, boom, []

    def _request_path(self, gate, pr_number):
        return self.state_dir / "review_gates" / "requests" / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate, pr_number):
        return self.state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"

    def request_and_execute(self, **kwargs):
        self.calls.append(kwargs)
        if self.boom:
            raise RuntimeError("gate manager exploded")
        self._request_path(GATE, PR).write_text(json.dumps({"gate": GATE, "status": "completed"}), encoding="utf-8")
        self._result_path(GATE, PR).write_text(self.text, encoding="utf-8")
        return {"pr_number": PR, "gates": [], "has_required_failure": False}


def _pass_record(state_dir):
    report = state_dir.parent / "r.md"
    report.write_text("report", encoding="utf-8")
    return json.dumps({"gate": GATE, "pr_number": PR, "commit_sha": A, "status": "pass",
                       "contract_hash": "h", "report_path": str(report)})


_MOVED = f"result records commit {A[:8]} but the PR head is {B[:8]} — this verdict is about other code"
_UNKNOWN = (f"codex_gate for PR #{PR} returned an unrecognised result status ('') — refusing to record that "
            "as fulfilled; staying pending until a recognised status is seen or this escalates")


@pytest.mark.parametrize("text,head,boom,force,action,reason,detail", [
    ("pass", A, False, "", "fulfilled", None, None),
    ("pass", A, False, "operator reason", "fulfilled", None, None),
    ("pass", B, False, "", "pending", "stale_evidence_sha_mismatch", _MOVED),
    ("{not json", A, False, "", "pending", "gate_status_unknown", _UNKNOWN),
    ("[1, 2]", A, False, "", "not_executable", "runner_error", "AttributeError: 'list' object has no attribute 'get'"),
    ("", A, True, "", "not_executable", "runner_error", "RuntimeError: gate manager exploded"),
])
def test_fulfil_attempt_gate(tmp_path, monkeypatch, text, head, boom, force, action, reason, detail):
    monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)
    state_dir = tmp_path / "vnx-data" / "state"
    for sub in ("requests", "results"):
        (state_dir / "review_gates" / sub).mkdir(parents=True)
    manager = _Manager(state_dir, _pass_record(state_dir) if text == "pass" else text, boom)
    monkeypatch.setattr(runner, "_build_manager", lambda sd: manager)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo, snapshot=None: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "o/r")
    monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr: head)
    monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr, owner_repo, snapshot=None: "OPEN")
    monkeypatch.setitem(sys.modules, "review_gate_manager",
                        types.SimpleNamespace(_compute_changed_files=lambda branch: ["scripts/lib/foo.py"]))
    path = register_obligation(state_dir, dispatch_id="dlv-r28", gate=GATE, project_id="vnx-dev", pr_number=PR)

    outcome = runner.fulfill_obligation(state_dir, path, json.loads(path.read_text()), force_rerun=force)

    stored = json.loads(obligation_path(state_dir, "dlv-r28").read_text())
    if action == "fulfilled":
        assert sorted(outcome) == ["action", "dispatch_id", "gate", "has_required_failure", "request_path", "result_path"]
        assert outcome["result_path"] == str(manager._result_path(GATE, PR))
        assert outcome["has_required_failure"] is False
    else:
        assert outcome == {"dispatch_id": "dlv-r28", "gate": GATE, "action": action, "detail": detail}
        assert stored["reason"] == reason
    assert (outcome["action"], stored["status"], stored["attempts"]) == (action, action, 1)
    assert manager.calls == [{
        "pr_number": PR, "branch": manager.calls[0]["branch"], "review_stack": [GATE], "risk_class": "medium",
        "changed_files": ["scripts/lib/foo.py"], "mode": "per_pr", "dispatch_id": "dlv-r28",
        **({"force_rerun": force} if force else {}),
    }]
    for sha in (A, B):
        assert not gate_run_guard.run_lock_path(state_dir, pr_ref=PR, gate=GATE, head_sha=sha).exists()


def test_fulfil_without_a_gate_is_an_error(tmp_path):
    outcome = runner.fulfill_obligation(tmp_path, tmp_path / "x.json", {"dispatch_id": "d"})
    assert outcome == {"dispatch_id": "d", "gate": "", "action": "error", "detail": "obligation has no gate"}


_EMPTY = dict.fromkeys(("dispatch_id", "gate", "pr_number", "obligation_status", "obligation_path",
                        "evidence_result_path"), None) | {"head_sha": "", "evidence_commit_sha": "", "binding": "unknown"}


def test_stale_unresolved_open_pr_list():
    gh = runner._stale_terminal_evidence([], {"prs": None})
    assert gh == [_EMPTY | {"detail": (
        "the open-PR list could not be resolved (gh failure) — stale-terminal-evidence coverage is undetermined "
        "this run, not clear: an empty result here must never be read as 'nothing is stale'. "
        "Rerun once `gh pr list` succeeds.")}]
    (repo,) = runner._stale_terminal_evidence([], {"prs": None, "owner_repo_unresolvable": True})
    assert {k: v for k, v in repo.items() if k != "detail"} == _EMPTY
    assert repo["detail"].startswith("no GitHub owner/repo could be resolved for this runner — the project checkout")
    assert repo["detail"].endswith("Register the project checkout, or run from within it, then rerun.")


def test_stale_per_obligation_findings_and_cap(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "_pr_head_sha_for_gate", lambda pr, snapshot=None: calls.append(pr) or B)
    files = {"good": json.dumps({"commit_sha": B}), "old": json.dumps({"commit_sha": A}), "bad": "{x", "list": "[1]"}
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    rows = [("d1", "fulfilled", 7, "good"), ("d2", "fulfilled", 7, "old"), (None, "fulfilled", 7, "bad"),
            ("d4", "fulfilled", 7, "list"), ("d5", "fulfilled", 7, "gone"), ("d6", "pending", 7, "old"),
            ("d7", "fulfilled", 8, "old"), ("d8", "fulfilled", 7, None)]
    obligations = [(tmp_path / f"o{i}.json", {"dispatch_id": d, "gate": GATE, "status": s, "pr_number": pr,
                                              "result_path": str(tmp_path / f) if f else None})
                   for i, (d, s, pr, f) in enumerate(rows, 1)]
    findings = runner._stale_terminal_evidence(
        obligations, {"prs": [{"number": 7}, "junk", {"number": "9"}], "suspect": True})

    assert [f["dispatch_id"] for f in findings] == ["d2", "o3", "d4", "d5", None]
    assert calls == [7]
    assert findings[0] == {
        "dispatch_id": "d2", "gate": GATE, "pr_number": 7, "obligation_status": "fulfilled",
        "obligation_path": str(tmp_path / "o2.json"), "evidence_result_path": str(tmp_path / "old"),
        "head_sha": B, "evidence_commit_sha": A, "binding": "mismatch", "detail": (
            f"codex_gate obligation for dispatch d2 (PR #7) closed 'fulfilled' against commit {A[:8]}, but the PR "
            f"head is now {B[:8]} — this evidence is about code that is no longer the PR's head. NOT auto-fixed: "
            "reopen by hand via scripts/gate_obligation_reopen_stale_evidence.py.")}
    head = ("codex_gate obligation for dispatch {} (PR #7) closed 'fulfilled' but its sha binding to the PR head "
            "could not be verified ({}")
    tail = " — undetermined, not cleared: review manually or via scripts/gate_obligation_reopen_stale_evidence.py."
    for finding, did, why in zip(findings[1:4], ("o3", "d4", "d5"), (
            "evidence file is not valid JSON: ", "evidence file does not contain a JSON object)",
            "evidence file unreadable: ")):
        assert finding["binding"] == "unknown" and finding["evidence_commit_sha"] == ""
        assert finding["detail"].startswith(head.format(did, why)) and finding["detail"].endswith(tail)
    assert findings[4] == _EMPTY | {"detail": (
        f"the open-PR list hit the --limit {runner._OPEN_PR_LIST_LIMIT} cap (3 PRs returned) — additional open PRs "
        "may exist beyond the cap and were not checked this run (OI-1766): coverage is undetermined, not complete.")}


def _git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Merge-base unmoved, a glm_gate verdict on the old head, gh replaced."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    shas = []
    for body in ("x = 1\n", "x = 2\n", "x = 3\n"):
        (root / "a.py").write_text(body)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", body)
        shas.append(_git(root, "rev-parse", "HEAD"))
        if len(shas) == 1:
            _git(root, "checkout", "-q", "-b", "feature")
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "state"))
    view = json.dumps({"headRefOid": shas[2], "headRefName": "feature", "files": [{"path": "a.py"}, {}]})
    monkeypatch.setattr(cli, "_gh", lambda args, timeout=30: view if args[1] == "view" else "diff --git a/a.py\n")
    result = tmp_path / "state" / "review_gates" / "results" / "pr-5-glm_gate.json"
    result.parent.mkdir(parents=True)

    def seed(contract_hash=cli.current_contract_hash("glm_gate", 5)):
        result.write_text(json.dumps({
            "gate": "glm_gate", "pr_number": 5, "status": "pass", "contract_hash": contract_hash,
            "report_path": str(tmp_path / "r.md"), "dispatch_id": "glm-gate-pr5-1700000000", "commit_sha": shas[1]}))

    seed()
    argv = ["--pr", "5", "--gate", "glm_gate", "--project-root", str(root), "--base-ref", "main"]
    return types.SimpleNamespace(old=shas[1], new=shas[2], result=result, seed=seed, argv=argv,
                                 register=tmp_path / "state" / "dispatch_register.ndjson")


def test_reanchor_refused_and_allowed_write_nothing(env, capsys):
    before = env.result.read_text()
    assert cli.main(env.argv) == cli.EXIT_OK
    first, second = capsys.readouterr().out.splitlines()
    assert first.startswith("ALLOWED  #5 glm_gate: ")
    assert second == f"         old {env.old[:12]} -> new {env.new[:12]}; re-run with --apply to write it"
    assert cli.main(env.argv + ["--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["decision"]["allowed"] is True
    assert env.result.read_text() == before and not env.register.exists()
    env.seed("different")
    assert cli.main(env.argv) == cli.EXIT_REFUSED
    first, second = capsys.readouterr().out.splitlines()
    assert first.startswith("REFUSED  #5 glm_gate: ") and "contract hash changed" in first
    assert second == "         the gate has to run — this is not a saving that was available"
    assert cli.main(env.argv + ["--json"]) == cli.EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert sorted(payload) == ["applied", "decision", "gate", "pr"] and payload["applied"] is False


def test_reanchor_apply(env, capsys):
    assert cli.main(env.argv + ["--apply"]) == cli.EXIT_OK
    assert capsys.readouterr().out == (
        f"RE-ANCHORED  #5 glm_gate onto {env.new[:12]} (from {env.old[:12]}) -> {env.result}\n")
    record = json.loads(env.result.read_text())
    assert (record["commit_sha"], record["reanchored_from_commit_sha"]) == (env.new, env.old)
    assert record["evidence_source"] == "reanchored"
    assert [json.loads(x)["event"] for x in env.register.read_text().splitlines()] == ["gate_reanchored"]
    env.seed()
    assert cli.main(env.argv + ["--apply", "--json"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert (payload["applied"], payload["result_path"], payload["register_path"]) == (
        True, str(env.result), str(env.register))


def _disk_full(path, record):
    raise OSError("disk full")


def test_reanchor_failures(env, capsys, monkeypatch):
    env.result.parent.chmod(0o500)
    try:
        assert cli.main(env.argv + ["--apply"]) == cli.EXIT_WRITE_FAILED
    finally:
        env.result.parent.chmod(0o700)
    assert capsys.readouterr().err.startswith("gate-reanchor: the guarded write refused: ")
    assert not env.register.exists()
    monkeypatch.setattr(cli.state_writer, "append_locked", _disk_full)
    assert cli.main(env.argv + ["--apply"]) == cli.EXIT_WRITE_FAILED
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith(
        f"gate-reanchor: the record was written to {env.result} but the register line failed: disk full.")
    env.result.unlink()
    assert cli.main(env.argv) == cli.EXIT_BAD_INPUT
    assert capsys.readouterr().err.startswith("gate-reanchor: cannot establish the inputs: no existing glm_gate result")
    assert cli.main(["--pr", "5", "--gate", "codex_gate"]) == cli.EXIT_BAD_INPUT
    assert capsys.readouterr().err.startswith("gate-reanchor: codex_gate has no diff-derived contract hash.")
