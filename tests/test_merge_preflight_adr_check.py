#!/usr/bin/env python3
"""Tests for the ADR-number merge preflight (Golf B, B6).

Covers ``merge_preflight_adr_check`` (the fail-closed check itself) and its
wiring into ``pr_merge.main()`` via ``_run_adr_gate``. Every ``gh`` call is
mocked at the ``_capture`` seam (same pattern as
``test_merge_preflight_ci_check.py``): no real subprocess, no network, no gh
auth dependency.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import merge_preflight_adr_check as adr_check


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _pr_files_json(entries: List[Dict[str, Any]]) -> str:
    """Simulate ``gh api ... --paginate --slurp`` output: a single outer
    array wrapping one page (the entries list itself). Multi-page slurped
    output is exercised separately in TestGetPrAddedAdrFilesPagination.
    """
    return json.dumps([entries])


def _main_listing_json(names: List[str]) -> str:
    return json.dumps(
        [{"name": n, "type": "file", "path": f"docs/governance/decisions/{n}"} for n in names]
    )


class TestGetPrAddedAdrFiles:
    def test_only_added_status_counts(self, monkeypatch):
        """A modified/renamed existing ADR, or an unrelated file, must not surface."""
        entries = [
            {"filename": "docs/governance/decisions/ADR-038-x.md", "status": "added"},
            {"filename": "docs/governance/decisions/ADR-020-y.md", "status": "modified"},
            {"filename": "docs/governance/decisions/ADR-021-renamed.md", "status": "renamed",
             "previous_filename": "docs/governance/decisions/ADR-021-old-name.md"},
            {"filename": "scripts/pr_merge.py", "status": "added"},
        ]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(_pr_files_json(entries)), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert err is None
        assert added == {"38": "docs/governance/decisions/ADR-038-x.md"}

    def test_renamed_to_a_different_number_is_a_new_number_claim(self, monkeypatch):
        """A rename that changes the ADR NUMBER (not just wording) claims the
        new number exactly as much as a brand-new file would — unlike
        test_only_added_status_counts's rename, which keeps the same number.
        """
        entries = [
            {"filename": "docs/governance/decisions/ADR-038-my-new-decision.md",
             "status": "renamed",
             "previous_filename": "docs/governance/decisions/ADR-031-orchestration-target-ratification.md"},
        ]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(_pr_files_json(entries)), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert err is None
        assert added == {"38": "docs/governance/decisions/ADR-038-my-new-decision.md"}

    def test_copied_to_a_different_number_is_a_new_number_claim(self, monkeypatch):
        entries = [
            {"filename": "docs/governance/decisions/ADR-040-copy.md", "status": "copied",
             "previous_filename": "docs/governance/decisions/ADR-010-source.md"},
        ]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(_pr_files_json(entries)), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert err is None
        assert added == {"40": "docs/governance/decisions/ADR-040-copy.md"}

    def test_no_added_adr_files_yields_empty_dict(self, monkeypatch):
        entries = [{"filename": "scripts/pr_merge.py", "status": "modified"}]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(_pr_files_json(entries)), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert err is None
        assert added == {}

    def test_gh_failure_is_no_go_fail_closed(self, monkeypatch):
        monkeypatch.setattr(
            adr_check, "_capture",
            lambda argv, *, timeout, cwd=None: (_proc("", returncode=1, stderr="HTTP 404"), None),
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert err["verdict"] == "NO-GO"
        assert "niet toetsbaar" in err["message"]

    def test_gh_missing_binary_is_no_go(self, monkeypatch):
        monkeypatch.setattr(adr_check, "_capture", lambda argv, *, timeout, cwd=None: (None, "missing"))

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert err["verdict"] == "NO-GO"
        assert "gh CLI niet beschikbaar" in err["message"]

    def test_unparseable_json_is_no_go(self, monkeypatch):
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc("not json"), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert err["verdict"] == "NO-GO"
        assert "niet te parsen" in err["message"]


class TestGetPrAddedAdrFilesPagination:
    """B6 fix-forward 2: ``gh api --paginate`` without ``--slurp`` prints one
    JSON document PER PAGE back-to-back, which is not valid JSON once a PR
    has more files than fit on one page. ``--slurp`` wraps all pages into a
    single outer JSON array of pages; this module must flatten that, and
    must refuse (not crash) on the old, un-slurped shape.
    """

    def test_multi_page_slurped_output_is_flattened_and_page_two_adr_is_caught(self, monkeypatch):
        page_one = [
            {"filename": "docs/governance/decisions/ADR-020-x.md", "status": "modified"},
            {"filename": "scripts/pr_merge.py", "status": "modified"},
        ]
        page_two = [
            {"filename": "docs/governance/decisions/ADR-041-late-page.md", "status": "added"},
        ]
        slurped_stdout = json.dumps([page_one, page_two])
        seen_argv: List[List[str]] = []

        def fake_capture(argv, *, timeout, cwd=None):
            seen_argv.append(argv)
            return _proc(slurped_stdout), None

        monkeypatch.setattr(adr_check, "_capture", fake_capture)

        added, err = adr_check.get_pr_added_adr_files(1)

        assert err is None
        assert added == {"41": "docs/governance/decisions/ADR-041-late-page.md"}
        assert "--paginate" in seen_argv[0]
        assert "--slurp" in seen_argv[0]

    def test_unslurped_multi_document_output_is_no_go_not_a_crash(self, monkeypatch):
        """The old, buggy shape: two JSON documents concatenated back-to-back
        (what ``--paginate`` alone produces on a multi-page PR). This must
        never crash the preflight — it must NO-GO with a message that points
        at pagination/slurping, not a generic parse error.
        """
        unslurped_stdout = (
            json.dumps([{"filename": "docs/governance/decisions/ADR-020-x.md", "status": "modified"}])
            + json.dumps([{"filename": "docs/governance/decisions/ADR-041-y.md", "status": "added"}])
        )
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(unslurped_stdout), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert err["verdict"] == "NO-GO"
        assert "slurp" in err["message"].lower() or "paginering" in err["message"].lower()

    def test_slurped_page_that_is_not_an_array_is_no_go(self, monkeypatch):
        """A slurped page must itself be an array (the files endpoint returns
        an array per page); anything else is an unexpected shape, refused.
        """
        malformed_stdout = json.dumps([{"not": "an array"}])
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(malformed_stdout), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert err["verdict"] == "NO-GO"
        assert "onverwacht antwoordformaat" in err["message"]


class TestMalformedApiEntriesFailClosed:
    """B6 fix-forward 3 (codex round 2): an entry the check cannot READ must
    never be skipped. A ``continue`` on a malformed entry lets a partially
    unreadable response build an incomplete picture and answer "no collision"
    — a silent pass through a fail-closed gate. Both live reads (the PR's file
    list and the base-branch contents listing) must refuse instead, naming the
    index of the offending entry.
    """

    def test_pr_files_non_dict_entry_between_valid_ones_is_no_go_with_its_index(self, monkeypatch):
        entries = [
            {"filename": "docs/governance/decisions/ADR-020-x.md", "status": "modified"},
            "not-an-object",
            {"filename": "docs/governance/decisions/ADR-041-y.md", "status": "added"},
        ]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(_pr_files_json(entries)), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert err["verdict"] == "NO-GO"
        assert "entry 1" in err["message"]
        assert "not-an-object" in err["message"]
        assert "niet toetsbaar" in err["message"]

    def test_pr_entry_without_filename_is_no_go(self, monkeypatch):
        entries = [{"status": "added"}]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(_pr_files_json(entries)), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert err["verdict"] == "NO-GO"
        assert "entry 0" in err["message"]
        assert "filename" in err["message"]

    def test_pr_entry_without_status_is_no_go(self, monkeypatch):
        entries = [{"filename": "docs/governance/decisions/ADR-041-y.md"}]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(_pr_files_json(entries)), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert err["verdict"] == "NO-GO"
        assert "entry 0" in err["message"]
        assert "status" in err["message"]

    def test_pr_entry_with_non_string_filename_is_no_go(self, monkeypatch):
        entries = [{"filename": 42, "status": "added"}]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(_pr_files_json(entries)), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert err["verdict"] == "NO-GO"
        assert "filename" in err["message"]

    def test_rename_with_non_string_previous_filename_is_no_go_not_a_crash(self, monkeypatch):
        """``previous_filename`` is fed straight into a regex; a non-string
        value would raise TypeError out of the gate instead of refusing.
        """
        entries = [
            {"filename": "docs/governance/decisions/ADR-041-y.md", "status": "renamed",
             "previous_filename": {"unexpected": "object"}},
        ]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(_pr_files_json(entries)), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert err["verdict"] == "NO-GO"
        assert "previous_filename" in err["message"]

    def test_main_listing_non_dict_entry_is_no_go_with_its_index(self, monkeypatch):
        entries = [
            {"name": "ADR-038-x.md", "type": "file"},
            ["not", "an", "object"],
        ]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(json.dumps(entries)), None)
        )

        numbers, err = adr_check.get_main_adr_numbers()

        assert numbers is None
        assert err["verdict"] == "NO-GO"
        assert "entry 1" in err["message"]
        assert "niet toetsbaar" in err["message"]

    def test_main_listing_entry_without_name_is_no_go(self, monkeypatch):
        entries = [{"type": "file"}]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(json.dumps(entries)), None)
        )

        numbers, err = adr_check.get_main_adr_numbers()

        assert numbers is None
        assert err["verdict"] == "NO-GO"
        assert "name" in err["message"]

    def test_main_listing_entry_without_type_is_no_go(self, monkeypatch):
        entries = [{"name": "ADR-038-x.md"}]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(json.dumps(entries)), None)
        )

        numbers, err = adr_check.get_main_adr_numbers()

        assert numbers is None
        assert err["verdict"] == "NO-GO"
        assert "type" in err["message"]

    def test_malformed_entry_preview_is_truncated(self, monkeypatch):
        """A huge malformed blob must not flood the merge output: the entry is
        shown in shortened form, not in full.
        """
        entries = [{"filename": "docs/governance/decisions/ADR-041-y.md", "status": "added"}, "z" * 5000]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(_pr_files_json(entries)), None)
        )

        added, err = adr_check.get_pr_added_adr_files(1)

        assert added is None
        assert len(err["message"]) < 600

    def test_check_refuses_end_to_end_on_a_malformed_pr_files_entry(self, monkeypatch):
        """The whole gate, not just the reader: a malformed PR-files response
        must reach pr_merge as NO-GO, never as 'no new ADR files -> GO'.
        """
        def fake_capture(argv, *, timeout, cwd=None):
            joined = " ".join(argv)
            if "pulls" in joined:
                return _proc(json.dumps([["not-an-object"]])), None
            return _proc(_main_listing_json(["ADR-038-x.md"])), None

        monkeypatch.setattr(adr_check, "_capture", fake_capture)
        monkeypatch.setattr(adr_check.shutil, "which", lambda b: "/usr/bin/gh")

        result = adr_check.check_adr_numbers_for_pr(1790)

        assert result["verdict"] == "NO-GO"
        assert "niet toetsbaar" in result["message"]

    def test_check_refuses_end_to_end_on_a_malformed_main_listing_entry(self, monkeypatch):
        """Same for the base-branch listing: an unreadable entry there could
        hide a real collision, so it must refuse before the comparison. The
        READABLE listing entries here do not collide with the PR's number, so
        the old ``continue`` answered GO on a response it could not fully read.
        """
        def fake_capture(argv, *, timeout, cwd=None):
            joined = " ".join(argv)
            if "pulls" in joined:
                return _proc(
                    _pr_files_json(
                        [{"filename": "docs/governance/decisions/ADR-038-x.md", "status": "added"}]
                    )
                ), None
            return _proc(json.dumps([{"name": "ADR-039-y.md", "type": "file"}, 7])), None

        monkeypatch.setattr(adr_check, "_capture", fake_capture)
        monkeypatch.setattr(adr_check.shutil, "which", lambda b: "/usr/bin/gh")

        result = adr_check.check_adr_numbers_for_pr(1790)

        assert result["verdict"] == "NO-GO"
        assert "niet toetsbaar" in result["message"]


class TestGetMainAdrNumbers:
    def test_parses_directory_listing(self, monkeypatch):
        monkeypatch.setattr(
            adr_check, "_capture",
            lambda argv, *, timeout, cwd=None: (
                _proc(_main_listing_json(["ADR-038-receipt-outcome-identity.md", "ADR-039-x.md"])), None,
            ),
        )

        numbers, err = adr_check.get_main_adr_numbers()

        assert err is None
        assert numbers == {
            "38": "ADR-038-receipt-outcome-identity.md",
            "39": "ADR-039-x.md",
        }

    def test_non_adr_and_directory_entries_are_ignored(self, monkeypatch):
        entries = [
            {"name": "ADR-038-x.md", "type": "file"},
            {"name": "README.md", "type": "file"},
            {"name": "subdir", "type": "dir"},
        ]
        monkeypatch.setattr(
            adr_check, "_capture", lambda argv, *, timeout, cwd=None: (_proc(json.dumps(entries)), None)
        )

        numbers, err = adr_check.get_main_adr_numbers()

        assert err is None
        assert numbers == {"38": "ADR-038-x.md"}

    def test_api_failure_on_main_listing_is_fail_closed(self, monkeypatch):
        """A broken main-listing call must refuse, never read as 'no ADRs on main'."""
        monkeypatch.setattr(
            adr_check, "_capture",
            lambda argv, *, timeout, cwd=None: (_proc("", returncode=1, stderr="rate limited"), None),
        )

        numbers, err = adr_check.get_main_adr_numbers()

        assert numbers is None
        assert err["verdict"] == "NO-GO"
        assert "niet toetsbaar" in err["message"]

    def test_base_ref_with_special_characters_is_url_encoded(self, monkeypatch):
        """B6 fix-forward 2: ``base_ref`` is spliced straight into a query
        string (``?ref={base_ref}``); a branch name carrying '?' or '&' must
        be percent-encoded, not pasted in literally.
        """
        seen_argv: List[List[str]] = []

        def fake_capture(argv, *, timeout, cwd=None):
            seen_argv.append(argv)
            return _proc(_main_listing_json([])), None

        monkeypatch.setattr(adr_check, "_capture", fake_capture)

        numbers, err = adr_check.get_main_adr_numbers(base_ref="weird?ref&name")

        assert err is None
        assert numbers == {}
        call = " ".join(seen_argv[0])
        assert "ref=weird%3Fref%26name" in call
        assert "ref=weird?ref&name" not in call


class TestCheckAdrNumbersForPr:
    def _mock_gh(self, monkeypatch, *, pr_entries, main_names=None, main_error=False, gh_present=True):
        calls: Dict[str, int] = {"n": 0, "pulls": 0, "contents": 0}

        def fake_capture(argv, *, timeout, cwd=None):
            calls["n"] += 1
            joined = " ".join(argv)
            if "pulls" in joined:
                calls["pulls"] += 1
                return _proc(_pr_files_json(pr_entries)), None
            if "contents" in joined:
                calls["contents"] += 1
                if main_error:
                    return _proc("", returncode=1, stderr="boom"), None
                return _proc(_main_listing_json(main_names or [])), None
            raise AssertionError(f"unexpected gh call: {argv}")

        monkeypatch.setattr(adr_check, "_capture", fake_capture)
        monkeypatch.setattr(
            adr_check.shutil, "which", lambda b: ("/usr/bin/gh" if gh_present else None)
        )
        return calls

    def test_colliding_added_adr_number_is_no_go_naming_both_files(self, monkeypatch):
        """PR adds ADR-038-x.md; main already has ADR-038-y.md -> NO-GO naming '038' and both files."""
        self._mock_gh(
            monkeypatch,
            pr_entries=[{"filename": "docs/governance/decisions/ADR-038-x.md", "status": "added"}],
            main_names=["ADR-038-y.md"],
        )

        result = adr_check.check_adr_numbers_for_pr(1790)

        assert result["verdict"] == "NO-GO"
        assert result["message"] == (
            "ADR-038 botst: deze PR voegt 'docs/governance/decisions/ADR-038-x.md' toe, "
            "maar ADR-038 staat al op main als 'ADR-038-y.md'. Kies een vrij ADR-nummer."
        )
        assert result["colliding_number"] == "38"
        assert result["pr_file"] == "docs/governance/decisions/ADR-038-x.md"
        assert result["main_file"] == "ADR-038-y.md"

    def test_modified_existing_adr_is_not_a_collision(self, monkeypatch):
        """Same number but the PR entry status is MODIFIED (not added) -> GO."""
        self._mock_gh(
            monkeypatch,
            pr_entries=[{"filename": "docs/governance/decisions/ADR-038-x.md", "status": "modified"}],
            main_names=["ADR-038-x.md"],
        )

        result = adr_check.check_adr_numbers_for_pr(1790)

        assert result["verdict"] == "GO"

    def test_renamed_existing_adr_is_not_a_collision(self, monkeypatch):
        """A rename (status=renamed) of an existing ADR is not an add -> GO."""
        self._mock_gh(
            monkeypatch,
            pr_entries=[{"filename": "docs/governance/decisions/ADR-038-x.md", "status": "renamed",
                         "previous_filename": "docs/governance/decisions/ADR-038-old.md"}],
            main_names=["ADR-038-old.md"],
        )

        result = adr_check.check_adr_numbers_for_pr(1790)

        assert result["verdict"] == "GO"

    def test_added_adr_with_a_free_number_is_go(self, monkeypatch):
        self._mock_gh(
            monkeypatch,
            pr_entries=[{"filename": "docs/governance/decisions/ADR-040-new.md", "status": "added"}],
            main_names=["ADR-038-x.md", "ADR-039-y.md"],
        )

        result = adr_check.check_adr_numbers_for_pr(1790)

        assert result["verdict"] == "GO"

    def test_main_listing_api_failure_is_fail_closed(self, monkeypatch):
        """API failure on the main-listing call: refusal, never a silent pass."""
        self._mock_gh(
            monkeypatch,
            pr_entries=[{"filename": "docs/governance/decisions/ADR-038-x.md", "status": "added"}],
            main_error=True,
        )

        result = adr_check.check_adr_numbers_for_pr(1790)

        assert result["verdict"] == "NO-GO"
        assert "niet toetsbaar" in result["message"]

    def test_no_new_adr_files_is_go_and_skips_the_main_call(self, monkeypatch):
        """No added ADR files -> GO without ever querying the main-listing."""
        calls = self._mock_gh(
            monkeypatch, pr_entries=[{"filename": "scripts/pr_merge.py", "status": "modified"}],
        )

        result = adr_check.check_adr_numbers_for_pr(1790)

        assert result["verdict"] == "GO"
        assert calls["pulls"] == 1
        assert calls["contents"] == 0, "main-listing must not be queried when there is nothing to check"

    def test_gh_missing_is_no_go(self, monkeypatch):
        self._mock_gh(monkeypatch, pr_entries=[], gh_present=False)

        result = adr_check.check_adr_numbers_for_pr(1790)

        assert result["verdict"] == "NO-GO"
        assert "gh CLI niet beschikbaar" in result["message"]

    def test_renamed_into_a_number_already_on_main_is_a_collision(self, monkeypatch):
        """Leeszetel finding 3: main has ADR-038; a PR renames an UNRELATED
        existing ADR (031) to claim 038 too. Before the fix, status="renamed"
        was skipped outright and this returned a false GO with a message that
        claimed "no new ADR files" even though the PR does claim a new number.
        """
        self._mock_gh(
            monkeypatch,
            pr_entries=[{
                "filename": "docs/governance/decisions/ADR-038-my-new-decision.md",
                "status": "renamed",
                "previous_filename": "docs/governance/decisions/ADR-031-orchestration-target-ratification.md",
            }],
            main_names=["ADR-038-receipt-outcome-identity.md"],
        )

        result = adr_check.check_adr_numbers_for_pr(1790)

        assert result["verdict"] == "NO-GO"
        assert result["colliding_number"] == "38"
        assert result["pr_file"] == "docs/governance/decisions/ADR-038-my-new-decision.md"
        assert result["main_file"] == "ADR-038-receipt-outcome-identity.md"


class TestPrMergeAdrGateWiring:
    """pr_merge.main() refuses before merge on an ADR-number collision, in the
    same fail-closed style as the CI and review gates.
    """

    def _no_go_adr(self):
        return {
            "verdict": "NO-GO",
            "message": (
                "ADR-038 botst: deze PR voegt 'docs/governance/decisions/ADR-038-x.md' toe, "
                "maar ADR-038 staat al op main als 'ADR-038-y.md'. Kies een vrij ADR-nummer."
            ),
            "colliding_number": "38",
            "pr_file": "docs/governance/decisions/ADR-038-x.md",
            "main_file": "ADR-038-y.md",
        }

    def _go_adr(self):
        return {
            "verdict": "GO",
            "message": "Geen ADR-nummerbotsing: 0 nieuw(e) ADR-bestand(en) getoetst tegen main",
            "colliding_number": None, "pr_file": None, "main_file": None,
        }

    def _go_gate(self, **kw):
        gate = {
            "verdict": "GO",
            "message": "VNX CI geslaagd op aaaaaaaaaaaa",
            "ci_conclusion": "success", "ran_on_sha": True,
            "head_sha": "a" * 40, "ci_run_id": 1, "workflow_name": "VNX CI",
            "overridden": False, "override_reason": None,
        }
        gate.update(kw)
        return gate

    def _ok_dry_run_result(self):
        return {
            "success": True, "pr_number": 1790, "dispatch_id": "", "merge_method": "squash",
            "pr_title": "", "branch": "", "receipt_status": None, "receipt_ok": False,
            "register_ok": False, "error": "", "dry_run": True, "overlaps": [],
        }

    def test_adr_collision_refuses_before_merge(self, monkeypatch, capsys):
        import pr_merge

        merge_called = []
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (self._go_gate(), None))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (self._go_gate(), None))
        monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: self._no_go_adr())
        monkeypatch.setattr(
            pr_merge, "merge_pr",
            lambda **k: merge_called.append(1) or self._ok_dry_run_result(),
        )

        rc = pr_merge.main(["--pr", "1790", "--dry-run"])

        assert rc == pr_merge.EXIT_ERROR
        assert not merge_called, "merge_pr must not run when the ADR gate is NO-GO"
        err = capsys.readouterr().err
        assert "NO-GO" in err
        assert "ADR-038" in err
        assert "botst" in err

    def test_adr_collision_json_output(self, monkeypatch, capsys):
        """Leeszetel finding 4: without --dry-run and a merge_pr mock, a GO
        verdict here (e.g. if the ADR gate regresses) would fall through into
        a REAL `gh pr merge` on a real PR number. Mirrors the sibling test
        above: --dry-run plus a merge_pr mock that proves it never ran.
        """
        import pr_merge

        merge_called = []
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (self._go_gate(), None))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (self._go_gate(), None))
        monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: self._no_go_adr())
        monkeypatch.setattr(
            pr_merge, "merge_pr",
            lambda **k: merge_called.append(1) or self._ok_dry_run_result(),
        )

        rc = pr_merge.main(["--pr", "1790", "--dry-run", "--json"])

        assert rc == pr_merge.EXIT_ERROR
        assert not merge_called, "merge_pr must not run when the ADR gate is NO-GO"
        # Pre-existing, out-of-scope quirk: main() unconditionally prints plain
        # "CI gate: ..." / "Review gate: ..." text on a GO verdict, ignoring
        # --json, so stdout is not pure JSON once an earlier gate has already
        # succeeded. Isolate the JSON object itself rather than asserting the
        # whole of stdout parses (not this gate's defect to fix; B6's file
        # scope is the new call only).
        stdout = capsys.readouterr().out
        out = json.loads(stdout[stdout.index("{"):])
        assert out["success"] is False
        assert "ADR-038" in out["error"]
        assert out["adr_gate"]["colliding_number"] == "38"

    def test_adr_go_proceeds_to_merge(self, monkeypatch, capsys):
        import pr_merge

        merge_called = []
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (self._go_gate(), None))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (self._go_gate(), None))
        monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: self._go_adr())
        monkeypatch.setattr(
            pr_merge, "merge_pr",
            lambda **k: merge_called.append(1) or self._ok_dry_run_result(),
        )

        rc = pr_merge.main(["--pr", "1790", "--dry-run"])

        assert rc == pr_merge.EXIT_OK
        assert merge_called, "merge_pr must run when all three gates are GO"
        assert "ADR gate" in capsys.readouterr().out


class TestRunAdrGateOi1518Recovery:
    """pr_merge._run_adr_gate's own logic (leeszetel findings 2 + 7): the
    OI-1518-recovery skip for an already-MERGED PR, and reading the check's
    base branch from the PR's real ``baseRefName`` instead of a hardcoded
    "main". These call ``pr_merge._run_adr_gate`` directly (not via
    ``main()``) and stub only ``pr_merge.check_adr_numbers_for_pr`` — the
    live-gh delegate — so the gate function's own branching runs for real.
    """

    def test_already_merged_pr_skips_the_live_check_entirely(self, monkeypatch):
        import pr_merge

        called = []
        monkeypatch.setattr(
            pr_merge, "check_adr_numbers_for_pr",
            lambda *a, **k: called.append(1) or {"verdict": "NO-GO", "message": "must not run"},
        )

        result = pr_merge._run_adr_gate(
            1790, pr_data={"state": "MERGED", "mergedAt": "2026-09-06T10:00:00Z"}
        )

        assert result["verdict"] == "GO"
        assert "MERGED" in result["message"]
        assert not called, "an already-merged PR must skip the live ADR check entirely"

    def test_open_pr_still_runs_the_live_check(self, monkeypatch):
        import pr_merge

        seen = {}

        def fake_check(pr_number, *, project_root=None, base_ref="main"):
            seen["pr_number"] = pr_number
            seen["base_ref"] = base_ref
            return {"verdict": "GO", "message": "checked"}

        monkeypatch.setattr(pr_merge, "check_adr_numbers_for_pr", fake_check)

        result = pr_merge._run_adr_gate(1790, pr_data={"state": "OPEN"})

        assert result == {"verdict": "GO", "message": "checked"}
        assert seen["pr_number"] == 1790

    def test_closed_but_not_merged_pr_still_runs_the_live_check(self, monkeypatch):
        """CLOSED (never merged) is not MERGED: the collision can still be real."""
        import pr_merge

        called = []
        monkeypatch.setattr(
            pr_merge, "check_adr_numbers_for_pr",
            lambda *a, **k: called.append(1) or {"verdict": "GO", "message": "checked"},
        )

        pr_merge._run_adr_gate(1790, pr_data={"state": "CLOSED"})

        assert called, "a CLOSED-but-not-merged PR must still run the live check"

    def test_base_ref_is_taken_from_pr_datas_base_ref_name(self, monkeypatch):
        import pr_merge

        seen = {}

        def fake_check(pr_number, *, project_root=None, base_ref="main"):
            seen["base_ref"] = base_ref
            return {"verdict": "GO", "message": "checked"}

        monkeypatch.setattr(pr_merge, "check_adr_numbers_for_pr", fake_check)

        pr_merge._run_adr_gate(1790, pr_data={"state": "OPEN", "baseRefName": "release/1.6"})

        assert seen["base_ref"] == "release/1.6"

    def test_missing_pr_data_defaults_base_ref_to_main(self, monkeypatch):
        import pr_merge

        seen = {}

        def fake_check(pr_number, *, project_root=None, base_ref="main"):
            seen["base_ref"] = base_ref
            return {"verdict": "GO", "message": "checked"}

        monkeypatch.setattr(pr_merge, "check_adr_numbers_for_pr", fake_check)

        pr_merge._run_adr_gate(1790)

        assert seen["base_ref"] == "main"
