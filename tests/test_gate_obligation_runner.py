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
import os
import sys
import time
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
    REASON_NO_PR_BRANCH_GONE,
    REASON_PR_CLOSED,
    REASON_PR_MERGED,
    STATUS_FAILED,
    STATUS_FULFILLED,
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    STATUS_RETIRED,
    STATUS_UNRESOLVABLE,
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


# OI-1571 tak 3: attempt_gate now resolves a PR head sha up front for every
# obligation it processes. The default here is an arbitrary fixed sentinel
# most tests never need to match against anything — _patch_manager stubs
# _get_pr_head_sha_for_gate to this value purely for hermeticity (no test in
# this file may shell out to a real `gh pr view`). Tests that DO care about
# sha binding (TestTakeoverChainEvidence) stamp their fake managers' result
# records with this same value so "the happy path still works" stays the
# default, and override it locally to build a mismatch.
_DEFAULT_TEST_HEAD_SHA = "deadbeef00" * 4


def _patch_manager(
    monkeypatch, manager: "_FakeReviewGateManager", *, head_sha: str = _DEFAULT_TEST_HEAD_SHA,
    pr_state: "str | None" = "OPEN",
) -> None:
    """Hermetic patch: no git, no gh, no real gate — only the fake manager.

    ``pr_state`` (OI-1508) stubs the RESOLVED branch's own ``gh pr view``
    state check to a fixed answer — default ``"OPEN"`` so every test in this
    file that registers an obligation WITH a ``pr_number`` (a RESOLVED
    resolution) keeps reaching ``attempt_gate`` exactly as before this fix,
    unless a test overrides it to exercise the new retire/undetermined
    outcomes.
    """
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: head_sha)
    # raising=False: on unfixed pre-OI-1508 code this attribute does not
    # exist yet — the RED proof for OI-1508 must fail on BEHAVIOR (the gate
    # manager gets called when it must not be), never on this shared test
    # helper's own AttributeError.
    monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr_number, owner_repo: pr_state, raising=False)
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


# ---------------------------------------------------------------------------
# D2e (dispatch 20260830-120000-d2e-takeover-keten-bewijs) — the review-gate
# takeover chain (codex_gate -> kimi_gate -> glm_gate -> deepseek_gate,
# gate_request_handler._build_review_gate_takeover_chain) substitutes a
# successor gate as the READER at request time, but writes that successor's
# verdict under its OWN name (pr-<n>-<successor>.json), never under the
# originally declared gate's. This runner used to read only
# manager._result_path(<declared gate>, pr_number) -- live evidence, PR
# #1726: pr-1726-codex_gate.json stayed a stale lane_exhausted record while
# pr-1726-kimi_gate.json carried the real, complete-evidence verdict, and
# the obligation declared against codex_gate never found it.
# ---------------------------------------------------------------------------


class _TakeoverFakeReviewGateManager:
    """Writes real request records for the declared gate, but the RESULT
    record for ``target_gate`` (defaulting to the declared gate itself when
    ``None``) -- mirrors ``_dispatch_review_seat`` walking PAST an
    already-exhausted declared gate without touching its own result file,
    and dispatching the takeover successor instead.
    """

    def __init__(
        self, state_dir: Path, *, target_gate: "str | None", status: str,
        report_path: Path, contract_hash: str = "sha256:deadbeef",
        commit_sha: str = "deadbeef00" * 4,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.target_gate = target_gate
        self.status = status
        self.report_path = report_path
        self.contract_hash = contract_hash
        # OI-1571 tak 3: matches _patch_manager's default _get_pr_head_sha_for_gate
        # stub so "the successor's evidence is current" is the default shape
        # -- pass a different value to build a mismatch/unverifiable record.
        self.commit_sha = commit_sha
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
                json.dumps({"gate": gate, "pr_number": pr_number, "status": "requested"}),
                encoding="utf-8",
            )
        target = self.target_gate or review_stack[0]
        self._result_path(target, pr_number).write_text(
            json.dumps({
                "gate": target,
                "pr_number": pr_number,
                "dispatch_id": dispatch_id,
                "status": self.status,
                "contract_hash": self.contract_hash,
                "report_path": str(self.report_path),
                "commit_sha": self.commit_sha,
            }),
            encoding="utf-8",
        )
        return {"pr_number": pr_number, "branch": branch, "gates": [], "has_required_failure": False}


def _seed_stuck_gate_result(state_dir: Path, gate: str, pr_number: int) -> None:
    """Pre-seed a declared gate's OWN result record stuck on a classic
    ``lane_exhausted`` shape (``status="unavailable"``, empty evidence) --
    the exact pr-1726-codex_gate.json shape measured live, never rewritten
    by :class:`_TakeoverFakeReviewGateManager` when ``target_gate`` names a
    successor instead.
    """
    (state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json").write_text(
        json.dumps({
            "gate": gate, "pr_number": pr_number, "status": "unavailable",
            "reason": "dispatch_error", "contract_hash": "", "report_path": "",
        }),
        encoding="utf-8",
    )


class TestTakeoverChainEvidence:
    @pytest.fixture(autouse=True)
    def _clean_takeover_chain_env(self, monkeypatch):
        # Hermetic: the default chain (codex_gate,kimi_gate,glm_gate,
        # deepseek_gate) must come from the registry default, never from
        # whatever happens to be set in the ambient shell/CI environment.
        monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)
        monkeypatch.delenv("VNX_OVERRIDE_VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)

    def test_fulfilled_via_takeover_successor_evidence(self, tmp_path, monkeypatch):
        """RED on unfixed main (measured 2026-08-30, D2e): codex_gate's own
        record stays stuck ``unavailable`` forever while kimi_gate already
        carries a complete-evidence PASS for the same PR -- the runner must
        find it and book the obligation fulfilled, naming kimi_gate as the
        gate that actually decided.
        """
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-d2e-takeover-pass", gate="codex_gate",
            project_id="vnx-dev", pr_number=1726,
        )
        _seed_stuck_gate_result(state_dir, "codex_gate", 1726)
        report_file = state_dir / "unified_reports" / "kimi-gate-pr1726.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("kimi_gate report body", encoding="utf-8")

        manager = _TakeoverFakeReviewGateManager(
            state_dir, target_gate="kimi_gate", status="pass", report_path=report_file,
        )
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 0
        record = _read_obligation(state_dir, "20260830-d2e-takeover-pass")
        assert record["status"] == STATUS_FULFILLED
        assert record["resolved_by_gate"] == "kimi_gate"
        assert record["takeover_hops"] == ["codex_gate", "kimi_gate"]
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_FULFILLED
        assert outcome["resolved_by_gate"] == "kimi_gate"

    def test_failed_via_takeover_successor_evidence_never_reads_as_fulfilled(self, tmp_path, monkeypatch):
        """A DECIDED FAIL at the successor discharges the obligation but must
        never be laundered into a clean 'fulfilled' -- mirrors the BETA3-C2
        fulfill_by_failed_evidence discipline, now for takeover evidence."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-d2e-takeover-fail", gate="codex_gate",
            project_id="vnx-dev", pr_number=1729,
        )
        _seed_stuck_gate_result(state_dir, "codex_gate", 1729)
        report_file = state_dir / "unified_reports" / "kimi-gate-pr1729.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("kimi_gate report body", encoding="utf-8")

        manager = _TakeoverFakeReviewGateManager(
            state_dir, target_gate="kimi_gate", status="failed", report_path=report_file,
        )
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 0
        record = _read_obligation(state_dir, "20260830-d2e-takeover-fail")
        assert record["status"] == STATUS_FAILED
        assert record["resolved_by_gate"] == "kimi_gate"
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_FAILED

    def test_incomplete_successor_evidence_is_the_third_branch_stays_pending(self, tmp_path, monkeypatch):
        """Third branch (D2e): evidence at a successor that is NOT complete
        (report_path points at a file that does not exist) must never count
        -- and must never be silently folded into either 'found at the
        declared gate' or 'found via takeover'. The obligation stays
        pending, exactly as it does today with no successor at all."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-d2e-takeover-incomplete", gate="codex_gate",
            project_id="vnx-dev", pr_number=1730,
        )
        _seed_stuck_gate_result(state_dir, "codex_gate", 1730)

        manager = _TakeoverFakeReviewGateManager(
            state_dir, target_gate="kimi_gate", status="pass",
            report_path=state_dir / "unified_reports" / "never-written.md",
        )
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 1
        record = _read_obligation(state_dir, "20260830-d2e-takeover-incomplete")
        assert record["status"] == STATUS_PENDING
        assert "resolved_by_gate" not in record

    def test_declared_gate_evidence_still_found_directly_when_present(self, tmp_path, monkeypatch):
        """The existing path must keep working unchanged: when the declared
        gate itself produces complete, decided evidence, the takeover chain
        is never consulted at all."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-d2e-takeover-declared", gate="codex_gate",
            project_id="vnx-dev", pr_number=1731,
        )
        report_file = state_dir / "unified_reports" / "codex-gate-pr1731.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("codex_gate report body", encoding="utf-8")

        manager = _TakeoverFakeReviewGateManager(
            state_dir, target_gate=None, status="pass", report_path=report_file,
        )
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 0
        record = _read_obligation(state_dir, "20260830-d2e-takeover-declared")
        assert record["status"] == STATUS_FULFILLED
        assert "resolved_by_gate" not in record
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_FULFILLED
        assert "resolved_by_gate" not in outcome
        # OI-1571 tak 3, Klaar item 4: even the non-takeover fulfilment path
        # must fill fulfilled_by/takeover_gate/evidence_result_path.
        assert record["fulfilled_by"] == "codex_gate"
        assert record["takeover_gate"] is None
        assert record["evidence_result_path"] == record["result_path"]


# ---------------------------------------------------------------------------
# OI-1571 tak 3 (dispatch 20260830-153000-oi1569-quota-heeft-geen-tijddimensie):
# _has_decided_evidence never checked commit_sha, so evidence for a DIFFERENT
# commit than the PR's current head could still fulfil an obligation.
#
# TWO independently measured live shapes, both fixed by the SAME predicate
# (_has_decided_evidence(record, head_sha)), never two separate checks:
#
#   1. KRUIS-POORT (PR #1719): a codex-declared obligation booked fulfilled
#      via _find_takeover_successor_evidence off a glm_gate record from a
#      PRIOR commit -- the takeover-chain walk never checked the successor's
#      sha either.
#   2. ZELFDE-POORT (PR #1736): a codex-declared obligation booked fulfilled
#      off codex_gate's OWN result file, left on disk by an EARLIER dispatch
#      against the same PR and never overwritten by this attempt (the
#      gate_recorder overwrite guard preserves a decided verdict rather than
#      let a less-decided fresh attempt replace it -- see gate_executor.py's
#      OI-1488 note) -- the declared-gate gating check never checked sha
#      either, so it never even looked at the takeover chain.
# ---------------------------------------------------------------------------


class _NoOpReviewGateManager:
    """A ``request_and_execute`` that writes ONLY request records and
    touches NO result file at all -- mirrors the real overwrite guard
    (gate_recorder) refusing to let a fresh, less-decided attempt replace an
    existing decided verdict: from this runner's point of view, the result
    file on disk after the call is EXACTLY what it was before the call.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
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
                json.dumps({"gate": gate, "pr_number": pr_number, "status": "requested"}),
                encoding="utf-8",
            )
        return {"pr_number": pr_number, "branch": branch, "gates": [], "has_required_failure": False}


