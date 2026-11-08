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


# --- the slug folder is claimed atomically, an existing folder is never adopted ------------------


def _foreign_folder(wt, slug="alpha", skill_md=True):
    folder = _skills(wt) / slug
    folder.mkdir(parents=True, exist_ok=True)
    if skill_md:
        (folder / "SKILL.md").write_text("theirs\n")
    (folder / "user-data.txt").write_text("precious\n")


def _no_marker(wt, slug="alpha"):
    assert not (_skills(wt) / slug / ".vnx-managed.json").exists()


def _assert_foreign_intact(wt, slug="alpha"):
    assert (_skills(wt) / slug / "SKILL.md").read_bytes() == b"theirs\n"
    assert (_skills(wt) / slug / "user-data.txt").read_bytes() == b"precious\n"


def test_folder_created_between_the_check_and_the_claim_is_never_adopted(tmp_path, worktree):
    real = asm._materialize_one

    def racing(wt, slug, src, *args, **kwargs):
        if slug == "alpha":
            _foreign_folder(wt)
        return real(wt, slug, src, *args, **kwargs)

    with mock.patch.object(asm, "_materialize_one", racing):
        first = _call(worktree, tmp_path)
    assert (first["status"], first["reason"]) == ("partial", "not_owned:alpha")
    _no_marker(worktree)
    _assert_foreign_intact(worktree)
    second = _call(worktree, tmp_path)
    assert (second["status"], second["reason"]) == ("partial", "not_owned:alpha")
    _no_marker(worktree)
    _assert_foreign_intact(worktree)
    assert (_skills(worktree) / "beta" / "SKILL.md").read_text() == "beta\n"


def test_folder_that_exists_before_the_run_gets_no_marker_and_no_file(tmp_path, worktree):
    _foreign_folder(worktree)
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    _no_marker(worktree)
    _assert_foreign_intact(worktree)


def test_nested_folder_created_after_the_claim_refuses_the_slug_and_takes_the_marker_back(tmp_path, worktree):
    (worktree / "skills" / "alpha" / "refs").mkdir()
    (worktree / "skills" / "alpha" / "refs" / "a.md").write_text("ref\n")
    real_mkdir = os.mkdir

    def racing(path, mode=0o777, *, dir_fd=None):
        if path == "refs" and dir_fd is not None:
            real_mkdir(path, mode, dir_fd=dir_fd)
            (_skills(worktree) / "alpha" / "refs" / "theirs.txt").write_text("precious\n")
        return real_mkdir(path, mode, dir_fd=dir_fd)

    with mock.patch.object(asm.os, "mkdir", racing):
        result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    _no_marker(worktree)
    assert (_skills(worktree) / "alpha" / "refs" / "theirs.txt").read_text() == "precious\n"
    assert not (_skills(worktree) / "alpha" / "SKILL.md").exists()


def test_copy_failing_after_the_claim_keeps_the_marker_and_the_next_attempt_reclaims(tmp_path, worktree):
    real = asm._publish_file

    def failing(dir_fd, name, data, mode):
        if name == "SKILL.md" and data == b"alpha\n":
            raise PermissionError("no")
        return real(dir_fd, name, data, mode)

    with mock.patch.object(asm, "_publish_file", failing):
        first = _call(worktree, tmp_path)
    assert (first["status"], first["reason"]) == ("partial", "copy_failed:PermissionError")
    marker = json.loads((_skills(worktree) / "alpha" / ".vnx-managed.json").read_text())
    assert (marker["dispatch_id"], marker["slug"]) == ("dlv-r4", "alpha")
    assert not (_skills(worktree) / "alpha" / "SKILL.md").exists()
    second = _call(worktree, tmp_path)
    assert (second["status"], second["reason"]) == ("ok", None)
    assert (_skills(worktree) / "alpha" / "SKILL.md").read_text() == "alpha\n"


