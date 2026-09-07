#!/usr/bin/env python3
"""Tests for the branch-protection merge gate wired into pr_merge.py
(Golf B, B1).

``_run_branch_protection_gate`` runs four checks in order: (a) read main's
own YAML (a 404 on that exact path is the bootstrap no-op), (b) live vs
main's YAML drift (no override), (c) the PR's own YAML must not weaken
main's (``--allow-weaken`` is the only override), (d) the running
``scripts/pr_merge.py`` must hash-match main's copy. Only the two network
primitives (``fetch_yaml_from_ref``, ``fetch_live_protection``) and the door
hash-check are mocked per test — ``compare``/``is_weakening`` run for real.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import pr_merge
from forge_protection_drift import YamlFetchResult, to_normalized_dict, load_protection_config


SHA = "c" * 40
YAML_PATH = SCRIPTS_DIR / "forge" / "branch_protection.yaml"
PR_DATA = {
    "number": 1, "title": "t", "state": "OPEN",
    "headRefName": "feature/x", "baseRefName": "main", "headRefOid": SHA,
}


def _real_yaml_text() -> str:
    return YAML_PATH.read_text(encoding="utf-8")


def _real_norm() -> Dict[str, Any]:
    return to_normalized_dict(load_protection_config(YAML_PATH))


def _go(**overrides: Any) -> Dict[str, Any]:
    gate = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
    gate.update(overrides)
    return gate


def _found(text: str) -> YamlFetchResult:
    return YamlFetchResult(text=text, not_found=False, error=None)


def _not_found() -> YamlFetchResult:
    return YamlFetchResult(text=None, not_found=True, error=None)


def _fetch_error(msg: str = "gh api faalde (rc=1): boom") -> YamlFetchResult:
    return YamlFetchResult(text=None, not_found=False, error=msg)


def _stub_matching_door_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_merge, "_door_blob_hash_gate", lambda project_root: _go())


# ---------------------------------------------------------------------------
# _door_blob_hash_gate — direct
# ---------------------------------------------------------------------------

class TestDoorBlobHashGate:
    def _fake_run(self, *, local_hash="abc123", remote_hash="abc123", local_rc=0, remote_rc=0):
        def fake(argv, **kwargs):
            if argv[:2] == ["git", "hash-object"]:
                return subprocess.CompletedProcess(argv, local_rc, stdout=f"{local_hash}\n", stderr="")
            assert argv[0] == "gh"
            return subprocess.CompletedProcess(argv, remote_rc, stdout=f"{remote_hash}\n", stderr="boom" if remote_rc else "")
        return fake

    def test_matching_hashes_is_go(self, monkeypatch):
        monkeypatch.setattr(pr_merge.subprocess, "run", self._fake_run(local_hash="x", remote_hash="x"))
        result = pr_merge._door_blob_hash_gate(VNX_ROOT)
        assert result["verdict"] == "GO"

    def test_mismatched_hashes_is_no_go(self, monkeypatch):
        """(l) blob-hash of the running pr_merge.py differs from main: refused."""
        monkeypatch.setattr(pr_merge.subprocess, "run", self._fake_run(local_hash="local", remote_hash="onmain"))
        result = pr_merge._door_blob_hash_gate(VNX_ROOT)
        assert result["verdict"] == "NO-GO"
        assert "main" in result["message"]

    def test_local_git_failure_is_no_go(self, monkeypatch):
        monkeypatch.setattr(pr_merge.subprocess, "run", self._fake_run(local_rc=1))
        result = pr_merge._door_blob_hash_gate(VNX_ROOT)
        assert result["verdict"] == "NO-GO"

    def test_remote_gh_failure_is_no_go(self, monkeypatch):
        monkeypatch.setattr(pr_merge.subprocess, "run", self._fake_run(remote_rc=1))
        result = pr_merge._door_blob_hash_gate(VNX_ROOT)
        assert result["verdict"] == "NO-GO"


# ---------------------------------------------------------------------------
# _run_branch_protection_gate — the four steps
# ---------------------------------------------------------------------------

class TestRunBranchProtectionGate:
    def test_bootstrap_noop_when_main_has_no_yaml(self, monkeypatch):
        """(i) 404 on exactly the YAML path: no-op with the loud bootstrap message."""
        calls: List[str] = []

        def fake_fetch(project_root, ref, *a, **k):
            calls.append(ref)
            return _not_found()

        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", fake_fetch)
        live_calls: List[Any] = []
        monkeypatch.setattr(
            pr_merge, "fetch_live_protection", lambda *a, **k: live_calls.append(1) or _real_norm(),
        )

        result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)

        assert result["verdict"] == "GO"
        assert result["bootstrap"] is True
        assert "geen YAML op main" in result["message"]
        assert calls == ["main"]
        assert not live_calls, "the bootstrap no-op must not go on to read live state"

    def test_main_yaml_unreadable_blocks(self, monkeypatch):
        """(i) a non-404 fault (500/403) on the YAML read is a refusal."""
        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", lambda *a, **k: _fetch_error("HTTP 500"))
        result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)
        assert result["verdict"] == "NO-GO"
        assert "niet leesbaar" in result["message"]

    def test_main_yaml_forbidden_blocks(self, monkeypatch):
        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", lambda *a, **k: _fetch_error("HTTP 403"))
        result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)
        assert result["verdict"] == "NO-GO"

    def test_main_yaml_invalid_blocks(self, monkeypatch):
        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", lambda *a, **k: _found("branch: main\nnot_a_known_field: 1\n"))
        result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)
        assert result["verdict"] == "NO-GO"
        assert "ongeldig" in result["message"]

    def test_live_drift_blocks_naming_the_field(self, monkeypatch):
        """(b) live differs from main's own declared YAML: refused, field named."""
        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", lambda project_root, ref, *a, **k: _found(_real_yaml_text()))
        drifted_live = _real_norm()
        drifted_live["allow_force_pushes"] = True
        monkeypatch.setattr(pr_merge, "fetch_live_protection", lambda *a, **k: drifted_live)

        result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)

        assert result["verdict"] == "NO-GO"
        assert "allow_force_pushes" in result["message"]

    def test_live_fetch_exception_blocks(self, monkeypatch):
        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", lambda project_root, ref, *a, **k: _found(_real_yaml_text()))

        def raise_error(*a, **k):
            raise RuntimeError("network unreachable")

        monkeypatch.setattr(pr_merge, "fetch_live_protection", raise_error)
        result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)
        assert result["verdict"] == "NO-GO"
        assert "niet leesbaar" in result["message"]

    def _stub_main_matches_live(self, monkeypatch):
        """main's YAML, live state, and the PR-head's YAML all identical --
        the "nothing to refuse" baseline other tests build on."""
        live = _real_norm()
        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", lambda project_root, ref, *a, **k: _found(_real_yaml_text()))
        monkeypatch.setattr(pr_merge, "fetch_live_protection", lambda *a, **k: live)

    def test_pr_deletes_the_yaml_is_refused_with_the_path(self, monkeypatch):
        """(j) the PR removes scripts/forge/branch_protection.yaml: refused, path named."""
        def fake_fetch(project_root, ref, *a, **k):
            if ref == "main":
                return _found(_real_yaml_text())
            return _not_found()

        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", fake_fetch)
        monkeypatch.setattr(pr_merge, "fetch_live_protection", lambda *a, **k: _real_norm())

        result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)

        assert result["verdict"] == "NO-GO"
        assert "scripts/forge/branch_protection.yaml" in result["message"]
        assert "verwijdert" in result["message"]

    def test_pr_yaml_read_error_blocks(self, monkeypatch):
        def fake_fetch(project_root, ref, *a, **k):
            if ref == "main":
                return _found(_real_yaml_text())
            return _fetch_error("HTTP 500")

        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", fake_fetch)
        monkeypatch.setattr(pr_merge, "fetch_live_protection", lambda *a, **k: _real_norm())

        result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)
        assert result["verdict"] == "NO-GO"

    def test_pr_head_unresolvable_blocks(self, monkeypatch):
        self._stub_main_matches_live(monkeypatch)
        result = pr_merge._run_branch_protection_gate(1, pr_data={})
        assert result["verdict"] == "NO-GO"
        assert "PR-head" in result["message"]

    class TestWeakeningOnPrSide:
        """(k) the PR's own YAML weakens main's — enforce_admins: false, or a
        vnx-gate/* entry dropped from checks[]."""

        def _pr_yaml_with_enforce_admins_false(self) -> str:
            return _real_yaml_text().replace("enforce_admins: true", "enforce_admins: false")

        def test_enforce_admins_false_is_refused_without_allow_weaken(self, monkeypatch):
            def fake_fetch(project_root, ref, *a, **k):
                if ref == "main":
                    return _found(_real_yaml_text())
                return _found(self._pr_yaml_with_enforce_admins_false())

            monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", fake_fetch)
            monkeypatch.setattr(pr_merge, "fetch_live_protection", lambda *a, **k: _real_norm())
            _stub_matching_door_hash(monkeypatch)

            result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)

            assert result["verdict"] == "NO-GO"
            assert "enforce_admins" in result["message"]
            assert "allow-weaken" in result["message"]

        def test_enforce_admins_false_with_allow_weaken_and_reason_proceeds(self, monkeypatch):
            def fake_fetch(project_root, ref, *a, **k):
                if ref == "main":
                    return _found(_real_yaml_text())
                return _found(self._pr_yaml_with_enforce_admins_false())

            monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", fake_fetch)
            monkeypatch.setattr(pr_merge, "fetch_live_protection", lambda *a, **k: _real_norm())
            _stub_matching_door_hash(monkeypatch)

            result = pr_merge._run_branch_protection_gate(
                1, pr_data=PR_DATA, allow_weaken_reason="operator akkoord",
            )

            assert result["verdict"] == "GO"
            assert result["overridden"] is True
            assert result["override_reason"] == "operator akkoord"
            assert "OVERRIDE" in result["message"]

        def test_allow_weaken_with_empty_reason_is_refused(self, monkeypatch):
            def fake_fetch(project_root, ref, *a, **k):
                if ref == "main":
                    return _found(_real_yaml_text())
                return _found(self._pr_yaml_with_enforce_admins_false())

            monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", fake_fetch)
            monkeypatch.setattr(pr_merge, "fetch_live_protection", lambda *a, **k: _real_norm())

            result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA, allow_weaken_reason="   ")

            assert result["verdict"] == "NO-GO"
            assert result["overridden"] is True
            assert "niet-lege reden" in result["message"]

        def test_removing_a_vnx_gate_check_is_refused_without_allow_weaken(self, monkeypatch):
            main_text = _real_yaml_text().replace(
                "    - {context: \"vnx doctor smoke\", app_id: 15368}\n",
                "    - {context: \"vnx doctor smoke\", app_id: 15368}\n"
                "    - {context: \"vnx-gate/review\", app_id: 424242}\n",
            )
            pr_text = _real_yaml_text()  # PR-side lacks the vnx-gate/* entry main now has

            def fake_fetch(project_root, ref, *a, **k):
                if ref == "main":
                    return _found(main_text)
                return _found(pr_text)

            monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", fake_fetch)
            main_norm = to_normalized_dict(pr_merge.parse_protection_config(main_text))
            monkeypatch.setattr(pr_merge, "fetch_live_protection", lambda *a, **k: main_norm)

            result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)

            assert result["verdict"] == "NO-GO"
            assert "vnx-gate/review" in result["message"]

    def test_door_hash_check_runs_after_a_clean_pr_and_its_no_go_propagates(self, monkeypatch):
        self._stub_main_matches_live(monkeypatch)
        monkeypatch.setattr(
            pr_merge, "_door_blob_hash_gate",
            lambda project_root: {"verdict": "NO-GO", "message": "deur draait niet op main", "overridden": False, "override_reason": None},
        )

        result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)

        assert result["verdict"] == "NO-GO"
        assert "deur draait niet op main" in result["message"]

    def test_clean_pr_with_matching_door_hash_is_go(self, monkeypatch):
        self._stub_main_matches_live(monkeypatch)
        _stub_matching_door_hash(monkeypatch)

        result = pr_merge._run_branch_protection_gate(1, pr_data=PR_DATA)

        assert result["verdict"] == "GO"
        assert result["overridden"] is False


