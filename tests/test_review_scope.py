"""Deltareview DR-6 part 1: ``VNX_REVIEW_SCOPE`` reads fail-closed to ``full`` and ``delta`` is refused without evidence."""
from __future__ import annotations

import contextlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

import review_stack_support  # importing it puts scripts/ and scripts/lib on sys.path
import config_registry
import config_store_db
import gate_runner
import review_scope as rs
from gate_runner import GateRunner
from review_stack_support import VNX_ROOT

PROJECT, APPROVAL = "scope-project", "appr-scope"
STREAM = (VNX_ROOT / "tests" / "fixtures" / "gate_verdict" / "pr-1869-codex_gate-bare-verdict.ndjson").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    yield from review_stack_support.isolate_config_registry(monkeypatch)


@pytest.fixture
def store(tmp_path, monkeypatch):
    built = review_stack_support.build_project_store(tmp_path, monkeypatch)
    (built["project_root"] / ".vnx-project-id").write_text(PROJECT + "\n")
    monkeypatch.delenv("VNX_REVIEW_SCOPE", raising=False)
    built["db"] = built["state_dir"] / "runtime_coordination.db"
    return built


def _set(store, value, approval=APPROVAL, **kw):
    conn = sqlite3.connect(store["db"])
    try:
        return config_store_db.set_config(conn, PROJECT, rs.SCOPE_KEY, value, actor="test", approval_id=approval, **kw)
    finally:
        conn.close()


def _remove(store):
    conn = sqlite3.connect(store["db"])
    conn.execute("DELETE FROM project_config WHERE config_key = ?", (rs.SCOPE_KEY,))
    conn.commit()
    conn.close()


def _allow(monkeypatch, outcome="pass", dr7=True, ci=True):
    monkeypatch.setattr(rs, "measurement_outcome", lambda *a: outcome)
    monkeypatch.setattr(rs, "dr7_on_main", lambda root: dr7)
    monkeypatch.setattr(rs, "main_ci_green", lambda root: ci)


def test_the_key_is_registered_with_approval_and_an_empty_default():
    entry = config_registry.CONFIG_REGISTRY[rs.SCOPE_KEY]
    assert (entry.category, entry.requires_approval, entry.default) == ("gate", True, "")


def test_an_unset_key_reads_full(store):
    assert rs.active_review_scope(store["state_dir"]) == rs.ReviewScope("full", "unset")


@pytest.mark.parametrize("raw", ["", "  ", "everything", "DELTA_SHADOW", "delta,full"])
def test_an_empty_or_unknown_value_reads_full(store, monkeypatch, raw):
    monkeypatch.setenv(rs.SCOPE_KEY, raw)
    assert rs.active_review_scope(store["state_dir"]).value == "full"


def test_an_unreadable_store_reads_full(tmp_path):
    scope = rs.active_review_scope(tmp_path / "missing" / "state")
    assert scope.value == "full" and not scope.runs_shadow


