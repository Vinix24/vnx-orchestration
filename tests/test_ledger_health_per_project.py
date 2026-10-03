#!/usr/bin/env python3
"""OI-1942: ledger-health is one launchd job per project, guarded at install and at run.

A bare ``com.vnx.ledger-health`` Label let every project's ``vnx init`` (and every
leaking test) replace the one job; the vnx-dev beacon went 146 h without a write.
These tests drive the existing surfaces (``init_cmd._install_ledger_health_runner``,
``reload_plist.sh``, ``ledger_health.main``, ``doctor``) and assert behaviour: files
written, launchctl verbs logged, exit codes, doctor checks. Every launchd test puts a
recording ``launchctl`` stub first on PATH and asserts ``shutil.which`` finds it.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LAUNCHD_DIR = SCRIPTS_DIR / "launchd"
for _p in (VNX_ROOT, SCRIPTS_DIR, SCRIPTS_DIR / "lib", LAUNCHD_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import launchd_project_scope as lps  # noqa: E402
import ledger_health as lh  # noqa: E402
from launchd_test_support import make_central_engine, recording_launchctl, register_engine  # noqa: E402
from vnx_cli import _engine  # noqa: E402
from vnx_cli.commands import doctor, init_cmd  # noqa: E402

LABEL = "com.vnx.ledger-health"
RELOAD = LAUNCHD_DIR / "reload_plist.sh"


@pytest.fixture
def guard():
    """The shared install guard (a new surface: A3 tests it directly)."""
    import launchd_install_guard
    return launchd_install_guard


def _fake_engine(tmp_path: Path, name: str = "engine") -> Path:
    engine = tmp_path / name
    engine.mkdir()
    os.symlink(SCRIPTS_DIR, engine / "scripts", target_is_directory=True)
    return engine


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake home, a recording launchctl and a fake engine registered as ``alpha``."""
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    # Path.home is patched, HOME is not: the write guard reads HOME to learn which
    # LaunchAgents dir is the real one
    monkeypatch.setattr(Path, "home", lambda: home)
    log = recording_launchctl(tmp_path / "bin", monkeypatch)
    engine = _fake_engine(tmp_path)
    monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: engine)
    return type("Host", (), {"home": home, "log": log, "engine": engine, "tmp": tmp_path})


def _agents(host) -> Path:
    return host.home / "Library" / "LaunchAgents"


def _plist(host, project_id: str) -> dict:
    path = _agents(host) / f"{LABEL}.{project_id}.plist"
    assert path.is_file(), f"no per-project plist at {path}; found {sorted(p.name for p in _agents(host).iterdir())}"
    with path.open("rb") as fh:
        return plistlib.load(fh)


class TestA1TwoProjectsTwoJobs:
    def test_each_project_gets_its_own_label_file_and_logs(self, host):
        register_engine(host.home, host.engine, "alpha")

        assert init_cmd._install_ledger_health_runner(str(host.engine), project_id="alpha") is True
        assert init_cmd._install_ledger_health_runner(str(host.engine), project_id="beta") is True

        alpha, beta = _plist(host, "alpha"), _plist(host, "beta")
        assert alpha["Label"] == f"{LABEL}.alpha"
        assert beta["Label"] == f"{LABEL}.beta"
        assert alpha["EnvironmentVariables"]["VNX_PROJECT_ID"] == "alpha"
        assert beta["EnvironmentVariables"]["VNX_PROJECT_ID"] == "beta"
        assert alpha["StandardErrorPath"] == "/tmp/vnx-ledger-health-alpha.err"
        assert beta["StandardErrorPath"] == "/tmp/vnx-ledger-health-beta.err"
        assert alpha["RunAtLoad"] is True and beta["RunAtLoad"] is True
        assert not (_agents(host) / f"{LABEL}.plist").exists()

    def test_second_install_never_unloads_the_first_project(self, host):
        register_engine(host.home, host.engine, "alpha")
        init_cmd._install_ledger_health_runner(str(host.engine), project_id="alpha")
        host.log.write_text("", encoding="utf-8")

        init_cmd._install_ledger_health_runner(str(host.engine), project_id="beta")

        beta_calls = host.log.read_text(encoding="utf-8")
        assert "beta" in beta_calls
        assert "alpha" not in beta_calls


