#!/usr/bin/env python3
"""OI-1891: no test may mutate the operator's launchd domain.

Every test here puts a RECORDING STUB first on PATH and asserts that
``shutil.which("launchctl")`` is that stub before it acts, so the real binary
can never be reached from this file. Nothing here runs ``launchctl`` itself.

* B1: the conftest shim refuses mutating verbs, in Python and in bash, and fails
  the test that triggered it. Run in an inner pytest that loads the repo's own
  ``tests/conftest.py`` the same way before and after the fix.
* B2: the production backstop in ``vnx_paths``, with no conftest shim.
* B3: read verbs pass through. B4: no launchctl on PATH, no shim.
* B5: ``vnx init`` in the leaking shape never reaches launchctl.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import List, Optional

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT))

import vnx_paths


def _recording_stub(bin_dir: Path, *, list_output: str = "") -> Path:
    """A ``launchctl`` that plays the real binary: it only appends its argv to a log."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / "stub.log"
    log.touch()
    stub = bin_dir / "launchctl"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        f'[ "$1" = "list" ] && printf "%s\\n" "{list_output}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _logged(stub: Path) -> List[str]:
    return (stub.parent / "stub.log").read_text(encoding="utf-8").splitlines()


def _inner_env(path: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in (
        "PYTEST_CURRENT_TEST", "VNX_TEST_LAUNCHCTL_SHIM", "PYTEST_ADDOPTS",
    )}
    env["PATH"] = path
    env["PYTHONPATH"] = str(TESTS_DIR)
    return env


def _run_inner(tmp_path: Path, body: str, path: str) -> subprocess.CompletedProcess:
    """Run ``body`` as a test in a fresh pytest that loads this repo's tests/conftest.py."""
    inner = tmp_path / "inner" / "test_inner.py"
    inner.parent.mkdir(exist_ok=True)
    inner.write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider",
         "-q", "-s", str(inner)],
        cwd=str(inner.parent), env=_inner_env(path), capture_output=True, text=True,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# B1: the shim
# ---------------------------------------------------------------------------


def test_a_test_that_loads_or_bootstraps_fails_and_never_reaches_the_binary(
    tmp_path: Path,
) -> None:
    stub = _recording_stub(tmp_path / "bin")
    assert Path(stub).parent == tmp_path / "bin"

    result = _run_inner(
        tmp_path,
        """
        import subprocess

        def test_leaks():
            print("MARKER-INNER-RAN")
            subprocess.run(["launchctl", "load", "-w", "x.plist"])
            subprocess.run(["bash", "-c", "launchctl bootstrap gui/$(id -u) x.plist"])
        """,
        f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
    )

    output = result.stdout + result.stderr
    assert "MARKER-INNER-RAN" in output, output
    assert result.returncode != 0, output
    assert "[TEST ISOLATION GUARD]" in output, output
    mutating = [line for line in _logged(stub) if line.split()[:1] and line.split()[0] != "list"]
    assert mutating == [], f"het echte binary is bereikt: {mutating}"