def test_marker_that_cannot_be_published_leaves_no_claimed_folder(tmp_path, worktree):
    real = asm._publish_file

    def failing(dir_fd, name, data, mode):
        if name == ".vnx-managed.json" and b'"alpha"' in data:
            raise PermissionError("no")
        return real(dir_fd, name, data, mode)

    with mock.patch.object(asm, "_publish_file", failing):
        result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "copy_failed:PermissionError")
    assert not (_skills(worktree) / "alpha").exists()


def test_marker_that_is_a_symlink_is_never_reclaimed(tmp_path, worktree):
    _foreign_folder(worktree)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"schema": 1, "dispatch_id": "dlv-r4", "role": ROLE, "slug": "alpha"}))
    (_skills(worktree) / "alpha" / ".vnx-managed.json").symlink_to(elsewhere)
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    _assert_foreign_intact(worktree)


def test_marker_that_is_a_hard_link_is_never_reclaimed(tmp_path, worktree):
    _foreign_folder(worktree)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"schema": 1, "dispatch_id": "dlv-r4", "role": ROLE, "slug": "alpha"}))
    os.link(elsewhere, _skills(worktree) / "alpha" / ".vnx-managed.json")
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    _assert_foreign_intact(worktree)


def test_marker_that_is_a_directory_is_never_reclaimed(tmp_path, worktree):
    _foreign_folder(worktree)
    (_skills(worktree) / "alpha" / ".vnx-managed.json").mkdir()
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    _assert_foreign_intact(worktree)


def test_marker_carrying_another_slug_is_never_reclaimed(tmp_path, worktree):
    _foreign_folder(worktree)
    (_skills(worktree) / "alpha" / ".vnx-managed.json").write_text(
        json.dumps({"schema": 1, "dispatch_id": "dlv-r4", "role": ROLE, "slug": "beta"}))
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    _assert_foreign_intact(worktree)


# --- r3: ownership is checked and acted on through one directory fd ------------------------------


def _owned_alpha(wt):
    """A folder of an earlier attempt of this dispatch: marked for ``dlv-r4``, so the reclaim removes it."""
    _marker(wt, "alpha")
    (_skills(wt) / "alpha" / "SKILL.md").write_text("stale\n")


def _outside_folder(tmp_path, name="outside-alpha"):
    outside = tmp_path / name
    outside.mkdir()
    (outside / "SKILL.md").write_text("theirs\n")
    (outside / "user-data.txt").write_text("precious\n")
    return outside


def _assert_untouched(folder, names=("SKILL.md", "user-data.txt")):
    """The foreign data first, so a run that deleted it fails on the deleted file itself."""
    assert (folder / "user-data.txt").read_bytes() == b"precious\n"
    if "SKILL.md" in names:
        assert (folder / "SKILL.md").read_bytes() == b"theirs\n"
    assert sorted(os.listdir(folder)) == sorted(names)


def _swap_after_ownership_check(swap, slug="alpha"):
    """Run *swap* right after *slug*'s marker is parsed: the window between the ownership check and the removal.

    The seam is the marker's ``json.loads``. The pathname check of round 2 and the fd-bound check
    both call it at the decision, so the same test runs against either implementation.
    """
    real = json.loads
    fired: list[str] = []

    def loads(data, *args, **kwargs):
        out = real(data, *args, **kwargs)
        if not fired and isinstance(out, dict) and out.get("slug") == slug and out.get("dispatch_id") == "dlv-r4":
            fired.append(slug)
            swap()
        return out

    return mock.patch.object(asm.json, "loads", loads), fired


def test_folder_replaced_after_the_ownership_check_is_never_removed(tmp_path, worktree):
    _owned_alpha(worktree)
    moved = tmp_path / "moved-alpha"

    def swap():
        (_skills(worktree) / "alpha").rename(moved)
        _foreign_folder(worktree)

    patch, fired = _swap_after_ownership_check(swap)
    with patch:
        result = _call(worktree, tmp_path)
    _assert_untouched(_skills(worktree) / "alpha")
    assert fired == ["alpha"]
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    assert os.listdir(moved) == []  # the folder that was checked is the folder that was emptied
    assert (_skills(worktree) / "beta" / "SKILL.md").read_text() == "beta\n"