def test_a_failing_reader_reads_full_and_names_why(store, monkeypatch):
    monkeypatch.setattr(config_registry, "get_for_project", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    assert rs.active_review_scope(store["state_dir"]) == rs.ReviewScope("full", "unreadable: OSError")


def test_delta_shadow_with_an_approval_id_is_read_as_such(store):
    _set(store, "delta_shadow")
    assert rs.active_review_scope(store["state_dir"]).value == "delta_shadow"


@pytest.mark.parametrize("approval", [None, "", "  "])
def test_a_delta_row_without_an_approval_id_reads_full(store, approval):
    conn = sqlite3.connect(store["db"])
    config_store_db.ensure_config_tables(conn)
    conn.execute("INSERT INTO project_config (project_id, config_key, config_value, config_type, updated_by, approval_id) "
                 "VALUES (?, ?, 'delta', 'string', 'test', ?)", (PROJECT, rs.SCOPE_KEY, approval))
    conn.commit()
    conn.close()
    assert rs.active_review_scope(store["state_dir"]) == rs.ReviewScope("full", "delta without an approval id on its row")


def test_a_delta_env_value_without_a_row_reads_full(store, monkeypatch):
    monkeypatch.setenv(rs.SCOPE_KEY, "delta")
    assert rs.active_review_scope(store["state_dir"]).value == "full"


def test_delta_with_an_approval_id_and_passed_evidence_reads_delta(store, monkeypatch):
    _allow(monkeypatch)
    _set(store, "delta", planted_manifest=Path("manifest.json"))
    assert rs.active_review_scope(store["state_dir"]).value == "delta"


def test_a_write_of_delta_without_an_approval_id_is_refused_before_any_check(store, monkeypatch):
    _allow(monkeypatch)
    with pytest.raises(PermissionError, match="approval_id"):
        _set(store, "delta", approval=None, planted_manifest=Path("m.json"))


def test_a_write_of_delta_without_a_manifest_is_refused(store, monkeypatch):
    _allow(monkeypatch)
    with pytest.raises(PermissionError, match="no planted manifest"):
        _set(store, "delta")
    assert rs.active_review_scope(store["state_dir"]).value == "full"


@pytest.mark.parametrize("kwargs, text", [
    ({"outcome": "fail"}, "DR-5 measurement not satisfied"),
    ({"outcome": "insufficient"}, "DR-5 measurement not satisfied"),
    ({"dr7": False}, "DR-7 merged"),
    ({"ci": False}, "concluded success not satisfied"),
])
def test_a_write_of_delta_is_refused_when_a_check_is_not_satisfied(store, monkeypatch, kwargs, text):
    _allow(monkeypatch, **kwargs)
    with pytest.raises(PermissionError, match=text):
        _set(store, "delta", planted_manifest=Path("m.json"))
    assert rs.active_review_scope(store["state_dir"]).value == "full"


def test_a_write_of_delta_is_refused_when_a_check_cannot_be_made(store, monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(rs, "main_ci_green", lambda root: (_ for _ in ()).throw(FileNotFoundError("gh")))
    with pytest.raises(PermissionError, match="could not be checked"):
        _set(store, "delta", planted_manifest=Path("m.json"))


def test_an_unreadable_manifest_refuses_the_real_measurement(store, monkeypatch):
    monkeypatch.setattr(rs, "dr7_on_main", lambda root: True)
    monkeypatch.setattr(rs, "main_ci_green", lambda root: True)
    with pytest.raises(PermissionError, match="DR-5 measurement could not be checked"):
        _set(store, "delta", planted_manifest=store["project_root"] / "nope.json")


@pytest.mark.parametrize("payload", ['[{"conclusion": "failure"}]', '[{"conclusion": ""}]', '[{"conclusion": null}]',
                                     '[{"conclusion": "cancelled"}]', "[]", "{}", '[{}]'])
def test_main_ci_is_green_only_on_success(monkeypatch, payload):
    monkeypatch.setattr(rs, "_run", lambda cmd, cwd: payload)
    assert rs.main_ci_green(Path(".")) is False
    monkeypatch.setattr(rs, "_run", lambda cmd, cwd: '[{"conclusion": "success"}]')
    assert rs.main_ci_green(Path(".")) is True


WIRED = ("def merge_pr(n):\n    rec = _merge_tree_record(n)\n    _emit_receipt(n, merge_tree=rec)\n"
         "def _merge_tree_record(n):\n    return {}\n")
TEST_OK = "def test_six_questions_answerable_from_receipt_and_rounds():\n    pass\n"


def _main_repo(tmp_path, files):
    root = tmp_path / "repo"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    for cmd in ("init -q -b main", "add .", "commit -qm x", "update-ref refs/remotes/origin/main HEAD"):
        subprocess.run([*git, *cmd.split()], cwd=root, check=True)
    return root


@pytest.mark.parametrize("files", [
    {"tests/unimplemented.txt": "TODO: reconstruct pr_merged from merged_tree; implementation and test missing."},
    {rs.DR7_IMPL_FILE: "def merge_pr(n):\n    pass\ndef _merge_tree_record(n):\n    return {}\n", rs.DR7_TEST_FILE: TEST_OK},
    {rs.DR7_IMPL_FILE: WIRED, rs.DR7_TEST_FILE: "def test_other():\n    pass\n"},
    {rs.DR7_IMPL_FILE: WIRED, "tests/test_elsewhere.py": TEST_OK},
    {rs.DR7_IMPL_FILE: WIRED.replace("merge_tree=rec", "x=rec"), rs.DR7_TEST_FILE: TEST_OK},
    {rs.DR7_IMPL_FILE: "# merge_pr calls _merge_tree_record( and _emit_receipt(merge_tree=\n", rs.DR7_TEST_FILE: TEST_OK},
])
def test_dr7_is_not_on_main_without_all_of_its_evidence(tmp_path, files):
    root = _main_repo(tmp_path, files)
    with contextlib.suppress(RuntimeError):  # a file absent on main cannot be read: the check refuses
        assert rs.dr7_on_main(root) is False


def test_dr7_is_on_main_with_the_call_the_keyword_and_the_test(tmp_path):
    assert rs.dr7_on_main(_main_repo(tmp_path, {rs.DR7_IMPL_FILE: WIRED, rs.DR7_TEST_FILE: TEST_OK})) is True


def test_the_planted_manifest_is_read_from_main_not_from_the_worktree(tmp_path):
    manifest = '{"schema": "delta_planted/1", "cases": [{"head": "abc"}]}'
    root = _main_repo(tmp_path, {"m.json": manifest})
    assert rs._manifest_on_main(Path("m.json"), root) == [{"head": "abc"}]
    (root / "fresh.json").write_text(manifest)  # written after main, so not on main
    with pytest.raises(RuntimeError):
        rs._manifest_on_main(Path("fresh.json"), root)
    with pytest.raises(PermissionError, match="outside the project"):
        rs._manifest_on_main(tmp_path / "elsewhere.json", root)


def test_full_and_delta_shadow_need_no_measurement(store):
    _set(store, "delta_shadow")
    _set(store, "full")
    assert rs.active_review_scope(store["state_dir"]).value == "full"


def _fire(store, monkeypatch):
    calls = []
    monkeypatch.setattr(gate_runner, "create_gate_worktree", lambda **kw: store["data_dir"] / "_wt")
    monkeypatch.setattr(gate_runner, "remove_gate_worktree", lambda *a, **kw: None)
    monkeypatch.setattr(GateRunner, "_run_with_stall_detection", lambda self, **kw: {
        "status": "completed", "stdout": STREAM, "stderr": "", "duration_seconds": 1.0, "partial_output_lines": 1,
        "runner_pid": 1, "returncode": 0})
    monkeypatch.setattr(gate_runner.review_delta_shadow, "run_shadow", lambda **kw: calls.append(kw))
    payload = {"gate": "codex_gate", "status": "requested", "provider": "codex", "branch": "dispatch/test", "pr_number": 7,
               "pr_id": "PR-7", "commit_sha": "abc1234", "review_mode": "per_pr", "risk_class": "medium",
               "changed_files": ["scripts/lib/x.py"], "requested_at": "2026-11-12T10:00:00Z",
               "report_path": str(store["reports_dir"] / "pr-7-codex_gate.md"), "prompt": "Review",
               "codex_launch": {"model": "gpt-6-astra", "reasoning_effort": "medium"}}
    GateRunner(state_dir=store["state_dir"], reports_dir=store["reports_dir"])._run_subprocess_path(
        gate="codex_gate", binary="codex", prompt="Review", pr_number=7, pr_id="PR-7", request_payload=payload)
    return calls


def test_the_shadow_does_not_run_under_full_and_runs_under_delta_shadow_and_stops_on_rollback(store, monkeypatch):
    assert _fire(store, monkeypatch) == []
    _set(store, "delta_shadow")
    assert len(_fire(store, monkeypatch)) == 1
    _set(store, "full")
    assert _fire(store, monkeypatch) == []
    _set(store, "delta_shadow")
    _remove(store)
    assert _fire(store, monkeypatch) == []
