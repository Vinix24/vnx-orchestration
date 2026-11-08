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


# ---------------------------------------------------------------------------
# Round 2: isolated merge-base registry and ledger-first outcomes
# ---------------------------------------------------------------------------

LOCAL_CONFIG = "isolation:\n  scope_type: coding_worktree\n  allowed_paths: ['/', '/tmp']\n"


@pytest.fixture
def repo_without_agents(tmp_path, monkeypatch):
    """A repo whose merge base has the permissions YAML and no ``agents/`` tree at all."""
    monkeypatch.delenv("VNX_ENFORCE_WORKER_PERMISSIONS", raising=False)
    monkeypatch.delenv("VNX_WORKER_ENFORCEMENT_SKIP", raising=False)
    root = tmp_path / "repo-noagents"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".vnx").mkdir()
    (root / YAML_PATH).write_text(_profiles())
    return root, _commit(root, "base without agents")


def _local_agents(tmp_path: Path, monkeypatch) -> Path:
    """A local registry the working tree / install could discover, widening the role's policy."""
    local = tmp_path / "local-install"
    (local / "agents" / ROLE).mkdir(parents=True)
    (local / "agents" / ROLE / "CLAUDE.md").write_text("# local\n")
    (local / "agents" / ROLE / "config.yaml").write_text(LOCAL_CONFIG)
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(local))
    monkeypatch.setenv("PROJECT_ROOT", str(local))
    monkeypatch.setenv("VNX_HOME", str(local))
    return local / "agents"


def _empty_registry_digest(tmp_path: Path) -> str:
    scratch = tmp_path / "empty-registry"
    (scratch / ".vnx").mkdir(parents=True, exist_ok=True)
    (scratch / "agents").mkdir(exist_ok=True)
    (scratch / YAML_PATH).write_text(_profiles())
    return resolve_worker_profile(ROLE, yaml_path=scratch / YAML_PATH,
                                  agents_dir=scratch / "agents").policy_digest


def _local_digest(local_agents: Path, tmp_path: Path) -> str:
    scratch = tmp_path / "local-digest"
    (scratch / ".vnx").mkdir(parents=True, exist_ok=True)
    (scratch / YAML_PATH).write_text(_profiles())
    return resolve_worker_profile(ROLE, yaml_path=scratch / YAML_PATH,
                                  agents_dir=local_agents).policy_digest


def _ledger(dispatch_dir: Path) -> list:
    path = dispatch_dir.parent / "state" / "t0_receipts.ndjson"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event_type") == "permission_policy_checked"]


def test_merge_base_without_agents_ignores_local_registry(repo_without_agents, tmp_path, monkeypatch):
    root, sha = repo_without_agents
    expected = _empty_registry_digest(tmp_path)
    plain = gate._policy_at_merge_base(root, sha, ROLE)[0]
    local_agents = _local_agents(tmp_path, monkeypatch)
    widened = _local_digest(local_agents, tmp_path)
    assert widened != expected, "the local registry must change the digest for this test to mean anything"
    assert gate._policy_at_merge_base(root, sha, ROLE)[0] == plain == expected


def test_local_registry_digest_in_spec_is_mismatch_and_hold_under_enforcement(
        repo_without_agents, tmp_path, monkeypatch):
    root, sha = repo_without_agents
    local_agents = _local_agents(tmp_path, monkeypatch)
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    dd = tmp_path / "dispatches"
    _spec(dd, _local_digest(local_agents, tmp_path))
    result = _run(monkeypatch, root, dd, sha)
    check = _perm(result)
    assert check["outcome"] == "mismatch" and check["status"] == "HOLD"
    assert result["verdict"] == "HOLD"


def test_materialized_agents_dir_is_inside_dest_when_merge_base_has_no_agents(repo_without_agents, tmp_path):
    root, sha = repo_without_agents
    dest = tmp_path / "dest"
    dest.mkdir()
    agents = gate._materialize_policy_sources(root, sha, ROLE, dest)
    assert agents == dest / "agents" and agents.is_dir() and list(agents.iterdir()) == []