def _seed_decided_gate_result(
    state_dir: Path, gate: str, pr_number: int, *, commit_sha: str, report_path: Path,
    status: str = "pass", contract_hash: str = "sha256:deadbeef",
) -> None:
    """Pre-seed a gate's OWN result record as a DECIDED, complete-evidence
    verdict for ``commit_sha`` -- the shape a genuinely-reviewed commit
    leaves behind, used here to build a STALE record for an OLDER commit
    than the one :func:`_patch_manager`'s stub reports as the PR head.
    """
    (state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json").write_text(
        json.dumps({
            "gate": gate, "pr_number": pr_number, "status": status,
            "contract_hash": contract_hash, "report_path": str(report_path),
            "commit_sha": commit_sha,
        }),
        encoding="utf-8",
    )


class TestShaBindingBlocksStaleEvidence:
    @pytest.fixture(autouse=True)
    def _clean_takeover_chain_env(self, monkeypatch):
        monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)
        monkeypatch.delenv("VNX_OVERRIDE_VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)

    def test_kruis_poort_stale_takeover_successor_evidence_never_fulfills(self, tmp_path, monkeypatch):
        """(a) kruis-poort, PR #1719 shape: codex's own record stays stuck
        unavailable (lane_exhausted), and the takeover successor (glm_gate)
        DOES carry complete, decided evidence -- but for an OLDER commit
        than the PR's current head. Must NOT fulfil."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-oi1571-kruis-poort", gate="codex_gate",
            project_id="vnx-dev", pr_number=1719,
        )
        _seed_stuck_gate_result(state_dir, "codex_gate", 1719)
        report_file = state_dir / "unified_reports" / "glm-gate-pr1719.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("glm_gate report body", encoding="utf-8")

        manager = _TakeoverFakeReviewGateManager(
            state_dir, target_gate="glm_gate", status="pass", report_path=report_file,
            commit_sha="ffffffffffffffffffffffffffffffffffffff",  # a DIFFERENT commit than the head
        )
        _patch_manager(monkeypatch, manager)  # head_sha defaults to "deadbeef00" * 4

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 1, (
            "stale takeover evidence (wrong commit) must never fulfil the obligation"
        )
        record = _read_obligation(state_dir, "20260830-oi1571-kruis-poort")
        assert record["status"] == STATUS_PENDING
        assert "resolved_by_gate" not in record
        outcome = summary["outcomes"][0]
        assert outcome["action"] == "pending"

    def test_kruis_poort_matching_sha_still_fulfills(self, tmp_path, monkeypatch):
        """Control for (a): the exact same shape, but the successor's
        commit_sha matches the PR head -- must still fulfil via takeover
        (never a false negative introduced by the sha check)."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-oi1571-kruis-poort-match", gate="codex_gate",
            project_id="vnx-dev", pr_number=1720,
        )
        _seed_stuck_gate_result(state_dir, "codex_gate", 1720)
        report_file = state_dir / "unified_reports" / "glm-gate-pr1720.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("glm_gate report body", encoding="utf-8")

        manager = _TakeoverFakeReviewGateManager(
            state_dir, target_gate="glm_gate", status="pass", report_path=report_file,
            # default commit_sha matches _patch_manager's default head_sha
        )
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 0
        record = _read_obligation(state_dir, "20260830-oi1571-kruis-poort-match")
        assert record["status"] == STATUS_FULFILLED
        assert record["resolved_by_gate"] == "glm_gate"

    def test_zelfde_poort_stale_declared_gate_evidence_never_fulfills(self, tmp_path, monkeypatch):
        """(b) zelfde-poort, PR #1736 shape: codex_gate's OWN result record
        is a DECIDED, complete-evidence PASS -- but left on disk by an
        EARLIER dispatch against the same PR, for an OLDER commit. This
        attempt's manager.request_and_execute does not overwrite it (mirrors
        the real overwrite guard). Must NOT fulfil off it, and the takeover
        chain must actually be consulted (no successor exists here either,
        so it stays pending)."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-oi1571-zelfde-poort", gate="codex_gate",
            project_id="vnx-dev", pr_number=1736,
        )
        report_file = state_dir / "unified_reports" / "codex-gate-pr1736-earlier-dispatch.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("codex_gate report body from an earlier dispatch", encoding="utf-8")
        _seed_decided_gate_result(
            state_dir, "codex_gate", 1736,
            commit_sha="ffffffffffffffffffffffffffffffffffffff",  # a DIFFERENT, OLDER commit
            report_path=report_file,
        )

        manager = _NoOpReviewGateManager(state_dir)
        _patch_manager(monkeypatch, manager)  # head_sha defaults to "deadbeef00" * 4

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 1, (
            "a same-gate result left on disk for a DIFFERENT commit must "
            "never silently fulfil the obligation"
        )
        record = _read_obligation(state_dir, "20260830-oi1571-zelfde-poort")
        assert record["status"] == STATUS_PENDING
        assert record["reason"] == "stale_evidence_sha_mismatch"
        assert "fulfilled_by" not in record
        outcome = summary["outcomes"][0]
        assert outcome["action"] == "pending"

    def test_zelfde_poort_matching_sha_still_fulfills(self, tmp_path, monkeypatch):
        """Control for (b): codex_gate's own record is decided, complete,
        AND for the current head -- must fulfil directly, exactly as
        test_declared_gate_evidence_still_found_directly_when_present
        already proves for the takeover-chain-untouched case; this control
        additionally proves it through the _NoOpReviewGateManager shape."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-oi1571-zelfde-poort-match", gate="codex_gate",
            project_id="vnx-dev", pr_number=1737,
        )
        report_file = state_dir / "unified_reports" / "codex-gate-pr1737.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("codex_gate report body", encoding="utf-8")
        _seed_decided_gate_result(
            state_dir, "codex_gate", 1737,
            commit_sha="deadbeef00" * 4,  # matches _patch_manager's default head_sha
            report_path=report_file,
        )

        manager = _NoOpReviewGateManager(state_dir)
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 0
        record = _read_obligation(state_dir, "20260830-oi1571-zelfde-poort-match")
        assert record["status"] == STATUS_FULFILLED
        assert record["fulfilled_by"] == "codex_gate"

    def test_unknown_sha_binding_suspends_judgement_third_branch(self, tmp_path, monkeypatch):
        """The third branch: the declared gate's own record is decided and
        complete, but its commit_sha is EMPTY (unverifiable) -- must neither
        silently accept (fulfil) nor silently refuse (retire/escalate). It
        must stay pending with a reason that names the third branch
        explicitly, distinct from both the happy path and the mismatch path.
        """
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-oi1571-unknown-sha", gate="codex_gate",
            project_id="vnx-dev", pr_number=1738,
        )
        report_file = state_dir / "unified_reports" / "codex-gate-pr1738.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("codex_gate report body", encoding="utf-8")
        _seed_decided_gate_result(
            state_dir, "codex_gate", 1738, commit_sha="", report_path=report_file,
        )

        manager = _NoOpReviewGateManager(state_dir)
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert summary["pending_after"] == 1
        record = _read_obligation(state_dir, "20260830-oi1571-unknown-sha")
        assert record["status"] == STATUS_PENDING
        assert record["reason"] == "sha_binding_unverifiable"


# ---------------------------------------------------------------------------
# OI-1569 Klaar item 8: a loud tripwire, not just a code comment, for the
# exact measured signature of a silent stale-evidence fulfilment — a
# terminal booking whose evidence FILE predates the attempt that used it.
# The sha check above already prevents the specific PR #1719/#1736 defect
# from fulfilling silently; this is a second, independent, cheap safety net
# that stays useful even if some future change reopens a different hole.
# ---------------------------------------------------------------------------


