"""Characterisation of ``remove_dispatch_worktree`` and ``ProviderAdapter.run`` (golf schoon R34).

Written green on the unsplit code; the extract-method commit must keep it green.
Real temp git repos and claim registry; only provider spawns (external processes) are patched.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_worktree_isolation as dwi  # noqa: E402
from dispatch_spec import Provider  # noqa: E402
from envelope_adapters_provider import ProviderAdapter  # noqa: E402
from provider_dispatch import _build_lane_key  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)], check=True, capture_output=True)
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare), str(local)], check=True, capture_output=True)
    (local / "README.md").write_text("init\n")
    for args in (("checkout", "-b", "main"), ("config", "user.email", "t@t.local"), ("config", "user.name", "T"),
                 ("add", "README.md"), ("commit", "-m", "initial"), ("push", "-u", "origin", "main")):
        _git(local, *args)
    data_dir = tmp_path / "vnx-data"
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    monkeypatch.delenv("VNX_BENCH_WORKTREE_ROOT", raising=False)
    monkeypatch.setattr(dwi, "_resolve_fabric_version_marker", lambda: "1.9.0")
    return SimpleNamespace(local=local, data_dir=data_dir)


def _claim(repo, did: str) -> Path:
    return dwi.claims_dir_for_state_dir(repo.data_dir.resolve() / "state") / f"{dwi._sanitize_dispatch_id(did)}.json"


def _messages(caplog) -> list:
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "dispatch_worktree_isolation"]


def _teardown(repo, caplog, did: str, **kw):
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="dispatch_worktree_isolation"):
        return dwi.remove_dispatch_worktree(did, project_root=repo.local, data_dir=repo.data_dir, **kw)


def test_absent_worktree_returns_none(repo, caplog):
    assert _teardown(repo, caplog, "r34-absent") is None
    assert _messages(caplog)[-1][1].startswith("remove_dispatch_worktree: already absent:")


def test_clean_teardown_removes_tree_claim_and_emits_event(repo, caplog):
    wt = dwi.create_dispatch_worktree("r34-clean", project_root=repo.local)
    assert _teardown(repo, caplog, "r34-clean", terminal_id="T9") == "clean"
    assert not wt.exists() and not _claim(repo, "r34-clean").exists()
    infos = [m for lvl, m in _messages(caplog) if lvl == "INFO"]
    assert infos[0].startswith("remove_dispatch_worktree: classified r34-clean as clean (branch=dispatch/r34-clean base_sha=")
    assert infos[-1] == "remove_dispatch_worktree: emitted provider_teardown_worktree state=clean for r34-clean"
    assert not [m for lvl, m in _messages(caplog) if lvl == "WARNING"]
    events = [json.loads(x) for x in (repo.data_dir / "events" / "T9.ndjson").read_text().splitlines()]
    assert [e["type"] for e in events] == ["provider_teardown_worktree"]
    assert events[0]["data"]["fabric_version_at_create"] == "1.9.0"


def test_head_jump_and_fabric_drift_are_warned(repo, caplog, monkeypatch):
    dwi.create_dispatch_worktree("r34-jump", project_root=repo.local)
    before = _git(repo.local, "rev-parse", "HEAD").strip()
    (repo.local / "x.txt").write_text("x\n")
    _git(repo.local, "add", "x.txt")
    _git(repo.local, "commit", "-m", "jump")
    after = _git(repo.local, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(dwi, "_resolve_fabric_version_marker", lambda: "2.0.0")
    assert _teardown(repo, caplog, "r34-jump") == "clean"
    warnings = [m for lvl, m in _messages(caplog) if lvl == "WARNING"]
    assert warnings[0] == (
        "OI-975 HEAD-JUMP DETECTED: main checkout HEAD changed during dispatch r34-jump "
        f"(was {before[:12]}, now {after[:12]}) — the checkout no longer matches what this dispatch "
        "was launched against, so any post-dispatch verification on this checkout may measure "
        "different code than the dispatch actually built"
    )
    assert warnings[1].startswith("OI-1149/OI-1061 FABRIC VERSION DRIFT: dispatch r34-jump was created against fabric '1.9.0' but the fabric is now '2.0.0'")
    assert len(warnings) == 2


def test_review_only_discards_staged_reproduction(repo, caplog):
    wt = dwi.create_dispatch_worktree("r34-review", project_root=repo.local)
    base = _git(wt, "rev-parse", "HEAD").strip()
    (wt / "pr_file.py").write_text("print(1)\n")
    _git(wt, "add", "pr_file.py")
    assert _teardown(repo, caplog, "r34-review", review_only=True) == "clean"
    assert not wt.exists()
    assert ("INFO", f"remove_dispatch_worktree: review_only reset r34-review to base {base[:8]} "
            "(no local commits — disposable PR reproduction discarded)") in _messages(caplog)


def test_review_only_with_local_commit_keeps_the_work(repo, caplog):
    wt = dwi.create_dispatch_worktree("r34-review-c", project_root=repo.local)
    base = _git(wt, "rev-parse", "HEAD").strip()
    (wt / "work.py").write_text("w\n")
    _git(wt, "add", "work.py")
    _git(wt, "-c", "user.email=t@t.local", "-c", "user.name=T", "commit", "-m", "work")
    head = _git(wt, "rev-parse", "HEAD").strip()
    assert _teardown(repo, caplog, "r34-review-c", review_only=True) == "dirty"
    assert (wt / "work.py").exists() and _claim(repo, "r34-review-c").exists()
    assert ("INFO", f"remove_dispatch_worktree: review_only skip reset for r34-review-c (HEAD={head[:8]} "
            f"base={base[:8]}) — local commits present, degrading to normal classification so real "
            "work is never discarded") in _messages(caplog)


def test_pre_l3_claim_with_unresolvable_base_ref_warns_fail_closed(repo, caplog):
    wt = dwi.create_dispatch_worktree("r34-prel3", project_root=repo.local)
    claim_path = _claim(repo, "r34-prel3")
    claim = json.loads(claim_path.read_text())
    claim.update(base_sha="", base_ref="origin/no-such-ref")
    claim_path.write_text(json.dumps(claim) + "\n")
    result = _teardown(repo, caplog, "r34-prel3")
    assert ("WARNING", "remove_dispatch_worktree: cannot resolve base_ref 'origin/no-such-ref' for r34-prel3; "
            "treating as dirty (fail-closed)") in _messages(caplog)
    assert result == "committed" and not wt.exists()


class _Spawn:
    returncode = 0
    error = None
    timed_out = False
    stopped_early = False
    completion_text = "done"
    event_writer_failures = 0
    token_usage = {"input_tokens": 1, "output_tokens": 2}


def _plan(provider: str, model: str = "default") -> SimpleNamespace:
    return SimpleNamespace(provider=Provider(provider), model=model, dispatch_id="r34-d", target_id="T1",
                           deadline_seconds=900, task_class=None, role="backend-developer", project_id="p1")


@pytest.mark.parametrize("provider,resolver,model", [
    ("litellm:deepseek", "_resolve_deepseek_model", "ds-m"),
    ("litellm:zai", "_resolve_zai_model", "zai-m"),
    ("litellm:moonshot", "_resolve_moonshot_model", "moon-m"),
])
def test_litellm_branch_resolves_model_and_lane(provider, resolver, model):
    with patch(f"provider_dispatch.{resolver}", return_value=model) as res, \
         patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=_Spawn()) as spawn:
        out = ProviderAdapter().run(_plan(provider), "do it", cwd=Path("/tmp"))
    assert res.called
    kw = spawn.call_args.kwargs
    sub = provider.split(":")[1]
    assert (kw["model"], kw["sub_provider"], kw["lane"]) == (model, sub, _build_lane_key(sub, None))
    assert (kw["prompt"], kw["dispatch_id"], kw["terminal_id"], kw["cwd"]) == ("do it", "r34-d", "T1", Path("/tmp"))
    assert (out.status, out.returncode, out.model, out.completion_text) == ("success", 0, model, "done")


@pytest.mark.parametrize("provider,spawn_path,label", [
    ("litellm:deepseek", "provider_spawns.litellm_spawn.spawn_litellm", "litellm"),
    ("gemini", "provider_spawns.gemini_spawn.spawn_gemini", "gemini"),
])
def test_broken_pipe_is_a_structured_failure(provider, spawn_path, label):
    with patch(spawn_path, side_effect=BrokenPipeError("gone")):
        out = ProviderAdapter().run(_plan(provider, model="m1"), "x")
    assert (out.status, out.returncode, out.error) == ("failure", 1, f"{label} spawn BrokenPipeError: gone")


def test_gemini_model_ignores_the_deleted_variable(monkeypatch):
    monkeypatch.setenv("VNX_GEMINI_MODEL", "gemini-x")
    with patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=_Spawn()) as spawn:
        out = ProviderAdapter().run(_plan("gemini", model="sonnet"), "x")
    assert spawn.call_args.kwargs["model"] == "gemini-2.5-pro" and out.model == "gemini-2.5-pro"


@pytest.mark.parametrize("attrs,expected", [
    ({"returncode": 2, "error": "boom"}, ("failure", 2, "boom", False)),
    ({"returncode": 0, "timed_out": True}, ("timeout", 0, None, True)),
    ({}, ("success", 0, None, False)),
])
def test_local_gemma_outcomes(attrs, expected):
    res = _Spawn()
    for k, v in attrs.items():
        setattr(res, k, v)
    with patch("provider_spawns.local_gemma_spawn.spawn_local_gemma", return_value=res) as spawn:
        out = ProviderAdapter().run(_plan("local-gemma"), "inst")
    assert spawn.call_args.kwargs == {
        "instruction": "inst", "model": spawn.call_args.kwargs["model"], "role": "backend-developer",
        "deadline_seconds": 300, "dispatch_id": "r34-d", "project_id": "p1",
    }
    assert (out.status, out.returncode, out.error, out.timed_out) == expected
    assert out.model == spawn.call_args.kwargs["model"]


def test_unsupported_provider_raises_value_error():
    with pytest.raises(ValueError, match="ProviderAdapter: unsupported provider"):
        ProviderAdapter().run(_plan("claude"), "x")