def _prepare(case: str, root: Path, sha: str, dd: Path, monkeypatch):
    """Returns (merge_base_sha, expected outcome, expected enforcement label)."""
    enforcement = "on" if case == "mismatch_on" else "off"
    if enforcement == "on":
        monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    if case == "match":
        _spec(dd, _digest_of(root, _profiles()))
        return sha, "match", enforcement
    if case in ("mismatch_on", "mismatch_off"):
        _spec(dd, "b" * 64)
        return sha, "mismatch", enforcement
    if case == "policy_absent":
        _spec(dd, None)
        return sha, "policy_absent", enforcement
    (root / YAML_PATH).write_text("profiles: [unclosed\n")
    broken = _commit(root, "broken")
    _spec(dd, "a" * 64)
    return broken, "evidence_refused", enforcement


@pytest.mark.parametrize("case", ["match", "mismatch_on", "mismatch_off", "policy_absent", "evidence_refused"])
def test_every_outcome_lands_in_the_ledger_before_the_check_is_listed(repo, tmp_path, monkeypatch, case):
    root, sha = repo
    dd = tmp_path / "dispatches"
    merge_base, outcome, enforcement = _prepare(case, root, sha, dd, monkeypatch)
    seen_at_listing = []
    real_check = gate.check_permission_policy

    def spy(*a, **k):
        check = real_check(*a, **k)
        seen_at_listing.append(len(_ledger(dd)))  # ledger rows once the check exists, before any derived write
        return check

    monkeypatch.setattr(gate, "check_permission_policy", spy)
    result = _run(monkeypatch, root, dd, merge_base)
    rows = _ledger(dd)
    assert len(rows) == 1 and seen_at_listing == [1]
    row = rows[0]
    assert row["outcome"] == outcome and row["enforcement"] == enforcement
    assert row["pr"] == "#1" and row["head_sha"] == merge_base and row["merge_base"] == merge_base
    assert row["role"] == ROLE and row["dispatch_id"] == DISPATCH_ID
    assert row["spec_digest"] == (None if case == "policy_absent" else row["spec_digest"])
    if outcome in ("match", "mismatch"):
        assert row["merge_base_digest"] == _digest_of(root, _profiles())
    else:
        assert row["merge_base_digest"] is None
    assert _perm(result)["receipt_written"] is True


