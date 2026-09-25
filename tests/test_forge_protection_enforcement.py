#!/usr/bin/env python3
"""The reader half of OI-1849: ``enforcement`` and ``ci_workflow`` in
``branch_protection.yaml``, the reading order of the file, and lowering the
enforcement as a weakening.

Reader only. Per the two-PR contract (docs/operations/FORGE_GATE.md, OI-1672) the
fields exist in the schema before any YAML carries them, and the fabric's own
``scripts/forge/branch_protection.yaml`` is deliberately left without them here:
without the field it means ``enforce``, which is what it has always been.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import forge_protection_drift as fpd
from merge_target_helpers import (
    CONSUMER_REPO,
    HEAD_SHA,
    MAIN_SHA,
    consumer_yaml,
    contents_response,
    install_gh_stub,
    make_consumer,
)

SHIPPED_YAML = VNX_ROOT / "scripts" / "forge" / "branch_protection.yaml"


def _norm(**kwargs):
    return fpd.to_normalized_dict(fpd.parse_protection_config(consumer_yaml(**kwargs)))


class TestParsingEnforcement:
    def test_a_file_that_does_not_say_is_enforce(self):
        config = fpd.parse_protection_config(consumer_yaml())
        assert config.enforcement == "enforce"
        assert config.ci_workflow is None

    @pytest.mark.parametrize("mode", ["enforce", "warn", "off"])
    def test_each_mode_is_read(self, mode):
        assert fpd.parse_protection_config(consumer_yaml(enforcement=mode)).enforcement == mode

    def test_the_bare_word_off_is_read_as_off(self):
        """PyYAML reads a bare ``off`` as the boolean False. The documented value must work as
        documented, so that boolean is ``off``, and nothing else is guessed at."""
        assert "enforcement: off" in consumer_yaml(enforcement="off")
        assert fpd.parse_protection_config(consumer_yaml(enforcement="false")).enforcement == "off"

    def test_on_names_no_mode_and_says_which_modes_exist(self):
        with pytest.raises(fpd.ProtectionConfigError, match="enforce, warn of off"):
            fpd.parse_protection_config(consumer_yaml(enforcement="on"))

    @pytest.mark.parametrize("value", ["strict", "Enforce", "\"\"", "3"])
    def test_anything_else_is_refused(self, value):
        with pytest.raises(fpd.ProtectionConfigError, match="enforcement moet een van"):
            fpd.parse_protection_config(consumer_yaml(enforcement=value))

    def test_a_misspelled_field_is_still_an_unknown_field(self):
        text = consumer_yaml() + "enforcment: warn\n"
        with pytest.raises(fpd.ProtectionConfigError, match="onbekende velden"):
            fpd.parse_protection_config(text)

    def test_the_shipped_fabric_yaml_still_parses_and_means_enforce(self):
        config = fpd.load_protection_config(SHIPPED_YAML)
        assert config.enforcement == "enforce"
        assert config.ci_workflow is None


class TestParsingCiWorkflow:
    def test_the_name_is_read_and_trimmed(self):
        assert fpd.parse_protection_config(consumer_yaml(ci_workflow=" CI/CD Pipeline ")).ci_workflow == "CI/CD Pipeline"

    @pytest.mark.parametrize("value", ['""', '"   "'])
    def test_an_empty_name_is_refused(self, value):
        text = consumer_yaml() + f"ci_workflow: {value}\n"
        with pytest.raises(fpd.ProtectionConfigError, match="ci_workflow"):
            fpd.parse_protection_config(text)

    @pytest.mark.parametrize("value", ["7", "[CI]", "true"])
    def test_a_name_that_is_not_a_string_is_refused(self, value):
        text = consumer_yaml() + f"ci_workflow: {value}\n"
        with pytest.raises(fpd.ProtectionConfigError, match="ci_workflow"):
            fpd.parse_protection_config(text)


class TestEnforcementIsNotProtectionState:
    """``enforcement`` and ``ci_workflow`` are policy about the door. GitHub does not hold
    them, so they can never be drift."""

    def test_the_normalized_dict_carries_the_resolved_value(self):
        assert _norm()["enforcement"] == "enforce"
        assert _norm(enforcement="warn")["enforcement"] == "warn"

    def test_compare_never_reports_it(self):
        assert fpd.compare(_norm(enforcement="enforce"), _norm(enforcement="off")) == []

    def test_a_live_state_without_the_key_is_not_drift(self):
        live = {k: v for k, v in _norm().items() if k not in ("enforcement", "ci_workflow")}
        assert fpd.compare(_norm(enforcement="warn", ci_workflow="CI"), live) == []
        assert fpd.compare(live, _norm(enforcement="warn", ci_workflow="CI")) == []

    def test_the_normalized_dict_carries_the_declared_workflow(self):
        assert _norm()["ci_workflow"] is None
        assert _norm(ci_workflow="CI")["ci_workflow"] == "CI"

    def test_compare_never_reports_the_workflow(self):
        assert fpd.compare(_norm(), _norm(ci_workflow="CI")) == []
        assert fpd.compare(_norm(ci_workflow="CI"), _norm(ci_workflow="Always Green")) == []


class TestLoweringIsAWeakening:
    @pytest.mark.parametrize("old,new", [
        ("enforce", "warn"), ("warn", "off"), ("enforce", "off"),
    ])
    def test_a_lower_mode_is_a_weakening(self, old, new):
        weak, fields = fpd.is_weakening(_norm(enforcement=old), _norm(enforcement=new))
        assert weak is True
        assert fields == ["enforcement"]

    @pytest.mark.parametrize("old,new", [
        ("off", "warn"), ("warn", "enforce"), ("off", "enforce"),
        ("enforce", "enforce"), ("warn", "warn"), ("off", "off"),
    ])
    def test_the_same_or_a_higher_mode_is_not(self, old, new):
        assert fpd.is_weakening(_norm(enforcement=old), _norm(enforcement=new)) == (False, [])

    def test_removing_the_field_resolves_to_enforce_and_lowers_nothing(self):
        """Absent means ``enforce``: taking the field out can only keep or raise the mode,
        whichever explicit value main had."""
        for old in ("enforce", "warn", "off"):
            assert fpd.is_weakening(_norm(enforcement=old), _norm()) == (False, [])

    def test_it_is_judged_next_to_the_protection_fields(self):
        old = _norm(enforcement="enforce")
        new = _norm(enforcement="warn")
        new["allow_force_pushes"] = True
        weak, fields = fpd.is_weakening(old, new)
        assert weak is True
        assert set(fields) == {"allow_force_pushes", "enforcement"}

    def test_a_live_state_has_no_policy_to_lower(self):
        """apply_branch_protection asks is_weakening(live, yaml): live carries no key."""
        live = {k: v for k, v in _norm().items() if k not in ("enforcement", "ci_workflow")}
        assert fpd.is_weakening(live, _norm(enforcement="off")) == (False, [])
        assert fpd.is_weakening(live, _norm(ci_workflow="Always Green")) == (False, [])


class TestChangingTheCiWorkflowIsAWeakening:
    """The CI gate asks for the workflow main declares. A PR that edits the declaration picks
    the workflow every later PR is judged on, so changing, adding and removing it all move the
    gate, and none of them is a strengthening the door can tell apart from a weakening."""

    @pytest.mark.parametrize("old,new", [
        ("CI", "Always Green"),
        (None, "Always Green"),
        ("CI", None),
    ])
    def test_a_changed_added_or_removed_workflow_is_a_weakening(self, old, new):
        weak, fields = fpd.is_weakening(_norm(ci_workflow=old), _norm(ci_workflow=new))

        assert weak is True
        assert len(fields) == 1 and fields[0].startswith("ci_workflow")

    @pytest.mark.parametrize("old,new,shown_old,shown_new", [
        ("CI", "Always Green", "'CI'", "'Always Green'"),
        (None, "Always Green", "niet gedeclareerd", "'Always Green'"),
        ("CI", None, "'CI'", "niet gedeclareerd"),
    ])
    def test_the_message_names_the_field_and_both_values(self, old, new, shown_old, shown_new):
        _, fields = fpd.is_weakening(_norm(ci_workflow=old), _norm(ci_workflow=new))

        assert fields == [f"ci_workflow: {shown_old} -> {shown_new}"]

    @pytest.mark.parametrize("value", [None, "CI", "CI/CD Pipeline"])
    def test_an_unchanged_workflow_is_not(self, value):
        assert fpd.is_weakening(_norm(ci_workflow=value), _norm(ci_workflow=value)) == (False, [])

    def test_a_stray_space_is_not_a_change(self):
        """The parser trims, so the two sides compare as the same name."""
        assert fpd.is_weakening(_norm(ci_workflow="CI"), _norm(ci_workflow="  CI  ")) == (False, [])

    def test_it_is_judged_next_to_the_other_fields(self):
        old = _norm(enforcement="enforce", ci_workflow="CI")
        new = _norm(enforcement="warn", ci_workflow="Always Green")
        weak, fields = fpd.is_weakening(old, new)

        assert weak is True
        assert len(fields) == 2
        assert "enforcement" in fields
        assert any(f.startswith("ci_workflow") for f in fields)


class TestReadingOrder:
    def test_no_file_is_none(self, tmp_path):
        assert fpd.find_local_protection_yaml(tmp_path) is None

    def test_the_fabric_style_path_is_found(self, tmp_path):
        path = tmp_path / "scripts" / "forge" / "branch_protection.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(consumer_yaml(), encoding="utf-8")
        assert fpd.find_local_protection_yaml(tmp_path) == path

    def test_the_dot_vnx_copy_is_found(self, tmp_path):
        path = tmp_path / ".vnx" / "branch_protection.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(consumer_yaml(), encoding="utf-8")
        assert fpd.find_local_protection_yaml(tmp_path) == path

    def test_the_dot_vnx_copy_shadows_the_fabric_style_path(self, tmp_path):
        for relative in fpd.PROTECTION_YAML_SEARCH_PATHS:
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(consumer_yaml(), encoding="utf-8")
        assert fpd.find_local_protection_yaml(tmp_path) == tmp_path / ".vnx" / "branch_protection.yaml"

    def test_the_order_is_the_project_copy_then_the_fabric_path(self):
        assert fpd.PROTECTION_YAML_SEARCH_PATHS == (
            ".vnx/branch_protection.yaml", "scripts/forge/branch_protection.yaml",
        )
        assert fpd.PROTECTION_YAML_RELATIVE_PATH == fpd.PROTECTION_YAML_SEARCH_PATHS[-1]

    def test_a_missing_file_is_treated_as_warn_by_the_door(self):
        assert fpd.MISSING_FILE_ENFORCEMENT == "warn"
        assert fpd.DEFAULT_ENFORCEMENT == "enforce"


PROJECT_PATH = ".vnx/branch_protection.yaml"
FABRIC_PATH = "scripts/forge/branch_protection.yaml"


def _found(repo, path, text, ref="main"):
    return {"match": f"contents/{path}?ref={ref}", "repo": repo, "stdout": contents_response(text)}


def _missing(repo, path, ref="main"):
    return {"match": f"contents/{path}?ref={ref}", "repo": repo, "rc": 1, "stderr": "gh: Not Found (HTTP 404)\n"}


def _confirmed_main(repo):
    return {"match": "commits/main", "repo": repo, "stdout": MAIN_SHA + "\n"}


class TestFetchCiWorkflowFromMain:
    """The one reader of a project's declared workflow: main's copy over the contents API, for
    the merge door and for every gate that runs beside a checkout (``pre_merge_gate``, the
    ``merge_preflight_ci_check`` CLI). Never the checkout, which can be on the PR's branch."""

    @pytest.fixture()
    def project(self, tmp_path):
        return make_consumer(tmp_path)

    def _read(self, project, tmp_path, monkeypatch, rules):
        stub = install_gh_stub(tmp_path, monkeypatch, rules)
        return fpd.fetch_ci_workflow_from_main(project), stub

    def test_the_declared_name_is_returned(self, project, tmp_path, monkeypatch):
        got, stub = self._read(project, tmp_path, monkeypatch, [_found(CONSUMER_REPO, PROJECT_PATH, consumer_yaml(ci_workflow="CI"))])

        assert got == ("CI", "")
        assert stub.repos_of("contents/") == {CONSUMER_REPO}

    def test_a_file_that_does_not_say_declares_none(self, project, tmp_path, monkeypatch):
        got, _ = self._read(project, tmp_path, monkeypatch, [_found(CONSUMER_REPO, PROJECT_PATH, consumer_yaml())])

        assert got == (None, "")

    def test_no_file_on_main_declares_none(self, project, tmp_path, monkeypatch):
        rules = [_missing(CONSUMER_REPO, PROJECT_PATH), _missing(CONSUMER_REPO, FABRIC_PATH), _confirmed_main(CONSUMER_REPO)]

        got, _ = self._read(project, tmp_path, monkeypatch, rules)

        assert got == (None, "")

    def test_the_fabric_style_path_is_read_when_there_is_no_project_copy(self, project, tmp_path, monkeypatch):
        rules = [
            _missing(CONSUMER_REPO, PROJECT_PATH), _confirmed_main(CONSUMER_REPO),
            _found(CONSUMER_REPO, FABRIC_PATH, consumer_yaml(ci_workflow="CI/CD Pipeline")),
        ]

        got, _ = self._read(project, tmp_path, monkeypatch, rules)

        assert got == ("CI/CD Pipeline", "")

    def test_the_project_copy_shadows_the_fabric_style_path(self, project, tmp_path, monkeypatch):
        rules = [
            _found(CONSUMER_REPO, PROJECT_PATH, consumer_yaml(ci_workflow="CI")),
            _found(CONSUMER_REPO, FABRIC_PATH, consumer_yaml(ci_workflow="Other")),
        ]

        got, stub = self._read(project, tmp_path, monkeypatch, rules)

        assert got == ("CI", "")
        assert stub.calls_with(f"contents/{FABRIC_PATH}") == []

    def test_a_broken_file_is_an_error_not_a_guess(self, project, tmp_path, monkeypatch):
        rules = [_found(CONSUMER_REPO, PROJECT_PATH, "branch: main\nnot_a_field: 1\n")]

        name, error = self._read(project, tmp_path, monkeypatch, rules)[0]

        assert name is None
        assert PROJECT_PATH in error and "op main ongeldig" in error

    def test_a_file_that_cannot_be_read_is_an_error_and_stops_the_walk(self, project, tmp_path, monkeypatch):
        rules = [
            {"match": f"contents/{PROJECT_PATH}?ref=main", "repo": CONSUMER_REPO, "rc": 1, "stderr": "gh: Server Error (HTTP 500)\n"},
            _found(CONSUMER_REPO, FABRIC_PATH, consumer_yaml(ci_workflow="CI")),
        ]

        (name, error), stub = self._read(project, tmp_path, monkeypatch, rules)

        assert name is None
        assert "op main niet leesbaar" in error and "HTTP 500" in error
        assert stub.calls_with(f"contents/{FABRIC_PATH}") == []

    def test_a_404_on_a_ref_that_cannot_be_confirmed_is_an_error_not_an_absent_file(self, project, tmp_path, monkeypatch):
        """``gh`` words a missing path and an unresolvable repo alike, so a 404 alone is not
        "the project declares nothing"."""
        rules = [_missing(CONSUMER_REPO, PROJECT_PATH), _missing(CONSUMER_REPO, FABRIC_PATH)]

        name, error = self._read(project, tmp_path, monkeypatch, rules)[0]

        assert name is None
        assert "niet bevestigd" in error

    def test_a_fetch_of_its_own_replaces_the_read_of_each_path(self, project):
        seen = []

        def fetch(root, ref, path):
            seen.append((root, ref, path))
            return fpd.YamlFetchResult(text=consumer_yaml(ci_workflow="Injected"), not_found=False, error=None)

        assert fpd.fetch_ci_workflow_from_main(project, fetch=fetch) == ("Injected", "")
        assert seen == [(project, "main", PROJECT_PATH)]


