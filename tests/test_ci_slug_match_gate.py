"""Tests for the CI Dispatch-ID slug-match gate (check_ci_slug_match.py).

Covers:
  - branch_slug()        : strip prefix, normalise
  - dispatch_id_slug()   : extract slug segment from dispatch ID
  - slugs_match()        : normalised comparison
  - scan_commits()       : per-commit analysis
  - run_gate()           : end-to-end with mocked git helpers
  - main()               : CLI entrypoint integration
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from check_ci_slug_match import (
    CommitResult,
    branch_slug,
    dispatch_id_slug,
    main,
    run_gate,
    scan_commits,
    slugs_match,
)


# ---------------------------------------------------------------------------
# branch_slug
# ---------------------------------------------------------------------------


class TestBranchSlug:
    def test_strips_fix_prefix(self):
        assert branch_slug("fix/ci-slug-match-gate") == "ci-slug-match-gate"

    def test_strips_feat_prefix(self):
        assert branch_slug("feat/my-new-feature") == "my-new-feature"

    def test_strips_feature_prefix(self):
        assert branch_slug("feature/roadmap-updates") == "roadmap-updates"

    def test_strips_chore_prefix(self):
        assert branch_slug("chore/remove-chain-scripts") == "remove-chain-scripts"

    def test_strips_docs_prefix(self):
        assert branch_slug("docs/readme-refresh-2026") == "readme-refresh-2026"

    def test_strips_test_prefix(self):
        assert branch_slug("test/headless-burnin") == "headless-burnin"

    def test_strips_refactor_prefix(self):
        assert branch_slug("refactor/split-dispatch") == "split-dispatch"

    def test_strips_ci_prefix(self):
        assert branch_slug("ci/update-workflows") == "update-workflows"

    def test_strips_hotfix_prefix(self):
        assert branch_slug("hotfix/crash-on-startup") == "crash-on-startup"

    def test_no_prefix_passthrough(self):
        assert branch_slug("main") == "main"

    def test_no_prefix_with_hyphens(self):
        assert branch_slug("vnx-slug-gate") == "vnx-slug-gate"

    def test_underscores_become_hyphens(self):
        assert branch_slug("fix/vnx_slug_gate") == "vnx-slug-gate"

    def test_lowercased(self):
        assert branch_slug("fix/CI-Slug-Gate") == "ci-slug-gate"

    def test_strips_origin_prefix(self):
        assert branch_slug("origin/fix/ci-slug-match-gate") == "ci-slug-match-gate"

    def test_whitespace_stripped(self):
        assert branch_slug("  fix/ci-slug-match-gate  ") == "ci-slug-match-gate"


# ---------------------------------------------------------------------------
# dispatch_id_slug
# ---------------------------------------------------------------------------


class TestDispatchIdSlug:
    def test_extracts_slug_track_a(self):
        assert dispatch_id_slug("20260423-230100-ci-slug-match-gate-A") == "ci-slug-match-gate"

    def test_extracts_slug_track_b(self):
        assert dispatch_id_slug("20260423-230100-ci-slug-match-gate-B") == "ci-slug-match-gate"

    def test_extracts_slug_track_c(self):
        assert dispatch_id_slug("20260423-230100-ci-slug-match-gate-C") == "ci-slug-match-gate"

    def test_extracts_multi_word_slug(self):
        assert dispatch_id_slug("20260101-000000-headless-gate-dispatch-id-A") == "headless-gate-dispatch-id"

    def test_extracts_single_word_slug(self):
        assert dispatch_id_slug("20260423-100000-fix-A") == "fix"

    def test_returns_none_for_invalid_format(self):
        assert dispatch_id_slug("not-a-dispatch-id") is None

    def test_returns_none_for_missing_track(self):
        assert dispatch_id_slug("20260423-230100-ci-slug-match-gate") is None

    def test_returns_none_for_invalid_track_letter(self):
        # D is not a valid track letter
        assert dispatch_id_slug("20260423-230100-ci-slug-match-gate-D") is None

    def test_returns_none_for_wrong_date_format(self):
        assert dispatch_id_slug("2026042-230100-slug-A") is None

    def test_normalises_underscores_to_hyphens(self):
        assert dispatch_id_slug("20260423-230100-ci_slug_match-A") == "ci-slug-match"

    def test_normalises_to_lowercase(self):
        assert dispatch_id_slug("20260423-230100-CI-SLUG-GATE-B") == "ci-slug-gate"

    def test_strips_whitespace_from_input(self):
        assert dispatch_id_slug("  20260423-230100-ci-slug-match-gate-B  ") == "ci-slug-match-gate"


# ---------------------------------------------------------------------------
# slugs_match
# ---------------------------------------------------------------------------


class TestSlugsMatch:
    def test_identical_slugs(self):
        assert slugs_match("ci-slug-match-gate", "ci-slug-match-gate")

    def test_case_insensitive(self):
        assert slugs_match("CI-SLUG", "ci-slug")

    def test_underscore_hyphen_equivalence(self):
        assert slugs_match("ci_slug_gate", "ci-slug-gate")

    def test_different_slugs_return_false(self):
        assert not slugs_match("ci-slug-gate", "headless-dispatch")

    def test_extra_hyphens_stripped_from_ends(self):
        assert slugs_match("-ci-slug-", "ci-slug")

    def test_empty_slugs_match(self):
        assert slugs_match("", "")

    def test_one_empty_one_nonempty(self):
        assert not slugs_match("", "ci-slug")


# ---------------------------------------------------------------------------
# scan_commits
# ---------------------------------------------------------------------------


class TestScanCommits:
    def _make_commit(self, sha: str, body: str) -> tuple[str, str]:
        return (sha, body)

    def test_commit_with_dispatch_id_and_matching_slug(self):
        commits = [
            self._make_commit(
                "abc12345",
                "feat: add slug gate\n\nDispatch-ID: 20260423-230100-ci-slug-match-gate-B\n",
            )
        ]
        results = scan_commits(commits, "ci-slug-match-gate")
        assert len(results) == 1
        cr = results[0]
        assert cr.has_dispatch_id
        assert cr.all_slugs_match

    def test_commit_missing_dispatch_id(self):
        commits = [
            self._make_commit("dead0001", "fix: typo in readme\n\nNo dispatch here.\n")
        ]
        results = scan_commits(commits, "ci-slug-match-gate")
        cr = results[0]
        assert not cr.has_dispatch_id
        assert cr.slug_matches == []

    def test_commit_with_mismatching_slug(self):
        commits = [
            self._make_commit(
                "bead0001",
                "feat: unrelated\n\nDispatch-ID: 20260423-120000-other-feature-A\n",
            )
        ]
        results = scan_commits(commits, "ci-slug-match-gate")
        cr = results[0]
        assert cr.has_dispatch_id
        assert not cr.all_slugs_match

    def test_multiple_commits_mixed_results(self):
        commits = [
            self._make_commit(
                "sha00001",
                "feat: good\n\nDispatch-ID: 20260423-230100-ci-slug-match-gate-B\n",
            ),
            self._make_commit("sha00002", "fix: no id\n\nNo dispatch.\n"),
        ]
        results = scan_commits(commits, "ci-slug-match-gate")
        assert results[0].has_dispatch_id
        assert not results[1].has_dispatch_id

    def test_subject_extracted_from_body(self):
        commits = [
            self._make_commit(
                "sha00003",
                "My commit subject\n\nDispatch-ID: 20260423-230100-ci-slug-match-gate-A\n",
            )
        ]
        results = scan_commits(commits, "ci-slug-match-gate")
        assert results[0].subject == "My commit subject"

    def test_multiple_dispatch_ids_in_one_commit(self):
        commits = [
            self._make_commit(
                "sha00004",
                "feat: multi\n\n"
                "Dispatch-ID: 20260423-100000-ci-slug-match-gate-A\n"
                "Dispatch-ID: 20260423-110000-ci-slug-match-gate-B\n",
            )
        ]
        results = scan_commits(commits, "ci-slug-match-gate")
        cr = results[0]
        assert len(cr.dispatch_ids) == 2
        assert cr.all_slugs_match

    def test_invalid_dispatch_id_format_not_matched(self):
        commits = [
            self._make_commit(
                "sha00005",
                "fix: partial\n\nDispatch-ID: not-a-valid-id\n",
            )
        ]
        results = scan_commits(commits, "ci-slug-match-gate")
        # The regex will find "not-a-valid-id" but dispatch_id_slug returns None
        cr = results[0]
        # has_dispatch_id is True (line matched), but slug_matches is [] (parse failed)
        assert cr.has_dispatch_id
        assert cr.slug_matches == []

    def test_empty_commit_list(self):
        results = scan_commits([], "ci-slug-match-gate")
        assert results == []


# ---------------------------------------------------------------------------
# run_gate (end-to-end with mocked git)
# ---------------------------------------------------------------------------


class TestRunGate:
    def _patch_git(self, commits: list[tuple[str, str]], branch: str = "fix/ci-slug-match-gate"):
        """Context manager: patch commits_since + resolve_base_ref."""
        return (
            patch("check_ci_slug_match.commits_since", return_value=commits),
            patch("check_ci_slug_match.resolve_base_ref", return_value="main"),
        )

    def test_pass_when_all_commits_have_matching_slug(self, capsys):
        commits = [
            ("abc00001", "feat: gate\n\nDispatch-ID: 20260423-230100-ci-slug-match-gate-B\n"),
        ]
        with patch("check_ci_slug_match.commits_since", return_value=commits), \
             patch("check_ci_slug_match.resolve_base_ref", return_value="main"):
            rc = run_gate("main", "fix/ci-slug-match-gate", enforce=True)
        assert rc == 0

    def test_fail_in_enforce_mode_when_dispatch_id_missing(self, capsys):
        commits = [
            ("abc00002", "fix: typo\n\nNo dispatch.\n"),
        ]
        with patch("check_ci_slug_match.commits_since", return_value=commits), \
             patch("check_ci_slug_match.resolve_base_ref", return_value="main"):
            rc = run_gate("main", "fix/ci-slug-match-gate", enforce=True)
        assert rc == 1

    def test_warn_in_shadow_mode_when_dispatch_id_missing(self, capsys):
        commits = [
            ("abc00003", "fix: typo\n\nNo dispatch.\n"),
        ]
        with patch("check_ci_slug_match.commits_since", return_value=commits), \
             patch("check_ci_slug_match.resolve_base_ref", return_value="main"):
            rc = run_gate("main", "fix/ci-slug-match-gate", enforce=False)
        assert rc == 0
        out = capsys.readouterr().out
        assert "WARN" in out or "shadow" in out.lower()

    def test_fail_in_enforce_mode_for_slug_mismatch(self, capsys):
        commits = [
            ("abc00004", "feat: unrelated\n\nDispatch-ID: 20260423-120000-other-feature-A\n"),
        ]
        with patch("check_ci_slug_match.commits_since", return_value=commits), \
             patch("check_ci_slug_match.resolve_base_ref", return_value="main"):
            rc = run_gate("main", "fix/ci-slug-match-gate", enforce=True)
        assert rc == 1

    def test_pass_with_no_commits(self, capsys):
        with patch("check_ci_slug_match.commits_since", return_value=[]), \
             patch("check_ci_slug_match.resolve_base_ref", return_value="main"):
            rc = run_gate("main", "fix/ci-slug-match-gate", enforce=True)
        assert rc == 0

    def test_error_when_base_ref_not_found(self, capsys):
        with patch(
            "check_ci_slug_match.resolve_base_ref",
            side_effect=RuntimeError("not found"),
        ):
            rc = run_gate("nonexistent-branch", "fix/ci-slug-match-gate", enforce=True)
        assert rc == 2

    def test_error_when_git_log_fails(self, capsys):
        with patch("check_ci_slug_match.resolve_base_ref", return_value="main"), \
             patch(
                 "check_ci_slug_match.commits_since",
                 side_effect=RuntimeError("git failed"),
             ):
            rc = run_gate("main", "fix/ci-slug-match-gate", enforce=True)
        assert rc == 2

    def test_pass_output_contains_result_pass(self, capsys):
        commits = [
            ("abc00005", "feat: ok\n\nDispatch-ID: 20260423-230100-ci-slug-match-gate-B\n"),
        ]
        with patch("check_ci_slug_match.commits_since", return_value=commits), \
             patch("check_ci_slug_match.resolve_base_ref", return_value="main"):
            run_gate("main", "fix/ci-slug-match-gate", enforce=True)
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_output_shows_branch_slug(self, capsys):
        commits = [
            ("abc00006", "feat: ok\n\nDispatch-ID: 20260423-230100-ci-slug-match-gate-B\n"),
        ]
        with patch("check_ci_slug_match.commits_since", return_value=commits), \
             patch("check_ci_slug_match.resolve_base_ref", return_value="main"):
            run_gate("main", "fix/ci-slug-match-gate", enforce=True)
        out = capsys.readouterr().out
        assert "ci-slug-match-gate" in out


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------


class TestMainCLI:
    def test_main_returns_zero_for_no_commits(self):
        with patch("check_ci_slug_match.resolve_base_ref", return_value="main"), \
             patch("check_ci_slug_match.commits_since", return_value=[]), \
             patch("check_ci_slug_match.current_branch", return_value="fix/ci-slug-match-gate"):
            rc = main(["--base-ref", "main", "--branch-name", "fix/ci-slug-match-gate"])
        assert rc == 0

    def test_main_accepts_branch_name_arg(self):
        with patch("check_ci_slug_match.resolve_base_ref", return_value="main"), \
             patch("check_ci_slug_match.commits_since", return_value=[]):
            rc = main(["--branch-name", "fix/ci-slug-match-gate", "--base-ref", "main"])
        assert rc == 0

    def test_main_shadow_mode_by_default(self):
        commits = [("sha11111", "fix: typo\n\nNo dispatch.\n")]
        with patch("check_ci_slug_match.resolve_base_ref", return_value="main"), \
             patch("check_ci_slug_match.commits_since", return_value=commits), \
             patch("check_ci_slug_match.current_branch", return_value="fix/ci-slug-match-gate"):
            rc = main(["--branch-name", "fix/ci-slug-match-gate", "--base-ref", "main"])
        # Shadow mode: do not fail
        assert rc == 0

    def test_main_enforce_flag_fails_on_missing_id(self):
        commits = [("sha22222", "fix: typo\n\nNo dispatch.\n")]
        with patch("check_ci_slug_match.resolve_base_ref", return_value="main"), \
             patch("check_ci_slug_match.commits_since", return_value=commits):
            rc = main([
                "--branch-name", "fix/ci-slug-match-gate",
                "--base-ref", "main",
                "--enforce",
            ])
        assert rc == 1

    def test_main_uses_github_head_ref_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_HEAD_REF", "fix/ci-slug-match-gate")
        monkeypatch.setenv("GITHUB_BASE_REF", "main")
        commits = [
            ("sha33333", "feat: gate\n\nDispatch-ID: 20260423-230100-ci-slug-match-gate-B\n"),
        ]
        with patch("check_ci_slug_match.resolve_base_ref", return_value="main"), \
             patch("check_ci_slug_match.commits_since", return_value=commits):
            rc = main([])
        assert rc == 0

    def test_main_enforce_via_env_var(self, monkeypatch):
        monkeypatch.setenv("VNX_SLUG_ENFORCEMENT", "1")
        commits = [("sha44444", "fix: no dispatch\n")]
        with patch("check_ci_slug_match.resolve_base_ref", return_value="main"), \
             patch("check_ci_slug_match.commits_since", return_value=commits):
            rc = main(["--branch-name", "fix/ci-slug-match-gate", "--base-ref", "main"])
        assert rc == 1
