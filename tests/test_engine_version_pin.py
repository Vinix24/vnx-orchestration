"""Tests for pip-CLI .vnx-version pin resolution (dispatch 20260717-vnx-pin-robustness).

Covers `_engine.find_version_pin` (upward traversal, mirrors the bash shim's
`find_version_pin`) and `_engine.engine_root` honoring a pin that resolves to
an installed `~/.vnx-system/versions/<pin>` tree, with a graceful fallback to
the existing sibling-of-vnx_cli resolution in every other case.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vnx_cli import _engine


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    proj = tmp_path / "project"
    (proj / "sub" / "nested").mkdir(parents=True)
    monkeypatch.chdir(proj)
    return proj


def _install_version(home, pin):
    version_dir = home / ".vnx-system" / "versions" / pin
    (version_dir / "scripts").mkdir(parents=True)
    return version_dir


class TestFindVersionPin:
    def test_no_pin_returns_none(self, project_dir):
        assert _engine.find_version_pin(project_dir) is None

    def test_pin_in_cwd_is_found(self, project_dir):
        (project_dir / ".vnx-version").write_text("v1.4.0\n")
        assert _engine.find_version_pin(project_dir) == "v1.4.0"

    def test_pin_traverses_upward_from_subdir(self, project_dir):
        (project_dir / ".vnx-version").write_text("v1.4.0\n")
        nested = project_dir / "sub" / "nested"
        assert _engine.find_version_pin(nested) == "v1.4.0"

    def test_first_line_is_stripped(self, project_dir):
        (project_dir / ".vnx-version").write_text("  v1.4.0  \nignored-second-line\n")
        assert _engine.find_version_pin(project_dir) == "v1.4.0"

    def test_blank_pin_file_returns_none(self, project_dir):
        (project_dir / ".vnx-version").write_text("\n")
        assert _engine.find_version_pin(project_dir) is None

    def test_closer_pin_wins_over_ancestor_pin(self, project_dir):
        (project_dir / ".vnx-version").write_text("v1.0.0\n")
        nested = project_dir / "sub" / "nested"
        (nested / ".vnx-version").write_text("v2.0.0\n")
        assert _engine.find_version_pin(nested) == "v2.0.0"


class TestEngineRootPinResolution:
    def test_no_pin_falls_back_to_default(self, fake_home, project_dir):
        # No .vnx-version anywhere upward of project_dir (tmp_path is isolated).
        result = _engine.engine_root()
        assert result != (fake_home / ".vnx-system" / "versions")
        assert (result / "scripts").is_dir(), "fallback engine root must still be a valid engine tree"

    def test_pin_resolves_to_installed_version_dir(self, fake_home, project_dir):
        version_dir = _install_version(fake_home, "v9.9.9")
        (project_dir / ".vnx-version").write_text("v9.9.9\n")

        assert _engine.engine_root() == version_dir.resolve()

    def test_pin_resolves_from_nested_cwd(self, fake_home, project_dir, monkeypatch):
        version_dir = _install_version(fake_home, "v9.9.9")
        (project_dir / ".vnx-version").write_text("v9.9.9\n")
        monkeypatch.chdir(project_dir / "sub" / "nested")

        assert _engine.engine_root() == version_dir.resolve()

    def test_pin_without_installed_version_falls_back(self, fake_home, project_dir):
        (project_dir / ".vnx-version").write_text("v9.9.9\n")
        # No matching dir under fake_home/.vnx-system/versions/.

        result = _engine.engine_root()
        assert result != (fake_home / ".vnx-system" / "versions" / "v9.9.9").resolve()
        assert (result / "scripts").is_dir(), "fallback engine root must still be a valid engine tree"

    def test_invalid_pin_falls_back(self, fake_home, project_dir):
        (project_dir / ".vnx-version").write_text("evil pin with spaces\n")

        result = _engine.engine_root()
        assert result != (fake_home / ".vnx-system" / "versions" / "evil pin with spaces").resolve()
        assert (result / "scripts").is_dir()

    def test_dotdot_pin_cannot_escape_versions_root(self, fake_home, project_dir):
        # ".." passes the shim's char-class regex but must not resolve outside
        # the versions root — the containment check must catch what the regex
        # alone permits.
        (project_dir / ".vnx-version").write_text("..\n")

        result = _engine.engine_root()
        versions_root = (fake_home / ".vnx-system" / "versions").resolve()
        assert result != versions_root.parent
        assert (result / "scripts").is_dir()

    def test_pinned_root_not_flagged_as_packaged_install(self, fake_home, project_dir):
        version_dir = _install_version(fake_home, "v9.9.9")
        (project_dir / ".vnx-version").write_text("v9.9.9\n")

        root = _engine.engine_root()
        assert root == version_dir.resolve()
        assert _engine.is_packaged_install(root) is False
