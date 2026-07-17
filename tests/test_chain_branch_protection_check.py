"""Tests for ADR-034 §6 step 2b's branch-protection activation precondition:
``chain_origin_anchor.check_branch_protection`` / ``_owner_repo_from_remote``,
and the CLI wrapper ``scripts/chain_branch_protection_check.py``.

Mocks `gh api` output — no network, no real GitHub access. Covers the
dispatch's required cases: protected-with-check, protected-without-check,
unprotected, and api-error (all except the first must fail closed, mirroring
verify_chain's "can't check is never assume fine" contract).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import chain_origin_anchor as coa  # noqa: E402
from chain_origin_anchor import ANCHOR_IMMUTABILITY_CHECK_NAME, check_branch_protection  # noqa: E402

import chain_branch_protection_check as cli  # noqa: E402


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _protection_payload(*, check_present: bool, enforce_admins: bool, force_pushes_blocked: bool) -> dict:
    contexts = [ANCHOR_IMMUTABILITY_CHECK_NAME] if check_present else ["some-other-check"]
    return {
        "required_status_checks": {"strict": True, "contexts": contexts, "checks": []},
        "enforce_admins": {"enabled": enforce_admins},
        "allow_force_pushes": {"enabled": not force_pushes_blocked},
    }


def _run_git(*args: str, cwd) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed in {cwd}: {result.stderr}")
    return result.stdout


# ---------------------------------------------------------------------------
# check_branch_protection — all three required conditions
# ---------------------------------------------------------------------------


def test_protected_with_check_confirms(monkeypatch):
    payload = _protection_payload(check_present=True, enforce_admins=True, force_pushes_blocked=True)

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["gh", "api"]
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is True
    assert status.required_check_present is True
    assert status.enforce_admins is True
    assert status.force_pushes_blocked is True
    assert status.reason is None


def test_protected_without_required_check_fails_closed(monkeypatch):
    payload = _protection_payload(check_present=False, enforce_admins=True, force_pushes_blocked=True)

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is False
    assert status.required_check_present is False
    assert "required check" in (status.reason or "")


def test_enforce_admins_off_fails_closed(monkeypatch):
    payload = _protection_payload(check_present=True, enforce_admins=False, force_pushes_blocked=True)

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is False
    assert "enforce_admins" in (status.reason or "")


def test_force_pushes_allowed_fails_closed(monkeypatch):
    payload = _protection_payload(check_present=True, enforce_admins=True, force_pushes_blocked=False)

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is False
    assert "force-push" in (status.reason or "")


def test_missing_allow_force_pushes_key_fails_closed(monkeypatch):
    """Finding 2 (ADR-034 fix-r1): a payload with the required check present
    and enforce_admins on, but NO `allow_force_pushes` key at all, must fail
    closed — not silently read as "force-pushes blocked"."""
    payload = {
        "required_status_checks": {
            "strict": True, "contexts": [ANCHOR_IMMUTABILITY_CHECK_NAME], "checks": [],
        },
        "enforce_admins": {"enabled": True},
        # no "allow_force_pushes" key at all
    }

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is False
    assert status.force_pushes_blocked is False
    assert "force-push" in (status.reason or "")


def test_unparseable_allow_force_pushes_enabled_fails_closed(monkeypatch):
    """`allow_force_pushes.enabled` present but not a boolean (malformed API
    response) must also fail closed, not be coerced into "blocked"."""
    payload = {
        "required_status_checks": {
            "strict": True, "contexts": [ANCHOR_IMMUTABILITY_CHECK_NAME], "checks": [],
        },
        "enforce_admins": {"enabled": True},
        "allow_force_pushes": {"enabled": None},
    }

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is False
    assert status.force_pushes_blocked is False
    assert "force-push" in (status.reason or "")


# ---------------------------------------------------------------------------
# Fail-closed on anything that prevents a real answer
# ---------------------------------------------------------------------------


def test_unprotected_branch_fails_closed(monkeypatch):
    """gh api returns non-zero (404) when a branch has no protection at all."""

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(1, stdout="", stderr='{"message":"Branch not protected","status":"404"}')

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is False
    assert "branch-protection lookup failed" in (status.reason or "")


def test_api_timeout_fails_closed(monkeypatch):
    """A transient gh failure (auth/network) must never read as confirmed."""

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 20)

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is False
    assert "gh api invocation failed" in (status.reason or "")


def test_gh_not_installed_fails_closed(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("gh: command not found")

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is False
    assert "gh api invocation failed" in (status.reason or "")


def test_malformed_json_response_fails_closed(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout="not json")

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is False
    assert "malformed" in (status.reason or "")


def test_non_dict_json_response_fails_closed(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout="[1, 2, 3]")

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    status = check_branch_protection(Path("/tmp"), "main", owner_repo="acme/repo")
    assert status.confirmed is False
    assert "unexpected gh api response shape" in (status.reason or "")


# ---------------------------------------------------------------------------
# owner/repo resolution from the git remote
# ---------------------------------------------------------------------------


def test_owner_repo_resolved_from_github_https_remote(tmp_path):
    _run_git("init", "-b", "main", str(tmp_path), cwd=tmp_path.parent)
    _run_git("remote", "add", "origin", "https://github.com/Vinix24/vnx-orchestration.git", cwd=tmp_path)
    resolved = coa._owner_repo_from_remote(tmp_path)
    assert resolved == "Vinix24/vnx-orchestration"


def test_owner_repo_resolved_from_github_ssh_remote(tmp_path):
    _run_git("init", "-b", "main", str(tmp_path), cwd=tmp_path.parent)
    _run_git("remote", "add", "origin", "git@github.com:Vinix24/vnx-orchestration.git", cwd=tmp_path)
    resolved = coa._owner_repo_from_remote(tmp_path)
    assert resolved == "Vinix24/vnx-orchestration"


def test_owner_repo_none_for_non_github_remote(tmp_path):
    _run_git("init", "-b", "main", str(tmp_path), cwd=tmp_path.parent)
    _run_git("remote", "add", "origin", "/some/local/path/repo.git", cwd=tmp_path)
    resolved = coa._owner_repo_from_remote(tmp_path)
    assert resolved is None


def test_owner_repo_none_when_remote_missing(tmp_path):
    _run_git("init", "-b", "main", str(tmp_path), cwd=tmp_path.parent)
    resolved = coa._owner_repo_from_remote(tmp_path)
    assert resolved is None


def test_no_resolvable_owner_repo_fails_closed(tmp_path):
    _run_git("init", "-b", "main", str(tmp_path), cwd=tmp_path.parent)
    status = check_branch_protection(tmp_path, "main")
    assert status.confirmed is False
    assert "could not resolve owner/repo" in (status.reason or "")


# ---------------------------------------------------------------------------
# CLI wrapper (scripts/chain_branch_protection_check.py)
# ---------------------------------------------------------------------------


def test_cli_exit_0_when_confirmed(monkeypatch, capsys):
    payload = _protection_payload(check_present=True, enforce_admins=True, force_pushes_blocked=True)

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    rc = cli.main(["--project-root", "/tmp", "--owner-repo", "acme/repo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CONFIRMED" in out


def test_cli_exit_1_when_not_confirmed(monkeypatch, capsys):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(1, stdout="", stderr="not found")

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    rc = cli.main(["--project-root", "/tmp", "--owner-repo", "acme/repo"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "NOT CONFIRMED" in out


def test_cli_json_output(monkeypatch, capsys):
    payload = _protection_payload(check_present=True, enforce_admins=True, force_pushes_blocked=True)

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(coa.subprocess, "run", fake_run)
    rc = cli.main(["--project-root", "/tmp", "--owner-repo", "acme/repo", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["confirmed"] is True
    assert out["owner_repo"] == "acme/repo"
