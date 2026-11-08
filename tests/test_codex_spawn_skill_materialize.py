"""The native codex destination of the skill materializer and its input contract (skilldisc R4, ADR-059)."""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import agent_skill_materialize as asm
import envelope_adapters_claude as eac
import envelope_adapters_provider as eap
from envelope_types import EnvelopeSpec
from provider_spawns import codex_spawn

ROLE = "backend-developer"
IGNORE = ".vnx-agent-skills/\n.agents/\n"
DECLARED = {"alpha", "beta"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


def _make_worktree(tmp_path: Path, gitignore: str = IGNORE) -> Path:
    main = tmp_path / "main"
    main.mkdir(parents=True)
    _git(main, "init", "-q", "-b", "main")
    (main / ".gitignore").write_text(gitignore)
    for slug in sorted(DECLARED):
        (main / "skills" / slug).mkdir(parents=True)
        (main / "skills" / slug / "SKILL.md").write_text(f"{slug}\n")
    agent = main / "agents" / ROLE
    agent.mkdir(parents=True)
    (agent / "CLAUDE.md").write_text("role\n")
    (agent / "config.yaml").write_text("skills:\n  - alpha\n  - beta\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "dispatch/x", str(wt))
    return wt


@pytest.fixture
def worktree(tmp_path):
    return _make_worktree(tmp_path)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for key in ("VNX_AGENT_LAYOUT", "VNX_OVERRIDE_AGENT_LAYOUT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "dispatch_worktree_isolation.resolve_consumer_project_root", lambda: tmp_path / "main"
    )


def _call(wt, tmp_path, **over):
    args = dict(worktree=wt, provider="codex", mode="on", dispatch_id="dlv-r4",
                state_dir=tmp_path / "state", project_id="proj")
    args.update(over)
    return asm.materialize_skill_set(args.pop("role", ROLE), **args)


def _skills(wt) -> Path:
    return wt / ".agents" / "skills"


def _marker(wt, slug, **fields):
    data = {"schema": 1, "dispatch_id": "dlv-r4", "role": ROLE, "slug": slug, "sha256": "0" * 64}
    data.update(fields)
    (_skills(wt) / slug).mkdir(parents=True, exist_ok=True)
    (_skills(wt) / slug / ".vnx-managed.json").write_text(json.dumps(data))


# --- the three plan fixtures -------------------------------------------------------------------


def test_foreign_folder_stays_and_the_run_is_partial(tmp_path, worktree):
    (_skills(worktree) / "vreemd").mkdir(parents=True)
    (_skills(worktree) / "vreemd" / "SKILL.md").write_text("mine\n")
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:vreemd")
    assert set(os.listdir(_skills(worktree))) == DECLARED | {"vreemd"}
    assert (_skills(worktree) / "vreemd").is_dir()
    assert (_skills(worktree) / "vreemd" / "SKILL.md").read_text() == "mine\n"


def test_clean_worktree_gets_exactly_the_declared_set(tmp_path, worktree):
    result = _call(worktree, tmp_path)
    assert result["status"] == "ok" and result["reason"] is None
    assert set(os.listdir(_skills(worktree))) == DECLARED
    for slug in DECLARED:
        skill = _skills(worktree) / slug
        assert (skill / "SKILL.md").read_text() == f"{slug}\n"
        assert not os.path.islink(skill / "SKILL.md")
        marker = json.loads((skill / ".vnx-managed.json").read_text())
        assert set(marker) == {"schema", "dispatch_id", "role", "slug", "sha256"}
        assert (marker["dispatch_id"], marker["role"], marker["slug"]) == ("dlv-r4", ROLE, slug)
    assert [e["dest"] for e in result["skills"]] == [".agents/skills/alpha", ".agents/skills/beta"]
    assert _git(worktree, "status", "--porcelain") == ""
    record = json.loads(Path(result["record"]).read_text())
    assert record["provider"] == "codex" and record["status"] == "ok"


def test_project_root_is_refused(tmp_path, worktree, monkeypatch):
    monkeypatch.setattr("dispatch_worktree_isolation.resolve_consumer_project_root", lambda: worktree)
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("skipped", "path_outside_worktree")
    assert not (worktree / ".agents").exists()


# --- ownership ----------------------------------------------------------------------------------


def test_marker_of_another_dispatch_is_not_removed(tmp_path, worktree):
    _marker(worktree, "alpha", dispatch_id="dlv-other")
    (_skills(worktree) / "alpha" / "SKILL.md").write_text("theirs\n")
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    assert (_skills(worktree) / "alpha" / "SKILL.md").read_text() == "theirs\n"
    assert (_skills(worktree) / "beta" / "SKILL.md").read_text() == "beta\n"


@pytest.mark.parametrize("raw", ["{not json", "[]", json.dumps({"schema": 1, "dispatch_id": "dlv-r4", "slug": "x"}),
                                 json.dumps({"schema": 2, "dispatch_id": "dlv-r4", "slug": "old"}), "null", ""])
def test_unreadable_or_mismatching_marker_is_foreign(tmp_path, worktree, raw):
    (_skills(worktree) / "old").mkdir(parents=True)
    (_skills(worktree) / "old" / "SKILL.md").write_text("keep\n")
    (_skills(worktree) / "old" / ".vnx-managed.json").write_text(raw)
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:old")
    assert (_skills(worktree) / "old" / "SKILL.md").read_text() == "keep\n"


def test_own_marker_is_reclaimed_so_the_set_is_exact(tmp_path, worktree):
    _marker(worktree, "stale")
    (_skills(worktree) / "stale" / "SKILL.md").write_text("old\n")
    _marker(worktree, "alpha")
    (_skills(worktree) / "alpha" / "extra.txt").write_text("leftover\n")
    result = _call(worktree, tmp_path)
    assert result["status"] == "ok"
    assert set(os.listdir(_skills(worktree))) == DECLARED
    assert not (_skills(worktree) / "alpha" / "extra.txt").exists()
    assert (_skills(worktree) / "alpha" / "SKILL.md").read_text() == "alpha\n"


def test_a_second_attempt_of_the_same_dispatch_is_idempotent(tmp_path, worktree):
    first = _call(worktree, tmp_path)
    second = _call(worktree, tmp_path)
    assert (first["status"], second["status"]) == ("ok", "ok")
    assert set(os.listdir(_skills(worktree))) == DECLARED
    assert first["record"] != second["record"] and second["record"].endswith("codex.attempt-2.json")


def test_symlinked_destination_is_refused_without_removal(tmp_path, worktree):
    outside = tmp_path / "outside"
    (outside / "skills" / "keep").mkdir(parents=True)
    (outside / "skills" / "keep" / "SKILL.md").write_text("x\n")
    os.symlink(outside, worktree / ".agents")
    result = _call(worktree, tmp_path)
    assert result["status"] == "skipped"
    assert (outside / "skills" / "keep" / "SKILL.md").read_text() == "x\n"


def test_destination_that_git_does_not_ignore_gets_nothing(tmp_path):
    wt = _make_worktree(tmp_path / "t", ".vnx-agent-skills/\n")
    result = _call(wt, tmp_path)
    assert (result["status"], result["reason"]) == ("skipped", "not_ignored")
    assert not (wt / ".agents").exists()


@pytest.mark.parametrize("missing,reason", [("role", "no_role"), ("worktree", "no_cwd"), ("state_dir", "no_state_dir"),
                                            ("dispatch_id", "no_dispatch_id"), ("project_id", "no_project_id")])
@pytest.mark.parametrize("value", [None, ""])
def test_missing_field_is_skipped_and_writes_nothing(tmp_path, worktree, missing, reason, value):
    result = _call(worktree, tmp_path, **{missing: value})
    assert (result["status"], result["reason"]) == ("skipped", reason)
    assert not (worktree / ".agents").exists()


def test_function_reads_no_env(tmp_path, worktree):
    assert not re.search(r"os\.environ|getenv", inspect.getsource(asm))
    with mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True):
        result = _call(worktree, tmp_path)
    assert result["status"] == "ok"
    assert set(os.listdir(_skills(worktree))) == DECLARED


def test_off_and_unknown_provider_write_nothing(tmp_path, worktree):
    assert _call(worktree, tmp_path, mode="off")["reason"] == "layout_off"
    assert _call(worktree, tmp_path, provider="kimi")["reason"] == "provider_unsupported"
    assert not (worktree / ".agents").exists()


# --- the input contract on the four routes ---------------------------------------------------------


def test_spawn_codex_takes_three_keyword_only_parameters():
    params = inspect.signature(codex_spawn.spawn_codex).parameters
    for name in ("role", "state_dir", "project_id"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default is None


def test_spawn_codex_materializes_before_the_launch(tmp_path, worktree):
    seen = {}

    def _launch(*_a, **_k):
        seen["set"] = set(os.listdir(_skills(worktree)))
        return None, codex_spawn._launch_failure(1, "stop")

    with mock.patch("agent_layout.layout_mode", return_value="on"), \
         mock.patch.object(codex_spawn, "_resolve_launch_model", return_value=("m", None)), \
         mock.patch.object(codex_spawn, "_launch_codex_proc", side_effect=_launch):
        codex_spawn.spawn_codex("p", "m", "dlv-r4", "T1", cwd=worktree, role=ROLE,
                                state_dir=tmp_path / "state", project_id="proj")
    assert seen["set"] == DECLARED
    assert (tmp_path / "state" / "agent_skill_materializations" / "dlv-r4" / "codex.attempt-1.json").is_file()


def test_spawn_codex_does_nothing_when_the_layout_is_off(tmp_path, worktree):
    with mock.patch.object(codex_spawn, "_resolve_launch_model", return_value=("m", None)), \
         mock.patch.object(codex_spawn, "_launch_codex_proc",
                           return_value=(None, codex_spawn._launch_failure(1, "stop"))):
        codex_spawn.spawn_codex("p", "m", "dlv-r4", "T1", cwd=worktree, role=ROLE,
                                state_dir=tmp_path / "state", project_id="proj")
    assert not (worktree / ".agents").exists() and not (tmp_path / "state").exists()


def _fake_result():
    return SimpleNamespace(returncode=0, completion_text="", token_usage={}, session_id=None, error=None,
                           timed_out=False, event_writer_failures=0, stopped_early=False, events_written=0)


def test_envelope_codex_adapter_passes_the_five_fields(tmp_path, worktree):
    spec = EnvelopeSpec(dispatch_id="dlv-r4", terminal_id="T1", provider="codex", model="m", instruction="do it",
                        role=ROLE, pr_id=None, data_dir=tmp_path / "data", state_dir=tmp_path / "state")
    fake = mock.Mock(return_value=_fake_result())
    with mock.patch("provider_spawns.codex_spawn.spawn_codex", fake), \
         mock.patch("project_scope.resolve_stamp_project_id", return_value="proj"), \
         mock.patch("provider_switch.worker_env_stamps", return_value={}):
        eac.CodexAdapter().run(spec, cwd=worktree)
    kw = fake.call_args.kwargs
    assert (kw["cwd"], kw["role"], kw["state_dir"], kw["dispatch_id"], kw["project_id"]) == (
        worktree, ROLE, tmp_path / "state", "dlv-r4", "proj")


def test_provider_lane_run_codex_passes_the_five_fields(tmp_path, worktree):
    plan = SimpleNamespace(provider=SimpleNamespace(value="codex"), model="m", dispatch_id="dlv-r4",
                           target_id="T1", role=ROLE, project_id="proj")
    fake = mock.Mock(return_value=_fake_result())
    with mock.patch("provider_spawns.codex_spawn.spawn_codex", fake), \
         mock.patch("provider_dispatch._resolve_state_dir", return_value=tmp_path / "state"):
        eap.ProviderAdapter()._run_codex(plan, "do it", event_writer=None, cwd=worktree, role_env={})
    kw = fake.call_args.kwargs
    assert (kw["cwd"], kw["role"], kw["state_dir"], kw["dispatch_id"], kw["project_id"]) == (
        worktree, ROLE, tmp_path / "state", "dlv-r4", "proj")


def _dispatch_codex(tmp_path, worker_cwd):
    import provider_dispatch as pd

    args = Namespace(role=ROLE, dispatch_id="dlv-r4", terminal_id="T1")
    fake = mock.Mock(return_value=_fake_result())
    with mock.patch("provider_spawns.codex_spawn.spawn_codex", fake), \
         mock.patch("agent_layout.layout_mode", return_value="on"), \
         mock.patch("project_scope.resolve_stamp_project_id", return_value="proj"), \
         mock.patch.object(pd, "_resolve_state_dir", return_value=tmp_path / "state"), \
         mock.patch.object(pd, "_codex_event_store", return_value=mock.Mock()), \
         mock.patch.object(pd, "_codex_model_or_record_failure", return_value="m"), \
         mock.patch.object(pd, "_enrich_instruction", return_value="do it"), \
         mock.patch.object(pd, "_prepare_provider_workdir", return_value=(None, worker_cwd)), \
         mock.patch.object(pd, "_finish_provider_worktree"), mock.patch.object(pd, "_emit_governance"), \
         mock.patch.object(pd, "_event_store_safety_net"), mock.patch.object(pd, "_args_worker_env", return_value={}):
        pd._dispatch_codex(args)
    return fake.call_args.kwargs


def test_dispatch_codex_passes_the_five_fields(tmp_path, worktree):
    kw = _dispatch_codex(tmp_path, worktree)
    assert (kw["cwd"], kw["role"], kw["state_dir"], kw["dispatch_id"], kw["project_id"]) == (
        worktree, ROLE, tmp_path / "state", "dlv-r4", "proj")
    assert not (tmp_path / "state").exists()


def test_dispatch_codex_on_the_project_root_records_not_isolated(tmp_path):
    kw = _dispatch_codex(tmp_path, tmp_path / "main")
    assert kw["state_dir"] is None and kw["role"] == ROLE
    record = json.loads((tmp_path / "state" / "agent_skill_materializations" / "dlv-r4"
                         / "codex.attempt-1.json").read_text())
    assert (record["status"], record["reason"], record["provider"]) == ("skipped", "not_isolated", "codex")


def test_adapter_execute_without_a_worktree_is_skipped_no_cwd(tmp_path, worktree):
    from adapters.codex_adapter import CodexAdapter

    results = []
    real = asm.materialize_skill_set

    def spy(*args, **kwargs):
        results.append(real(*args, **kwargs))
        return results[-1]

    with mock.patch("agent_layout.layout_mode", return_value="on"), \
         mock.patch.object(codex_spawn, "_resolve_launch_model", return_value=("m", None)), \
         mock.patch.object(codex_spawn, "_launch_codex_proc",
                           return_value=(None, codex_spawn._launch_failure(1, "stop"))), \
         mock.patch.object(asm, "materialize_skill_set", spy):
        CodexAdapter("T1").execute("do it", {"role": ROLE, "dispatch_id": "dlv-r4"})
    assert [(r["status"], r["reason"]) for r in results] == [("skipped", "no_cwd")]
    assert not (worktree / ".agents").exists()