class TestFastFulfillmentTripwire:
    def test_evidence_file_touched_by_this_attempt_is_silent(self, tmp_path, monkeypatch):
        """Control: the overwhelmingly common case — the result file is
        freshly written by THIS attempt — must never trip the warning."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-oi1569-fresh-evidence", gate="ci_gate",
            project_id="vnx-dev", pr_number=9670,
        )
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        outcome = summary["outcomes"][0]
        assert "fast_fulfillment_warning" not in outcome

    def test_stale_result_file_is_rescued_pre_execution_since_oi_1612(self, tmp_path, monkeypatch):
        """Superseded by OI-1612: before that fix, this exact record (no
        ``dispatch_id`` field at all — only ``pr_number``) was INVISIBLE to
        the pre-execution evidence lookup (dispatch_id-only join), so the
        RESOLVED branch always fell through to ``attempt_gate``, called the
        (no-op) manager, and read this same stale file back — which is what
        the mtime tripwire below used to catch as its only signal.

        OI-1612 indexes this record by ``(pr_number, gate)`` too, so the
        pre-execution rescue now finds and books it BEFORE ``attempt_gate``
        is ever reached — exactly the same class of wasted re-run OI-1508
        eliminated for the dispatch_id-matching case. The gate manager must
        never be invoked at all; the old mtime-staleness path this test used
        to exercise is now provably unreachable for a RESOLVED obligation
        with a pre-existing sha-matching decided result, since RESOLVED is
        the only resolution that ever reaches ``attempt_gate`` and it always
        supplies the same ``pr_number`` the pre-execution rescue now checks.
        """
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260830-oi1569-stale-evidence", gate="codex_gate",
            project_id="vnx-dev", pr_number=9671,
        )
        report_file = state_dir / "unified_reports" / "codex-gate-pr9671.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("codex_gate report body", encoding="utf-8")
        result_path = state_dir / "review_gates" / "results" / "pr-9671-codex_gate.json"
        result_path.write_text(
            json.dumps({
                "gate": "codex_gate", "pr_number": 9671, "status": "pass",
                "contract_hash": "sha256:deadbeef", "report_path": str(report_file),
                "commit_sha": _DEFAULT_TEST_HEAD_SHA,
            }),
            encoding="utf-8",
        )
        old_mtime = time.time() - (runner._FAST_FULFILLMENT_MTIME_THRESHOLD_SECONDS + 120)
        os.utime(result_path, (old_mtime, old_mtime))

        manager = _NoOpReviewGateManager(state_dir)
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_FULFILLED, "the sha still matches, so this must still fulfil"
        assert outcome["detail"] == (
            "rescued by the OI-1388 evidence discriminator — a gate had "
            "already reviewed and approved this dispatch"
        )
        assert manager.calls == [], (
            "OI-1612: pre-existing pr_number-matched evidence must be "
            "rescued BEFORE the gate manager is ever invoked — never "
            "re-attempted just because its dispatch_id does not match"
        )
        # OI-1764 point 2: this rescue books STATUS_FULFILLED from a result
        # file this attempt never touched (mtime set stale above) — exactly
        # the shape _flag_fast_fulfillment_if_evidence_predates_attempt
        # exists to flag. Before OI-1764 this early (pre-execution) route
        # never computed the warning at all, unlike the late (post-execution)
        # route, so it was silently absent here even though the underlying
        # evidence is exactly as stale-by-mtime as the case that route DOES
        # warn on.
        assert "fast_fulfillment_warning" in outcome
        assert "not provably a fresh run this cycle" in outcome["fast_fulfillment_warning"]


# ---------------------------------------------------------------------------
# OI-1508: the RESOLVED branch of _pre_execution_decision used to return
# {"kind": "attempt_gate"} unconditionally — never checking for existing
# evidence, never checking whether the PR was still open. Measured live
# against the central store: 258 obligations would re-run a gate (110-880s
# each), 95 of them already carrying their own pr_number, at least 19 of
# those already with a DECIDED PASS on disk, and a 40-PR sample coming back
# 36 MERGED / 4 CLOSED / 0 OPEN.
# ---------------------------------------------------------------------------


class TestResolvedBranchChecksEvidenceBeforeGating:
    """RED on unfixed main (measured 2026-09-02, the OI-1508 defect): a
    RESOLVED obligation (``pr_number`` already known) with a pre-existing
    DECIDED PASS for the same dispatch_id+gate still called the gate
    manager and booked the obligation via the unconditional "attempt_gate"
    path, never even looking at the evidence already on disk.

    Driven through the stable :func:`runner.run` entry point rather than
    calling ``_pre_execution_decision`` directly — the OI-1508 fix adds a
    ``state_dir`` parameter to that function, so a direct call would fail
    unfixed code on a signature mismatch (an interface error) instead of on
    the actual regression. Through ``run()`` the RED run fails on OBSERVED
    BEHAVIOR: the gate manager gets invoked when it must not be, and the
    booked reason differs.
    """

    def test_pre_existing_pass_evidence_is_stamped_without_gating(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260902-oi1508-resolved-evidence", gate="codex_gate",
            project_id="vnx-dev", pr_number=50001,
        )
        report_file = state_dir / "unified_reports" / "codex-gate-pr50001.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("codex_gate report body", encoding="utf-8")
        result_path = state_dir / "review_gates" / "results" / "pr-50001-codex_gate.json"
        result_path.write_text(
            json.dumps({
                "gate": "codex_gate", "pr_number": 50001,
                "dispatch_id": "20260902-oi1508-resolved-evidence",
                "status": "pass", "contract_hash": "sha256:deadbeef",
                "report_path": str(report_file), "commit_sha": _DEFAULT_TEST_HEAD_SHA,
            }),
            encoding="utf-8",
        )
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager)

        summary = runner.run(state_dir)

        assert manager.calls == [], (
            "existing evidence must be found BEFORE the RESOLVED branch "
            "gates — a gate must never be re-run when a decided verdict "
            "already exists for this dispatch+gate (OI-1508)"
        )
        record = _read_obligation(state_dir, "20260902-oi1508-resolved-evidence")
        assert record["status"] == STATUS_FULFILLED
        assert record["reason"] == "fulfilled_by_existing_evidence"
        assert record["result_path"] == str(result_path)
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_FULFILLED


class TestResolvedBranchPreExecutionDecision:
    """Direct unit coverage of every outcome the RESOLVED branch of
    ``_pre_execution_decision`` can now reach (OI-1508) — one test per tak,
    a positive alongside each negative so every individual check can fail
    on its own: bewijs-aanwezig-pass, bewijs-aanwezig-fail,
    sha-onverifieerbaar, PR-open, PR-merged, PR-closed, and
    PR-status-onbepaalbaar.
    """

    GATE = "codex_gate"
    DISPATCH_ID = "20260902-oi1508-branch-coverage"
    PR_NUMBER = 50100
    OWNER_REPO = "Vinix24/vnx-orchestration"
    HEAD_SHA = _DEFAULT_TEST_HEAD_SHA

    def _resolution(self) -> "runner.PrResolution":
        return runner.PrResolution(
            runner.RESOLUTION_RESOLVED, pr_number=self.PR_NUMBER, owner_repo=self.OWNER_REPO,
        )

    def _seed_evidence(self, state_dir: Path, *, status: str, commit_sha: str = "__default__") -> Path:
        report_file = state_dir / "unified_reports" / f"{self.GATE}-pr{self.PR_NUMBER}.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("report body", encoding="utf-8")
        result_path = state_dir / "review_gates" / "results" / f"pr-{self.PR_NUMBER}-{self.GATE}.json"
        result_path.write_text(
            json.dumps({
                "gate": self.GATE, "pr_number": self.PR_NUMBER, "dispatch_id": self.DISPATCH_ID,
                "status": status, "contract_hash": "sha256:deadbeef",
                "report_path": str(report_file),
                "commit_sha": self.HEAD_SHA if commit_sha == "__default__" else commit_sha,
            }),
            encoding="utf-8",
        )
        return result_path

    def _decide(self, state_dir: Path, monkeypatch, *, attempts: int = 1) -> dict:
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: self.HEAD_SHA)
        index = runner._index_gate_results(state_dir)
        return runner._pre_execution_decision(
            state_dir, self.DISPATCH_ID, self.GATE, self._resolution(), attempts, index,
        )

    # -- bewijs-aanwezig-pass ----------------------------------------------

    def test_evidence_present_pass_rescues_instead_of_gating(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        self._seed_evidence(state_dir, status="pass")
        decision = self._decide(state_dir, monkeypatch)
        assert decision["kind"] == "fulfill_by_evidence"

    # -- bewijs-aanwezig-fail -----------------------------------------------

    def test_evidence_present_fail_discharges_without_a_clean_pass(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        self._seed_evidence(state_dir, status="failed")
        decision = self._decide(state_dir, monkeypatch)
        assert decision["kind"] == "fulfill_by_failed_evidence"

    # -- sha-onverifieerbaar --------------------------------------------------

    def test_evidence_present_unverifiable_sha_suspends_judgement(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        self._seed_evidence(state_dir, status="pass", commit_sha="")
        decision = self._decide(state_dir, monkeypatch)
        assert decision["kind"] == "sha_unverifiable"

    # -- PR-open --------------------------------------------------------------

    def test_pr_open_still_attempts_the_gate(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr, owner_repo: "OPEN")
        decision = self._decide(state_dir, monkeypatch)
        assert decision["kind"] == "attempt_gate"

    # -- PR-merged --------------------------------------------------------------

    def test_pr_merged_retires_with_pr_merged_reason(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr, owner_repo: "MERGED")
        decision = self._decide(state_dir, monkeypatch)
        assert decision["kind"] == "retire"
        assert decision["retire_reason"] == REASON_PR_MERGED

    # -- PR-closed --------------------------------------------------------------

    def test_pr_closed_retires_with_pr_closed_reason(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr, owner_repo: "CLOSED")
        decision = self._decide(state_dir, monkeypatch)
        assert decision["kind"] == "retire"
        assert decision["retire_reason"] == REASON_PR_CLOSED

    # -- PR-status-onbepaalbaar ---------------------------------------------

    def test_pr_state_undetermined_neither_retires_nor_gates(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr, owner_repo: None)
        decision = self._decide(state_dir, monkeypatch, attempts=1)
        assert decision["kind"] == "unresolvable"
        assert decision["detail"] is not None and "could not be determined" in decision["detail"]

    def test_pr_state_undetermined_escalates_past_threshold(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr, owner_repo: None)
        decision = self._decide(
            state_dir, monkeypatch, attempts=runner._UNRESOLVABLE_ESCALATION_ATTEMPTS,
        )
        assert decision["kind"] == "escalate"


class TestResolvedBranchRetireIntegration:
    """End-to-end (through ``runner.run``) proof that a merged/closed PR
    without rescuing evidence is actually retired on disk, and that the
    boundary is hard: an OPEN PR is NEVER retired, regardless of age."""

    def test_merged_pr_without_evidence_is_retired(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260902-oi1508-merged-retire", gate="codex_gate",
            project_id="vnx-dev", pr_number=50002,
        )
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager, pr_state="MERGED")

        summary = runner.run(state_dir)

        assert manager.calls == [], "a merged PR must never re-fire the gate"
        record = _read_obligation(state_dir, "20260902-oi1508-merged-retire")
        assert record["status"] == STATUS_RETIRED
        assert record["reason"] == REASON_PR_MERGED
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_RETIRED

    def test_closed_pr_without_evidence_is_retired(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260902-oi1508-closed-retire", gate="codex_gate",
            project_id="vnx-dev", pr_number=50003,
        )
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager, pr_state="CLOSED")

        summary = runner.run(state_dir)

        assert manager.calls == [], "a closed PR must never re-fire the gate"
        record = _read_obligation(state_dir, "20260902-oi1508-closed-retire")
        assert record["status"] == STATUS_RETIRED
        assert record["reason"] == REASON_PR_CLOSED
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_RETIRED

    def test_open_pr_is_never_retired_gate_still_runs(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260902-oi1508-open-control", gate="codex_gate",
            project_id="vnx-dev", pr_number=50004,
        )
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager, pr_state="OPEN")

        summary = runner.run(state_dir)

        assert len(manager.calls) == 1, "an OPEN PR must still be gated"
        record = _read_obligation(state_dir, "20260902-oi1508-open-control")
        assert record["status"] == STATUS_FULFILLED
        assert record["status"] != STATUS_RETIRED
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_FULFILLED

    def test_undetermined_pr_state_stays_unresolvable_never_retired(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260902-oi1508-undetermined", gate="codex_gate",
            project_id="vnx-dev", pr_number=50005,
        )
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager, pr_state=None)

        summary = runner.run(state_dir)

        assert manager.calls == [], "an undeterminable PR state must never gate"
        record = _read_obligation(state_dir, "20260902-oi1508-undetermined")
        assert record["status"] == STATUS_UNRESOLVABLE
        assert record["status"] != STATUS_RETIRED
        outcome = summary["outcomes"][0]
        assert outcome["action"] == "unresolvable"


# ---------------------------------------------------------------------------
# OI-1612: the OI-1388 evidence index joined on (dispatch_id, gate) alone.
# A gate result record's dispatch_id names the POORTRUN that produced it,
# never the build dispatch the obligation was declared for — item 9's
# ``manager.request_and_execute(..., dispatch_id=dispatch_id)`` is the ONE
# exception, a result THIS runner writes for itself. For a RESOLVED
# obligation (pr_number already known), the old join could therefore never
# match a pre-existing result written by any OTHER process — measured live
# on vnx-dev: would_stamp stayed at 0 across 593 and 594 obligations, even
# though 345 of 524 results carried a dispatch_id (just never the right
# one). The fix also indexes results by (pr_number, gate) and has the
# RESOLVED branch of _pre_execution_decision look up by pr_number in
# addition to dispatch_id — AWAITING/UNRESOLVABLE, which never have a
# pr_number to look up by, are unchanged.
# ---------------------------------------------------------------------------


class TestEvidenceIndexJoinsOnPrNumber:
    """RED on #1745 (dispatch_id-only join) / GREEN on OI-1612 for the exact
    live case measured on vnx-dev: obligation
    ``20260902-oi1599-verplichting-krijgt-pr-r2`` (glm_gate, PR #1744) vs.
    result record ``pr-1744-glm_gate.json``, written under the poortrun's OWN
    dispatch_id (``glm-gate-pr1744-1788369563``), never the obligation's.
    """

    GATE = "glm_gate"
    PR_NUMBER = 1744
    OBLIGATION_DISPATCH_ID = "20260902-oi1599-verplichting-krijgt-pr-r2"
    POORTRUN_DISPATCH_ID = "glm-gate-pr1744-1788369563"
    HEAD_SHA = _DEFAULT_TEST_HEAD_SHA

    def _seed_result(
        self,
        state_dir: Path,
        *,
        dispatch_id: str,
        pr_number: int = PR_NUMBER,
        gate: str = GATE,
        status: str = "pass",
        commit_sha: "str | None" = "__default__",
        filename: "str | None" = None,
    ) -> Path:
        commit_sha = self.HEAD_SHA if commit_sha == "__default__" else commit_sha
        filename = filename or f"pr-{pr_number}-{gate}"
        report_file = state_dir / "unified_reports" / f"{filename}.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("report body", encoding="utf-8")
        result_path = state_dir / "review_gates" / "results" / f"{filename}.json"
        result_path.write_text(
            json.dumps({
                "gate": gate, "pr_number": pr_number, "dispatch_id": dispatch_id,
                "status": status, "contract_hash": "sha256:deadbeef",
                "report_path": str(report_file), "commit_sha": commit_sha,
            }),
            encoding="utf-8",
        )
        return result_path

    # -- the live case: RED on #1745, GREEN on OI-1612 (also condition a) --

    def test_pr_number_matched_evidence_rescues_when_dispatch_id_differs(self, tmp_path, monkeypatch):
        """The exact vnx-dev case (r2 / pr-1744-glm_gate). PR merged, so the
        ONLY thing standing between retire and fulfil is whether the
        evidence lookup can find this record at all — a poortrun's OWN
        dispatch_id, never the obligation's, is exactly what #1745 could not
        join on."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id=self.OBLIGATION_DISPATCH_ID, gate=self.GATE,
            project_id="vnx-dev", pr_number=self.PR_NUMBER,
        )
        self._seed_result(state_dir, dispatch_id=self.POORTRUN_DISPATCH_ID)
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager, pr_state="MERGED")

        summary = runner.run(state_dir)

        assert manager.calls == [], (
            "existing pr_number-matched evidence must be found BEFORE the "
            "RESOLVED branch retires or re-gates"
        )
        record = _read_obligation(state_dir, self.OBLIGATION_DISPATCH_ID)
        assert record["status"] == STATUS_FULFILLED, (
            f"expected fulfilled via the OI-1612 rescue, got {record['status']!r} "
            f"(reason={record.get('reason')!r}) — on #1745 this stays RETIRED "
            "because the dispatch_id-only join never finds a poortrun's own "
            "result record"
        )
        assert record["status"] != STATUS_RETIRED
        assert record["reason"] == "fulfilled_by_existing_evidence"
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_FULFILLED

    # -- condition b: AWAITING/UNRESOLVABLE keep working via dispatch_id ---

    def test_awaiting_branch_still_rescues_via_dispatch_id_match(self, tmp_path, monkeypatch):
        """Control for condition (b): an AWAITING obligation (no PR found,
        dead branch) has no pr_number to look up by at all — it must keep
        finding evidence exactly the way it did before OI-1612, via a
        dispatch_id match."""
        state_dir = _make_state_dir(tmp_path)
        dispatch_id = "20260902-oi1612-awaiting-dispatch-id-match"
        register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
        )
        monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
        monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
        monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: None)
        monkeypatch.setattr(runner, "_branch_exists_on_github", lambda did, owner_repo: False)
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: self.HEAD_SHA)
        self._seed_result(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", pr_number=50200,
            filename="pr-50200-codex_gate",
        )

        summary = runner.run(state_dir)

        record = _read_obligation(state_dir, dispatch_id)
        assert record["status"] == STATUS_FULFILLED
        assert record["status"] != STATUS_RETIRED
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_FULFILLED

    def test_fulfilling_result_without_pr_number_ignores_by_pr_index(self, tmp_path, monkeypatch):
        """Direct boundary proof for (b): when a caller passes no
        ``pr_number`` (exactly what the AWAITING/UNRESOLVABLE call sites
        do), a record only reachable via ``index.by_pr`` must NOT be found —
        even though the SAME gate result would be found if ``pr_number``
        were supplied. Proves those two call sites cannot accidentally start
        matching on pr_number just because the index now carries one."""
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: self.HEAD_SHA)
        state_dir = _make_state_dir(tmp_path)
        self._seed_result(
            state_dir, dispatch_id="", pr_number=50201, gate="codex_gate",
            filename="pr-50201-codex_gate",
        )
        index = runner._index_gate_results(state_dir)

        no_pr = runner._fulfilling_result(index, "some-other-dispatch-id", "codex_gate")
        assert no_pr["kind"] == "absent"

        with_pr = runner._fulfilling_result(
            index, "some-other-dispatch-id", "codex_gate", pr_number=50201,
        )
        assert with_pr["kind"] == "found"

    # -- condition c: a pr_number-only record (no dispatch_id) must not be -
    # silently skipped anymore ----------------------------------------------

    def test_index_places_dispatch_id_less_record_under_by_pr(self, tmp_path):
        state_dir = _make_state_dir(tmp_path)
        self._seed_result(
            state_dir, dispatch_id="", pr_number=50202, gate="codex_gate",
            filename="pr-50202-codex_gate",
        )
        index = runner._index_gate_results(state_dir)
        assert index.by_pr.get((50202, "codex_gate"))
        assert index.by_dispatch == {}

    def test_record_without_dispatch_id_or_pr_number_is_never_indexed(self, tmp_path):
        """Negative control for (c): a record needs at least ONE of
        dispatch_id/pr_number (plus gate) to be indexed at all — a record
        carrying neither must still be invisible, exactly as before
        OI-1612."""
        state_dir = _make_state_dir(tmp_path)
        report_file = state_dir / "unified_reports" / "orphan.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("report body", encoding="utf-8")
        result_path = state_dir / "review_gates" / "results" / "orphan-codex_gate.json"
        result_path.write_text(
            json.dumps({
                "gate": "codex_gate", "pr_number": None, "dispatch_id": "",
                "status": "pass", "contract_hash": "sha256:deadbeef",
                "report_path": str(report_file), "commit_sha": self.HEAD_SHA,
            }),
            encoding="utf-8",
        )
        index = runner._index_gate_results(state_dir)
        assert index.by_dispatch == {}
        assert index.by_pr == {}

    # -- condition d: pr_number-matched evidence about a DIFFERENT commit --
    # is still rejected, unchanged -------------------------------------------

    def test_pr_number_matched_evidence_with_wrong_sha_is_not_rescued(self, tmp_path, monkeypatch):
        """Negative: same live shape as the RED/GREEN case above, except the
        pr_number-matched record is about a DIFFERENT commit. The
        pr_number-based lookup must not bypass the sha binding check — this
        must still retire, exactly as the mismatched-evidence path already
        did before OI-1612."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260902-oi1612-wrong-sha", gate=self.GATE,
            project_id="vnx-dev", pr_number=self.PR_NUMBER,
        )
        self._seed_result(
            state_dir, dispatch_id="glm-gate-pr1744-other-run",
            commit_sha="c0ffee00" * 5,
        )
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager, pr_state="MERGED")

        summary = runner.run(state_dir)

        assert manager.calls == [], "a merged PR must never re-fire the gate"
        record = _read_obligation(state_dir, "20260902-oi1612-wrong-sha")
        assert record["status"] == STATUS_RETIRED
        assert record["status"] != STATUS_FULFILLED
        assert "rejected as rescue evidence" in record["reason_detail"], (
            "the retired obligation's own record must document that a "
            "pr_number-matched candidate existed but was rejected on its "
            "sha binding, not silently ignored"
        )
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_RETIRED

    def test_pr_number_matched_evidence_with_matching_sha_is_rescued(self, tmp_path, monkeypatch):
        """Positive control alongside (d): identical setup with the correct
        sha must fulfil — proves the rejection above is about the sha, not
        some other accidental difference."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260902-oi1612-right-sha", gate=self.GATE,
            project_id="vnx-dev", pr_number=self.PR_NUMBER,
        )
        self._seed_result(state_dir, dispatch_id="glm-gate-pr1744-other-run-2")
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager, pr_state="MERGED")

        summary = runner.run(state_dir)

        assert manager.calls == []
        record = _read_obligation(state_dir, "20260902-oi1612-right-sha")
        assert record["status"] == STATUS_FULFILLED
        outcome = summary["outcomes"][0]
        assert outcome["action"] == STATUS_FULFILLED

    # -- condition e: two obligations sharing a PR share the SAME evidence -
    # legitimately (one file is the single source of truth for that
    # (pr_number, gate)) — and share the SAME sha rejection too ------------

    def test_two_obligations_same_pr_both_rescued_by_shared_evidence(self, tmp_path, monkeypatch):
        """Positive: obligation A and obligation B both declare glm_gate
        against the SAME PR (a re-dispatched fix-forward, e.g. #1729's three
        obligations) — the single per-(pr_number, gate) result file is
        legitimate evidence for BOTH, since it genuinely reflects the state
        of that one PR; this is sharing, not stealing."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260902-oi1612-shared-a", gate=self.GATE,
            project_id="vnx-dev", pr_number=self.PR_NUMBER,
        )
        register_obligation(
            state_dir, dispatch_id="20260902-oi1612-shared-b", gate=self.GATE,
            project_id="vnx-dev", pr_number=self.PR_NUMBER,
        )
        self._seed_result(state_dir, dispatch_id="glm-gate-pr1744-shared-run")
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager, pr_state="MERGED")

        summary = runner.run(state_dir)

        assert manager.calls == []
        record_a = _read_obligation(state_dir, "20260902-oi1612-shared-a")
        record_b = _read_obligation(state_dir, "20260902-oi1612-shared-b")
        assert record_a["status"] == STATUS_FULFILLED
        assert record_b["status"] == STATUS_FULFILLED
        assert record_a["result_path"] == record_b["result_path"]

    def test_two_obligations_same_pr_neither_rescued_by_mismatched_evidence(self, tmp_path, monkeypatch):
        """Negative alongside (e): sharing a pr_number must never let EITHER
        obligation bypass the sha check — both must be rejected identically
        when the shared record is about a different commit."""
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260902-oi1612-shared-mismatch-a", gate=self.GATE,
            project_id="vnx-dev", pr_number=self.PR_NUMBER,
        )
        register_obligation(
            state_dir, dispatch_id="20260902-oi1612-shared-mismatch-b", gate=self.GATE,
            project_id="vnx-dev", pr_number=self.PR_NUMBER,
        )
        self._seed_result(
            state_dir, dispatch_id="glm-gate-pr1744-shared-mismatch-run",
            commit_sha="badc0de0" * 5,
        )
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager, pr_state="MERGED")

        summary = runner.run(state_dir)

        assert manager.calls == []
        record_a = _read_obligation(state_dir, "20260902-oi1612-shared-mismatch-a")
        record_b = _read_obligation(state_dir, "20260902-oi1612-shared-mismatch-b")
        assert record_a["status"] == STATUS_RETIRED
        assert record_b["status"] == STATUS_RETIRED


# ---------------------------------------------------------------------------
# OI-1751: a PR the obligation store can never see
#
# Every obligation this runner acts on already exists as a file on disk
# before this module runs (registered at ``vnx dispatch`` time). The
# per-obligation loop can therefore never notice a PR that never got an
# obligation registered in the first place — and PR discovery in this file
# only ever works by matching a head branch against
# ``dispatch/<dispatch_id>`` (:func:`gate_obligation_runner._pr_from_github`).
# A PR on any other branch (``docs/...``, ``fix/...``) is invisible to the
# whole mechanism. Measured live on mission-control: five poortruns in one
# day, none invoked the review-gate stack, because that day's PRs were named
# ``docs/`` and ``fix/``.
# ---------------------------------------------------------------------------


class TestFindPRsWithoutObligation:
    """Unit coverage for :func:`gate_obligation_runner._find_prs_without_obligation`."""

    def test_flags_pr_matching_neither_branch_nor_pr_number(self, tmp_path):
        state_dir = _make_state_dir(tmp_path)
        open_prs = [
            {"number": 10, "headRefName": "dispatch/20260910-known"},
            {"number": 20, "headRefName": "fix/typo"},
        ]
        obligations = [(state_dir / "obl.json", {"dispatch_id": "20260910-known"})]

        result = runner._find_prs_without_obligation(open_prs, obligations)

        assert [r["pr_number"] for r in result] == [20]
        assert result[0]["branch"] == "fix/typo"
        assert "20" in result[0]["reason"]

    def test_matches_by_stamped_pr_number_even_off_convention_branch(self, tmp_path):
        """A rework/RESOLVED obligation stamps ``pr_number`` directly onto its
        record — that alone must be enough to clear a PR, even when its
        branch does not (or no longer) match ``dispatch/<id>``."""
        state_dir = _make_state_dir(tmp_path)
        open_prs = [{"number": 30, "headRefName": "feat/whatever"}]
        obligations = [
            (state_dir / "obl.json", {"dispatch_id": "20260910-rework", "pr_number": 30}),
        ]

        result = runner._find_prs_without_obligation(open_prs, obligations)

        assert result == []

    def test_gh_unavailable_returns_empty_not_a_crash(self):
        result = runner._find_prs_without_obligation(None, [])

        assert result == []


class TestListOpenPRs:
    """Unit coverage for :func:`gate_obligation_runner._list_open_prs` — the
    single shared open-PR fetch OI-1764's fix-forward factored out so
    :func:`_find_prs_without_obligation` and :func:`_stale_terminal_evidence`
    never each issue their own `gh pr list --state open` call."""

    def test_normal_list_is_not_suspect(self, monkeypatch):
        monkeypatch.setattr(
            runner, "_gh_json",
            lambda args, owner_repo=None: [{"number": 1, "headRefName": "dispatch/x"}],
        )

        result = runner._list_open_prs("Vinix24/vnx-orchestration")

        assert result["prs"] == [{"number": 1, "headRefName": "dispatch/x"}]
        assert result["suspect"] is False

    def test_gh_failure_is_none_and_suspect(self, monkeypatch):
        monkeypatch.setattr(runner, "_gh_json", lambda args, owner_repo=None: None)

        result = runner._list_open_prs("Vinix24/vnx-orchestration")

        assert result["prs"] is None
        assert result["suspect"] is True

    def test_list_at_limit_cap_is_suspect(self, monkeypatch):
        """OI-1766: a list hitting the --limit cap exactly might be
        truncated — gh gives no way to tell, so it must be flagged."""
        capped = [{"number": n, "headRefName": f"dispatch/{n}"} for n in range(runner._OPEN_PR_LIST_LIMIT)]
        monkeypatch.setattr(runner, "_gh_json", lambda args, owner_repo=None: capped)

        result = runner._list_open_prs("Vinix24/vnx-orchestration")

        assert result["prs"] == capped
        assert result["suspect"] is True

    def test_list_under_limit_is_not_suspect(self, monkeypatch):
        under_cap = [
            {"number": n, "headRefName": f"dispatch/{n}"}
            for n in range(runner._OPEN_PR_LIST_LIMIT - 1)
        ]
        monkeypatch.setattr(runner, "_gh_json", lambda args, owner_repo=None: under_cap)

        result = runner._list_open_prs("Vinix24/vnx-orchestration")

        assert result["suspect"] is False


class TestPRsWithoutObligationReportedByRun:
    """Integration coverage through :func:`gate_obligation_runner.run` — the
    LOUD reporting this dispatch requires, not just the helper's own return
    value.

    RED on unfixed main (measured against this worktree's pre-fix
    ``gate_obligation_runner.py``): ``run()``'s summary carries no
    ``prs_without_obligation`` key at all (``.get(...)`` degrades to
    ``None``/``[]``), and nothing is logged for PR #4242's ``docs/fix-typo``
    branch — the assertion fails on the missing BEHAVIOR (a clean
    ``AssertionError`` with the message below), never on an
    ``AttributeError``/``ImportError``, because every symbol the test touches
    (``runner.run``, ``summary.get``) already exists on unfixed main.
    """

    def test_pr_without_obligation_is_reported_not_silently_skipped(
        self, tmp_path, monkeypatch, caplog,
    ):
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="20260917-oi1751-linked", gate="codex_gate",
            project_id="vnx-dev", pr_number=4241,
        )
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager)

        def fake_gh_json(args, *, owner_repo=None):
            if args[:4] == ["pr", "list", "--state", "open"]:
                return [
                    {"number": 4241, "headRefName": "dispatch/20260917-oi1751-linked"},
                    {"number": 4242, "headRefName": "docs/fix-typo"},
                ]
            return None

        monkeypatch.setattr(runner, "_gh_json", fake_gh_json)
        caplog.set_level("WARNING", logger="gate_obligation_runner")

        summary = runner.run(state_dir)

        unlinked = summary.get("prs_without_obligation") or []
        assert any(u.get("pr_number") == 4242 for u in unlinked), (
            "PR #4242 (branch docs/fix-typo, no obligation) must be reported "
            "by run(), not silently dropped from the poortstapel (OI-1751)"
        )
        entry = next(u for u in unlinked if u["pr_number"] == 4242)
        assert entry["branch"] == "docs/fix-typo"
        assert "4242" in entry["reason"]
        assert any("4242" in record.message for record in caplog.records), (
            "a PR without an obligation must be logged loudly (WARNING), not "
            "only returned in the summary dict"
        )
        # Control: the properly-linked PR must never be flagged.
        assert not any(u.get("pr_number") == 4241 for u in unlinked)

    def test_no_owner_repo_degrades_to_empty_not_a_crash(self, tmp_path, monkeypatch):
        """No GitHub owner/repo resolves (e.g. a local-only checkout) —
        the sweep must degrade to an empty finding list, exactly like every
        other owner_repo-gated lookup in this module, never raise."""
        state_dir = _make_state_dir(tmp_path)
        manager = _FakeReviewGateManager(state_dir, result_status="pass")
        _patch_manager(monkeypatch, manager)
        monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: None)

        summary = runner.run(state_dir, write=False)

        assert summary.get("prs_without_obligation") == []


# ---------------------------------------------------------------------------
# OI-1764: terminal evidence that went stale AFTER the obligation closed.
# The per-obligation loop skips every already-terminal record outright
# (``if status in TERMINAL_STATUSES: continue``) — nothing else in this
# runner ever looks at one again, so a PR that landed a fix-forward push
# AFTER its gate obligation already closed fulfilled/failed goes unnoticed
# forever. :func:`_stale_terminal_evidence` is the read-only detector.
# ---------------------------------------------------------------------------


def _write_gate_result(
    state_dir: Path, name: str, *, commit_sha: str, status: str = "pass",
    pr_number: int = 1864, gate: str = "codex_gate",
) -> Path:
    path = state_dir / "review_gates" / "results" / name
    path.write_text(
        json.dumps({
            "gate": gate, "pr_number": pr_number, "status": status,
            "commit_sha": commit_sha,
        }),
        encoding="utf-8",
    )
    return path


def _open_pr_lookup(*numbers: int, suspect: bool = False) -> dict:
    """Build a :func:`gate_obligation_runner._list_open_prs`-shaped result
    marking exactly ``numbers`` as open — the shared fixture every
    ``_stale_terminal_evidence`` unit test uses now that the function is
    scoped to open PRs (OI-1764 fix-forward, PR #1865 review)."""
    return {
        "prs": [{"number": n, "headRefName": f"dispatch/pr-{n}"} for n in numbers],
        "suspect": suspect,
    }


class TestStaleTerminalEvidenceDetection:
    """Unit coverage for :func:`gate_obligation_runner._stale_terminal_evidence`."""

    def test_mismatch_is_reported_in_its_own_bucket(self, tmp_path, monkeypatch):
        """RED before OI-1764: a terminal obligation whose evidence names a
        commit that is no longer the PR head was invisible — this function
        did not exist and nothing else ever re-examines a terminal record.
        A REAL, differing sha on each side, never an empty one — an empty
        sha would test the ``unknown`` branch below, not ``mismatch``."""
        state_dir = _make_state_dir(tmp_path)
        old_sha = "e222601b" + "0" * 32
        new_sha = "ff5183e8" + "1" * 32
        result_path = _write_gate_result(state_dir, "pr-1864-codex_gate.json", commit_sha=old_sha)
        record = {
            "dispatch_id": "20260910-oi1764-mismatch", "gate": "codex_gate", "pr_number": 1864,
            "status": STATUS_FULFILLED, "evidence_result_path": str(result_path),
        }
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: new_sha)

        findings = runner._stale_terminal_evidence(
            [(state_dir / "obl.json", record)], _open_pr_lookup(1864),
        )

        assert len(findings) == 1
        finding = findings[0]
        assert finding["binding"] == "mismatch"
        assert finding["dispatch_id"] == "20260910-oi1764-mismatch"
        assert finding["evidence_commit_sha"] == old_sha
        assert finding["head_sha"] == new_sha
        assert old_sha[:8] in finding["detail"]
        assert new_sha[:8] in finding["detail"]

    def test_unknown_is_reported_distinctly_from_mismatch(self, tmp_path, monkeypatch):
        """An empty commit_sha must land in the THIRD bucket (unknown), never
        silently folded into mismatch or silently cleared as a match."""
        state_dir = _make_state_dir(tmp_path)
        result_path = _write_gate_result(state_dir, "pr-1865-codex_gate.json", commit_sha="")
        record = {
            "dispatch_id": "20260910-oi1764-unknown", "gate": "codex_gate", "pr_number": 1865,
            "status": STATUS_FAILED, "evidence_result_path": str(result_path),
        }
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: "cccccccc" * 5)

        findings = runner._stale_terminal_evidence(
            [(state_dir / "obl.json", record)], _open_pr_lookup(1865),
        )

        assert len(findings) == 1
        assert findings[0]["binding"] == "unknown"
        assert findings[0]["dispatch_id"] == "20260910-oi1764-unknown"

    def test_match_is_the_silent_control_case(self, tmp_path, monkeypatch):
        """Control, required: without this, every open PR whose evidence
        is genuinely current would also get flagged."""
        state_dir = _make_state_dir(tmp_path)
        current_sha = "dddddddd" * 5
        result_path = _write_gate_result(state_dir, "pr-1866-codex_gate.json", commit_sha=current_sha)
        record = {
            "dispatch_id": "20260910-oi1764-match", "gate": "codex_gate", "pr_number": 1866,
            "status": STATUS_FULFILLED, "evidence_result_path": str(result_path),
        }
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: current_sha)

        findings = runner._stale_terminal_evidence(
            [(state_dir / "obl.json", record)], _open_pr_lookup(1866),
        )

        assert findings == []

    def test_non_terminal_obligation_is_skipped(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        result_path = _write_gate_result(state_dir, "pr-1867-codex_gate.json", commit_sha="eeeeeeee" * 5)
        record = {
            "dispatch_id": "20260910-oi1764-pending", "gate": "codex_gate", "pr_number": 1867,
            "status": STATUS_PENDING, "evidence_result_path": str(result_path),
        }
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: "ffffffff" * 5)

        findings = runner._stale_terminal_evidence(
            [(state_dir / "obl.json", record)], _open_pr_lookup(1867),
        )

        assert findings == []

    def test_retired_without_evidence_path_is_skipped_without_a_gh_call(self, tmp_path, monkeypatch):
        """A retired obligation carries no result/evidence path by
        construction (nothing ever reviewed it) — must be skipped before any
        PR-head lookup is even attempted, never a wasted/erroring gh call.

        PR #1865 review (second fix-forward): ``pr_number`` is deliberately
        IN the open-PR set here (unlike the original version of this test,
        which left it ``None`` against an empty open-PR list) — otherwise
        the skip could just as well be caused by the open-PR-set check a few
        lines above the evidence-path check, and this test would pass even
        if the evidence-path branch were broken. Only the missing evidence
        path may cause the skip now."""
        state_dir = _make_state_dir(tmp_path)

        def _boom(pr_number):
            raise AssertionError("must not resolve a PR head for evidence-less obligation")

        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", _boom)
        record = {
            "dispatch_id": "20260910-oi1764-retired", "gate": "codex_gate", "pr_number": 9010,
            "status": STATUS_RETIRED, "evidence_result_path": None, "result_path": None,
        }

        findings = runner._stale_terminal_evidence(
            [(state_dir / "obl.json", record)], _open_pr_lookup(9010),
        )

        assert findings == []

    def test_missing_evidence_file_is_reported_as_unknown_not_a_crash(self, tmp_path, monkeypatch):
        """The obligation POINTS at an evidence file that is no longer on
        disk (pruned, moved). Must never raise, and must land in the
        ``unknown`` bucket — this is exactly the undeterminable case, not a
        silent match and not a silent mismatch."""
        state_dir = _make_state_dir(tmp_path)
        missing_path = state_dir / "review_gates" / "results" / "pr-1869-codex_gate.json"
        record = {
            "dispatch_id": "20260910-oi1764-missing-evidence", "gate": "codex_gate",
            "pr_number": 1869, "status": STATUS_FULFILLED,
            "evidence_result_path": str(missing_path),
        }
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: "abababab" * 5)

        findings = runner._stale_terminal_evidence(
            [(state_dir / "obl.json", record)], _open_pr_lookup(1869),
        )

        assert len(findings) == 1
        assert findings[0]["binding"] == "unknown"
        assert "unreadable" in findings[0]["detail"] or "could not be verified" in findings[0]["detail"]

    def test_falls_back_to_result_path_when_no_evidence_result_path(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        old_sha = "11111111" * 5
        new_sha = "22222222" * 5
        result_path = _write_gate_result(state_dir, "pr-1868-codex_gate.json", commit_sha=old_sha)
        record = {
            "dispatch_id": "20260910-oi1764-fallback", "gate": "codex_gate", "pr_number": 1868,
            "status": STATUS_FULFILLED, "result_path": str(result_path),
        }
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: new_sha)

        findings = runner._stale_terminal_evidence(
            [(state_dir / "obl.json", record)], _open_pr_lookup(1868),
        )

        assert len(findings) == 1
        assert findings[0]["binding"] == "mismatch"


