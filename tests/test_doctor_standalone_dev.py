#!/usr/bin/env python3
"""Tests for `vnx doctor` standalone-dev layout awareness.

Dispatch-ID: 20260627-doctor-standalone-dev

The vnx-orchestration source checkout has PROJECT_ROOT == VNX_HOME and carries no
.vnx-install-mode=central marker. There the consumer bootstrap (config.yml, the SessionStart
hook, .claude/skills/skills.yaml) is legitimately absent — the repo is the SOURCE, not a
`vnx init`-ed consumer. Those checks must read as WARN (doctor stays green), NOT FAIL, so
`vnx doctor` is a trustworthy pre-cutover gate: green means green in every layout. A central
install with PROJECT_ROOT == VNX_HOME remains a real mis-detection (still FAIL).
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import vnx_doctor as d  # noqa: E402


def _statuses(results, name):
    return [r.status for r in results if r.name == name]


# ---------------------------------------------------------------------------
# Layout detection
# ---------------------------------------------------------------------------

def test_standalone_dev_true_without_marker(tmp_path):
    # PROJECT_ROOT == VNX_HOME, no .vnx-install-mode marker → source checkout.
    assert d._is_standalone_dev(tmp_path, tmp_path) is True


def test_standalone_dev_false_with_central_marker(tmp_path):
    (tmp_path / ".vnx-install-mode").write_text("central\n")
    assert d._is_central_install(tmp_path) is True
    assert d._is_standalone_dev(tmp_path, tmp_path) is False


def test_standalone_dev_false_when_project_differs(tmp_path):
    proj = tmp_path / "proj"
    home = tmp_path / "home"
    proj.mkdir()
    home.mkdir()
    assert d._is_standalone_dev(proj, home) is False


# ---------------------------------------------------------------------------
# Checks downgrade to WARN in standalone-dev, stay FAIL for a real consumer
# ---------------------------------------------------------------------------

def test_hooks_warn_in_standalone_dev(tmp_path):
    paths = {"PROJECT_ROOT": str(tmp_path), "VNX_HOME": str(tmp_path)}
    res = d.check_hooks(paths)
    assert _statuses(res, "hooks") == [d.WARN]
    assert "standalone-dev" in res[0].message


def test_hooks_fail_in_consumer_missing_bootstrap(tmp_path):
    proj = tmp_path / "proj"
    home = tmp_path / "home"
    proj.mkdir()
    home.mkdir()
    paths = {"PROJECT_ROOT": str(proj), "VNX_HOME": str(home)}
    res = d.check_hooks(paths)
    assert _statuses(res, "hooks") == [d.FAIL]


def test_templates_skills_warn_in_standalone_dev(tmp_path):
    # No skills.yaml, standalone-dev → the skills-registry check is WARN (not FAIL).
    paths = {"PROJECT_ROOT": str(tmp_path), "VNX_HOME": str(tmp_path),
             "VNX_SKILLS_DIR": str(tmp_path / ".claude" / "skills")}
    res = d.check_templates(paths)
    assert _statuses(res, "template")[-1] == d.WARN  # the trailing skills check
    assert any("standalone-dev" in r.message for r in res if r.status == d.WARN)


def test_templates_skills_fail_in_consumer(tmp_path):
    # A real consumer (PROJECT_ROOT != VNX_HOME) missing skills.yaml → still FAIL.
    proj = tmp_path / "proj"
    home = tmp_path / "home"
    proj.mkdir()
    home.mkdir()
    paths = {"PROJECT_ROOT": str(proj), "VNX_HOME": str(home),
             "VNX_SKILLS_DIR": str(proj / ".claude" / "skills")}
    res = d.check_templates(paths)
    assert _statuses(res, "template")[-1] == d.FAIL


def test_config_warn_in_standalone_dev(tmp_path):
    # All required dirs present (so dir checks PASS) but no .vnx/config.yml, standalone-dev.
    for sub in ("state", "logs", "pids", "locks", "dispatches", "reports"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / ".vnx").mkdir(exist_ok=True)
    paths = {
        "PROJECT_ROOT": str(tmp_path), "VNX_HOME": str(tmp_path),
        "VNX_DATA_DIR": str(tmp_path), "VNX_STATE_DIR": str(tmp_path / "state"),
        "VNX_LOGS_DIR": str(tmp_path / "logs"), "VNX_PIDS_DIR": str(tmp_path / "pids"),
        "VNX_LOCKS_DIR": str(tmp_path / "locks"),
        "VNX_DISPATCH_DIR": str(tmp_path / "dispatches"),
        "VNX_REPORTS_DIR": str(tmp_path / "reports"),
    }
    (tmp_path / "dispatches" / "pending").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dispatches" / "active").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dispatches" / "completed").mkdir(parents=True, exist_ok=True)
    res = d.check_directories(paths)
    config_status = _statuses(res, "file")
    assert config_status == [d.WARN], f"expected config WARN in standalone-dev, got {config_status}"
    # And no FAILs leaked from the dir checks.
    assert d.FAIL not in [r.status for r in res]


def test_central_install_misdetect_still_fails(tmp_path):
    # Central install with PROJECT_ROOT == VNX_HOME (marker present) is a REAL mis-detection:
    # config, hooks, skills, and directories must all still FAIL (never downgraded to WARN).
    (tmp_path / ".vnx-install-mode").write_text("central\n")
    home = tmp_path
    paths = {
        "PROJECT_ROOT": str(home), "VNX_HOME": str(home),
        "VNX_DATA_DIR": str(home / "missing-data"),
        "VNX_STATE_DIR": str(home / "missing-data" / "state"),
        "VNX_LOGS_DIR": str(home / "missing-data" / "logs"),
        "VNX_PIDS_DIR": str(home / "missing-data" / "pids"),
        "VNX_LOCKS_DIR": str(home / "missing-data" / "locks"),
        "VNX_DISPATCH_DIR": str(home / "missing-data" / "dispatches"),
        "VNX_REPORTS_DIR": str(home / "missing-data" / "reports"),
        "VNX_SKILLS_DIR": str(home / ".claude" / "skills"),
    }
    assert d._is_standalone_dev(home, home) is False
    assert _statuses(d.check_hooks(paths), "hooks") == [d.FAIL]
    assert _statuses(d.check_templates(paths), "template")[-1] == d.FAIL
    dir_res = d.check_directories(paths)
    assert _statuses(dir_res, "file") == [d.FAIL]  # config is FAIL, not WARN
    assert d.WARN not in [r.status for r in dir_res]  # nothing downgraded


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
