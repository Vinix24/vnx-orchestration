#!/usr/bin/env python3
"""Tests for scripts/vnx_doctor.py::check_branch_protection_drift (Golf B, B1).

(m) pass (no diffs), fail (with the differing fields), warn (unreachable
live state, or the YAML missing on disk). Uses the SAME comparator
(``forge_protection_drift.compare``) the merge-door preflight and
``apply_branch_protection.py`` use — only ``fetch_live_protection`` is
mocked per test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import vnx_doctor
import forge_protection_drift as fpd

YAML_PATH = VNX_ROOT / "scripts" / "forge" / "branch_protection.yaml"


def _real_norm() -> Dict[str, Any]:
    return fpd.to_normalized_dict(fpd.load_protection_config(YAML_PATH))


def _paths(vnx_home: Path) -> Dict[str, str]:
    return {"VNX_HOME": str(vnx_home)}


class TestCheckBranchProtectionDrift:
    def test_pass_when_live_matches_the_yaml(self, monkeypatch):
        monkeypatch.setattr(fpd, "fetch_live_protection", lambda *a, **k: _real_norm())

        results = vnx_doctor.check_branch_protection_drift(_paths(VNX_ROOT))

        assert len(results) == 1
        assert results[0].status == vnx_doctor.PASS
        assert results[0].name == "branch_protection_drift"

    def test_fail_when_live_drifts_naming_the_fields(self, monkeypatch):
        drifted = _real_norm()
        drifted["allow_force_pushes"] = True
        drifted["enforce_admins"] = False
        monkeypatch.setattr(fpd, "fetch_live_protection", lambda *a, **k: drifted)

        results = vnx_doctor.check_branch_protection_drift(_paths(VNX_ROOT))

        assert len(results) == 1
        assert results[0].status == vnx_doctor.FAIL
        assert "allow_force_pushes" in results[0].message
        assert "enforce_admins" in results[0].message
        assert set(results[0].details) == {"allow_force_pushes", "enforce_admins"}

    def test_warn_when_live_state_is_unreachable(self, monkeypatch):
        def raise_drift_error(*a, **k):
            raise fpd.ProtectionDriftError("gh CLI niet beschikbaar: boom")

        monkeypatch.setattr(fpd, "fetch_live_protection", raise_drift_error)

        results = vnx_doctor.check_branch_protection_drift(_paths(VNX_ROOT))

        assert len(results) == 1
        assert results[0].status == vnx_doctor.WARN
        assert "niet leesbaar" in results[0].message

    def test_warn_when_yaml_missing_on_disk(self, tmp_path):
        empty_vnx_home = tmp_path / "no-scripts-here"
        empty_vnx_home.mkdir()

        results = vnx_doctor.check_branch_protection_drift(_paths(empty_vnx_home))

        assert len(results) == 1
        assert results[0].status == vnx_doctor.WARN
        assert "ontbreekt" in results[0].message

    def test_fail_when_yaml_on_disk_is_invalid(self, monkeypatch, tmp_path):
        vnx_home = tmp_path / "vnx-system"
        forge_dir = vnx_home / "scripts" / "forge"
        forge_dir.mkdir(parents=True)
        (forge_dir / "branch_protection.yaml").write_text("branch: main\nnot_a_known_field: 1\n", encoding="utf-8")

        results = vnx_doctor.check_branch_protection_drift(_paths(vnx_home))

        assert len(results) == 1
        assert results[0].status == vnx_doctor.FAIL
        assert "ongeldig" in results[0].message

    def test_run_doctor_never_fails_on_this_check_with_a_synthetic_vnx_home(self):
        """run_doctor's existing healthy-system test (tmp VNX_HOME, no
        scripts/forge/ present) must keep getting WARN, not FAIL, from this
        check -- a bare tmp dir is "not evidence of drift", not "drifted"."""
        tmp_home = Path(__file__).resolve().parent  # any dir without scripts/forge/
        results = vnx_doctor.check_branch_protection_drift(_paths(tmp_home))
        assert results[0].status in (vnx_doctor.WARN, vnx_doctor.PASS)