def test_folder_replaced_by_a_link_after_the_ownership_check_is_never_followed(tmp_path, worktree):
    _owned_alpha(worktree)
    outside = _outside_folder(tmp_path)

    def swap():
        (_skills(worktree) / "alpha").rename(tmp_path / "moved-alpha")
        (_skills(worktree) / "alpha").symlink_to(outside, target_is_directory=True)

    patch, fired = _swap_after_ownership_check(swap)
    with patch:
        result = _call(worktree, tmp_path)
    _assert_untouched(outside)
    assert fired == ["alpha"]
    assert os.path.islink(_skills(worktree) / "alpha")
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    assert (_skills(worktree) / "beta" / "SKILL.md").read_text() == "beta\n"


@pytest.mark.parametrize("with_files", [False, True], ids=["empty-replacement", "replacement-with-data"])
def test_folder_replaced_after_it_was_emptied_is_never_removed(tmp_path, worktree, with_files):
    _owned_alpha(worktree)
    real = asm._unlink_if_same
    seen: dict[str, int] = {}

    def unlink_then_swap(dir_fd, name, ident):
        out = real(dir_fd, name, ident)
        if name == asm.MARKER_NAME and not seen:  # the marker goes last, right before the rmdir by name
            (_skills(worktree) / "alpha").rename(tmp_path / "moved-alpha")
            if with_files:
                _foreign_folder(worktree)
            else:
                (_skills(worktree) / "alpha").mkdir()
            seen["ino"] = (_skills(worktree) / "alpha").stat().st_ino
        return out

    with mock.patch.object(asm, "_unlink_if_same", unlink_then_swap):
        result = _call(worktree, tmp_path)
    assert (_skills(worktree) / "alpha").stat().st_ino == seen["ino"]
    if with_files:
        _assert_untouched(_skills(worktree) / "alpha")
    else:
        assert os.listdir(_skills(worktree) / "alpha") == []
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")


def test_nested_folder_replaced_by_a_link_before_the_reclaim_reaches_it_is_unlinked_not_followed(tmp_path, worktree):
    _owned_alpha(worktree)
    nested = _skills(worktree) / "alpha" / "refs"
    nested.mkdir()
    (nested / "old.md").write_text("old\n")
    outside = _outside_folder(tmp_path)
    real = asm._remove_entry
    fired: list[str] = []

    def swap_then_remove(parent_fd, name):
        if name == "refs" and not fired:
            fired.append(name)
            nested.rename(tmp_path / "moved-refs")
            nested.symlink_to(outside, target_is_directory=True)
        return real(parent_fd, name)

    with mock.patch.object(asm, "_remove_entry", swap_then_remove):
        result = _call(worktree, tmp_path)
    _assert_untouched(outside)
    assert fired == ["refs"]
    assert (result["status"], result["reason"]) == ("ok", None)
    assert sorted(os.listdir(_skills(worktree) / "alpha")) == [".vnx-managed.json", "SKILL.md"]


def test_nested_folder_replaced_by_a_link_after_its_lstat_is_never_followed(tmp_path, worktree):
    _owned_alpha(worktree)
    nested = _skills(worktree) / "alpha" / "refs"
    nested.mkdir()
    outside = _outside_folder(tmp_path)
    real_open = os.open
    fired: list[str] = []

    def swap_then_open(path, flags, *args, **kwargs):
        if path == "refs" and flags == asm.DIR_FLAGS and not fired:
            fired.append(path)
            nested.rename(tmp_path / "moved-refs")
            nested.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    with mock.patch.object(asm.os, "open", swap_then_open):
        result = _call(worktree, tmp_path)
    _assert_untouched(outside)
    assert fired == ["refs"]
    assert os.path.islink(nested)
    assert (result["status"], result["reason"]) == ("partial", "not_owned:alpha")
    _no_marker(worktree)