class TestA2UnregisteredCloneIsRefused:
    def test_vnx_init_installer_returns_false_and_touches_nothing(self, host, capsys):
        # no registry entry for the engine
        assert init_cmd._install_ledger_health_runner(str(host.engine), project_id="alpha") is False

        assert list(_agents(host).iterdir()) == []
        assert host.log.read_text(encoding="utf-8") == ""
        assert "not a central install" in capsys.readouterr().out

    def test_reload_plist_exits_nonzero_with_nothing_written(self, host):
        env = {"HOME": str(host.home), "PATH": os.environ["PATH"], "VNX_HOME": str(host.engine)}
        result = subprocess.run(
            ["bash", str(RELOAD), LABEL, "alpha"], capture_output=True, text=True, env=env, timeout=60
        )

        assert result.returncode != 0, result.stdout + result.stderr
        assert "REFUSING" in result.stderr
        assert list(_agents(host).iterdir()) == []
        assert host.log.read_text(encoding="utf-8") == ""

    def test_reload_plist_installs_from_a_registered_engine(self, host):
        register_engine(host.home, host.engine, "alpha")
        env = {"HOME": str(host.home), "PATH": os.environ["PATH"], "VNX_HOME": str(host.engine)}
        result = subprocess.run(
            ["bash", str(RELOAD), LABEL, "alpha"], capture_output=True, text=True, env=env, timeout=60
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert _plist(host, "alpha")["Label"] == f"{LABEL}.alpha"
        assert "load" in host.log.read_text(encoding="utf-8")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


class TestA3InstallGuardRoots:
    """New-surface tests: the guard module has no base-code counterpart."""

    def test_central_install_is_accepted(self, guard, tmp_path):
        make_central_engine(tmp_path / "central")
        assert guard.refusal_reason(tmp_path / "central") is None

    def test_registered_primary_checkout_is_accepted(self, guard, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        _git(checkout, "init", "-q")
        register_engine(home, checkout, "alpha")
        assert guard.refusal_reason(checkout) is None

    def test_linked_worktree_of_a_registered_checkout_is_refused(self, guard, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        _git(checkout, "init", "-q")
        (checkout / "f").write_text("x", encoding="utf-8")
        _git(checkout, "add", "f")
        _git(checkout, "commit", "-qm", "init")
        register_engine(home, checkout, "alpha")
        _git(checkout, "add", ".vnx-project-id")
        _git(checkout, "commit", "-qm", "marker")
        linked = tmp_path / "linked"
        _git(checkout, "worktree", "add", "-q", str(linked), "-b", "side")

        assert (linked / ".vnx-project-id").read_text(encoding="utf-8").strip() == "alpha"
        reason = guard.refusal_reason(linked)
        assert reason is not None and "linked git worktree" in reason

    def test_registered_path_with_a_different_marker_is_refused(self, guard, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)
        checkout = tmp_path / "checkout"
        register_engine(home, checkout, "alpha")
        (checkout / ".vnx-project-id").write_text("beta\n", encoding="utf-8")
        assert guard.refusal_reason(checkout) is not None

    def test_clone_carrying_a_registered_id_at_another_path_is_refused(self, guard, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)
        register_engine(home, tmp_path / "primary", "alpha")
        clone = tmp_path / "clone"
        clone.mkdir()
        (clone / ".vnx-project-id").write_text("alpha\n", encoding="utf-8")
        assert guard.refusal_reason(clone) is not None

    def test_dir_under_vnx_data_worktrees_is_refused_even_when_central(self, guard, tmp_path):
        engine = make_central_engine(tmp_path / ".vnx-data" / "worktrees" / "dispatch-x")
        reason = guard.refusal_reason(engine)
        assert reason is not None and ".vnx-data/worktrees" in reason


class TestA4LeftoverPlaceholdersNeverInstall:
    def test_unknown_placeholder_raises_and_writes_nothing(self, host, monkeypatch):
        register_engine(host.home, host.engine, "alpha")
        # a template copy with one extra placeholder, in an engine of its own
        engine = host.tmp / "engine-extra"
        (engine / "scripts" / "launchd").mkdir(parents=True)
        template = (LAUNCHD_DIR / f"{LABEL}.plist").read_text(encoding="utf-8")
        (engine / "scripts" / "launchd" / f"{LABEL}.plist").write_text(
            template.replace("<key>PATH</key>", "<key>X</key><string>${VNX_UNKNOWN}</string><key>PATH</key>"),
            encoding="utf-8",
        )
        register_engine(host.home, engine, "gamma")
        monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: engine)

        with pytest.raises(RuntimeError, match="VNX_UNKNOWN"):
            init_cmd._install_ledger_health_runner(str(engine), project_id="alpha")

        assert list(_agents(host).iterdir()) == []
        assert host.log.read_text(encoding="utf-8") == ""

    @pytest.mark.parametrize("bad_id", ["", "${VNX_PROJECT_ID}", "Alpha", "a"])
    def test_invalid_project_id_raises_and_writes_nothing(self, host, bad_id):
        register_engine(host.home, host.engine, "alpha")

        with pytest.raises(RuntimeError, match="project id"):
            init_cmd._install_ledger_health_runner(str(host.engine), project_id=bad_id)

        assert list(_agents(host).iterdir()) == []
        assert host.log.read_text(encoding="utf-8") == ""


class TestA5LiteralIdNeverResolvesSilently:
    def test_main_exits_2_and_writes_no_beacon(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        store = home / ".vnx-data" / "alpha"
        (store / "state").mkdir(parents=True)
        (store / "state" / lh.REGISTER_NAME).write_text("", encoding="utf-8")
        (store / "state" / lh.LEDGER_NAME).write_text("", encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / ".vnx-project-id").write_text("alpha\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.delenv("VNX_STATE_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.delenv("VNX_DATA_HOME", raising=False)
        monkeypatch.setenv("VNX_PROJECT_ID", "${VNX_PROJECT_ID}")
        monkeypatch.chdir(repo)

        rc = lh.main([])

        assert rc == 2
        assert "not a project id" in capsys.readouterr().err
        assert list(tmp_path.rglob("ledger_health.json")) == []


def _patch_launchctl_list(monkeypatch, labels):
    monkeypatch.setattr(sys, "platform", "darwin")
    text = "PID\tStatus\tLabel\n" + "".join(f"-\t0\t{label}\n" for label in labels)
    monkeypatch.setattr(lps, "_run_real_launchctl_list", lambda: text)


class TestA6DoctorSeesTheJob:
    def test_bare_label_is_flagged_and_missing_instance_fails(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        (project / _engine.PROJECT_FILE_NAME).write_text("alpha\n", encoding="utf-8")
        _patch_launchctl_list(monkeypatch, [LABEL])

        by_name = {c.name: c for c in doctor._check_launchd_agents(project)}

        assert f"launchd:{LABEL}" in by_name, sorted(by_name)
        assert by_name[f"launchd:{LABEL}"].status == doctor.FAIL
        assert f"{LABEL}.alpha" in by_name[f"launchd:{LABEL}"].detail
        assert f"launchd:{LABEL}:non_per_project_label" in by_name, sorted(by_name)
        assert by_name[f"launchd:{LABEL}:non_per_project_label"].status == doctor.WARN

    def test_loaded_per_project_job_passes(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        (project / _engine.PROJECT_FILE_NAME).write_text("alpha\n", encoding="utf-8")
        _patch_launchctl_list(monkeypatch, [f"{LABEL}.alpha"])

        by_name = {c.name: c for c in doctor._check_launchd_agents(project)}

        assert f"launchd:{LABEL}" in by_name, sorted(by_name)
        assert by_name[f"launchd:{LABEL}"].status == doctor.PASS
        assert f"launchd:{LABEL}:non_per_project_label" not in by_name


class TestA7DoctorBeaconNamesTheJob:
    @pytest.fixture(autouse=True)
    def _project(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "alpha")

    def test_absent_beacon_is_warn_naming_the_job(self, tmp_path):
        result = doctor._check_ledger_health(tmp_path)
        assert result.status == doctor.WARN
        assert f"{LABEL}.alpha" in result.detail

    def test_stale_beacon_names_age_window_job_and_err_path(self, tmp_path):
        health = tmp_path / "health"
        health.mkdir()
        now = time.time()
        (health / "ledger_health.json").write_text(json.dumps({
            "component": lh.COMPONENT_NAME,
            "last_run_ts": now - 146 * 3600,
            "last_run_iso": "2026-09-25T19:12:20Z",
            "status": "ok",
            "expected_interval_seconds": 86400,
        }), encoding="utf-8")

        result = doctor._check_ledger_health(tmp_path)

        assert result.status == doctor.WARN
        assert "146" in result.detail
        assert "24.0h" in result.detail
        assert f"{LABEL}.alpha" in result.detail
        assert "/tmp/vnx-ledger-health-alpha.err" in result.detail
        assert "rerun ledger_health.py" not in result.detail


def _run_doctor_json(project: Path, monkeypatch, capsys) -> list:
    import argparse
    monkeypatch.setattr(doctor._engine, "resolve_data_root", lambda project_dir: project / "_data")
    _patch_launchctl_list(monkeypatch, [])
    doctor.vnx_doctor(argparse.Namespace(project_dir=str(project), json=True, strict=False))
    return json.loads(capsys.readouterr().out)["checks"]


class TestA8InstalledLiteralIsReported:
    def test_plist_with_a_literal_project_id_gives_a_doctor_warn_naming_the_file(
        self, tmp_path, monkeypatch, capsys
    ):
        home = tmp_path / "home"
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        bad = agents / f"{LABEL}.plist"
        bad.write_bytes(plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": ["/bin/bash", "-c", "cd /x && exec python3 scripts/ledger_health.py"],
            "EnvironmentVariables": {"VNX_PROJECT_ID": "${VNX_PROJECT_ID}"},
        }))
        (agents / "com.vnx.other.plist").write_bytes(
            plistlib.dumps({"Label": "com.vnx.other", "ProgramArguments": ["/bin/true"]})
        )
        project = tmp_path / "project"
        project.mkdir()
        (project / _engine.PROJECT_FILE_NAME).write_text("alpha\n", encoding="utf-8")

        checks = _run_doctor_json(project, monkeypatch, capsys)

        flagged = [c for c in checks if str(bad) in c["detail"] and c["status"] == "WARN"]
        assert len(flagged) == 1, checks
        assert "${VNX_PROJECT_ID}" in flagged[0]["detail"]
        assert not [c for c in checks if "com.vnx.other.plist" in c["detail"]]

    def test_clean_agents_dir_gives_no_placeholder_check(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        assert doctor._check_installed_plist_placeholders() == []

    def test_unreadable_plist_is_a_warn_not_a_crash(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        (agents / "com.vnx.broken.plist").write_text("not xml", encoding="utf-8")
        checks = doctor._check_installed_plist_placeholders()
        assert [c.status for c in checks] == [doctor.WARN]


class TestTemplateContract:
    """New-surface tests: the machine-wide allowlist and the placeholder rule."""

    def test_a_placeholder_template_with_a_bare_label_is_flagged(self, tmp_path):
        for family in lps.REQUIRED_PER_PROJECT_FAMILIES:
            (tmp_path / f"{family}.plist").write_bytes(plistlib.dumps({"Label": f"{family}.${{VNX_PROJECT_ID}}"}))
        (tmp_path / "com.vnx.sneaky.plist").write_text(
            "<?xml version='1.0'?><plist version='1.0'><dict><key>Label</key><string>com.vnx.sneaky</string>"
            "<key>E</key><string>${VNX_PROJECT_ID}</string></dict></plist>", encoding="utf-8",
        )
        result = lps.check_template_contract(tmp_path)
        assert ("com.vnx.sneaky", "label_not_project_scoped") in {
            (v["family"], v["kind"]) for v in result["violations"]
        }

    def test_machine_wide_templates_are_exempt(self, tmp_path):
        for family in lps.REQUIRED_PER_PROJECT_FAMILIES:
            (tmp_path / f"{family}.plist").write_bytes(plistlib.dumps({"Label": f"{family}.${{VNX_PROJECT_ID}}"}))
        for name in lps.MACHINE_WIDE_TEMPLATES:
            (tmp_path / f"{name}.plist").write_text(
                f"<?xml version='1.0'?><plist version='1.0'><dict><key>Label</key><string>{name}</string>"
                "<key>E</key><string>${VNX_PROJECT_ID}</string></dict></plist>", encoding="utf-8",
            )
        assert lps.check_template_contract(tmp_path)["ok"] is True
