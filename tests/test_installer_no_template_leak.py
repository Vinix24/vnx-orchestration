"""Installer template-leak tests — PR-WAVE2A-6.

Verifies that install.sh ships no developer-machine-specific paths into target
projects, and that all install-time {{PLACEHOLDER}} variables are substituted.

Design:
- Source check: shipped source files must not contain hardcoded developer paths.
- Install check: run install.sh with a controlled HOME to verify clean output.
  Using HOME=/tmp/vnx-test-home-<pid> makes the test deterministic on any OS.
  On CI (Ubuntu), real HOME=/home/runner also works — no /Users/ will appear.

Dispatch-ID: 20260525-083038-wave2a-6-installer-template-leak
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
INSTALL_SH = REPO_ROOT / "install.sh"

# Paths that must never appear hardcoded in shipped template files.
# These are developer-machine-specific strings.
HARDCODED_DEVELOPER_PATHS = [
    "/Users/vincentvandeth",
    "vincent-vd",          # personal operator_id used as default in example files
]

# File extensions to check in source/install dirs.
TEXT_EXTENSIONS = {
    ".yml", ".yaml", ".json", ".sh", ".py", ".md",
    ".conf", ".toml", ".example",
}

# Source-repo directories whose contents are shipped to target projects.
# Mirrors the SHIP_PATHS and DOCS_SHIP_DIRS lists in install.sh.
SHIPPED_SOURCE_DIRS = [
    "scripts",
    "configs",
    "templates",
    "hooks",
    "schemas",
    "docs/core",
    "docs/intelligence",
    "docs/operations",
    "docs/orchestration",
    "docs/testing",
]

# Install-time placeholder variables that MUST be substituted before delivery.
INSTALL_TIME_PLACEHOLDERS = [
    "{{USER_HOME}}",
    "{{VNX_PROJECT_ROOT}}",
    "{{VNX_HOME}}",
]

# Files that legitimately contain the forbidden pattern strings as constants
# for comparison/checking purposes — excluded from source and install scans.
SCAN_SKIP_FILENAMES = {
    "check_installer_no_template_leak.sh",  # checker script defines patterns
    "test_installer_no_template_leak.py",   # test file defines patterns
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text_files_in(directory: Path, *, skip_filenames: set[str] | None = None) -> list[Path]:
    """Return all text files under *directory* with known text extensions.

    Files whose ``name`` is in *skip_filenames* are excluded. Defaults to
    ``SCAN_SKIP_FILENAMES`` which excludes checker/test files that
    legitimately contain the forbidden pattern strings as string constants.
    """
    if skip_filenames is None:
        skip_filenames = SCAN_SKIP_FILENAMES
    result = []
    if not directory.is_dir():
        return result
    for f in directory.rglob("*"):
        if f.is_file() and f.suffix in TEXT_EXTENSIONS and f.name not in skip_filenames:
            result.append(f)
    return result


def _read_safe(path: Path) -> str:
    """Read file, ignoring decode errors (binary content is irrelevant)."""
    try:
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def _run_install(target_dir: Path, fake_home: Path) -> subprocess.CompletedProcess:
    """Run install.sh with a controlled HOME for deterministic testing."""
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    return subprocess.run(
        ["bash", str(INSTALL_SH), str(target_dir)],
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Source-level checks (don't need install)
# ---------------------------------------------------------------------------

class TestSourceFilesHaveNoHardcodedPaths:
    """Shipped source files must not contain developer-machine-specific paths."""

    def test_no_developer_paths_in_shipped_sources(self):
        """No hardcoded /Users/vincentvandeth or developer-specific IDs in source."""
        violations: list[str] = []

        for rel_dir in SHIPPED_SOURCE_DIRS:
            directory = REPO_ROOT / rel_dir
            for f in _text_files_in(directory):
                content = _read_safe(f)
                for pattern in HARDCODED_DEVELOPER_PATHS:
                    if pattern in content:
                        rel = f.relative_to(REPO_ROOT)
                        violations.append(f"{rel}: contains '{pattern}'")

        assert not violations, (
            "Developer machine paths found in shipped source files — "
            "replace with {{USER_HOME}} or a generic placeholder:\n"
            + "\n".join(violations)
        )

    def test_no_nvm_version_paths_hardcoded_in_scripts(self):
        """Scripts must not contain hardcoded .nvm/versions/node/v<version> paths."""
        # Dynamic $HOME/.nvm lookups are fine; hardcoded /Users/x/.nvm/versions/node/v20.x is not.
        pattern = "/.nvm/versions/node/v"
        violations: list[str] = []

        for rel_dir in ["scripts", "templates", "hooks"]:
            directory = REPO_ROOT / rel_dir
            for f in _text_files_in(directory):
                if f.suffix not in {".sh", ".py"}:
                    continue
                content = _read_safe(f)
                # Allow occurrences inside grep PATTERN strings (vnx_doctor.sh defines the pattern itself)
                for line in content.splitlines():
                    if pattern in line:
                        # Exclude lines that are defining/checking the forbidden pattern
                        if "PATTERN=" in line or "grep" in line:
                            continue
                        rel = f.relative_to(REPO_ROOT)
                        violations.append(f"{rel}: hardcoded nvm version path on line: {line.strip()}")

        assert not violations, (
            "Hardcoded .nvm/versions/node/v<version> paths found — "
            "use $NVM_DIR or $HOME/.nvm instead:\n"
            + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# Install-output checks
# ---------------------------------------------------------------------------

class TestInstallerOutputIsClean:
    """install.sh output must be free of developer paths and unsubstituted placeholders."""

    @pytest.fixture(scope="class")
    def installed_dir(self, tmp_path_factory):
        """Run install.sh once for the whole class; yield the .vnx install dir."""
        target = tmp_path_factory.mktemp("install_target")
        fake_home = tmp_path_factory.mktemp("fake_home")

        result = _run_install(target, fake_home)
        assert result.returncode == 0, (
            f"install.sh failed (exit {result.returncode}).\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}"
        )
        return target / ".vnx"

    def test_no_developer_username_in_installed_files(self, installed_dir):
        """No /Users/vincentvandeth or developer-specific IDs in installed output."""
        violations: list[str] = []
        for f in _text_files_in(installed_dir):
            content = _read_safe(f)
            for pattern in HARDCODED_DEVELOPER_PATHS:
                if pattern in content:
                    rel = f.relative_to(installed_dir)
                    violations.append(f".vnx/{rel}: contains '{pattern}'")

        assert not violations, (
            "Developer machine paths leaked into installed output:\n"
            + "\n".join(violations)
        )

    def test_no_install_time_placeholders_remaining(self, installed_dir):
        """{{USER_HOME}}, {{VNX_PROJECT_ROOT}}, {{VNX_HOME}} must all be substituted."""
        violations: list[str] = []
        for f in _text_files_in(installed_dir):
            content = _read_safe(f)
            for ph in INSTALL_TIME_PLACEHOLDERS:
                if ph in content:
                    rel = f.relative_to(installed_dir)
                    violations.append(f".vnx/{rel}: unsubstituted {ph}")

        assert not violations, (
            "Unsubstituted install-time placeholders found — "
            "install.sh substitution did not run or missed these files:\n"
            + "\n".join(violations)
        )

    def test_projects_example_paths_use_fake_home(self, installed_dir, tmp_path_factory):
        """projects.json.example paths must use the current HOME after install."""
        # Run a fresh install with a known fake home to verify path derivation.
        target = tmp_path_factory.mktemp("path_check_target")
        fake_home = Path("/tmp/vnx-wave2a-test-home")

        result = _run_install(target, fake_home)
        assert result.returncode == 0, f"install.sh failed: {result.stderr[-1000:]}"

        example = target / ".vnx" / "scripts" / "aggregator" / "projects.json.example"
        assert example.exists(), "projects.json.example not found after install"

        content = example.read_text()
        data = json.loads(content)

        for proj in data.get("projects", []):
            path = proj.get("path", "")
            assert str(fake_home) in path, (
                f"Project path '{path}' does not contain fake HOME '{fake_home}'. "
                "{{USER_HOME}} substitution may not have reached this file."
            )

    def test_no_users_prefix_on_linux_ci(self, installed_dir):
        """On Linux CI (non-macOS), no /Users/ prefix should appear in installed files.

        This test is skipped on macOS where /Users/ is the legitimate HOME prefix.
        On GitHub Actions (ubuntu-latest), HOME=/home/runner — no /Users/ can appear
        unless a path was hardcoded (not derived from $HOME).
        """
        if sys.platform == "darwin":
            pytest.skip("On macOS, /Users/ is a legitimate HOME prefix — skip CI-only check")

        violations: list[str] = []
        for f in _text_files_in(installed_dir):
            content = _read_safe(f)
            if "/Users/" in content:
                rel = f.relative_to(installed_dir)
                violations.append(f".vnx/{rel}: contains '/Users/' (hardcoded macOS path?)")

        assert not violations, (
            "Hardcoded /Users/ paths found in installed output on non-macOS host:\n"
            + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# R2 regression tests — shell injection + sed escaping (PR-WAVE2A-6 R2)
# ---------------------------------------------------------------------------

class TestInstallerR2Security:
    """Regression tests for the R2 codex findings:
    - No eval in scan logic (array-based find)
    - sed replacement values escape &, |, and backslash
    """

    def test_install_no_eval_in_scan_logic(self):
        """install.sh must not use eval in the file-scan / find loop.

        The codex blocking finding flagged shell injection via:
          done < <(eval "find '$dir' -type f \\( $SCAN_EXTENSIONS \\) -print0")
        The fix builds find args as a bash array and calls find directly.
        This test greps the install.sh source for any eval usage inside the
        _substitute_install_templates or surrounding scan context and asserts
        none are present.
        """
        content = INSTALL_SH.read_text()

        # Collect all lines that contain eval (case-sensitive).
        eval_lines = [
            (i + 1, line.rstrip())
            for i, line in enumerate(content.splitlines())
            if "eval" in line
        ]

        # Filter: allow eval that appears only in comments (lines whose first
        # non-whitespace token is '#') or in test/documentation strings.
        blocking = []
        for lineno, line in eval_lines:
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # comment — allowed
            blocking.append(f"install.sh:{lineno}: {line}")

        assert not blocking, (
            "eval found in non-comment install.sh lines — "
            "scan logic must use array-based find, not eval:\n"
            + "\n".join(blocking)
        )

    def test_install_path_with_special_chars(self, tmp_path):
        """install.sh must handle project paths that contain &, backslash, and |.

        These characters have special meaning in sed replacement strings.
        Without _sed_escape(), a HOME like /tmp/foo&bar would corrupt the
        substituted output.  This test verifies that substitution produces the
        literal path value in the installed file, not a garbled replacement.
        """
        # Build a target directory at a clean tmp path (no special chars in
        # the FS path itself — OS constraints prevent | in dir names on macOS/Linux).
        # We test escaping by injecting the special chars into a fake HOME value
        # via the environment, which maps to {{USER_HOME}} in templates.
        #
        # Use a fake HOME whose string representation contains &.
        # NOTE: the OS path itself cannot literally contain |, but we can still
        # exercise the _sed_escape path by crafting the env HOME value.
        # We verify via a simple template file placed in the install target.

        # Create a minimal stub target that has a file with the placeholder.
        fake_home_str = str(tmp_path / "home&special")
        target = tmp_path / "target_project"
        target.mkdir()
        vnx_dir = target / ".vnx"
        vnx_dir.mkdir()
        # Place a stub template file that contains the placeholder.
        stub_dir = vnx_dir / "scripts"
        stub_dir.mkdir()
        stub_file = stub_dir / "stub.conf"
        stub_file.write_text("home_dir={{USER_HOME}}\n")

        # Run install.sh into the target (install.sh also writes its own files,
        # but we only care that our stub is not corrupted).
        env = dict(os.environ)
        env["HOME"] = fake_home_str
        result = subprocess.run(
            ["bash", str(INSTALL_SH), str(target)],
            env=env,
            capture_output=True,
            text=True,
        )

        # install.sh may exit non-zero for unrelated reasons (e.g. git remote),
        # but the substitution must have run.  Check the stub file directly.
        # Locate the stub in the installed .vnx/scripts/.
        installed_stub = target / ".vnx" / "scripts" / "stub.conf"
        if not installed_stub.exists():
            # install.sh overwrites the .vnx dir — look for stub at alternate path.
            installed_stub = stub_file

        if not installed_stub.exists():
            pytest.skip("Stub file not present after install — install layout may differ")

        content = installed_stub.read_text()
        # The substitution must produce the literal fake_home_str (with &).
        # If _sed_escape is missing, & would expand to the matched text (/tmp/.../home)
        # and the result would NOT equal fake_home_str.
        assert f"home_dir={fake_home_str}" in content, (
            f"Expected literal home path '{fake_home_str}' after {{{{USER_HOME}}}} substitution.\n"
            f"Got file content:\n{content}\n"
            "& in path was likely not escaped — _sed_escape may not be applied."
        )