# ---------------------------------------------------------------------------
# OI-1764 fix-forward (PR #1865 review): scope to OPEN PRs only.
#
# The first shipped version resolved the PR head for EVERY unique PR number
# across the full store, terminal or not, open or closed — measured live
# 2026-09-17: 291 unique PR numbers, ~0.53s per resolution, ~156s per run()
# call, unconditionally. A closed/merged PR's head is frozen, so an
# obligation that just misses it would be reported as "stale" on every
# future run forever — permanent noise. These tests prove the detector now
# resolves a head ONLY for a PR that is BOTH open AND carries a terminal
# obligation with an evidence path, and that a failed/suspect open-PR list
# never silently reads as "nothing is stale" (OI-1766).
# ---------------------------------------------------------------------------


class TestStaleTerminalEvidenceOpenPrScoping:
    def test_closed_pr_terminal_evidence_is_never_head_resolved(self, tmp_path, monkeypatch):
        """RED before this fix-forward: the detector took no open-PR list at
        all and resolved every unique PR number in the store, closed or
        not. Call-counted via monkeypatch — never a real network hit."""
        state_dir = _make_state_dir(tmp_path)
        closed_result = _write_gate_result(
            state_dir, "pr-9001-codex_gate.json", commit_sha="11111111" * 5, pr_number=9001,
        )
        closed_record = {
            "dispatch_id": "20260910-closed", "gate": "codex_gate", "pr_number": 9001,
            "status": STATUS_FULFILLED, "evidence_result_path": str(closed_result),
        }
        open_result = _write_gate_result(
            state_dir, "pr-9002-codex_gate.json", commit_sha="22222222" * 5, pr_number=9002,
        )
        open_record = {
            "dispatch_id": "20260910-open", "gate": "codex_gate", "pr_number": 9002,
            "status": STATUS_FULFILLED, "evidence_result_path": str(open_result),
        }
        calls: list = []

        def counting_head(pr_number):
            calls.append(pr_number)
            return "33333333" * 5

        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", counting_head)

        runner._stale_terminal_evidence(
            [(state_dir / "obl1.json", closed_record), (state_dir / "obl2.json", open_record)],
            _open_pr_lookup(9002),  # 9001 is deliberately NOT in the open list
        )

        assert calls == [9002], (
            "the detector must resolve the head ONLY for PR #9002 (open) — "
            "PR #9001 is not in the open-PR list and must never be resolved "
            "(OI-1764 fix-forward: 291 resolutions -> 3 on the live store)"
        )

    def test_open_pr_with_stale_evidence_is_still_reported(self, tmp_path, monkeypatch):
        """Control: scoping to open PRs must not gut the detector — an open
        PR with genuinely stale evidence is still reported."""
        state_dir = _make_state_dir(tmp_path)
        old_sha = "44444444" * 5
        new_sha = "55555555" * 5
        result_path = _write_gate_result(
            state_dir, "pr-9003-codex_gate.json", commit_sha=old_sha, pr_number=9003,
        )
        record = {
            "dispatch_id": "20260910-open-stale", "gate": "codex_gate", "pr_number": 9003,
            "status": STATUS_FULFILLED, "evidence_result_path": str(result_path),
        }
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: new_sha)

        findings = runner._stale_terminal_evidence(
            [(state_dir / "obl.json", record)], _open_pr_lookup(9003),
        )

        assert len(findings) == 1
        assert findings[0]["binding"] == "mismatch"
        assert findings[0]["dispatch_id"] == "20260910-open-stale"

    def test_gh_failure_reports_undetermined_not_silence(self, tmp_path, monkeypatch):
        """A failed open-PR list (``gh`` itself failed) must never read as
        'nothing is stale' (OI-1766) — a loud, explicit undetermined
        finding instead, and the per-obligation sweep must not even
        attempt a head resolution it cannot trust the scope of."""
        state_dir = _make_state_dir(tmp_path)
        result_path = _write_gate_result(
            state_dir, "pr-9004-codex_gate.json", commit_sha="66666666" * 5, pr_number=9004,
        )
        record = {
            "dispatch_id": "20260910-list-failed", "gate": "codex_gate", "pr_number": 9004,
            "status": STATUS_FULFILLED, "evidence_result_path": str(result_path),
        }

        def _boom(pr_number):
            raise AssertionError("must not resolve any PR head when the open-PR list itself failed")

        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", _boom)

        findings = runner._stale_terminal_evidence(
            [(state_dir / "obl.json", record)], {"prs": None, "suspect": True},
        )

        assert len(findings) == 1
        assert findings[0]["binding"] == "unknown"
        assert findings[0]["dispatch_id"] is None
        assert "undetermined" in findings[0]["detail"]

    def test_suspect_truncated_list_still_reports_and_appends_warning(self, tmp_path, monkeypatch):
        """The open-PR list hit the --limit cap (OI-1766): the PRs actually
        returned are still checked (real findings must not be dropped), but
        an extra loud finding notes coverage is not guaranteed complete."""
        state_dir = _make_state_dir(tmp_path)
        old_sha = "77777777" * 5
        new_sha = "88888888" * 5
        result_path = _write_gate_result(
            state_dir, "pr-9005-codex_gate.json", commit_sha=old_sha, pr_number=9005,
        )
        record = {
            "dispatch_id": "20260910-suspect", "gate": "codex_gate", "pr_number": 9005,
            "status": STATUS_FULFILLED, "evidence_result_path": str(result_path),
        }
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: new_sha)

        findings = runner._stale_terminal_evidence(
            [(state_dir / "obl.json", record)], _open_pr_lookup(9005, suspect=True),
        )

        assert len(findings) == 2
        real = [f for f in findings if f["dispatch_id"] == "20260910-suspect"]
        assert len(real) == 1
        assert real[0]["binding"] == "mismatch"
        coverage_warning = [f for f in findings if f["dispatch_id"] is None]
        assert len(coverage_warning) == 1
        assert coverage_warning[0]["binding"] == "unknown"
        assert "undetermined" in coverage_warning[0]["detail"]