def test_a_read_verb_passes_through_and_does_not_fail_the_test(tmp_path: Path) -> None:
    stub = _recording_stub(tmp_path / "bin", list_output="STUB-LIST-OUTPUT")

    result = _run_inner(
        tmp_path,
        """
        import subprocess

        def test_reads():
            out = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
            assert out.returncode == 0
            print("GOT:" + out.stdout.strip())
        """,
        f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "GOT:STUB-LIST-OUTPUT" in output, output
    assert _logged(stub) == ["list"]


def test_without_a_launchctl_on_path_no_shim_is_installed(tmp_path: Path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()

    result = _run_inner(
        tmp_path,
        """
        import os, shutil

        def test_no_shim():
            assert shutil.which("launchctl") is None
            assert "VNX_TEST_LAUNCHCTL_SHIM" not in os.environ
            print("MARKER-NO-SHIM")
        """,
        str(empty),
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "MARKER-NO-SHIM" in output, output


# ---------------------------------------------------------------------------
# B2: the production backstop (no conftest shim on PATH)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_first_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A recording stub as THE launchctl, with the conftest shim taken out of play."""
    stub = _recording_stub(tmp_path / "stubbin")
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("VNX_TEST_LAUNCHCTL_SHIM", raising=False)
    import shutil

    assert shutil.which("launchctl") == str(stub), "de stub moet de launchctl op PATH zijn"
    return stub


def _fake_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from vnx_cli.commands import init_cmd

    real_engine = init_cmd._engine.engine_root()
    engine = tmp_path / "fake-vnx-engine"
    engine.mkdir()
    for name in ("scripts", "templates", "schemas"):
        if (real_engine / name).is_dir():
            os.symlink(real_engine / name, engine / name, target_is_directory=True)
    monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: engine)
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return init_cmd, engine


def test_init_install_refuses_a_real_launchctl_under_a_test_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_first_on_path: Path
) -> None:
    init_cmd, engine = _fake_engine(tmp_path, monkeypatch)

    with pytest.raises(vnx_paths.TestIsolationGuardError, match=r"\[TEST ISOLATION GUARD\]"):
        init_cmd._install_launchd_agent(
            str(engine), "com.vnx.gate-obligation-runner", project_id="t"
        )

    assert _logged(stub_first_on_path) == []


def test_dream_scheduler_install_and_uninstall_refuse_a_real_launchctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_first_on_path: Path
) -> None:
    from dream import scheduler

    home = tmp_path / "fake-home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    plist = home / "Library" / "LaunchAgents" / scheduler._PLIST_NAME
    plist.write_text("<plist/>", encoding="utf-8")

    with pytest.raises(vnx_paths.TestIsolationGuardError):
        scheduler._install_macos("t", tmp_path, "/x/vnx")
    with pytest.raises(vnx_paths.TestIsolationGuardError):
        scheduler._uninstall_macos()

    assert _logged(stub_first_on_path) == []
    assert plist.exists(), "de uninstall mag niets verwijderen voor hij de guard passeert"


def test_the_backstop_lets_the_conftest_shim_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shim is the one launchctl a test may reach, so an install that reaches it passes."""
    init_cmd, engine = _fake_engine(tmp_path, monkeypatch)
    stub = _recording_stub(tmp_path / "stubbin")
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VNX_TEST_LAUNCHCTL_SHIM", str(stub))
    import shutil

    assert shutil.which("launchctl") == str(stub)

    init_cmd._install_launchd_agent(str(engine), "com.vnx.gate-obligation-runner", project_id="t")

    assert [line.split()[0] for line in _logged(stub)][:2] == ["unload", "load"]


def test_the_backstop_does_not_fire_when_there_is_no_launchctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux CI: nothing to protect, and a test that fakes subprocess.run must keep passing."""
    from unittest.mock import MagicMock

    init_cmd, engine = _fake_engine(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    monkeypatch.delenv("VNX_TEST_LAUNCHCTL_SHIM", raising=False)
    import shutil

    assert shutil.which("launchctl") is None
    monkeypatch.setattr(
        init_cmd.subprocess, "run",
        lambda cmd, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )

    init_cmd._install_launchd_agent(str(engine), "com.vnx.gate-obligation-runner", project_id="t")


# ---------------------------------------------------------------------------
# B5: `vnx init` in the leaking shape
# ---------------------------------------------------------------------------


def test_vnx_init_with_a_tmp_home_and_an_engine_outside_worktrees_never_reaches_launchctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_first_on_path: Path
) -> None:
    """The shape of the OI-1891 leak: only ``Path.home`` patched, an engine root
    outside ``.vnx-data/worktrees/`` (so the worktree guard does not skip the
    install), no ``subprocess.run`` fake."""
    import argparse

    from vnx_cli.commands.init_cmd import vnx_init

    _init_cmd, _engine_root = _fake_engine(tmp_path, monkeypatch)
    for key in ("VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_DATA_HOME", "XDG_DATA_HOME",
                "VNX_STATE_DIR", "VNX_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    project = tmp_path / "project0"
    project.mkdir()

    rc = vnx_init(argparse.Namespace(
        project_path=None, project_dir=str(project), project_id=None, template="default",
        force=False, non_interactive=False, set_version=None,
    ))

    assert rc == 0
    assert _logged(stub_first_on_path) == []
