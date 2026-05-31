"""Regression tests for vnx_doctor template scan — PR-WAVE2A-7.

Covers:
- OI-1557: T0.md shipped with '.claude/vnx-system/' literal — should be $VNX_HOME
- vnx_doctor.sh glob asymmetry: --glob "$TEMPLATES_DIR/**/*.md" is an absolute-path
  glob that ripgrep evaluates against relative file paths, so it silently excludes
  all .md files on CI where the absolute root differs. --glob '**/*.md' is the fix.

Three test classes:
1. TestDoctorFlagsLegacyPathInTemplate  — vnx_doctor.sh flags T0.md when it
   contains the legacy literal. Uses a temporary in-place modification of the
   actual T0.md (restored in a finally block) because vnx_paths.sh derives
   VNX_HOME from its own script location and ignores env overrides from other
   project trees.
2. TestDoctorGlobConsistency  — running vnx_doctor.sh from two different working
   directories produces identical exit codes and match counts (regression for the
   absolute-path glob bug).
3. TestCurrentT0TemplateIsClean  — current T0.md is clean (post-fix).

Dispatch-ID: 20260525-095545-wave2a-7-doctor-template-hygiene
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
DOCTOR_SH = REPO_ROOT / "scripts" / "vnx_doctor.sh"
T0_TEMPLATE = REPO_ROOT / "templates" / "terminals" / "T0.md"

LEGACY_LITERAL = ".claude/vnx-system/"

# vnx_paths.sh derives VNX_HOME from its own script location and rejects
# any inherited VNX_HOME that points elsewhere — so we run the doctor against
# the actual repo root and use in-place T0.md patching for dirty-state tests.
DOCTOR_ENV = dict(os.environ)


def _run_doctor(cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run vnx_doctor.sh, capture output, never raise on non-zero exit."""
    return subprocess.run(
        ["bash", str(DOCTOR_SH)],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else str(REPO_ROOT),
        env=DOCTOR_ENV,
    )


@contextlib.contextmanager
def _dirty_t0(extra_line: str):
    """Context manager: inject *extra_line* into T0.md then restore original.

    Guaranteed restore via finally — safe even if the test fails or errors.
    """
    original = T0_TEMPLATE.read_text()
    T0_TEMPLATE.write_text(original + "\n" + extra_line + "\n")
    try:
        yield
    finally:
        T0_TEMPLATE.write_text(original)


# ---------------------------------------------------------------------------
# 1. Doctor flags T0.md when it contains the legacy literal
# ---------------------------------------------------------------------------