def test_spec_missing_outcome_lands_in_the_ledger(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    dd.mkdir()
    _run(monkeypatch, root, dd, sha)
    rows = _ledger(dd)
    assert [r["outcome"] for r in rows] == ["spec_missing"] and rows[0]["spec_digest"] is None


def test_failing_receipt_writer_makes_the_check_unverified(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    _spec(dd, _digest_of(root, _profiles()))

    def boom(*a, **k):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(gate, "emit_governance_receipt", boom)
    result = _run(monkeypatch, root, dd, sha)
    check = _perm(result)
    assert check["status"] == gate.SKIPPED_UNVERIFIED and check["receipt_written"] is False
    assert "ledger unavailable" in check["detail"] and result["verdict"] == "HOLD"


@pytest.mark.parametrize("append_status", [None, "", "refused", "failed"])
def test_unproven_append_status_makes_the_check_unverified(repo, tmp_path, monkeypatch, append_status):
    root, sha = repo
    dd = tmp_path / "dispatches"
    _spec(dd, _digest_of(root, _profiles()))
    monkeypatch.setattr(gate, "emit_governance_receipt", lambda *a, **k: {"append_status": append_status})
    check = _perm(_run(monkeypatch, root, dd, sha))
    assert check["status"] == gate.SKIPPED_UNVERIFIED and check["receipt_written"] is False


# ---------------------------------------------------------------------------
# Round 3: one receipt identity per decision, the evaluated head is always recorded,
# and a deduplicated receipt is never reported as written.
# ---------------------------------------------------------------------------

def _local_check(root: Path, dd: Path) -> dict:
    return gate.check_permission_policy("#1", root, dd, None, state_dir=dd.parent / "state")


def _freeze_clock(monkeypatch) -> list:
    """Freeze the receipt clock; returns the list that collects every emitted receipt (with its key)."""
    import governance_receipts
    monkeypatch.setattr(governance_receipts, "utc_now_iso", lambda: "2026-11-08T10:00:00Z")
    emitted: list = []
    real = gate.emit_governance_receipt

    def capture(*a, **k):
        receipt = real(*a, **k)
        emitted.append(receipt)
        return receipt

    monkeypatch.setattr(gate, "emit_governance_receipt", capture)
    return emitted


def _two_local_heads(root: Path, sha: str, dd: Path):
    """Two local heads with two different merge bases, checked in the same frozen second."""
    _spec(dd, _digest_of(root, _profiles()))
    _git(root, "update-ref", "refs/remotes/origin/main", sha)
    (root / "a.txt").write_text("a")
    head1 = _commit(root, "head1")
    first = _local_check(root, dd)
    _git(root, "update-ref", "refs/remotes/origin/main", head1)
    (root / "b.txt").write_text("b")
    head2 = _commit(root, "head2")
    second = _local_check(root, dd)
    return (sha, head1, first), (head1, head2, second)


def test_local_head_checks_get_distinct_receipts_with_the_real_head(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    emitted = _freeze_clock(monkeypatch)
    (base1, head1, first), (base2, head2, second) = _two_local_heads(root, sha, dd)
    assert first["receipt_written"] is True and second["receipt_written"] is True
    rows = _ledger(dd)
    assert [(r["head_sha"], r["merge_base"]) for r in rows] == [(head1, base1), (head2, base2)]
    assert all(len(r["head_sha"]) == 40 and r["commit_sha"] == r["head_sha"] for r in rows)
    keys = [r["idempotency_key"] for r in emitted]
    assert len(keys) == 2 and keys[0] != keys[1] and all(r["append_status"] == "appended" for r in emitted)


def test_two_decisions_on_the_same_head_do_not_share_an_identity(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    emitted = _freeze_clock(monkeypatch)
    _spec(dd, _digest_of(root, _profiles()))
    assert _local_check(root, dd)["receipt_written"] is True
    assert _local_check(root, dd)["receipt_written"] is True
    rows = _ledger(dd)
    assert len(rows) == 2 and rows[0]["trigger"] != rows[1]["trigger"]
    assert emitted[0]["idempotency_key"] != emitted[1]["idempotency_key"]


def test_duplicate_append_status_is_not_reported_as_written(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    _spec(dd, _digest_of(root, _profiles()))
    monkeypatch.setattr(gate, "emit_governance_receipt", lambda *a, **k: {"append_status": "duplicate"})
    check = _local_check(root, dd)
    assert check["status"] == gate.SKIPPED_UNVERIFIED and check["receipt_written"] is False
    assert "duplicate" in check["detail"]


def test_unresolvable_local_head_is_unverified_and_writes_no_receipt(repo, tmp_path, monkeypatch):
    root, sha = repo
    dd = tmp_path / "dispatches"
    _spec(dd, _digest_of(root, _profiles()))
    monkeypatch.setattr(gate, "_resolve_local_head_sha", lambda project_root: None)
    check = _local_check(root, dd)
    assert check["status"] == gate.SKIPPED_UNVERIFIED and check["receipt_written"] is False
    assert _ledger(dd) == []


def test_resolve_local_head_sha_is_none_outside_a_repository(tmp_path):
    assert gate._resolve_local_head_sha(tmp_path / "not-a-repo") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gate._resolve_local_head_sha(plain) is None