class TestStaleTerminalEvidenceReportedByRun:
    """Integration coverage through :func:`gate_obligation_runner.run` — the
    diagnostic must surface in the summary AND must never mutate the
    obligation it reports on."""

    def test_run_reports_stale_terminal_evidence_and_mutates_nothing(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(
            runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration",
        )
        monkeypatch.setattr(
            runner, "_gh_json",
            lambda args, owner_repo=None: (
                [{"number": 1864, "headRefName": "dispatch/whatever"}]
                if args[:4] == ["pr", "list", "--state", "open"] else None
            ),
        )
        dispatch_id = "20260917-oi1764-fixforward"
        obligation_file = register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate",
            project_id="vnx-dev", pr_number=1864,
        )
        old_sha = "e222601b" + "0" * 32
        new_sha = "ff5183e8" + "1" * 32
        result_path = _write_gate_result(
            state_dir, "pr-1864-codex_gate.json", commit_sha=old_sha, pr_number=1864,
        )
        update_obligation(
            obligation_file,
            status=STATUS_FULFILLED,
            pr_number=1864,
            result_path=str(result_path),
            evidence_result_path=str(result_path),
            resolved_at="2026-09-10T00:00:00Z",
            reason="fulfilled_by_existing_evidence",
        )
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: new_sha)

        before_obligation = obligation_file.read_bytes()
        before_result = result_path.read_bytes()

        summary = runner.run(state_dir)

        # Third case, required: the detection pass must mutate NEITHER file,
        # byte for byte.
        assert obligation_file.read_bytes() == before_obligation
        assert result_path.read_bytes() == before_result

        findings = summary.get("stale_terminal_evidence") or []
        assert len(findings) == 1
        finding = findings[0]
        assert finding["dispatch_id"] == dispatch_id
        assert finding["binding"] == "mismatch"
        assert summary.get("stale_terminal_evidence_mismatch_count") == 1
        assert summary.get("stale_terminal_evidence_unknown_count") == 0
        # Reviewed, just not of the code now on the PR — never counted
        # toward "still needs review".
        assert summary["pending_after"] == 0

    def test_run_stays_silent_when_evidence_is_still_current(self, tmp_path, monkeypatch):
        """Control through the real entry point: a terminal obligation whose
        evidence sha still matches the PR head must produce no finding and
        no count — otherwise every closed PR in the store would show up."""
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(
            runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration",
        )
        monkeypatch.setattr(
            runner, "_gh_json",
            lambda args, owner_repo=None: (
                [{"number": 1862, "headRefName": "dispatch/whatever"}]
                if args[:4] == ["pr", "list", "--state", "open"] else None
            ),
        )
        dispatch_id = "20260917-oi1764-current"
        obligation_file = register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate",
            project_id="vnx-dev", pr_number=1862,
        )
        current_sha = "465e083c" + "2" * 32
        result_path = _write_gate_result(
            state_dir, "pr-1862-codex_gate.json", commit_sha=current_sha, pr_number=1862,
        )
        update_obligation(
            obligation_file,
            status=STATUS_FULFILLED,
            pr_number=1862,
            result_path=str(result_path),
            evidence_result_path=str(result_path),
            resolved_at="2026-09-10T00:00:00Z",
            reason="fulfilled_by_existing_evidence",
        )
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: current_sha)

        summary = runner.run(state_dir)

        assert summary.get("stale_terminal_evidence") == []
        assert summary.get("stale_terminal_evidence_mismatch_count") == 0
        assert summary.get("stale_terminal_evidence_unknown_count") == 0

    def test_closed_pr_stale_evidence_is_no_longer_reported(self, tmp_path, monkeypatch):
        """OI-1764 fix-forward, item 2: a MERGED PR's head is frozen, so its
        terminal evidence would be reported as stale on EVERY future run
        forever — permanent noise, not a signal. A closed PR must produce
        no finding at all, even with a genuine sha mismatch on disk."""
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(
            runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration",
        )
        monkeypatch.setattr(
            runner, "_gh_json",
            lambda args, owner_repo=None: (
                [] if args[:4] == ["pr", "list", "--state", "open"] else None
            ),
        )
        dispatch_id = "20260917-oi1764-closed"
        obligation_file = register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate",
            project_id="vnx-dev", pr_number=1863,
        )
        old_sha = "e222601b" + "0" * 32
        result_path = _write_gate_result(
            state_dir, "pr-1863-codex_gate.json", commit_sha=old_sha, pr_number=1863,
        )
        update_obligation(
            obligation_file,
            status=STATUS_FULFILLED,
            pr_number=1863,
            result_path=str(result_path),
            evidence_result_path=str(result_path),
            resolved_at="2026-09-10T00:00:00Z",
            reason="fulfilled_by_existing_evidence",
        )

        def _boom(pr_number):
            raise AssertionError("must not resolve a head for a PR outside the open list")

        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", _boom)

        summary = runner.run(state_dir)

        assert summary.get("stale_terminal_evidence") == []
        assert summary.get("stale_terminal_evidence_mismatch_count") == 0
        assert summary.get("stale_terminal_evidence_unknown_count") == 0

    def test_failed_open_pr_list_is_reported_as_undetermined_through_run(self, tmp_path, monkeypatch):
        """OI-1766: a failed `gh pr list` must never make run()'s summary
        read as 'nothing is stale' — it must carry an explicit undetermined
        finding instead of an empty stale_terminal_evidence list."""
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(
            runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration",
        )
        monkeypatch.setattr(runner, "_gh_json", lambda args, owner_repo=None: None)
        dispatch_id = "20260917-oi1764-list-failed"
        obligation_file = register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate",
            project_id="vnx-dev", pr_number=1870,
        )
        result_path = _write_gate_result(
            state_dir, "pr-1870-codex_gate.json", commit_sha="cdcdcdcd" * 5, pr_number=1870,
        )
        update_obligation(
            obligation_file,
            status=STATUS_FULFILLED,
            pr_number=1870,
            result_path=str(result_path),
            evidence_result_path=str(result_path),
            resolved_at="2026-09-10T00:00:00Z",
            reason="fulfilled_by_existing_evidence",
        )

        def _boom(pr_number):
            raise AssertionError("must not resolve any head when the open-PR list itself failed")

        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", _boom)

        summary = runner.run(state_dir, write=False)

        findings = summary.get("stale_terminal_evidence") or []
        assert len(findings) == 1
        assert findings[0]["binding"] == "unknown"
        assert findings[0]["dispatch_id"] is None
        assert summary.get("stale_terminal_evidence_unknown_count") == 1

    def test_unresolvable_owner_repo_is_reported_as_undetermined_through_run(
        self, tmp_path, monkeypatch,
    ):
        """PR #1865 review, second fix-forward: an unresolvable owner/repo
        (e.g. an unregistered central-install checkout) used to make run()
        silently swap in an empty open-PR list — indistinguishable from a
        resolved, genuinely-empty one — so stale_terminal_evidence read as
        "checked, nothing found" when the truth was "never checked at all".
        Before this fix, this scenario produced ``stale_terminal_evidence ==
        []``, RED. It must now produce the same loud, distinct undetermined
        finding as an outright `gh pr list` failure (previous test), and
        `gh` must never even be invoked, since there is no owner/repo to
        query it with."""
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: None)

        def _no_gh(args, owner_repo=None):
            raise AssertionError("must not call gh when no owner/repo resolves")

        monkeypatch.setattr(runner, "_gh_json", _no_gh)
        dispatch_id = "20260917-oi1764-owner-repo-unresolvable"
        obligation_file = register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate",
            project_id="vnx-dev", pr_number=1871,
        )
        result_path = _write_gate_result(
            state_dir, "pr-1871-codex_gate.json", commit_sha="dededede" * 5, pr_number=1871,
        )
        update_obligation(
            obligation_file,
            status=STATUS_FULFILLED,
            pr_number=1871,
            result_path=str(result_path),
            evidence_result_path=str(result_path),
            resolved_at="2026-09-10T00:00:00Z",
            reason="fulfilled_by_existing_evidence",
        )

        def _boom(pr_number):
            raise AssertionError("must not resolve any head when owner/repo is unresolvable")

        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", _boom)

        summary = runner.run(state_dir, write=False)

        findings = summary.get("stale_terminal_evidence") or []
        assert len(findings) == 1, (
            "an unresolvable owner/repo must produce a loud undetermined "
            "finding, never a silent empty stale_terminal_evidence list"
        )
        assert findings[0]["binding"] == "unknown"
        assert findings[0]["dispatch_id"] is None
        assert "owner/repo" in findings[0]["detail"]
        assert summary.get("stale_terminal_evidence_unknown_count") == 1

    def test_run_shares_one_open_pr_list_call_and_resolves_heads_only_for_open_prs(
        self, tmp_path, monkeypatch,
    ):
        """Klaar-conditie 1 (dispatch OI-1764 fix-forward): a single run()
        call issues at most ONE `gh pr list --state open` — shared with
        prs_without_obligation, never a second round-trip — plus one head
        resolution per OPEN PR carrying a terminal obligation with
        evidence, never one per unique PR number in the whole store."""
        state_dir = _make_state_dir(tmp_path)
        monkeypatch.setattr(
            runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration",
        )

        gh_list_calls: list = []

        def fake_gh_json(args, *, owner_repo=None):
            if args[:4] == ["pr", "list", "--state", "open"]:
                gh_list_calls.append(args)
                return [{"number": 5001, "headRefName": "dispatch/20260917-open-one"}]
            return None

        monkeypatch.setattr(runner, "_gh_json", fake_gh_json)

        open_obligation = register_obligation(
            state_dir, dispatch_id="20260917-open-one", gate="codex_gate",
            project_id="vnx-dev", pr_number=5001,
        )
        open_result = _write_gate_result(
            state_dir, "pr-5001-codex_gate.json", commit_sha="99999999" * 5, pr_number=5001,
        )
        update_obligation(
            open_obligation,
            status=STATUS_FULFILLED, pr_number=5001,
            result_path=str(open_result), evidence_result_path=str(open_result),
            resolved_at="2026-09-10T00:00:00Z", reason="fulfilled_by_existing_evidence",
        )
        closed_obligation = register_obligation(
            state_dir, dispatch_id="20260917-closed-one", gate="codex_gate",
            project_id="vnx-dev", pr_number=5002,
        )
        closed_result = _write_gate_result(
            state_dir, "pr-5002-codex_gate.json", commit_sha="aaaaaaaa" * 5, pr_number=5002,
        )
        update_obligation(
            closed_obligation,
            status=STATUS_FULFILLED, pr_number=5002,
            result_path=str(closed_result), evidence_result_path=str(closed_result),
            resolved_at="2026-09-10T00:00:00Z", reason="fulfilled_by_existing_evidence",
        )

        head_calls: list = []

        def counting_head(pr_number):
            head_calls.append(pr_number)
            return "bbbbbbbb" * 5

        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", counting_head)

        summary = runner.run(state_dir)

        assert len(gh_list_calls) == 1, (
            "run() must issue exactly one `gh pr list --state open` call, "
            "shared between stale_terminal_evidence and prs_without_obligation"
        )
        assert head_calls == [5001], (
            "run() must resolve the head only for the OPEN PR (#5001) — the "
            "closed PR #5002 must never be resolved (OI-1764 fix-forward)"
        )
        findings = summary.get("stale_terminal_evidence") or []
        assert any(f["dispatch_id"] == "20260917-open-one" for f in findings)
        assert not any(f["dispatch_id"] == "20260917-closed-one" for f in findings)