class TestDoctorFlagsLegacyPathInTemplate:
    """vnx_doctor.sh must FAIL when T0.md contains '.claude/vnx-system/' literal."""

    def test_t0_template_scanned_for_legacy_path(self):
        """Doctor exits 1 when T0.md contains the forbidden legacy path.

        Uses a temporary in-place injection into the actual T0.md because
        vnx_paths.sh ignores VNX_HOME env overrides from other project trees.
        The original content is restored in a finally block.
        """
        dirty_line = "python3 .claude/vnx-system/scripts/lib/subprocess_dispatch.py \\"

        with _dirty_t0(dirty_line):
            result = _run_doctor()

        assert result.returncode != 0, (
            "vnx_doctor.sh must exit non-zero when T0.md contains the legacy path.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert LEGACY_LITERAL in combined, (
            f"Doctor output must mention the forbidden literal '{LEGACY_LITERAL}'.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_doctor_passes_after_legacy_injection_is_removed(self):
        """Doctor exits 0 after T0.md is restored to its clean state.

        Verifies that the _dirty_t0 context manager restores correctly and that
        the clean T0.md does not trigger doctor failures.
        """
        dirty_line = "python3 .claude/vnx-system/scripts/lib/subprocess_dispatch.py \\"

        with _dirty_t0(dirty_line):
            pass  # inject and immediately restore

        # After context manager exits, T0.md is back to clean.
        result = _run_doctor()
        assert result.returncode == 0, (
            "vnx_doctor.sh must exit 0 after dirty injection is reversed.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# 2. Doctor produces consistent results regardless of working directory
# ---------------------------------------------------------------------------

class TestDoctorGlobConsistency:
    """vnx_doctor.sh must produce identical exit codes from any working directory.

    Root cause of OI-1557 CI/local asymmetry: the rg --glob flag used
    '$TEMPLATES_DIR/**/*.md' with an absolute path prefix. ripgrep matches
    globs against relative file paths within the search roots, so the
    absolute-path glob silently excluded all .md files on CI where the
    absolute root differs from the developer machine.

    Fix: '--glob **/*.md' (no absolute prefix). This test verifies that both
    runs see the same match count, proving the glob reaches templates/ from
    any working directory.
    """

    def test_doctor_consistent_local_and_ci(self, tmp_path):
        """Doctor exit code and match count are identical from two different cwd paths."""
        dirty_line = "python3 .claude/vnx-system/scripts/lib/subprocess_dispatch.py \\"

        # Two distinct working directories simulate "local machine" vs "CI checkout dir".
        cwd_a = tmp_path / "run_a"
        cwd_b = tmp_path / "run_b"
        cwd_a.mkdir()
        cwd_b.mkdir()

        with _dirty_t0(dirty_line):
            result_a = _run_doctor(cwd=cwd_a)
            result_b = _run_doctor(cwd=cwd_b)

        # Both runs must agree on pass/fail.
        assert result_a.returncode == result_b.returncode, (
            f"Doctor exit code differs: cwd_a={result_a.returncode}, "
            f"cwd_b={result_b.returncode}.\n"
            f"stdout_a: {result_a.stdout}\n"
            f"stdout_b: {result_b.stdout}"
        )

        # Both runs must find the same number of lines mentioning the literal.
        def _count_match_lines(stdout: str) -> int:
            return sum(
                1 for line in stdout.splitlines()
                if LEGACY_LITERAL in line and line.strip()
            )

        count_a = _count_match_lines(result_a.stdout)
        count_b = _count_match_lines(result_b.stdout)

        assert count_a == count_b, (
            f"Match count differs: cwd_a={count_a}, cwd_b={count_b}.\n"
            "Indicates the glob uses an absolute path that resolves differently "
            "per working directory — use --glob '**/*.md' (no absolute prefix).\n"
            f"stdout_a: {result_a.stdout}\n"
            f"stdout_b: {result_b.stdout}"
        )

        # Sanity: the dirty template must have been detected in both runs.
        assert count_a >= 1, (
            "Doctor found 0 matches even with the legacy literal present in T0.md. "
            "The .md glob is not reaching templates/terminals/ — "
            "check that '--glob **/*.md' is used (not an absolute-path glob)."
        )


# ---------------------------------------------------------------------------
# 3. Current T0.md source file is clean (post-fix regression guard)
# ---------------------------------------------------------------------------

class TestCurrentT0TemplateIsClean:
    """T0.md in the repo must not contain '.claude/vnx-system/' after the fix."""

    def test_t0_template_has_no_hardcoded_legacy_path(self):
        """templates/terminals/T0.md must not contain the legacy installer path."""
        assert T0_TEMPLATE.exists(), (
            f"T0.md not found at {T0_TEMPLATE} — check repo layout."
        )
        content = T0_TEMPLATE.read_text()
        assert LEGACY_LITERAL not in content, (
            f"templates/terminals/T0.md still contains '{LEGACY_LITERAL}'.\n"
            "Replace with '$VNX_HOME/scripts/lib/subprocess_dispatch.py'.\n"
            "Relevant lines:\n"
            + "\n".join(
                f"  line {i+1}: {line}"
                for i, line in enumerate(content.splitlines())
                if LEGACY_LITERAL in line
            )
        )

    def test_no_other_templates_have_legacy_path(self):
        """T1/T2/T3 templates must also be free of the '.claude/vnx-system/' literal."""
        templates_dir = REPO_ROOT / "templates" / "terminals"
        violations: list[str] = []

        for tmpl in sorted(templates_dir.glob("*.md")):
            content = tmpl.read_text()
            for i, line in enumerate(content.splitlines()):
                if LEGACY_LITERAL in line:
                    violations.append(f"{tmpl.name}:{i+1}: {line.strip()}")

        assert not violations, (
            f"Found '{LEGACY_LITERAL}' in terminal templates:\n"
            + "\n".join(violations)
        )

    def test_doctor_exits_clean_on_current_repo(self):
        """vnx_doctor.sh must exit 0 on the current repo after the PR-WAVE2A-7 fix."""
        result = _run_doctor()
        assert result.returncode == 0, (
            "vnx_doctor.sh reports forbidden path references in the current repo.\n"
            "This means the wave2a-7 fix is incomplete or a new violation was introduced.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
