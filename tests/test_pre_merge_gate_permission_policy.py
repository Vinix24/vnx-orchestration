"""Permissions P7: the merge gate checks the spec's policy digest against the merge base.

The spec is found by Dispatch-ID; the digest is recomputed from the role sources as they are
at the merge base (never the working tree); the outcome always lands in the check list and
blocks only under worker-permission enforcement.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import pre_merge_gate as gate
from worker_permissions import resolve_worker_profile

ROLE = "backend-developer"
DISPATCH_ID = "dlv-p7test000001"
YAML_PATH = ".vnx/worker_permissions.yaml"


def _profiles(write_scope: str = "scripts/**") -> str:
    return (
        "version: 1\nprofiles:\n"
        f"  {ROLE}:\n"
        "    allowed_tools: [Read, Write, Edit, Bash]\n"
        "    denied_tools: [WebSearch]\n"
        f"    file_write_scope: ['{write_scope}']\n"
    )


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                         env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                              "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
                              "HOME": str(repo)})
    return out.stdout.strip()


def _write_sources(repo: Path, profiles: str) -> None:
    (repo / ".vnx").mkdir(exist_ok=True)
    (repo / YAML_PATH).write_text(profiles)
    (repo / "agents" / ROLE).mkdir(parents=True, exist_ok=True)
    (repo / "agents" / ROLE / "CLAUDE.md").write_text("# role\n")
    (repo / "agents" / ROLE / "config.yaml").write_text(
        "isolation:\n  scope_type: coding_worktree\n  allowed_paths: ['.']\n")


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_ENFORCE_WORKER_PERMISSIONS", raising=False)
    monkeypatch.delenv("VNX_WORKER_ENFORCEMENT_SKIP", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write_sources(root, _profiles())
    root_sha = _commit(root, "base")
    return root, root_sha


def _digest_of(repo: Path, profiles: str) -> str:
    """Digest of the same sources, composed via the one real composition path."""
    scratch = repo.parent / "digest-scratch"
    (scratch / ".vnx").mkdir(parents=True, exist_ok=True)
    (scratch / YAML_PATH).write_text(profiles)
    (scratch / "agents" / ROLE).mkdir(parents=True, exist_ok=True)
    (scratch / "agents" / ROLE / "CLAUDE.md").write_text("# role\n")
    (scratch / "agents" / ROLE / "config.yaml").write_text(
        "isolation:\n  scope_type: coding_worktree\n  allowed_paths: ['.']\n")
    return resolve_worker_profile(ROLE, yaml_path=scratch / YAML_PATH,
                                  agents_dir=scratch / "agents").policy_digest


def _spec(dispatch_dir: Path, digest, *, bucket: str = "pending", role=ROLE) -> Path:
    d = dispatch_dir / bucket / DISPATCH_ID
    d.mkdir(parents=True)
    body = {"dispatch_id": DISPATCH_ID, "role": role, "permission_policy_digest": digest}
    (d / "dispatch-spec.json").write_text(json.dumps(body))
    return d / "dispatch-spec.json"


def _pr_head(merge_base: str, branch: str = f"dispatch/{DISPATCH_ID}") -> "gate.ResolvedPRRef":
    return gate.ResolvedPRRef("#1", merge_base, branch, merge_base)


def _run(monkeypatch, repo_root: Path, dispatch_dir: Path, merge_base: str, branch: str = f"dispatch/{DISPATCH_ID}") -> dict:
    """The real run_gate_checks with every other check stubbed to GO."""
    go = lambda name: (lambda *a, **k: {"check": name, "status": "GO", "detail": "stub"})
    for name in ("check_open_items", "check_cqs", "check_git_cleanliness", "check_contract_verification",
                 "check_quality_advisory", "check_pr_size", "check_artifacts", "check_shell_syntax",
                 "check_net_deletion", "check_ci_workflow", "check_required_contexts", "check_pytest"):
        monkeypatch.setattr(gate, name, go(name))
    monkeypatch.setattr(gate, "_sync_fabric_finding", lambda *a, **k: None)
    return gate.run_gate_checks("#1", repo_root, dispatch_dir.parent / "state", dispatch_dir,
                                pr_head=_pr_head(merge_base, branch))


def _perm(result: dict) -> dict:
    found = [c for c in result["checks"] if c["check"] == "permission_policy"]
    assert found, "no permission_policy outcome in the check list"
    return found[0]


def test_gate_reports_policy_absent_for_spec_without_digest(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    _spec(dd, None)
    check = _perm(_run(monkeypatch, root, dd, sha))
    assert check["outcome"] == "policy_absent"
    assert check["status"] == "GO" and check["warning"] is False


def test_spec_resolved_by_dispatch_id_not_by_report_glob(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    _spec(dd, _digest_of(root, _profiles()))
    (dd / "active").mkdir()
    (dd / "active" / "unrelated-name.md").write_text(f"PR: #1\n\nDispatch-ID: {DISPATCH_ID}\n")
    check = _perm(_run(monkeypatch, root, dd, sha, branch="feature/not-a-dispatch-branch"))
    assert check["outcome"] == "match"


def test_spec_found_in_completed(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    _spec(dd, _digest_of(root, _profiles()), bucket="completed")
    assert _perm(_run(monkeypatch, root, dd, sha))["outcome"] == "match"


def test_match(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    _spec(dd, _digest_of(root, _profiles()))
    check = _perm(_run(monkeypatch, root, dd, sha))
    assert (check["outcome"], check["status"], check["warning"]) == ("match", "GO", False)


def test_mismatch_names_both_digests(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    old = _digest_of(root, _profiles("tests/**"))
    _spec(dd, old)
    check = _perm(_run(monkeypatch, root, dd, sha))
    assert check["outcome"] == "mismatch" and check["warning"] is True
    assert old in check["detail"] and _digest_of(root, _profiles()) in check["detail"]
    assert check["status"] == "GO"  # enforcement off: recorded, not blocking


def test_spec_missing(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    dd.mkdir()
    check = _perm(_run(monkeypatch, root, dd, sha))
    assert check["outcome"] == "spec_missing" and check["status"] == "GO"


def test_evidence_refused_on_broken_yaml_at_merge_base(repo, tmp_path, monkeypatch):
    root, _ = repo
    (root / YAML_PATH).write_text("profiles: [unclosed\n")
    broken = _commit(root, "broken")
    dd = tmp_path / "dispatches"
    _spec(dd, "a" * 64)
    check = _perm(_run(monkeypatch, root, dd, broken))
    assert check["outcome"] == "evidence_refused" and check["warning"] is True
    assert check["status"] == "GO"


def test_evidence_refused_on_unknown_role_in_no_register(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    _spec(dd, "a" * 64, role="no-such-role")
    assert _perm(_run(monkeypatch, root, dd, sha))["outcome"] == "evidence_refused"


def test_digest_comes_from_merge_base_not_working_tree(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    _spec(dd, _digest_of(root, _profiles()))
    (root / YAML_PATH).write_text(_profiles("elsewhere/**"))  # working tree only, uncommitted
    assert _perm(_run(monkeypatch, root, dd, sha))["outcome"] == "match"
    changed = _commit(root, "change")
    assert _perm(_run(monkeypatch, root, dd, changed))["outcome"] == "mismatch"


@pytest.mark.parametrize("case", ["policy_absent", "spec_missing", "evidence_refused", "mismatch"])
def test_enforcement_on_blocks_every_non_match(repo, tmp_path, monkeypatch, case):
    root, sha = repo
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    dd = tmp_path / "dispatches"
    dd.mkdir()
    if case == "policy_absent":
        _spec(dd, None)
    elif case == "evidence_refused":
        _spec(dd, "a" * 64, role="no-such-role")
    elif case == "mismatch":
        _spec(dd, "b" * 64)
    result = _run(monkeypatch, root, dd, sha)
    check = _perm(result)
    assert check["outcome"] == case
    assert check["status"] in ("HOLD", gate.SKIPPED_UNVERIFIED)
    assert result["verdict"] == "HOLD"


def test_enforcement_on_match_passes(repo, tmp_path, monkeypatch):
    root, sha = repo
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    dd = tmp_path / "dispatches"
    _spec(dd, _digest_of(root, _profiles()))
    result = _run(monkeypatch, root, dd, sha)
    assert _perm(result)["status"] == "GO" and result["verdict"] == "GO"


def test_enforcement_on_blocks_fallback_role(repo, tmp_path, monkeypatch):
    root, _ = repo
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    (root / YAML_PATH).write_text(f"version: 1\nprofiles:\n  {ROLE}: {{}}\n")
    fb = _commit(root, "empty profile")
    scratch = tmp_path / "fb"
    (scratch / ".vnx").mkdir(parents=True)
    (scratch / YAML_PATH).write_text(f"version: 1\nprofiles:\n  {ROLE}: {{}}\n")
    (scratch / "agents" / ROLE).mkdir(parents=True)
    (scratch / "agents" / ROLE / "CLAUDE.md").write_text("# role\n")
    (scratch / "agents" / ROLE / "config.yaml").write_text(
        "isolation:\n  scope_type: coding_worktree\n  allowed_paths: ['.']\n")
    profile = resolve_worker_profile(ROLE, yaml_path=scratch / YAML_PATH, agents_dir=scratch / "agents")
    assert profile.is_fallback
    dd = tmp_path / "dispatches"
    _spec(dd, profile.policy_digest)
    check = _perm(_run(monkeypatch, root, dd, fb))
    assert check["outcome"] == "match" and check["is_fallback"] is True
    assert check["status"] == "HOLD"


def test_enforcement_off_fallback_does_not_block(repo, tmp_path, monkeypatch):
    root, _ = repo
    (root / YAML_PATH).write_text(f"version: 1\nprofiles:\n  {ROLE}: {{}}\n")
    fb = _commit(root, "empty profile")
    dd = tmp_path / "dispatches"
    _spec(dd, "c" * 64)
    assert _perm(_run(monkeypatch, root, dd, fb))["status"] == "GO"


def test_malformed_spec_is_evidence_refused(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    p = _spec(dd, None)
    p.write_text("{not json")
    assert _perm(_run(monkeypatch, root, dd, sha))["outcome"] == "evidence_refused"


def test_dispatch_id_path_traversal_not_resolved(tmp_path):
    assert gate._find_spec_by_dispatch_id("../x", tmp_path) is None
    assert gate._find_spec_by_dispatch_id("", tmp_path) is None
