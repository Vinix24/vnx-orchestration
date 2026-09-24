#!/usr/bin/env python3
"""The merge door judges the project it is run for, and how strictly is the
project's call (OI-1849).

Every test drives the real ``pr_merge.main()`` from a fake central install
(a clone of ``fabric/x``) with a consumer checkout (``consumer/y``) as the
project, against a stub ``gh`` on PATH that resolves ``{owner}/{repo}`` from
the directory it is run in, as ``gh`` does, and logs the repo of every call.
Nothing reaches a real repo, a real ``gh`` or ``~/.vnx-system``.

Before OI-1849 the branch-protection gate, the CI gate and the ADR gate ran in
the install (``SCRIPT_DIR.parent``): the door proved "no drift" about
``fabric/x`` for a merge into ``consumer/y``. The tests here fail on that: a call
about the wrong repo either matches no rule (the stub answers 99 and logs it as
unmatched) or shows up in the log under the wrong repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import forge_protection_drift as fpd
import pr_merge
from merge_target_helpers import (
    CONSUMER_REPO,
    FABRIC_REPO,
    HEAD_SHA,
    MAIN_SHA,
    GhStub,
    consumer_yaml,
    contents_response,
    github_protection,
    install_gh_stub,
    isolate_project_env,
    make_consumer,
    make_install,
    not_found_rules,
    pr_view_json,
)

#: conftest replaces these on ``pr_merge`` for every test (offline defaults), so the
#: real ones are captured here, at import. ``getattr``: this file must also load
#: against a tree that predates OI-1849, where it fails on its assertions rather
#: than on an import.
_REAL_RESOLVE_MERGE_TARGET = getattr(pr_merge, "resolve_merge_target", None)
_REAL_FETCH_YAML_FROM_REF = fpd.fetch_yaml_from_ref
_REAL_DOOR_BLOB_HASH_GATE = pr_merge._door_blob_hash_gate
_REAL_MERGE_PR = pr_merge.merge_pr

MAIN_YAML_PATH = ".vnx/branch_protection.yaml"
FABRIC_STYLE_YAML_PATH = "scripts/forge/branch_protection.yaml"


def _go(**extra: Any) -> Dict[str, Any]:
    gate = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
    gate.update(extra)
    return gate


def _ci_runs(conclusion: str = "success") -> str:
    return json.dumps([{
        "conclusion": conclusion, "headSha": HEAD_SHA, "status": "completed",
        "databaseId": 11, "createdAt": "2026-09-24T10:00:00Z",
    }])


def _rules(
    *,
    main_yaml: Optional[str],
    head_yaml: Optional[str] = None,
    live: Optional[str] = None,
    ci_conclusion: str = "success",
    pr_states: Optional[List[str]] = None,
    first: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Everything ``consumer/y`` answers for one merge. ``main_yaml=None`` means the project
    has no file at all (a confirmed 404 on both candidate paths).

    ``head_yaml`` defaults to the same text as main's: a PR that leaves the file alone.
    ``first`` rules are consulted before all the others (first match wins), which is how a
    test overrides one answer.
    """
    repo = CONSUMER_REPO
    head_yaml = main_yaml if head_yaml is None else head_yaml
    states = pr_states or ["OPEN"]
    rules: List[Dict[str, Any]] = list(first or [])
    rules += [
        {"match": "repo view", "repo": repo, "stdout": repo + "\n"},
        {"match": "pr view", "repo": repo, "stdouts": [pr_view_json(state=s) for s in states]},
        {"match": "pr merge", "repo": repo, "stdout": ""},
        {"match": "auth status", "stdout": "Logged in\n"},
        {"match": "run list", "repo": repo, "stdout": _ci_runs(ci_conclusion)},
        {"match": "commits/main", "repo": repo, "stdout": MAIN_SHA + "\n"},
        {"match": f"commits/{HEAD_SHA}", "repo": repo, "stdout": HEAD_SHA + "\n"},
    ]
    for ref, text in (("main", main_yaml), (HEAD_SHA, head_yaml)):
        if text is None:
            for path in (MAIN_YAML_PATH, FABRIC_STYLE_YAML_PATH):
                rules.append({
                    "match": f"contents/{path}?ref={ref}", "repo": repo,
                    "rc": 1, "stderr": "gh: Not Found (HTTP 404)\n",
                })
        else:
            rules.append({
                "match": f"contents/{MAIN_YAML_PATH}?ref={ref}", "repo": repo,
                "stdout": contents_response(text),
            })
    if live is None:
        live = github_protection()
    rules += [
        {"match": "branches/main/protection", "repo": repo, "stdout": live},
        {"match": "/rulesets", "repo": repo, "stdout": "[]"},
        {"exact": f"api repos/{repo} --jq .allow_auto_merge", "stdout": "false\n"},
        {"exact": f"api repos/{repo}", "repo": repo, "stdout": json.dumps({"allow_auto_merge": False})},
    ]
    return rules