# ---------------------------------------------------------------------------
# main() wiring
# ---------------------------------------------------------------------------

class TestMainWiring:
    def _ok_dry_run_result(self):
        return {
            "success": True, "pr_number": 1, "dispatch_id": "", "merge_method": "squash",
            "pr_title": "", "branch": "", "receipt_status": None, "receipt_ok": False,
            "register_ok": False, "error": "", "dry_run": True, "overlaps": [],
        }

    def _bypass_everything_but_protection(self, monkeypatch):
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (_go(), dict(PR_DATA)))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (_go(), dict(PR_DATA)))
        monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: _go())
        monkeypatch.setattr(pr_merge, "_run_contract_invalid_gate", lambda *a, **k: _go(skipped=True))

    def test_main_invokes_the_branch_protection_gate(self, monkeypatch, capsys):
        """(f) presence test, pattern tests/test_pr_merge_ci_gate.py::TestMainGateWiring."""
        self._bypass_everything_but_protection(monkeypatch)
        seen: List[Any] = []
        monkeypatch.setattr(
            pr_merge, "_run_branch_protection_gate",
            lambda pr, **k: seen.append(k) or _go(),
        )
        monkeypatch.setattr(pr_merge, "merge_pr", lambda **k: self._ok_dry_run_result())

        rc = pr_merge.main(["--pr", "1", "--dry-run"])

        assert rc == pr_merge.EXIT_OK
        assert seen, "_run_branch_protection_gate must be invoked by main()"
        assert "Branch-protection gate" in capsys.readouterr().out

    def test_main_refuses_before_merge_when_gate_is_no_go(self, monkeypatch, capsys):
        self._bypass_everything_but_protection(monkeypatch)
        merge_called: List[Any] = []
        monkeypatch.setattr(
            pr_merge, "_run_branch_protection_gate",
            lambda pr, **k: {"verdict": "NO-GO", "message": "branch-protection wijkt af: X", "overridden": False, "override_reason": None},
        )
        monkeypatch.setattr(pr_merge, "merge_pr", lambda **k: merge_called.append(1) or self._ok_dry_run_result())

        rc = pr_merge.main(["--pr", "1", "--dry-run"])

        assert rc == pr_merge.EXIT_ERROR
        assert not merge_called
        assert "branch-protection wijkt af" in capsys.readouterr().err

    def test_main_forwards_allow_weaken_flag(self, monkeypatch):
        self._bypass_everything_but_protection(monkeypatch)
        seen: Dict[str, Any] = {}

        def fake_gate(pr, **k):
            seen.update(k)
            return _go()

        monkeypatch.setattr(pr_merge, "_run_branch_protection_gate", fake_gate)
        monkeypatch.setattr(pr_merge, "merge_pr", lambda **k: self._ok_dry_run_result())

        rc = pr_merge.main(["--pr", "1", "--dry-run", "--allow-weaken", "operator zei ja"])

        assert rc == pr_merge.EXIT_OK
        assert seen.get("allow_weaken_reason") == "operator zei ja"

    def test_main_json_no_go_outputs_json(self, monkeypatch, capsys):
        self._bypass_everything_but_protection(monkeypatch)
        monkeypatch.setattr(
            pr_merge, "_run_branch_protection_gate",
            lambda pr, **k: {"verdict": "NO-GO", "message": "branch-protection wijkt af: X", "overridden": False, "override_reason": None},
        )

        rc = pr_merge.main(["--pr", "1", "--json"])

        assert rc == pr_merge.EXIT_ERROR
        import json as _json
        stdout = capsys.readouterr().out
        out = _json.loads(stdout[stdout.index("{"):])
        assert out["success"] is False
        assert "branch-protection wijkt af" in out["error"]
