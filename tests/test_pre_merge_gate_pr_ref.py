#!/usr/bin/env python3
"""Tests for ``--pr`` reference resolution in pre_merge_gate (OI-1141).

Pins that ``--pr <nummer>`` makes the gate measure the PR as it exists on
GitHub — resolved via ``gh pr view --json headRefName,headRefOid`` and
diffed against ``merge-base(origin/main, head)`` — instead of silently
measuring the local working copy (HEAD). And that an unresolvable PR number
fails loudly, never falling back to HEAD.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import pre_merge_gate
from pre_merge_gate import (
    PRRefResolutionError,
    ResolvedPRRef,
    check_pr_size,
    resolve_pr_ref,
    run_gate_checks,
)


def _subprocess_result(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def state_dir(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    return sd


@pytest.fixture
def dispatch_dir(tmp_path):
    dd = tmp_path / "dispatches"
    for sub in ("pending", "active", "completed", "staging"):
        (dd / sub).mkdir(parents=True)
    return dd


# ---------------------------------------------------------------------------
# resolve_pr_ref — the head comes from gh, never from HEAD
# ---------------------------------------------------------------------------

class TestResolvePRRef:

    def test_resolves_head_oid_not_head(self, tmp_path):
        gh_ok = _subprocess_result(
            json.dumps({"headRefName": "feature/oi-1141", "headRefOid": "abc123"})
        )
        mb_ok = _subprocess_result("def456\n")
        with patch("pre_merge_gate.subprocess.run", side_effect=[gh_ok, mb_ok]) as mock_run:
            resolved = resolve_pr_ref("1522", tmp_path)

        assert isinstance(resolved, ResolvedPRRef)
        assert resolved.head_ref == "abc123"
        assert resolved.head_ref != "HEAD"
        assert resolved.head_ref_name == "feature/oi-1141"
        assert resolved.merge_base == "def456"

        # gh was really invoked with the PR number, not with a local ref.
        gh_argv = mock_run.call_args_list[0].args[0]
        assert gh_argv[0] == "gh"
        assert gh_argv[1:3] == ["pr", "view"]
        assert "1522" in gh_argv
        assert "--json" in gh_argv
        assert "headRefName,headRefOid" in gh_argv

    def test_merge_base_falls_back_to_origin_master(self, tmp_path):
        gh_ok = _subprocess_result(
            json.dumps({"headRefName": "b", "headRefOid": "abc123"})
        )
        main_fail = _subprocess_result("", returncode=1, stderr="unknown revision")
        master_ok = _subprocess_result("def456\n")
        with patch(
            "pre_merge_gate.subprocess.run",
            side_effect=[gh_ok, main_fail, master_ok],
        ):
            resolved = resolve_pr_ref("9", tmp_path)
        assert resolved.merge_base == "def456"


# ---------------------------------------------------------------------------
# The comparison uses merge-base, never the tip of origin/main
# ---------------------------------------------------------------------------

class TestPRSizeUsesMergeBase:

    def test_diff_uses_merge_base_not_base_tip(self, tmp_path):
        mb_ok = _subprocess_result("mb123\n")
        numstat_ok = _subprocess_result("10\t5\tfile.py\n")
        with patch(
            "pre_merge_gate.subprocess.run",
            side_effect=[mb_ok, numstat_ok],
        ) as mock_run:
            result = check_pr_size(tmp_path, head_ref="abc123")

        assert result["status"] == "GO"
        assert result["head_ref"] == "abc123"
        assert result["merge_base"] == "mb123"

        # The numstat diff is measured merge-base..head, not origin/main..head.
        diff_argv = mock_run.call_args_list[1].args[0]
        assert diff_argv[0] == "git"
        assert diff_argv[1] == "diff"
        assert "mb123" in diff_argv
        assert "abc123" in diff_argv
        assert "origin/main" not in diff_argv


# ---------------------------------------------------------------------------
# Fail loud — never fall back to HEAD
# ---------------------------------------------------------------------------

class TestFailsLoud:

    def test_unknown_pr_number_raises_and_never_falls_back(self, tmp_path):
        gh_fail = _subprocess_result(
            "",
            returncode=1,
            stderr="Could not resolve to a Pull Request with the number of 99999.",
        )
        with patch(
            "pre_merge_gate.subprocess.run", side_effect=[gh_fail]
        ) as mock_run:
            with pytest.raises(PRRefResolutionError) as exc:
                resolve_pr_ref("99999", tmp_path)
        assert "99999" in str(exc.value)
        # Only the gh call happened — no git fallback to HEAD was attempted.
        assert mock_run.call_count == 1

    def test_gh_missing_raises(self, tmp_path):
        with patch(
            "pre_merge_gate.subprocess.run",
            side_effect=FileNotFoundError("gh not found"),
        ):
            with pytest.raises(PRRefResolutionError) as exc:
                resolve_pr_ref("1", tmp_path)
        assert "gh CLI not available" in str(exc.value)

    def test_unparseable_gh_output_raises(self, tmp_path):
        gh_bad = _subprocess_result("not json")
        with patch("pre_merge_gate.subprocess.run", side_effect=[gh_bad]):
            with pytest.raises(PRRefResolutionError) as exc:
                resolve_pr_ref("1", tmp_path)
        assert "unparseable" in str(exc.value)

    def test_missing_head_ref_oid_raises(self, tmp_path):
        gh_empty = _subprocess_result(
            json.dumps({"headRefName": "b", "headRefOid": ""})
        )
        with patch("pre_merge_gate.subprocess.run", side_effect=[gh_empty]):
            with pytest.raises(PRRefResolutionError) as exc:
                resolve_pr_ref("1", tmp_path)
        assert "no headRefOid" in str(exc.value)

    def test_no_merge_base_raises(self, tmp_path):
        gh_ok = _subprocess_result(
            json.dumps({"headRefName": "b", "headRefOid": "abc123"})
        )
        main_fail = _subprocess_result("", returncode=1, stderr="unknown revision")
        master_fail = _subprocess_result("", returncode=1, stderr="unknown revision")
        with patch(
            "pre_merge_gate.subprocess.run",
            side_effect=[gh_ok, main_fail, master_fail],
        ):
            with pytest.raises(PRRefResolutionError) as exc:
                resolve_pr_ref("1", tmp_path)
        assert "merge-base" in str(exc.value)


# ---------------------------------------------------------------------------
# run_gate_checks threads the resolved head; HEAD stays the default
# ---------------------------------------------------------------------------

class TestRunGateChecksPRHead:

    def _spy_pr_size(self, monkeypatch, captured):
        def _spy(project_root, **kw):
            captured.update(kw)
            return {
                "check": "pr_size", "status": "GO", "detail": "spy",
                "lines_added": 0, "lines_removed": 0, "lines_changed": 0,
            }

        monkeypatch.setattr(pre_merge_gate, "check_pr_size", _spy)

    def _stub_heavy(self, monkeypatch):
        monkeypatch.setattr(
            pre_merge_gate, "check_ci_workflow",
            lambda project_root, **kw: {
                "check": "ci_workflow", "status": "GO", "detail": "stub",
                "ci_conclusion": "success", "ci_ran_on_sha": True,
            },
        )
        monkeypatch.setattr(
            pre_merge_gate, "check_net_deletion",
            lambda project_root, **kw: {
                "check": "net_deletion", "status": "GO", "detail": "stub",
                "deleted_count": 0, "deleted_files": [], "net_line_deletion": 0,
                "net_line_deletion_warn": False, "file_deletion_warn": False,
            },
        )

    def test_without_pr_head_measures_head(self, state_dir, dispatch_dir, tmp_path, monkeypatch):
        captured = {}
        self._spy_pr_size(monkeypatch, captured)
        self._stub_heavy(monkeypatch)

        result = run_gate_checks(
            pr_id="PR-6", project_root=tmp_path, state_dir=state_dir,
            dispatch_dir=dispatch_dir, skip_pytest=True,
        )
        assert captured.get("head_ref") == "HEAD"
        assert result["pr_ref"] == {"resolved": False, "head_ref": "HEAD"}

    def test_with_pr_head_measures_pr(self, state_dir, dispatch_dir, tmp_path, monkeypatch):
        captured = {}
        self._spy_pr_size(monkeypatch, captured)
        self._stub_heavy(monkeypatch)
        pr_head = ResolvedPRRef(
            pr_ref="1522", head_ref="abc123", head_ref_name="feature/o", merge_base="def456",
        )

        result = run_gate_checks(
            pr_id="1522", project_root=tmp_path, state_dir=state_dir,
            dispatch_dir=dispatch_dir, skip_pytest=True, pr_head=pr_head,
        )
        assert captured.get("head_ref") == "abc123"
        assert result["pr_ref"]["resolved"] is True
        assert result["pr_ref"]["head_ref"] == "abc123"
        assert result["pr_ref"]["head_ref_name"] == "feature/o"
        assert result["pr_ref"]["merge_base"] == "def456"


# ---------------------------------------------------------------------------
# run_gate_checks binds the ci_workflow check to the PR head (OI-1266)
# ---------------------------------------------------------------------------

class TestRunGateChecksCIHead:
    """OI-1266: the ci_workflow check must verify CI on the PR's exact commit.

    ``check_ci_workflow`` accepts ``branch`` + ``head_sha`` so both its
    ``gh run list`` query and its sha-match pin the PR as it exists on GitHub.
    Without threading those through from the resolved ``pr_head``, the check
    silently measures the local HEAD — booking local main's CI status as the
    PR's GO. These tests pin the provenance (the values come from ``pr_head``,
    not from local git) and the fail-closed refusal when the PR head can't be
    determined.
    """

    @staticmethod
    def _stub_heavy(monkeypatch):
        monkeypatch.setattr(
            pre_merge_gate, "check_pr_size",
            lambda project_root, **kw: {
                "check": "pr_size", "status": "GO", "detail": "stub",
                "lines_added": 0, "lines_removed": 0, "lines_changed": 0,
            },
        )
        monkeypatch.setattr(
            pre_merge_gate, "check_net_deletion",
            lambda project_root, **kw: {
                "check": "net_deletion", "status": "GO", "detail": "stub",
                "deleted_count": 0, "deleted_files": [], "net_line_deletion": 0,
                "net_line_deletion_warn": False, "file_deletion_warn": False,
            },
        )

    @staticmethod
    def _ci_router(*, local_sha, gh_runs):
        """Route subprocess.run so the local-HEAD fallback is a *different* sha.

        ``git rev-parse HEAD`` resolves to ``local_sha`` (the trap: only
        reached when the check wrongly falls back to local git), and
        ``gh run list`` returns ``gh_runs``. Every other git call returns an
        empty success so the remaining gate checks stay GO. All argv lists are
        recorded so tests can assert what was (never) invoked.
        """
        calls = []

        def _route(argv, **kw):
            calls.append(list(argv))
            if argv[:2] == ["git", "rev-parse"]:
                if len(argv) > 2 and argv[2] == "HEAD":
                    return MagicMock(returncode=0, stdout=local_sha + "\n", stderr="")
                if len(argv) > 2 and argv[2] == "--abbrev-ref":
                    return MagicMock(returncode=0, stdout="main\n", stderr="")
            if argv and argv[0] == "gh":
                return MagicMock(returncode=0, stdout=json.dumps(gh_runs), stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return _route, calls

    def test_ci_binds_to_pr_head_not_local_head(
        self, state_dir, dispatch_dir, tmp_path, monkeypatch
    ):
        pr_sha = "1" * 40
        local_sha = "9" * 40  # different — the local HEAD must never be used
        pr_branch = "feature/oi-1266"
        route, calls = self._ci_router(
            local_sha=local_sha,
            gh_runs=[{
                "conclusion": "success", "headSha": pr_sha,
                "status": "completed", "databaseId": 4242,
            }],
        )
        self._stub_heavy(monkeypatch)
        pr_head = ResolvedPRRef(
            pr_ref="1522", head_ref=pr_sha, head_ref_name=pr_branch,
            merge_base="0" * 40,
        )

        with patch("pre_merge_gate.subprocess.run", side_effect=route):
            result = run_gate_checks(
                pr_id="1522", project_root=tmp_path, state_dir=state_dir,
                dispatch_dir=dispatch_dir, skip_pytest=True, pr_head=pr_head,
            )

        ci_check = next(c for c in result["checks"] if c["check"] == "ci_workflow")
        assert ci_check["status"] == "GO"
        assert ci_check["ci_head_sha"] == pr_sha      # equal to the PR head
        assert ci_check["ci_head_sha"] != local_sha   # never the local HEAD

        # Provenance: gh was queried for the PR branch, and git rev-parse HEAD
        # (the silent local fallback) was never invoked.
        gh_calls = [a for a in calls if a and a[0] == "gh"]
        assert gh_calls, "gh run list was never invoked"
        gh_argv = gh_calls[0]
        assert gh_argv[gh_argv.index("--branch") + 1] == pr_branch
        assert not any(a[:2] == ["git", "rev-parse"] for a in calls)

    @pytest.mark.parametrize("head_ref,head_ref_name", [
        ("", "feature/oi-1266"),  # no sha — can't pin CI to a commit
        ("1" * 40, ""),            # no branch — can't scope the run list
        ("", ""),
    ])
    def test_undeterminable_pr_head_is_never_go(
        self, state_dir, dispatch_dir, tmp_path, monkeypatch, head_ref, head_ref_name
    ):
        # Without the fix, run_gate_checks falls back to the local HEAD; here
        # that fallback would resolve to local_sha and gh would report success
        # on it — i.e. the check would (wrongly) GO. With the fix, the empty
        # PR head short-circuits before any subprocess is consulted.
        local_sha = "9" * 40
        route, calls = self._ci_router(
            local_sha=local_sha,
            gh_runs=[{
                "conclusion": "success", "headSha": local_sha,
                "status": "completed", "databaseId": 1,
            }],
        )
        self._stub_heavy(monkeypatch)
        pr_head = ResolvedPRRef("1522", head_ref, head_ref_name, "0" * 40)

        with patch("pre_merge_gate.subprocess.run", side_effect=route):
            result = run_gate_checks(
                pr_id="1522", project_root=tmp_path, state_dir=state_dir,
                dispatch_dir=dispatch_dir, skip_pytest=True, pr_head=pr_head,
            )

        ci_check = next(c for c in result["checks"] if c["check"] == "ci_workflow")
        assert ci_check["status"] != "GO"
        assert result["verdict"] == "HOLD"
        # Fail-closed: the refusal must not have consulted local HEAD either.
        assert not any(a[:2] == ["git", "rev-parse"] for a in calls)


# ---------------------------------------------------------------------------
# main() wiring: --pr resolves, unresolvable --pr exits non-zero
# ---------------------------------------------------------------------------

class TestMainPRResolution:

    def _fake_env(self, tmp_path):
        return {
            "PROJECT_ROOT": str(tmp_path),
            "VNX_STATE_DIR": str(tmp_path / "state"),
            "VNX_DISPATCH_DIR": str(tmp_path / "dispatches"),
        }

    def _fake_result(self, pr_id, pr_ref):
        return {
            "pr_id": pr_id,
            "pr_ref": pr_ref,
            "verdict": "GO",
            "checked_at": "x",
            "total_checks": 0,
            "go_count": 0,
            "hold_count": 0,
            "skipped_unverified_count": 0,
            "checks": [],
            "hold_reasons": [],
        }

    def test_main_unknown_pr_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(pre_merge_gate, "ensure_env", lambda: self._fake_env(tmp_path))
        monkeypatch.setattr(
            pre_merge_gate, "resolve_pr_ref",
            MagicMock(side_effect=PRRefResolutionError("gh pr view 99999 failed: not found")),
        )
        with patch("sys.argv", ["pre_merge_gate.py", "--pr", "99999"]):
            rc = pre_merge_gate.main()
        assert rc == 10
        assert "99999" in capsys.readouterr().err

    def test_main_with_pr_resolves_and_passes_head(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pre_merge_gate, "ensure_env", lambda: self._fake_env(tmp_path))
        resolved = ResolvedPRRef("1522", "abc123", "feature/o", "def456")
        monkeypatch.setattr(pre_merge_gate, "resolve_pr_ref", MagicMock(return_value=resolved))
        captured = {}

        def _fake_run(**kw):
            captured.update(kw)
            return self._fake_result(kw["pr_id"], {
                "resolved": True, "pr_ref": "1522",
                "head_ref": "abc123", "head_ref_name": "feature/o", "merge_base": "def456",
            })

        monkeypatch.setattr(pre_merge_gate, "run_gate_checks", _fake_run)
        with patch("sys.argv", ["pre_merge_gate.py", "--pr", "1522", "--no-store"]):
            rc = pre_merge_gate.main()
        assert rc == 0
        assert captured["pr_id"] == "1522"
        assert captured["pr_head"] is resolved

    def test_main_without_pr_measures_head(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pre_merge_gate, "ensure_env", lambda: self._fake_env(tmp_path))
        captured = {}

        def _fake_run(**kw):
            captured.update(kw)
            return self._fake_result(kw["pr_id"], {"resolved": False, "head_ref": "HEAD"})

        monkeypatch.setattr(pre_merge_gate, "run_gate_checks", _fake_run)
        with patch("sys.argv", ["pre_merge_gate.py", "--no-store"]):
            rc = pre_merge_gate.main()
        assert rc == 0
        assert captured["pr_id"] == "HEAD"
        assert captured["pr_head"] is None