class TestFetchProjectYamlFromRef:
    def test_it_walks_the_reading_order_at_the_ref_it_is_given(self, tmp_path, monkeypatch):
        project = make_consumer(tmp_path)
        rules = [
            _missing(CONSUMER_REPO, PROJECT_PATH, ref=HEAD_SHA), {"match": f"commits/{HEAD_SHA}", "repo": CONSUMER_REPO, "stdout": HEAD_SHA + "\n"},
            _found(CONSUMER_REPO, FABRIC_PATH, consumer_yaml(), ref=HEAD_SHA),
        ]
        install_gh_stub(tmp_path, monkeypatch, rules)

        path, fetched = fpd.fetch_project_yaml_from_ref(project, HEAD_SHA)

        assert path == FABRIC_PATH
        assert fetched.text == consumer_yaml()

    def test_every_path_missing_is_a_not_found_with_no_path(self, tmp_path, monkeypatch):
        project = make_consumer(tmp_path)
        install_gh_stub(tmp_path, monkeypatch, [
            _missing(CONSUMER_REPO, PROJECT_PATH), _missing(CONSUMER_REPO, FABRIC_PATH), _confirmed_main(CONSUMER_REPO),
        ])

        path, fetched = fpd.fetch_project_yaml_from_ref(project, "main")

        assert path == ""
        assert fetched.not_found is True and fetched.error is None


class TestDriftCliDefaults:
    """``python3 scripts/lib/forge_protection_drift.py`` reads the same file the door does."""

    def test_no_file_in_the_project_is_an_error_naming_where_it_looked(self, tmp_path, capsys):
        rc = fpd.main(["--project-root", str(tmp_path)])

        assert rc == 1
        err = capsys.readouterr().err
        assert ".vnx/branch_protection.yaml" in err and "scripts/forge/branch_protection.yaml" in err

    def test_the_projects_own_file_is_compared_to_live_state(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / ".vnx" / "branch_protection.yaml"
        path.parent.mkdir()
        path.write_text(consumer_yaml(enforcement="warn"), encoding="utf-8")
        seen = []

        def fake_live(project_root, **kwargs):
            seen.append(Path(project_root))
            return _norm()

        monkeypatch.setattr(fpd, "fetch_live_protection", fake_live)

        rc = fpd.main(["--project-root", str(tmp_path)])

        assert rc == 0
        assert seen == [tmp_path]
        assert str(path) in capsys.readouterr().out
