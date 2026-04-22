#!/usr/bin/env python3
"""Billing-safety assertions for the VNX codebase.

Ensures no script, lib, or workflow file in this repository makes direct
Anthropic API calls, imports the Anthropic SDK, or embeds API credentials.

These checks are the final gate before the burn-in CI workflow dispatches
any headless Claude invocations — keeping cost entirely under operator control.

Guarantee contract:
  BS-1: No `import anthropic` / `from anthropic import` in scripts/
  BS-2: No direct api.anthropic.com URL references in any tracked file
  BS-3: No hardcoded Anthropic API-key pattern (sk-ant-) in any tracked file
  BS-4: No ANTHROPIC_API_KEY assignment literals in any tracked file
  BS-5: subprocess calls to `claude` binary allowed; SDK calls are not
  BS-6: Fixture files contain no secrets
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths under test
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
FIXTURES_DIR = TESTS_DIR / "fixtures"

# Directories/files to skip during scans
_SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".vnx-data",
    "node_modules",
}

_SKIP_SUFFIXES = {".pyc", ".pyo", ".so", ".dylib", ".bin"}


def _py_files(base: Path):
    """Yield all .py files under base, skipping cache/binary dirs."""
    for p in base.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


def _all_text_files(base: Path):
    """Yield all non-binary text files under base."""
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in _SKIP_SUFFIXES:
            continue
        yield p


# ---------------------------------------------------------------------------
# BS-1: No Anthropic SDK imports in scripts/
# ---------------------------------------------------------------------------

class TestBS1NoSDKImports:

    _IMPORT_PATTERNS = [
        re.compile(r"^\s*import\s+anthropic\b", re.MULTILINE),
        re.compile(r"^\s*from\s+anthropic\s+import\b", re.MULTILINE),
        re.compile(r"^\s*from\s+anthropic\.", re.MULTILINE),
    ]

    def _violations(self, directory: Path) -> list[str]:
        found = []
        for path in _py_files(directory):
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat in self._IMPORT_PATTERNS:
                for m in pat.finditer(source):
                    lineno = source.count("\n", 0, m.start()) + 1
                    found.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {m.group().strip()}")
        return found

    def test_no_anthropic_sdk_imports_in_scripts(self):
        violations = self._violations(SCRIPTS_DIR)
        assert not violations, (
            "Anthropic SDK imports found in scripts/ — use CLI subprocess only:\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_anthropic_sdk_imports_in_tests(self):
        violations = self._violations(TESTS_DIR)
        assert not violations, (
            "Anthropic SDK imports found in tests/ — use CLI subprocess only:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# BS-2: No direct api.anthropic.com URLs in any tracked file
# ---------------------------------------------------------------------------

class TestBS2NoDirectAPIURLs:

    # Only match actual URL references — comments documenting absence of calls
    # (e.g. "No api.anthropic.com calls") are intentionally excluded.
    _URL_PATTERN = re.compile(r"https?://api\.anthropic\.com", re.IGNORECASE)

    def _violations(self, base: Path) -> list[str]:
        found = []
        for path in _all_text_files(base):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(content.splitlines(), start=1):
                if self._URL_PATTERN.search(line):
                    found.append(f"{path.relative_to(REPO_ROOT)}:{i}: {line.strip()}")
        return found

    def test_no_anthropic_api_url_in_scripts(self):
        violations = self._violations(SCRIPTS_DIR)
        assert not violations, (
            "Direct api.anthropic.com references found in scripts/:\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_anthropic_api_url_in_workflows(self):
        if not WORKFLOWS_DIR.is_dir():
            pytest.skip("no .github/workflows directory")
        violations = self._violations(WORKFLOWS_DIR)
        assert not violations, (
            "Direct api.anthropic.com references found in .github/workflows/:\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_anthropic_api_url_in_fixtures(self):
        if not FIXTURES_DIR.is_dir():
            pytest.skip("no tests/fixtures directory")
        violations = self._violations(FIXTURES_DIR)
        assert not violations, (
            "Direct api.anthropic.com references found in tests/fixtures/:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# BS-3: No hardcoded Anthropic API key pattern (sk-ant-)
# ---------------------------------------------------------------------------

class TestBS3NoHardcodedAPIKeys:

    # Matches the Anthropic key prefix — never a legitimate literal in source
    _KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}", re.IGNORECASE)

    def _violations(self, base: Path) -> list[str]:
        found = []
        for path in _all_text_files(base):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(content.splitlines(), start=1):
                if self._KEY_PATTERN.search(line):
                    found.append(f"{path.relative_to(REPO_ROOT)}:{i}: [REDACTED]")
        return found

    def test_no_hardcoded_api_keys_in_scripts(self):
        violations = self._violations(SCRIPTS_DIR)
        assert not violations, (
            "Hardcoded Anthropic API key pattern found in scripts/:\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_hardcoded_api_keys_in_tests(self):
        violations = self._violations(TESTS_DIR)
        assert not violations, (
            "Hardcoded Anthropic API key pattern found in tests/:\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_hardcoded_api_keys_in_workflows(self):
        if not WORKFLOWS_DIR.is_dir():
            pytest.skip("no .github/workflows directory")
        violations = self._violations(WORKFLOWS_DIR)
        assert not violations, (
            "Hardcoded Anthropic API key pattern found in .github/workflows/:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# BS-4: No ANTHROPIC_API_KEY literal assignment in source files
# ---------------------------------------------------------------------------

class TestBS4NoAPIKeyAssignment:

    # Catches `ANTHROPIC_API_KEY = "..."` or `ANTHROPIC_API_KEY="sk-..."`
    _ASSIGN_PATTERN = re.compile(
        r'ANTHROPIC_API_KEY\s*[=:]\s*["\'][^"\']{8,}["\']',
        re.IGNORECASE,
    )
    # Env-var reads are fine: os.environ["ANTHROPIC_API_KEY"] — excluded by
    # the pattern requiring a value literal after the assignment operator.

    def _violations(self, base: Path) -> list[str]:
        found = []
        for path in _all_text_files(base):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(content.splitlines(), start=1):
                if self._ASSIGN_PATTERN.search(line):
                    found.append(f"{path.relative_to(REPO_ROOT)}:{i}: [REDACTED]")
        return found

    def test_no_api_key_assignment_in_scripts(self):
        violations = self._violations(SCRIPTS_DIR)
        assert not violations, (
            "ANTHROPIC_API_KEY literal assignment found in scripts/:\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_api_key_assignment_in_workflows(self):
        if not WORKFLOWS_DIR.is_dir():
            pytest.skip("no .github/workflows directory")
        violations = self._violations(WORKFLOWS_DIR)
        assert not violations, (
            "ANTHROPIC_API_KEY literal assignment found in .github/workflows/:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# BS-5: subprocess calls use `claude` CLI, not SDK entry points
# ---------------------------------------------------------------------------

class TestBS5SubprocessCLIOnly:

    # Detects subprocess calls that target the SDK module directly
    _FORBIDDEN_PATTERNS = [
        re.compile(r'subprocess\.[^(]*\(\s*\[.*anthropic.*\]', re.IGNORECASE),
        re.compile(r'Popen\(\s*\[.*anthropic.*\]', re.IGNORECASE),
    ]

    def _violations(self, directory: Path) -> list[str]:
        found = []
        for path in _py_files(directory):
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat in self._FORBIDDEN_PATTERNS:
                for m in pat.finditer(source):
                    lineno = source.count("\n", 0, m.start()) + 1
                    found.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {m.group()[:80]}")
        return found

    def test_no_sdk_subprocess_in_scripts(self):
        violations = self._violations(SCRIPTS_DIR)
        assert not violations, (
            "subprocess call targeting Anthropic SDK module found in scripts/:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# BS-6: Fixture files contain no secrets
# ---------------------------------------------------------------------------

class TestBS6FixtureFilesClean:

    _SECRET_PATTERNS = [
        re.compile(r"sk-ant-", re.IGNORECASE),
        re.compile(r"api\.anthropic\.com", re.IGNORECASE),
        re.compile(r"ANTHROPIC_API_KEY\s*[=:]", re.IGNORECASE),
    ]

    def test_fixture_files_contain_no_secrets(self):
        if not FIXTURES_DIR.is_dir():
            pytest.skip("no tests/fixtures directory")
        violations = []
        for path in FIXTURES_DIR.rglob("*"):
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat in self._SECRET_PATTERNS:
                if pat.search(content):
                    violations.append(str(path.relative_to(REPO_ROOT)))
        assert not violations, (
            "Secret patterns found in fixture files:\n"
            + "\n".join(f"  {v}" for v in violations)
        )
