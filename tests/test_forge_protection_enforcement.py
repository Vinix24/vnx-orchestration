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
from merge_target_helpers import consumer_yaml

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
    """It is a policy about the door. GitHub does not hold it, so it can never be drift."""

    def test_the_normalized_dict_carries_the_resolved_value(self):
        assert _norm()["enforcement"] == "enforce"
        assert _norm(enforcement="warn")["enforcement"] == "warn"

    def test_compare_never_reports_it(self):
        assert fpd.compare(_norm(enforcement="enforce"), _norm(enforcement="off")) == []

    def test_a_live_state_without_the_key_is_not_drift(self):
        live = {k: v for k, v in _norm().items() if k != "enforcement"}
        assert fpd.compare(_norm(enforcement="warn"), live) == []
        assert fpd.compare(live, _norm(enforcement="warn")) == []

    def test_ci_workflow_never_enters_the_normalized_dict(self):
        assert "ci_workflow" not in _norm(ci_workflow="CI")
        assert fpd.compare(_norm(), _norm(ci_workflow="CI")) == []


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
        live = {k: v for k, v in _norm().items() if k != "enforcement"}
        assert fpd.is_weakening(live, _norm(enforcement="off")) == (False, [])


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


class TestLoadLocalCiWorkflow:
    def _write(self, root, text):
        path = root / ".vnx" / "branch_protection.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_no_file_declares_none(self, tmp_path):
        assert fpd.load_local_ci_workflow(tmp_path) is None

    def test_a_file_without_the_field_declares_none(self, tmp_path):
        self._write(tmp_path, consumer_yaml())
        assert fpd.load_local_ci_workflow(tmp_path) is None

    def test_the_declared_name_is_returned(self, tmp_path):
        self._write(tmp_path, consumer_yaml(ci_workflow="CI"))
        assert fpd.load_local_ci_workflow(tmp_path) == "CI"

    def test_a_broken_file_is_an_error_not_a_guess(self, tmp_path):
        self._write(tmp_path, "branch: main\nnot_a_field: 1\n")
        with pytest.raises(fpd.ProtectionConfigError):
            fpd.load_local_ci_workflow(tmp_path)


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
