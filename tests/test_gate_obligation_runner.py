"""tests/test_gate_obligation_runner.py — OI-1253 runner store-resolution guard.

The runner must never silently serve a store it cannot attribute to a project.
A central install whose only identity signal was a release-time git origin
resolves no project_id (the origin is refused by ``_project_id_from_git_remote``),
and ``_resolve_state_root`` would otherwise fall back to a project-local dir
under the immutable install. The runner must fail LOUD with an actionable
message instead of writing to a fabricated or unattributable store.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import vnx_paths  # noqa: E402
import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import (  # noqa: E402
    STATUS_FULFILLED,
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    obligation_path,
    register_obligation,
    update_obligation,
)


class TestDefaultStateDirGuard:
    """A runner without ``--state-dir`` must fail LOUD when project_id is None."""

    def test_loud_when_project_id_unresolvable(self, monkeypatch):
        monkeypatch.setattr(
            vnx_paths, "_resolve_state_project_id", lambda project_root: None,
        )
        with pytest.raises(runner.UnresolvableProjectError) as excinfo:
            runner._default_state_dir()
        message = str(excinfo.value)
        assert "--state-dir" in message
        assert "VNX_PROJECT_ID" in message

    def test_resolves_when_project_id_present(self, monkeypatch):
        monkeypatch.setattr(
            vnx_paths, "_resolve_state_project_id", lambda project_root: "vnx-dev",
        )
        state_dir = runner._default_state_dir()
        assert state_dir.name == "state"

    def test_main_returns_20_with_loud_error(self, monkeypatch, capsys):
        monkeypatch.setattr(
            vnx_paths, "_resolve_state_project_id", lambda project_root: None,
        )
        rc = runner.main([])
        assert rc == 20
        err = capsys.readouterr().err
        assert "project_id" in err
        assert "--state-dir" in err

    def test_main_state_dir_missing_still_returns_20(self, tmp_path, capsys):
        missing = tmp_path / "does-not-exist" / "state"
        rc = runner.main(["--state-dir", str(missing)])
        assert rc == 20
        assert "state dir not found" in capsys.readouterr().err


class TestOwnerRepoFromRemoteUrl:
    """``_owner_repo_from_remote_url`` accepts GitHub https/ssh forms and refuses
    anything else, including a local-filesystem origin (OI-1253)."""

    def test_https_url(self):
        assert (
            runner._owner_repo_from_remote_url(
                "https://github.com/Vinix24/vnx-orchestration.git"
            )
            == "Vinix24/vnx-orchestration"
        )

    def test_ssh_url(self):
        assert (
            runner._owner_repo_from_remote_url(
                "git@github.com:Vinix24/vnx-orchestration.git"
            )
            == "Vinix24/vnx-orchestration"
        )

    def test_no_trailing_git(self):
        assert (
            runner._owner_repo_from_remote_url(
                "https://github.com/Vinix24/vnx-orchestration"
            )
            == "Vinix24/vnx-orchestration"
        )

    def test_local_filesystem_origin_refused(self):
        assert (
            runner._owner_repo_from_remote_url(
                "/var/folders/ab/cd/T/vnx-checkout"
            )
            is None
        )

    def test_non_github_host_refused(self):
        assert runner._owner_repo_from_remote_url("https://gitlab.com/foo/bar.git") is None


class TestGhJsonRepoScoping:
    """``gh`` must be told the repo explicitly, never infer it from the cwd."""

    def test_injects_repo_flag_when_owner_repo_given(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name: "/opt/homebrew/bin/gh")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout='{"number": 1}')

        monkeypatch.setattr(runner.subprocess, "run", fake_run)
        result = runner._gh_json(["pr", "list"], owner_repo="Vinix24/vnx-orchestration")
        assert result == {"number": 1}
        assert captured["cmd"] == [
            "gh", "--repo", "Vinix24/vnx-orchestration", "pr", "list",
        ]

    def test_omits_repo_flag_when_none(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name: "/opt/homebrew/bin/gh")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout='{"number": 1}')

        monkeypatch.setattr(runner.subprocess, "run", fake_run)
        runner._gh_json(["pr", "list"], owner_repo=None)
        assert captured["cmd"] == ["gh", "pr", "list"]

    def test_pr_from_github_requests_the_right_repo(self, monkeypatch):
        captured = {}

        def fake_gh_json(args, *, owner_repo=None):
            captured["args"] = args
            captured["owner_repo"] = owner_repo
            return [{"number": 42}]

        monkeypatch.setattr(runner, "_gh_json", fake_gh_json)
        number = runner._pr_from_github("20260816-foo", "Vinix24/vnx-orchestration")
        assert number == 42
        assert captured["owner_repo"] == "Vinix24/vnx-orchestration"
        assert "dispatch/20260816-foo" in captured["args"]


class TestResolveGithubOwnerRepo:
    """Repo identity must come from the project registry / checkout, not cwd."""

    def test_resolves_from_registry_checkout_first(self, monkeypatch, tmp_path):
        checkout = tmp_path / "vnx-orchestration"
        checkout.mkdir()
        monkeypatch.setattr(
            vnx_paths, "project_id_from_state_dir", lambda state_dir: "vnx-dev",
        )
        monkeypatch.setattr(runner, "_project_checkout_path", lambda pid: checkout)

        def fake_origin(root):
            return "https://github.com/Vinix24/vnx-orchestration.git"

        monkeypatch.setattr(runner, "_git_remote_origin", fake_origin)
        assert (
            runner._resolve_github_owner_repo(tmp_path / "state")
            == "Vinix24/vnx-orchestration"
        )

    def test_cwd_fallback_returns_none_for_local_origin(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vnx_paths, "project_id_from_state_dir", lambda state_dir: "",
        )
        monkeypatch.setattr(runner, "_project_checkout_path", lambda pid: None)
        # A release-time temp checkout is a local-filesystem origin, never a
        # GitHub identity: the fallback must NOT fabricate an owner/repo from it.
        monkeypatch.setattr(
            runner, "_git_remote_origin", lambda root: "/var/folders/ab/cd/T/checkout",
        )
        assert runner._resolve_github_owner_repo(tmp_path / "state") is None

    def test_returns_none_when_no_remote_at_all(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vnx_paths, "project_id_from_state_dir", lambda state_dir: "",
        )
        monkeypatch.setattr(runner, "_project_checkout_path", lambda pid: None)
        monkeypatch.setattr(runner, "_git_remote_origin", lambda root: None)
        assert runner._resolve_github_owner_repo(tmp_path / "state") is None


class TestPlistHasNoHardcodedProject:
    """The launchd template must pin NO project or store path (OI-1253)."""

    PLIST = ROOT / "scripts" / "launchd" / "com.vnx.gate-obligation-runner.plist"

    def test_template_has_no_hardcoded_project_or_store(self):
        content = self.PLIST.read_text(encoding="utf-8")
        assert "--state-dir" not in content
        assert "vnx-dev" not in content
        assert "~/.vnx-data" not in content
        assert "VNX_PROJECT_ID" in content
        assert "${VNX_PROJECT_ID}" in content


# ---------------------------------------------------------------------------
# OI-1400 residu — two defects, both driven through the real
# fulfill_obligation() against a real obligation file under
# review_gates/obligations/ and a real result file under
# review_gates/results/, with the status read back FROM DISK afterwards.
#
# Defect 1: a not_executable/provider_not_installed gate result used to burn
# the obligation terminal on the first attempt. A provider that is not
# installed today can be installed tomorrow — same class of "temporary"
# refusal as provider_disabled — and must take the same bounded
# pending/escalate route.
#
# Defect 2: the terminal branch always set outcome["action"] = "fulfilled",
# even when the record it had just written to disk carried status
# "not_executable" or "failed". The label must mirror the persisted record.
# ---------------------------------------------------------------------------


class _FakeReviewGateManager:
    """Writes real request+result JSON files — no gate actually runs.

    Mirrors ``tests/test_gate_obligations.py::_FakeManager`` (OI-1384/OI-1400
    fixtures for the runner's temporary-vs-permanent-refusal distinction),
    duplicated locally so this file's harness is self-contained.
    """

    def __init__(self, state_dir: Path, *, result_status: str, result_reason: str | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.result_status = result_status
        self.result_reason = result_reason
        self.calls = []

    def _request_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "requests" / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"

    def request_and_execute(self, *, pr_number, branch, review_stack, risk_class,
                             changed_files, mode, dispatch_id=""):
        self.calls.append(
            {"pr_number": pr_number, "branch": branch, "review_stack": list(review_stack)}
        )
        for gate in review_stack:
            self._request_path(gate, pr_number).write_text(
                json.dumps({"gate": gate, "pr_number": pr_number, "status": "completed"}),
                encoding="utf-8",
            )
            result_payload = {"gate": gate, "pr_number": pr_number, "status": self.result_status}
            if self.result_reason is not None:
                result_payload["reason"] = self.result_reason
            self._result_path(gate, pr_number).write_text(
                json.dumps(result_payload), encoding="utf-8",
            )
        return {"pr_number": pr_number, "branch": branch, "gates": [], "has_required_failure": False}


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    return state_dir


def _patch_manager(monkeypatch, manager: "_FakeReviewGateManager") -> None:
    """Hermetic patch: no git, no gh, no real gate — only the fake manager."""
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration")
    fake_rgm = types.ModuleType("review_gate_manager")
    fake_rgm._compute_changed_files = lambda branch: ["scripts/lib/foo.py"]
    monkeypatch.setitem(sys.modules, "review_gate_manager", fake_rgm)


def _read_obligation(state_dir: Path, dispatch_id: str) -> dict:
    return json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))


class TestProviderNotInstalledIsTemporary:
    """Defect 1: ``provider_not_installed`` must take the same bounded
    pending/escalate route as ``provider_disabled`` — not burn terminal on
    the first attempt.

    RED on unfixed main (measured 2026-08-23 against ``main@3cdadba4``):
    ``result_status='not_executable' reason='provider_not_installed' ->
    action=fulfilled record.status='not_executable' record.reason=None`` —
    an obligation with an empty contract_hash and empty report_path landed
    permanently closed on the very first attempt.
    """

    def test_stays_pending_not_terminal_on_first_attempt(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260823-oi1400r-not-installed", gate="codex_gate",
            project_id="vnx-dev", pr_number=9661,
        )
        manager = _FakeReviewGateManager(
            state_dir, result_status="not_executable", result_reason="provider_not_installed",
        )
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 1, (
            "a provider that is merely not installed yet must not burn the "
            "obligation terminal on the first attempt"
        )
        record = _read_obligation(state_dir, "20260823-oi1400r-not-installed")
        assert record["status"] == STATUS_PENDING
        assert record["attempts"] == 1
        assert record["reason"] == "gate_parked"
        assert "provider_not_installed" in record["reason_detail"]
        assert "not broken" in record["reason_detail"]
        # The stale wording ("a config flag has it disabled") does not apply
        # to a missing binary — the detail text must say so accurately.
        assert "config flag" not in record["reason_detail"]

    def test_escalates_to_not_executable_after_threshold(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        path = register_obligation(
            state_dir, dispatch_id="20260823-oi1400r-not-installed-escalate", gate="codex_gate",
            project_id="vnx-dev", pr_number=9664,
        )
        update_obligation(path, attempts=runner._TEMPORARY_REFUSAL_ESCALATION_ATTEMPTS - 1)
        manager = _FakeReviewGateManager(
            state_dir, result_status="not_executable", result_reason="provider_not_installed",
        )
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 0
        record = _read_obligation(state_dir, "20260823-oi1400r-not-installed-escalate")
        assert record["status"] == STATUS_NOT_EXECUTABLE
        assert record["reason"] == "gate_parked_timeout"
        assert record["attempts"] == runner._TEMPORARY_REFUSAL_ESCALATION_ATTEMPTS


class TestOutcomeActionMirrorsRecordStatus:
    """Defect 2: ``outcome["action"]`` must mirror the status just persisted
    to the obligation record — never a hardcoded ``"fulfilled"``.

    RED on unfixed main (measured 2026-08-23 against ``main@3cdadba4``): a
    ``not_executable/provider_not_configured`` result (a reason outside the
    temporary set, so it still resolves through the terminal branch even
    after the defect-1 fix) produced ``outcome["action"] == "fulfilled"``
    while the obligation record written to disk carried
    ``status == "not_executable"``.
    """

    def test_action_label_says_not_executable_when_record_does(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260823-oi1400r-action-label", gate="codex_gate",
            project_id="vnx-dev", pr_number=9662,
        )
        manager = _FakeReviewGateManager(
            state_dir, result_status="not_executable", result_reason="provider_not_configured",
        )
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        outcome = summary["outcomes"][0]
        record = _read_obligation(state_dir, "20260823-oi1400r-action-label")
        assert record["status"] == STATUS_NOT_EXECUTABLE
        assert outcome["action"] == record["status"], (
            "the returned action label must mirror the status actually "
            "written to the obligation record, not a hardcoded 'fulfilled'"
        )
        assert outcome["action"] == STATUS_NOT_EXECUTABLE

    def test_control_a_real_pass_still_reports_fulfilled(self, tmp_path, monkeypatch):
        """Must-pass control: without this, a fix that routes everything to
        pending would still make the two tests above pass for the wrong
        reason. A genuine pass verdict must still land fulfilled with an
        action label that says so."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260823-oi1400r-control-pass", gate="ci_gate",
            project_id="vnx-dev", pr_number=9663,
        )
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 0
        outcome = summary["outcomes"][0]
        record = _read_obligation(state_dir, "20260823-oi1400r-control-pass")
        assert record["status"] == STATUS_FULFILLED
        assert outcome["action"] == STATUS_FULFILLED
