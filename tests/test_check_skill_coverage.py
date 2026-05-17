"""Tests for scripts/check_skill_coverage.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure scripts/ is importable
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import logging

from check_skill_coverage import (
    _read_text,
    compute_missing,
    format_report,
    list_available_skills,
    main,
    scan_skill_references,
)


class TestScanSkillReferences:
    def test_empty_project_no_refs(self, tmp_path: Path) -> None:
        """A project with no dispatches, no YAML roles, and no skills dir has zero refs."""
        refs, skipped = scan_skill_references(tmp_path)
        assert refs == set()
        assert skipped == []

    def test_detects_role_in_vnx_yaml(self, tmp_path: Path) -> None:
        """Role declarations inside .vnx/*.yaml are picked up."""
        vnx = tmp_path / ".vnx"
        vnx.mkdir()
        (vnx / "workers.yaml").write_text("workers:\n  - role: backend-developer\n")
        refs, _ = scan_skill_references(tmp_path)
        assert "backend-developer" in refs

    def test_detects_role_in_dispatches(self, tmp_path: Path) -> None:
        """Role headers in .vnx-data/dispatches/*.md are picked up."""
        dispatches = tmp_path / ".vnx-data" / "dispatches"
        dispatches.mkdir(parents=True)
        (dispatches / "d1.md").write_text("# Dispatch\n\nRole: planner\n")
        refs, _ = scan_skill_references(tmp_path)
        assert "planner" in refs

    def test_detects_local_skills_dir(self, tmp_path: Path) -> None:
        """Skill names from the local skills/ directory are treated as refs."""
        skills = tmp_path / "skills"
        skills.mkdir()
        (skills / "test-engineer").mkdir()
        refs, _ = scan_skill_references(tmp_path)
        assert "test-engineer" in refs

    def test_malformed_skills_yaml_reported_not_silenced(self, tmp_path: Path) -> None:
        """Malformed skills.yaml is reported in skipped, scan continues without raising."""
        skills = tmp_path / "skills"
        skills.mkdir()
        (skills / "skills.yaml").write_text(":\t bad yaml: [\n")
        refs, skipped = scan_skill_references(tmp_path)
        assert len(skipped) == 1
        assert "skills.yaml" in skipped[0]["path"]
        assert "Error" in skipped[0]["error"]


class TestListAvailableSkills:
    def test_central_skills_only(self, tmp_path: Path) -> None:
        """Listing from a central skills dir returns all contained skills."""
        central = tmp_path / "central" / "skills"
        central.mkdir(parents=True)
        (central / "planner").mkdir()
        (central / "architect").mkdir()
        avail, skipped = list_available_skills(central, None)
        assert set(avail.keys()) == {"planner", "architect"}
        assert skipped == []

    def test_overrides_shadow_central(self, tmp_path: Path) -> None:
        """Overrides with the same name shadow central entries."""
        central = tmp_path / "central" / "skills"
        central.mkdir(parents=True)
        (central / "planner").mkdir()
        overrides = tmp_path / ".vnx-overrides" / "skills"
        overrides.mkdir(parents=True)
        (overrides / "planner").mkdir()
        avail, _ = list_available_skills(central, overrides)
        assert avail["planner"] == overrides / "planner"

    def test_malformed_central_skills_yaml_reported(self, tmp_path: Path) -> None:
        """Malformed skills.yaml in central dir is reported in skipped, not silently ignored."""
        central = tmp_path / "central" / "skills"
        central.mkdir(parents=True)
        (central / "skills.yaml").write_text(":\t bad yaml: [\n")
        avail, skipped = list_available_skills(central, None)
        assert len(skipped) == 1
        assert "Error" in skipped[0]["error"]

    def test_malformed_overrides_skills_yaml_reported(self, tmp_path: Path) -> None:
        """Malformed skills.yaml in overrides dir is reported in skipped."""
        central = tmp_path / "central" / "skills"
        central.mkdir(parents=True)
        overrides = tmp_path / ".vnx-overrides" / "skills"
        overrides.mkdir(parents=True)
        (overrides / "skills.yaml").write_text(":\t bad yaml: [\n")
        avail, skipped = list_available_skills(central, overrides)
        assert len(skipped) == 1
        assert "Error" in skipped[0]["error"]


class TestComputeMissing:
    def test_all_covered(self) -> None:
        refs = {"planner", "architect"}
        available = {"planner": Path("x"), "architect": Path("y")}
        assert compute_missing(refs, available) == set()

    def test_some_missing(self) -> None:
        refs = {"planner", "unknown-skill"}
        available = {"planner": Path("x")}
        assert compute_missing(refs, available) == {"unknown-skill"}


class TestFormatReport:
    def test_human_report_all_covered(self) -> None:
        text = format_report({"planner"}, {"planner": Path("x")}, set(), False)
        assert "skills referenced: 1" in text
        assert "All referenced skills are covered." in text

    def test_human_report_missing(self) -> None:
        text = format_report(
            {"planner", "missing-one"},
            {"planner": Path("x")},
            {"missing-one"},
            False,
        )
        assert "MISSING: missing-one" in text

    def test_human_report_shows_skipped(self) -> None:
        skipped = [{"path": "/skills/foo.yaml", "error": "YAMLError: bad mapping"}]
        text = format_report({"planner"}, {"planner": Path("x")}, set(), False, skipped)
        assert "SKIPPED (errors): 1" in text
        assert "foo.yaml" in text

    def test_json_structure(self) -> None:
        data = json.loads(
            format_report(
                {"planner"}, {"planner": Path("x")}, set(), True
            )
        )
        assert data["referenced_count"] == 1
        assert data["available_count"] == 1
        assert data["missing_count"] == 0
        assert data["covered"] is True
        assert data["referenced"] == ["planner"]
        assert data["skipped"] == []

    def test_json_structure_includes_skipped(self) -> None:
        skipped = [{"path": "x.yaml", "error": "YAMLError: boom"}]
        data = json.loads(
            format_report({"planner"}, {"planner": Path("x")}, set(), True, skipped)
        )
        assert len(data["skipped"]) == 1
        assert data["skipped"][0]["error"] == "YAMLError: boom"


class TestMain:
    def test_exit_zero_when_all_covered(self, tmp_path: Path) -> None:
        """Exit 0 when every referenced skill is available centrally."""
        central = tmp_path / "skills"
        central.mkdir()
        (central / "backend-developer").mkdir()
        vnx = tmp_path / ".vnx"
        vnx.mkdir()
        (vnx / "workers.yaml").write_text("workers:\n  - role: backend-developer\n")
        code = main(["--project-root", str(tmp_path), "--central-skills", str(central)])
        assert code == 0

    def test_exit_one_when_missing(self, tmp_path: Path) -> None:
        """Exit 1 when a referenced skill is missing from central+overrides."""
        central = tmp_path / "skills"
        central.mkdir()
        vnx = tmp_path / ".vnx"
        vnx.mkdir()
        (vnx / "workers.yaml").write_text("workers:\n  - role: missing-skill\n")
        code = main(["--project-root", str(tmp_path), "--central-skills", str(central)])
        assert code == 1

    def test_overrides_resolve_missing(self, tmp_path: Path) -> None:
        """A missing central skill resolved by an override yields exit 0."""
        central = tmp_path / "skills"
        central.mkdir()
        overrides = tmp_path / ".vnx-overrides" / "skills"
        overrides.mkdir(parents=True)
        (overrides / "missing-skill").mkdir()
        vnx = tmp_path / ".vnx"
        vnx.mkdir()
        (vnx / "workers.yaml").write_text("workers:\n  - role: missing-skill\n")
        code = main(
            [
                "--project-root",
                str(tmp_path),
                "--central-skills",
                str(central),
                "--overrides",
                str(overrides),
            ]
        )
        assert code == 0

    def test_json_output_flag(self, tmp_path: Path, capsys) -> None:
        """--json produces valid JSON on stdout with skipped field."""
        central = tmp_path / "skills"
        central.mkdir()
        (central / "planner").mkdir()
        vnx = tmp_path / ".vnx"
        vnx.mkdir()
        (vnx / "workers.yaml").write_text("workers:\n  - role: planner\n")
        main(["--project-root", str(tmp_path), "--central-skills", str(central), "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "referenced" in data
        assert "missing" in data
        assert "covered" in data
        assert "skipped" in data


class TestReadText:
    def test_unreadable_file_returns_none_and_logs_warning(self, tmp_path: Path, caplog) -> None:
        """_read_text returns None and emits a warning when the file cannot be read."""
        missing = tmp_path / "does_not_exist.txt"
        with caplog.at_level(logging.WARNING, logger="check_skill_coverage"):
            result = _read_text(missing)
        assert result is None
        assert len(caplog.records) == 1
        assert "cannot read" in caplog.records[0].message
        assert "does_not_exist.txt" in caplog.records[0].message

    def test_readable_file_returns_content(self, tmp_path: Path) -> None:
        """_read_text returns file contents on success."""
        f = tmp_path / "data.txt"
        f.write_text("hello")
        assert _read_text(f) == "hello"
