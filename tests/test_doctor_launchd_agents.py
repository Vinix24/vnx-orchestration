#!/usr/bin/env python3
"""Tests for C3 (golf C) — `vnx doctor` reporting per-project launchd state.

Before this dispatch, ``scripts/launchd/launchd_project_scope.py`` (the
per-project label guard #1769 shipped for OI-1509/OI-1510) had zero readers
outside its own test file: nothing in ``vnx doctor`` ever consulted it, so a
project whose ``com.vnx.receipt-processor`` or ``com.vnx.gate-obligation-
runner`` launchd job was entirely absent got a clean bill of health.

``vnx_cli/commands/doctor.py::_check_launchd_agents`` is the new reader.
Launchd state is always injected here — never read from the real host — by
monkeypatching ``launchd_project_scope._run_real_launchctl_list``, the same
single injectable reader ``launchd_project_scope.py``'s own test suite uses.
This guards against writing a second, doctor-local launchctl reader (the
dispatch's explicit instruction) and against a test that only measures
whichever host happens to run it.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
LAUNCHD_DIR = VNX_ROOT / "scripts" / "launchd"
sys.path.insert(0, str(VNX_ROOT))
sys.path.insert(0, str(LAUNCHD_DIR))

from vnx_cli.commands import doctor  # noqa: E402
from vnx_cli import _engine  # noqa: E402
import launchd_project_scope as lps  # noqa: E402

RECEIPT_PROCESSOR = "com.vnx.receipt-processor"
GATE_OBLIGATION = "com.vnx.gate-obligation-runner"
CLEANUP_WORKTREES = "com.vnx.cleanup-reviewed-worktrees"


def _write_marker(project_dir: Path, project_id: str) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / _engine.PROJECT_FILE_NAME).write_text(project_id + "\n", encoding="utf-8")


def _launchctl_text(labels) -> str:
    lines = ["PID\tStatus\tLabel"]
    for label in labels:
        lines.append(f"-\t0\t{label}")
    return "\n".join(lines)


def _patch(monkeypatch, *, platform: str = "darwin", labels=()):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(lps, "_run_real_launchctl_list", lambda: _launchctl_text(labels))


class TestMissingJobIsLoud:
    """The core red test: doctor on a project without a receipt-processor
    job must FAIL, and the failure must name the job."""

    def test_missing_receipt_processor_fails_with_job_name(self, tmp_path, monkeypatch):
        _write_marker(tmp_path, "vnx-dev")
        _patch(monkeypatch, labels=[f"{GATE_OBLIGATION}.vnx-dev"])  # receipt-processor absent

        checks = doctor._check_launchd_agents(tmp_path)
        by_name = {c.name: c for c in checks}

        rp_check = by_name[f"launchd:{RECEIPT_PROCESSOR}"]
        assert rp_check.status == doctor.FAIL, checks
        assert f"{RECEIPT_PROCESSOR}.vnx-dev" in rp_check.detail

        # The already-loaded family must not be flagged.
        gate_check = by_name[f"launchd:{GATE_OBLIGATION}"]
        assert gate_check.status == doctor.PASS, checks

    def test_missing_both_jobs_fails_both_with_distinct_job_names(self, tmp_path, monkeypatch):
        _write_marker(tmp_path, "vnx-dev")
        _patch(monkeypatch, labels=[])  # nothing loaded at all

        checks = doctor._check_launchd_agents(tmp_path)
        failed = {c.name: c for c in checks if c.status == doctor.FAIL}

        assert f"launchd:{RECEIPT_PROCESSOR}" in failed
        assert f"launchd:{GATE_OBLIGATION}" in failed
        assert f"{RECEIPT_PROCESSOR}.vnx-dev" in failed[f"launchd:{RECEIPT_PROCESSOR}"].detail
        assert f"{GATE_OBLIGATION}.vnx-dev" in failed[f"launchd:{GATE_OBLIGATION}"].detail

    def test_missing_cleanup_reviewed_worktrees_fails_with_job_name(
        self, tmp_path, monkeypatch
    ):
        """OI-1629 deel c red test: without the family registered, doctor
        produces no ``launchd:com.vnx.cleanup-reviewed-worktrees`` check at all
        (KeyError -> red); with the registration, a project with no cleanup
        instance must FAIL and the failure must name the job."""
        _write_marker(tmp_path, "vnx-dev")
        # receipt-processor and gate-obligation-runner both loaded; the
        # cleanup family has no instance at all.
        _patch(
            monkeypatch,
            labels=[
                f"{RECEIPT_PROCESSOR}.vnx-dev",
                f"{GATE_OBLIGATION}.vnx-dev",
            ],
        )

        checks = doctor._check_launchd_agents(tmp_path)
        by_name = {c.name: c for c in checks}

        cleanup_check = by_name[f"launchd:{CLEANUP_WORKTREES}"]
        assert cleanup_check.status == doctor.FAIL, checks
        assert f"{CLEANUP_WORKTREES}.vnx-dev" in cleanup_check.detail


class TestCleanStateIsGreen:
    def test_all_required_jobs_loaded_is_all_pass(self, tmp_path, monkeypatch):
        _write_marker(tmp_path, "vnx-dev")
        _patch(
            monkeypatch,
            labels=[
                f"{RECEIPT_PROCESSOR}.vnx-dev",
                f"{GATE_OBLIGATION}.vnx-dev",
                f"{CLEANUP_WORKTREES}.vnx-dev",
            ],
        )

        checks = doctor._check_launchd_agents(tmp_path)
        assert all(c.status != doctor.FAIL for c in checks), checks
        by_name = {c.name: c for c in checks}
        assert by_name[f"launchd:{RECEIPT_PROCESSOR}"].status == doctor.PASS
        assert by_name[f"launchd:{GATE_OBLIGATION}"].status == doctor.PASS
        assert by_name[f"launchd:{CLEANUP_WORKTREES}"].status == doctor.PASS

    def test_a_second_projects_own_scoped_job_never_counts_for_this_project(
        self, tmp_path, monkeypatch
    ):
        """A clean multi-project host: mission-control's own instance must
        never satisfy vnx-dev's requirement."""
        _write_marker(tmp_path, "vnx-dev")
        _patch(
            monkeypatch,
            labels=[
                f"{RECEIPT_PROCESSOR}.mission-control",
                f"{GATE_OBLIGATION}.mission-control",
                f"{CLEANUP_WORKTREES}.mission-control",
            ],
        )

        checks = doctor._check_launchd_agents(tmp_path)
        failed_names = {c.name for c in checks if c.status == doctor.FAIL}
        assert f"launchd:{RECEIPT_PROCESSOR}" in failed_names
        assert f"launchd:{GATE_OBLIGATION}" in failed_names
        assert f"launchd:{CLEANUP_WORKTREES}" in failed_names