class Door:
    """One arranged merge: the install, the consumer, the stub, and the capture of what
    ``merge_pr`` was handed."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        rules: List[Dict[str, Any]],
        *,
        install: Optional[Path] = None,
    ):
        self.install = install or make_install(tmp_path)
        self.consumer = make_consumer(tmp_path)
        self.gh: GhStub = install_gh_stub(tmp_path, monkeypatch, rules)
        self.merge_kwargs: List[Dict[str, Any]] = []
        self.monkeypatch = monkeypatch

        isolate_project_env(monkeypatch, self.install)
        monkeypatch.chdir(self.consumer)
        # The real resolver and the real YAML read, past conftest's offline stubs.
        monkeypatch.setattr(pr_merge, "resolve_merge_target", _REAL_RESOLVE_MERGE_TARGET, raising=False)
        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", _REAL_FETCH_YAML_FROM_REF)
        # Gates that are not what this file is about.
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (_go(), None))
        monkeypatch.setattr(pr_merge, "_run_contract_invalid_gate", lambda *a, **k: _go(skipped=True))
        monkeypatch.setattr(pr_merge, "check_adr_numbers_for_pr", lambda *a, **k: _go())
        monkeypatch.setattr(pr_merge, "merge_pr", self._capture_merge_pr)

    def _capture_merge_pr(self, **kwargs: Any) -> Dict[str, Any]:
        self.merge_kwargs.append(kwargs)
        return {
            "success": True, "pr_number": kwargs.get("pr_number"), "dispatch_id": "",
            "merge_method": "squash", "pr_title": "", "branch": "", "receipt_status": None,
            "receipt_ok": False, "register_ok": False, "error": "", "dry_run": True, "overlaps": [],
        }

    def run(self, *extra: str) -> int:
        return pr_merge.main(["--pr", "7", "--dry-run", *extra])

    def protection_record(self) -> Dict[str, Any]:
        """The branch-protection record ``main()`` handed to ``merge_pr`` for the receipt."""
        assert self.merge_kwargs, "the door refused before it reached the merge"
        gates = self.merge_kwargs[-1]["preflight_gates"]
        return next(g for g in gates if g["gate"] == "branch_protection")


@pytest.fixture()
def arrange(tmp_path, monkeypatch):
    def _arrange(**rule_kwargs: Any) -> Door:
        return Door(tmp_path, monkeypatch, _rules(**rule_kwargs))
    return _arrange


# ---------------------------------------------------------------------------
# Which repo every call goes to
# ---------------------------------------------------------------------------

class TestTargetRepo:
    def test_everything_that_judges_the_merge_asks_the_consumer_not_the_install(self, arrange, capsys):
        """The measured defect. Run from the install with cwd = the consumer, the YAML on
        main, the YAML on the PR head, live protection, the CI run and the PR itself are
        all questions about ``consumer/y``."""
        door = arrange(main_yaml=consumer_yaml(ci_workflow="CI"))

        rc = door.run()
        out = capsys.readouterr()

        assert out.out.startswith(f"doelrepo: {CONSUMER_REPO}\n"), out
        for needle in (
            f"contents/{MAIN_YAML_PATH}?ref=main",
            f"contents/{MAIN_YAML_PATH}?ref={HEAD_SHA}",
            "branches/main/protection",
            "/rulesets",
            "run list",
            "pr view",
        ):
            assert door.gh.repos_of(needle) == {CONSUMER_REPO}, (needle, door.gh.calls())
        assert FABRIC_REPO not in {call["repo"] for call in door.gh.calls()}
        assert door.gh.unmatched() == []
        assert rc == pr_merge.EXIT_OK, out.err

    def test_a_merge_goes_to_the_project_root_not_the_cwd(self, arrange, monkeypatch):
        """``VNX_PROJECT_ROOT`` names the project; the cwd is the install. ``gh pr merge``, the
        state check after it, the ADR gate and the overlap scan all follow the project."""
        door = arrange(main_yaml=consumer_yaml(), pr_states=["OPEN", "MERGED"])
        monkeypatch.setenv("VNX_PROJECT_ROOT", str(door.consumer))
        monkeypatch.chdir(door.install)
        monkeypatch.setattr(pr_merge, "merge_pr", _REAL_MERGE_PR)
        adr_roots: List[Path] = []
        monkeypatch.setattr(
            pr_merge, "check_adr_numbers_for_pr",
            lambda pr, **k: adr_roots.append(k["project_root"]) or _go(),
        )
        overlap_roots: List[Path] = []
        import file_scope_overlap
        monkeypatch.setattr(
            file_scope_overlap, "warn_overlaps",
            lambda branch, **k: overlap_roots.append(k["repo"]) or [],
        )
        monkeypatch.setattr(pr_merge, "_emit_receipt", lambda **k: {"append_status": "appended"})
        monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)
        monkeypatch.setattr(pr_merge, "_lookup_dispatch_id_by_pr_number", lambda n: "")

        rc = pr_merge.main(["--pr", "7"])

        assert rc == pr_merge.EXIT_OK
        merge_calls = door.gh.calls_with("pr merge")
        assert [call["repo"] for call in merge_calls] == [CONSUMER_REPO]
        assert Path(merge_calls[0]["cwd"]).resolve() == door.consumer
        assert door.gh.repos_of("pr view") == {CONSUMER_REPO}
        assert adr_roots == [door.consumer]
        assert overlap_roots == [door.consumer]

    def test_the_repo_is_named_in_the_json_payload_and_kept_off_stdout(self, arrange, capsys):
        """A ``--json`` consumer reads stdout: the repo line goes to stderr there, and the
        object carries it as ``target_repo``. (The gate lines that come before a later
        refusal were already on stdout, so the object is read from its first brace.)"""
        door = arrange(main_yaml=consumer_yaml(), live=github_protection(allow_force_pushes=True))

        rc = door.run("--json")
        out = capsys.readouterr()

        assert rc == pr_merge.EXIT_ERROR
        assert f"doelrepo: {CONSUMER_REPO}" in out.err
        assert "doelrepo" not in out.out
        payload = json.loads(out.out[out.out.index("{"):])
        assert payload["target_repo"] == CONSUMER_REPO
        assert payload["refused_by"] == "branch_protection"

    def test_a_target_that_cannot_be_named_refuses_before_any_gate(self, arrange, capsys):
        door = arrange(
            main_yaml=consumer_yaml(),
            first=[{"match": "repo view", "rc": 1, "stderr": "HTTP 401: Bad credentials\n"}],
        )

        rc = door.run()
        err = capsys.readouterr().err

        assert rc == pr_merge.EXIT_ERROR
        assert "doelrepo kon niet worden bepaald" in err
        assert "HTTP 401" in err
        assert door.gh.calls_with("pr view") == []
        assert door.merge_kwargs == []


# ---------------------------------------------------------------------------
# How strictly: enforcement in the project's YAML
# ---------------------------------------------------------------------------

DRIFTED = github_protection(allow_force_pushes=True)

#: What GitHub answers for a branch that has no protection at all.
NOT_PROTECTED = {
    "match": "branches/main/protection", "rc": 1, "stderr": "gh: Branch not protected (HTTP 404)\n",
}


class TestEnforcement:
    def test_enforce_and_drift_is_no_go(self, arrange, capsys):
        door = arrange(main_yaml=consumer_yaml(enforcement="enforce"), live=DRIFTED)

        rc = door.run()

        assert rc == pr_merge.EXIT_ERROR
        assert "allow_force_pushes" in capsys.readouterr().err
        assert door.merge_kwargs == []

    def test_a_file_that_does_not_say_is_enforce(self, arrange, capsys):
        door = arrange(main_yaml=consumer_yaml(), live=DRIFTED)

        rc = door.run()

        assert rc == pr_merge.EXIT_ERROR
        assert "allow_force_pushes" in capsys.readouterr().err

    def test_enforce_and_no_drift_goes_through_with_an_unmarked_record(self, arrange):
        door = arrange(main_yaml=consumer_yaml(enforcement="enforce"))

        rc = door.run()

        record = door.protection_record()
        assert rc == pr_merge.EXIT_OK
        assert (record["verdict"], record["mode"], record["reason_code"]) == ("GO", "enforce", "")

    def test_warn_and_drift_goes_through_and_says_so_in_output_and_record(self, arrange, capsys):
        door = arrange(main_yaml=consumer_yaml(enforcement="warn"), live=DRIFTED)

        rc = door.run()
        out = capsys.readouterr()

        record = door.protection_record()
        assert rc == pr_merge.EXIT_OK
        assert "WARN:" in out.err and "allow_force_pushes" in out.err
        assert record["verdict"] == "GO"
        assert record["mode"] == "warn"
        assert record["reason_code"] == pr_merge.REASON_PROTECTION_WARNED
        assert "allow_force_pushes" in record["message"]

    def test_warn_and_a_repo_without_protection_goes_through_and_says_so(self, arrange, capsys):
        """The consumer case that started this: no protection on GitHub at all (404)."""
        door = arrange(main_yaml=consumer_yaml(enforcement="warn"), first=[NOT_PROTECTED])

        rc = door.run()
        out = capsys.readouterr()

        record = door.protection_record()
        assert rc == pr_merge.EXIT_OK
        assert "niet leesbaar" in record["message"]
        assert "Branch not protected" in out.err
        assert record["reason_code"] == pr_merge.REASON_PROTECTION_WARNED

    def test_enforce_and_a_repo_without_protection_is_no_go(self, arrange, capsys):
        door = arrange(main_yaml=consumer_yaml(enforcement="enforce"), first=[NOT_PROTECTED])

        rc = door.run()

        assert rc == pr_merge.EXIT_ERROR
        assert "niet leesbaar" in capsys.readouterr().err

    def test_off_does_not_look_and_the_record_says_it_was_off(self, arrange):
        door = arrange(main_yaml=consumer_yaml(enforcement="off"), live=DRIFTED)

        rc = door.run()

        record = door.protection_record()
        assert rc == pr_merge.EXIT_OK
        assert (record["verdict"], record["mode"]) == ("GO", "off")
        assert record["reason_code"] == pr_merge.REASON_PROTECTION_OFF
        assert door.gh.calls_with("branches/main/protection") == []
        assert door.gh.calls_with(f"contents/{MAIN_YAML_PATH}?ref={HEAD_SHA}") == []

    def test_no_file_at_all_is_a_loud_warn_go_naming_the_repo(self, arrange, capsys):
        door = arrange(main_yaml=None)

        rc = door.run()
        out = capsys.readouterr()

        record = door.protection_record()
        assert rc == pr_merge.EXIT_OK
        assert f"geen branch_protection.yaml in {CONSUMER_REPO}, niets te toetsen" in record["message"]
        assert f"geen branch_protection.yaml in {CONSUMER_REPO}, niets te toetsen" in out.err
        assert record["mode"] == "warn"
        assert record["reason_code"] == pr_merge.REASON_PROTECTION_FILE_MISSING
        assert door.gh.calls_with("branches/main/protection") == []
        asked = [c["argv"][1] for c in door.gh.calls_with("contents/") if "ref=main" in " ".join(c["argv"])]
        paths = [a.split("/contents/")[1].split("?")[0] for a in asked]
        # In reading order, once for the CI gate's workflow name and once for the protection gate.
        assert paths == [MAIN_YAML_PATH, FABRIC_STYLE_YAML_PATH] * 2

    def test_the_fabric_style_path_is_read_when_the_project_has_no_dot_vnx_copy(self, tmp_path, monkeypatch, capsys):
        rules = _rules(main_yaml=None, first=[{
            "match": f"contents/{FABRIC_STYLE_YAML_PATH}?ref=main", "repo": CONSUMER_REPO,
            "stdout": contents_response(consumer_yaml(enforcement="off")),
        }])
        door = Door(tmp_path, monkeypatch, rules)

        rc = door.run()

        assert rc == pr_merge.EXIT_OK
        assert door.protection_record()["mode"] == "off"

    def test_the_dot_vnx_copy_shadows_the_fabric_style_path(self, tmp_path, monkeypatch):
        rules = _rules(main_yaml=consumer_yaml(enforcement="off"), first=[{
            "match": f"contents/{FABRIC_STYLE_YAML_PATH}?ref=main", "repo": CONSUMER_REPO,
            "stdout": contents_response(consumer_yaml(enforcement="enforce")),
        }])
        door = Door(tmp_path, monkeypatch, rules)

        door.run()

        assert door.protection_record()["mode"] == "off"
        assert door.gh.calls_with(f"contents/{FABRIC_STYLE_YAML_PATH}?ref=main") == []


class TestLoweringEnforcementIsAWeakening:
    def test_enforce_to_warn_needs_allow_weaken(self, arrange, capsys):
        door = arrange(
            main_yaml=consumer_yaml(enforcement="enforce"),
            head_yaml=consumer_yaml(enforcement="warn"),
        )

        rc = door.run()

        err = capsys.readouterr().err
        assert rc == pr_merge.EXIT_ERROR
        assert "--allow-weaken" in err and "enforcement" in err

    def test_enforce_to_warn_goes_through_with_a_reason(self, arrange, capsys):
        door = arrange(
            main_yaml=consumer_yaml(enforcement="enforce"),
            head_yaml=consumer_yaml(enforcement="warn"),
        )

        rc = door.run("--allow-weaken", "consumer wil eerst meten")

        record = door.protection_record()
        assert rc == pr_merge.EXIT_OK
        assert record["overridden"] is True
        assert "consumer wil eerst meten" in record["message"]

    def test_warn_to_off_needs_allow_weaken_even_though_warn_lets_drift_through(self, arrange, capsys):
        door = arrange(
            main_yaml=consumer_yaml(enforcement="warn"),
            head_yaml=consumer_yaml(enforcement="off"),
        )

        rc = door.run()

        assert rc == pr_merge.EXIT_ERROR
        assert "enforcement" in capsys.readouterr().err

    def test_an_empty_reason_is_refused(self, arrange, capsys):
        door = arrange(
            main_yaml=consumer_yaml(enforcement="enforce"),
            head_yaml=consumer_yaml(enforcement="warn"),
        )

        rc = door.run("--allow-weaken", "  ")

        assert rc == pr_merge.EXIT_ERROR
        assert "niet-lege reden" in capsys.readouterr().err

    @pytest.mark.parametrize("main_mode,head_mode", [
        ("warn", "enforce"), ("off", "warn"), ("off", "enforce"), ("enforce", "enforce"),
    ])
    def test_raising_or_keeping_the_mode_is_not_a_weakening(self, arrange, main_mode, head_mode):
        door = arrange(
            main_yaml=consumer_yaml(enforcement=main_mode),
            head_yaml=consumer_yaml(enforcement=head_mode),
        )

        assert door.run() == pr_merge.EXIT_OK

    def test_dropping_the_field_resolves_to_enforce_and_is_not_a_weakening(self, arrange):
        """Absent means enforce, so removing the field can only keep or raise the mode."""
        door = arrange(
            main_yaml=consumer_yaml(enforcement="warn"),
            head_yaml=consumer_yaml(),
        )

        assert door.run() == pr_merge.EXIT_OK

    def test_warn_still_refuses_a_pr_that_drops_a_required_check(self, arrange, capsys):
        """``warn`` softens what the door does about DRIFT. It does not soften what a PR may do
        to the declaration."""
        two_checks = consumer_yaml(enforcement="warn").replace(
            '    - {context: "CI", app_id: 15368}\n',
            '    - {context: "CI", app_id: 15368}\n    - {context: "Lint", app_id: 15368}\n',
        )
        live_with_both = json.loads(github_protection())
        live_with_both["required_status_checks"]["checks"].append({"context": "Lint", "app_id": 15368})
        # main requires two checks and live has both, so there is no drift to warn about;
        # the PR head drops one.
        door = arrange(
            main_yaml=two_checks, head_yaml=consumer_yaml(enforcement="warn"),
            live=json.dumps(live_with_both),
        )

        rc = door.run()

        err = capsys.readouterr().err
        assert rc == pr_merge.EXIT_ERROR
        assert "required_status_checks.checks[Lint]" in err and "--allow-weaken" in err

    def test_deleting_the_file_is_refused_and_points_at_off(self, arrange, capsys):
        gone_at_head = [
            {
                "match": f"contents/{path}?ref={HEAD_SHA}", "repo": CONSUMER_REPO,
                "rc": 1, "stderr": "gh: Not Found (HTTP 404)\n",
            }
            for path in (MAIN_YAML_PATH, FABRIC_STYLE_YAML_PATH)
        ]
        door = arrange(main_yaml=consumer_yaml(enforcement="warn"), first=gone_at_head)

        rc = door.run()

        err = capsys.readouterr().err
        assert rc == pr_merge.EXIT_ERROR
        assert "verwijdert" in err and "enforcement: off" in err


# ---------------------------------------------------------------------------
# The CI workflow name is the project's, and the CI gate is nobody's to soften
# ---------------------------------------------------------------------------

def _workflow_asked(gh: GhStub) -> str:
    calls = gh.calls_with("run list")
    assert calls, "the CI gate never asked for a run"
    argv = calls[0]["argv"]
    return argv[argv.index("--workflow") + 1]


class TestCiWorkflow:
    def test_the_project_names_its_workflow(self, arrange, monkeypatch):
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        door = arrange(main_yaml=consumer_yaml(ci_workflow="CI/CD Pipeline"))

        assert door.run() == pr_merge.EXIT_OK
        assert _workflow_asked(door.gh) == "CI/CD Pipeline"

    def test_the_project_outranks_the_environment(self, arrange, monkeypatch):
        monkeypatch.setenv("VNX_CI_WORKFLOW_NAME", "Env Flow")
        door = arrange(main_yaml=consumer_yaml(ci_workflow="CI"))

        door.run()

        assert _workflow_asked(door.gh) == "CI"

    def test_a_project_that_names_none_falls_to_the_environment(self, arrange, monkeypatch):
        monkeypatch.setenv("VNX_CI_WORKFLOW_NAME", "Env Flow")
        door = arrange(main_yaml=consumer_yaml())

        door.run()

        assert _workflow_asked(door.gh) == "Env Flow"

    def test_a_project_without_a_file_falls_to_the_environment_then_the_default(self, arrange, monkeypatch):
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        door = arrange(main_yaml=None)

        door.run()

        assert _workflow_asked(door.gh) == "VNX CI"

    def test_a_file_that_cannot_be_read_stops_the_ci_gate_instead_of_guessing_a_name(self, arrange, capsys):
        door = arrange(main_yaml=consumer_yaml(ci_workflow="CI"), first=[{
            "match": f"contents/{MAIN_YAML_PATH}?ref=main", "repo": CONSUMER_REPO,
            "rc": 1, "stderr": "gh: Server Error (HTTP 500)\n",
        }])

        rc = door.run()

        assert rc == pr_merge.EXIT_ERROR
        assert "CI-workflow van het project kon niet worden bepaald" in capsys.readouterr().err
        assert door.gh.calls_with("run list") == []

    def test_a_red_ci_run_stops_the_merge_whatever_the_enforcement(self, arrange, capsys):
        door = arrange(main_yaml=consumer_yaml(enforcement="off", ci_workflow="CI"), ci_conclusion="failure")

        rc = door.run()

        assert rc == pr_merge.EXIT_ERROR
        assert "failure" in capsys.readouterr().err
        assert door.merge_kwargs == []


# ---------------------------------------------------------------------------
# The door proves itself against the fabric, never against the consumer
# ---------------------------------------------------------------------------

def _door_files(install: Path) -> Dict[str, str]:
    """Write every door file into ``install`` and return ``{path: git blob hash}``."""
    hashes: Dict[str, str] = {}
    for path in pr_merge._DOOR_INTEGRITY_PATHS:
        target = install / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {path}\n", encoding="utf-8")
        out = subprocess.run(
            ["git", "hash-object", path], cwd=str(install), capture_output=True, text=True, check=True,
        )
        hashes[path] = out.stdout.strip()
    return hashes


def _door_rules(hashes: Dict[str, str], ref: str, *, repo: str = FABRIC_REPO) -> List[Dict[str, Any]]:
    return [
        {"match": f"contents/{path}?ref={ref}", "repo": repo, "stdout": sha + "\n"}
        for path, sha in hashes.items()
    ]


class TestDoorIntegrityFromAnInstall:
    def test_an_install_is_compared_to_its_own_release_in_the_fabric_repo(self, tmp_path, monkeypatch):
        install = make_install(tmp_path, version="1.6.3")
        hashes = _door_files(install)
        stub = install_gh_stub(tmp_path, monkeypatch, _door_rules(hashes, "v1.6.3"))

        result = _REAL_DOOR_BLOB_HASH_GATE(install)

        assert result["verdict"] == "GO", result
        assert "release v1.6.3" in result["message"]
        calls = stub.calls_with("contents/")
        assert len(calls) == len(pr_merge._DOOR_INTEGRITY_PATHS)
        assert {c["repo"] for c in calls} == {FABRIC_REPO}
        assert all(c["argv"][1].endswith("?ref=v1.6.3") for c in calls)
        assert stub.calls_with("ref=main") == []

    def test_an_install_that_differs_from_its_release_is_refused_naming_the_file(self, tmp_path, monkeypatch):
        install = make_install(tmp_path, version="1.6.3")
        hashes = _door_files(install)
        (install / "scripts/lib/forge_protection_drift.py").write_text("# edited\n", encoding="utf-8")
        install_gh_stub(tmp_path, monkeypatch, _door_rules(hashes, "v1.6.3"))

        result = _REAL_DOOR_BLOB_HASH_GATE(install)

        assert result["verdict"] == "NO-GO"
        assert "scripts/lib/forge_protection_drift.py" in result["message"]
        assert "v1.6.3" in result["message"]

    def test_an_install_whose_tag_is_not_in_the_fabric_repo_is_refused(self, tmp_path, monkeypatch):
        install = make_install(tmp_path, version="9.9.9")
        _door_files(install)
        install_gh_stub(tmp_path, monkeypatch, [
            {"match": "contents/", "rc": 1, "stderr": "gh: No commit found for the ref v9.9.9 (HTTP 404)\n"},
        ])

        result = _REAL_DOOR_BLOB_HASH_GATE(install)

        assert result["verdict"] == "NO-GO"
        assert "v9.9.9" in result["message"]

    def test_a_checkout_is_still_compared_to_main(self, tmp_path, monkeypatch):
        checkout = make_install(tmp_path, central=False)
        hashes = _door_files(checkout)
        stub = install_gh_stub(tmp_path, monkeypatch, _door_rules(hashes, "main"))

        result = _REAL_DOOR_BLOB_HASH_GATE(checkout)

        assert result["verdict"] == "GO", result
        assert "main" in result["message"]
        assert all(c["argv"][1].endswith("?ref=main") for c in stub.calls_with("contents/"))

    def test_the_door_asks_the_fabric_while_everything_else_asks_the_consumer(self, tmp_path, monkeypatch, capsys):
        """One merge, both repos: the running door (an install of fabric/x) is proven against
        fabric/x's release, the merge itself is judged against consumer/y."""
        install = make_install(tmp_path, version="1.6.3")
        hashes = _door_files(install)
        rules = _door_rules(hashes, "v1.6.3") + _rules(main_yaml=consumer_yaml(enforcement="enforce"))
        door = Door(tmp_path, monkeypatch, rules, install=install)
        monkeypatch.setattr(pr_merge, "ENGINE_ROOT", install, raising=False)
        monkeypatch.setattr(pr_merge, "_door_blob_hash_gate", _REAL_DOOR_BLOB_HASH_GATE)

        rc = door.run()

        assert rc == pr_merge.EXIT_OK, capsys.readouterr().err
        assert door.gh.repos_of("?ref=v1.6.3") == {FABRIC_REPO}
        assert door.gh.repos_of(f"contents/{MAIN_YAML_PATH}") == {CONSUMER_REPO}
        assert door.gh.repos_of("branches/main/protection") == {CONSUMER_REPO}