def test_unswapped_owned_folder_is_fully_reclaimed_and_its_links_are_never_followed(tmp_path, worktree):
    _owned_alpha(worktree)
    alpha = _skills(worktree) / "alpha"
    (alpha / "a" / "b").mkdir(parents=True)
    (alpha / "a" / "b" / "deep.txt").write_text("deep\n")
    outside = _outside_folder(tmp_path)
    (alpha / "dirlink").symlink_to(outside, target_is_directory=True)
    (alpha / "filelink").symlink_to(outside / "user-data.txt")
    os.mkfifo(alpha / "pipe")
    (alpha / (".SKILL.md.vnx-partial-" + "0" * 32)).write_bytes(b"al")
    result = _call(worktree, tmp_path)
    assert (result["status"], result["reason"]) == ("ok", None)
    _assert_untouched(outside)
    assert set(os.listdir(_skills(worktree))) == DECLARED
    assert sorted(os.listdir(alpha)) == [".vnx-managed.json", "SKILL.md"]
    assert (alpha / "SKILL.md").read_text() == "alpha\n"


@pytest.mark.parametrize("skill_md", [True, False], ids=["with-skill-md", "without-skill-md"])
def test_folder_swapped_in_between_the_claim_and_its_open_is_never_entered(tmp_path, worktree, skill_md):
    real_mkdir = os.mkdir
    fired: list[str] = []
    names = ("SKILL.md", "user-data.txt") if skill_md else ("user-data.txt",)

    def racing(path, mode=0o777, *, dir_fd=None):
        out = real_mkdir(path, mode, dir_fd=dir_fd)
        if path == "alpha" and dir_fd is not None and not fired:
            fired.append(path)
            (_skills(worktree) / "alpha").rename(tmp_path / "moved-alpha")
            _foreign_folder(worktree, skill_md=skill_md)
        return out

    with mock.patch.object(asm.os, "mkdir", racing):
        first = _call(worktree, tmp_path)
    _assert_untouched(_skills(worktree) / "alpha", names)
    assert fired == ["alpha"]
    assert (first["status"], first["reason"]) == ("partial", "not_owned:alpha")
    second = _call(worktree, tmp_path)
    _assert_untouched(_skills(worktree) / "alpha", names)
    assert (second["status"], second["reason"]) == ("partial", "not_owned:alpha")


def test_nested_folder_swapped_between_two_files_is_never_written_into(tmp_path, worktree):
    refs = worktree / "skills" / "alpha" / "refs"
    refs.mkdir()
    (refs / "a.md").write_text("a\n")
    (refs / "b.md").write_text("b\n")
    dest = _skills(worktree) / "alpha" / "refs"
    real = asm._publish_file
    fired: list[str] = []

    def swap_after_a(dir_fd, name, data, mode):
        out = real(dir_fd, name, data, mode)
        if name == "a.md" and not fired:
            fired.append(name)
            dest.rename(tmp_path / "moved-refs")
            dest.mkdir()
            (dest / "user-data.txt").write_text("precious\n")
        return out

    with mock.patch.object(asm, "_publish_file", swap_after_a):
        first = _call(worktree, tmp_path)
    _assert_untouched(dest, ("user-data.txt",))
    assert fired == ["a.md"]
    assert (first["status"], first["reason"]) == ("partial", "not_owned:alpha")
    _no_marker(worktree)
    assert not (_skills(worktree) / "alpha" / "SKILL.md").exists()
    assert os.listdir(tmp_path / "moved-refs") == []  # what the copy wrote went to its own folder and was taken back
    second = _call(worktree, tmp_path)
    _assert_untouched(dest, ("user-data.txt",))
    assert (second["status"], second["reason"]) == ("partial", "not_owned:alpha")
