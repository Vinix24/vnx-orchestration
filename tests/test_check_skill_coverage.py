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

from check_skill_coverage import (
    compute_missing,
    format_report,
    list_available_skills,
    main,
    scan_skill_references,
)


class TestScanSkillReferences:
    def test_empty_project_no_refs(self, tmp_path: Path) -> None:
        """A project with no dispatches, no YAML roles, and no skills dir has zero refs."""
        refs = scan_skill_references(tmp_path)
        assert refs == set()

    def test_detects_role_in_vnx_yaml(self, tmp_path: Path) -> None:
        """Role declarations inside .vnx/*.yaml are picked up."""
        vnx = tmp_path / ".vnx"
        vnx.mkdir()
        (vnx / "workers.yaml").write_text("workers:\n  - role: backend-developer\n")
        refs = scan_skill_references(tmp_path)
        assert "backend-developer" in refs

    def test_detects_role_in_dispatches(self, tmp_path: Path) -> None:
        """Role headers in .vnx-data/dispatches/*.md are picked up."""
        dispatches = tmp_path / ".vnx-data" / "dispatches"
        dispatches.mkdir(parents=True)
        (dispatches / "d1.md").write_text("# Dispatch\n\nRole: planner\n")
        refs = scan_skill_references(tmp_path)
        assert "planner" in refs

    def test_detects_local_skills_dir(self, tmp_path: Path) -> None:
        """Skill names from the local skills/ directory are treated as refs."""
        skills = tmp_path / "skills"
        skills.mkdir()
        (skills / "test-engineer").mkdir()
        refs = scan_skill_references(tmp_path)
        assert "test-engineer" in refs


class TestListAvailableSkills:
    def test_central_skills_only(self, tmp_path: Path) -> None:
        """Listing from a central skills dir returns all contained skills."""
        central = tmp_path / "central" / "skills"
        central.mkdir(parents=True)
        (central / "planner").mkdir()
        (central / "architect").mkdir()
        avail = list_available_skills(central, None)
        assert set(avail.keys()) == {"planner", "architect"}

    def test_overrides_shadow_central(self, tmp_path: Path) -> None:
        """Overrides with the same name shadow central entries."""
        central = tmp_path / "central" / "skills"
        central.mkdir(parents=True)
        (central / "planner").mkdir()
        overrides = tmp_path / ".vnx-overrides" / "skills"
        overrides.mkdir(parents=True)
        (overrides / "planner").mkdir()
        avail = list_available_skills(central, overrides)
        assert avail["planner"] == overrides / "planner"


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
        """--json produces valid JSON on stdout."""
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