class TestNonBehavioralGuards:
    """These must never crash or silently pass — every unmeasurable state is
    WARN, never a fabricated PASS or FAIL."""

    def test_no_project_id_marker_warns_not_crashes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        checks = doctor._check_launchd_agents(tmp_path)
        assert len(checks) == 1
        assert checks[0].status == doctor.WARN
        assert "vnx init" in checks[0].detail

    def test_non_darwin_platform_warns_not_fails(self, tmp_path, monkeypatch):
        _write_marker(tmp_path, "vnx-dev")
        monkeypatch.setattr(sys, "platform", "linux")
        checks = doctor._check_launchd_agents(tmp_path)
        assert len(checks) == 1
        assert checks[0].status == doctor.WARN
        assert "macOS-only" in checks[0].detail

    def test_launchctl_unavailable_warns_never_fabricates_missing(self, tmp_path, monkeypatch):
        _write_marker(tmp_path, "vnx-dev")
        monkeypatch.setattr(sys, "platform", "darwin")

        def _boom():
            raise lps.LaunchctlListFailedError("launchctl list exited 1: permission denied")

        monkeypatch.setattr(lps, "_run_real_launchctl_list", _boom)
        checks = doctor._check_launchd_agents(tmp_path)
        assert len(checks) == 1
        assert checks[0].status == doctor.WARN
        assert "launchctl unavailable" in checks[0].detail


class TestWiredIntoVnxDoctor:
    """Not just a standalone function: vnx_doctor's aggregate summary must
    actually include the launchd checks so `--strict` fails the run."""

    def test_vnx_doctor_json_includes_launchd_checks_and_counts_the_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        import argparse

        _write_marker(tmp_path, "vnx-dev")
        _patch(monkeypatch, labels=[])  # all required families absent

        # Isolate from the real .vnx/.vnx-data resolution machinery — only
        # the launchd check's behavior is under test here.
        monkeypatch.setattr(
            doctor._engine, "resolve_data_root", lambda project_dir: tmp_path / "_data"
        )

        args = argparse.Namespace(project_dir=str(tmp_path), json=True, strict=False)
        rc = doctor.vnx_doctor(args)
        assert rc == 1  # any FAIL check (strict or not) is a non-zero exit

        import json as _json
        payload = _json.loads(capsys.readouterr().out)
        launchd_checks = [c for c in payload["checks"] if c["name"].startswith("launchd:")]
        assert launchd_checks, "vnx doctor produced no launchd:* checks — _check_launchd_agents not wired in"
        assert any(c["status"] == "FAIL" for c in launchd_checks)
        assert payload["summary"]["fail"] >= 2
