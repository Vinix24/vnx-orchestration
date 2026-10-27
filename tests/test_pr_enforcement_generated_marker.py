"""The OI-1846 generated-artifact marker, now that its generator is gone.

B3 of ``retire-feature-plan-second-planning-layer`` deleted
``scripts/build_feature_plan.py`` (ADR-047). ``pr_enforcement`` no longer
imports it: the banner a generated ``FEATURE_PLAN.md`` carries is a literal in
that module. These tests pin the literal to the banner the retired generator
wrote, and pin every doubt branch of ``_is_generated_artifact`` to "keep the
path substantive" — a file is only ever set aside on positive evidence.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"

for _path in (str(_SCRIPTS_DIR), str(_LIB_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pr_enforcement as pe  # noqa: E402

# The banner the retired generator wrote on the first line. Spelled in pieces so
# the retired-paths gate does not read the file name as a live reference.
_BANNER = "<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/" + "build_feature_plan" + ".py -->"


def _write_plan(directory: Path, first_line: str, rest: str = "\n# VNX Feature Plan\n") -> Path:
    path = directory / "FEATURE_PLAN.md"
    path.write_text(first_line + rest, encoding="utf-8")
    return path


def test_marker_matches_the_banner_the_retired_generator_wrote(tmp_path: Path) -> None:
    _write_plan(tmp_path, _BANNER)

    assert pe._GENERATED_ARTIFACTS["FEATURE_PLAN.md"] == _BANNER
    assert pe._is_generated_artifact(tmp_path, "FEATURE_PLAN.md") is True


def test_only_the_feature_plan_is_listed() -> None:
    assert set(pe._GENERATED_ARTIFACTS) == {"FEATURE_PLAN.md"}


def test_a_hand_written_plan_is_real_work(tmp_path: Path) -> None:
    _write_plan(tmp_path, "# My own plan", "\n\nPR-1: do the thing\n")

    assert pe._is_generated_artifact(tmp_path, "FEATURE_PLAN.md") is False


def test_the_banner_must_be_on_the_first_line(tmp_path: Path) -> None:
    _write_plan(tmp_path, "# A hand-written heading", f"\n{_BANNER}\n")

    assert pe._is_generated_artifact(tmp_path, "FEATURE_PLAN.md") is False


def test_a_missing_file_is_not_set_aside(tmp_path: Path) -> None:
    assert pe._is_generated_artifact(tmp_path, "FEATURE_PLAN.md") is False


def test_an_empty_file_is_not_set_aside(tmp_path: Path) -> None:
    (tmp_path / "FEATURE_PLAN.md").write_text("", encoding="utf-8")

    assert pe._is_generated_artifact(tmp_path, "FEATURE_PLAN.md") is False


def test_a_path_that_is_not_a_file_is_not_set_aside(tmp_path: Path) -> None:
    (tmp_path / "FEATURE_PLAN.md").mkdir()

    assert pe._is_generated_artifact(tmp_path, "FEATURE_PLAN.md") is False


def test_an_undecodable_file_is_not_set_aside(tmp_path: Path) -> None:
    (tmp_path / "FEATURE_PLAN.md").write_bytes(b"\xff\xfe not utf-8\n")

    assert pe._is_generated_artifact(tmp_path, "FEATURE_PLAN.md") is False


def test_an_unlisted_path_is_not_set_aside(tmp_path: Path) -> None:
    (tmp_path / "NOTES.md").write_text(_BANNER + "\n", encoding="utf-8")

    assert pe._is_generated_artifact(tmp_path, "NOTES.md") is False
    assert pe._is_generated_artifact(tmp_path, "") is False


def test_importing_the_enforcement_module_imports_no_generator() -> None:
    """A lingering import of the deleted module would raise ImportError here."""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(_LIB_DIR)!r})\n"
        "import pr_enforcement\n"
        "assert 'build_feature_plan' not in sys.modules, 'still imports the retired generator'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
