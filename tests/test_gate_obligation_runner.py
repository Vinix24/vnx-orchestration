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

    def test_stale_result_file_trips_the_warning(self, tmp_path, monkeypatch):
        """RED on unfixed main (measured 2026-08-30: no such check existed
        at all): a result file whose mtime predates this attempt by well
        over the threshold, but whose commit_sha still happens to match the
        PR head (so the sha check alone does not intercept it — this is a
        deliberately independent second signal), must trip a LOUD,
        observable warning on the outcome."""
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
