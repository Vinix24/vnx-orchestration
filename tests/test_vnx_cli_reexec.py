#!/usr/bin/env python3
"""Tests for vnx_cli/_reexec.py — pip-CLI honors .vnx-version via re-exec.

Design-track ``pip-cli-honor-pin-via-reexec``. All tests run against a FAKE
central store under tmp_path; the operator's real ~/.vnx-system install and
.vnx-data runtime state are never touched.
"""

import os
import sys
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path so vnx_cli is importable without install
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vnx_cli import _engine
from vnx_cli import _reexec


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def central_store(tmp_path, monkeypatch):
    """A fake central store: <tmp>/versions/v1.3.0 stamped as the RUNNING
    central install (marker + VERSION + vnx_cli package), with engine_root()
    pointed at it."""
    versions = tmp_path / "versions"
    running = versions / "v1.3.0"
    (running / "vnx_cli").mkdir(parents=True)
    (running / "vnx_cli" / "__init__.py").write_text("", encoding="utf-8")
    (running / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    (running / "VERSION").write_text("1.3.0\n", encoding="utf-8")
    monkeypatch.setattr(_engine, "engine_root", lambda: running)
    # Hermetic: no inherited loop-guard flag / PYTHONPATH from the outer env.
    monkeypatch.delenv(_reexec.REEXEC_ENV_FLAG, raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    return versions


@pytest.fixture()
def execv_spy(monkeypatch):
    """Capture os.execv calls instead of replacing the test process."""
    calls = []
    monkeypatch.setattr(os, "execv", lambda path, args: calls.append((path, list(args))))
    return calls


def _add_version(versions: Path, name: str, version_file: str) -> Path:
    d = versions / name
    (d / "vnx_cli").mkdir(parents=True)
    (d / "vnx_cli" / "__init__.py").write_text("", encoding="utf-8")
    (d / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    (d / "VERSION").write_text(f"{version_file}\n", encoding="utf-8")
    return d


def _pin(project_dir: Path, value: str) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".vnx-version").write_text(f"{value}\n", encoding="utf-8")
    return project_dir


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

def test_no_pin_file_no_reexec(central_store, execv_spy, tmp_path):
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_pin_equals_running_version_no_reexec(central_store, execv_spy, tmp_path):
    _pin(tmp_path, "1.3.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_pin_matches_running_with_decorative_v(central_store, execv_spy, tmp_path):
    """Pin 'v1.3.0' must match running VERSION '1.3.0' (decorative v)."""
    _pin(tmp_path, "v1.3.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_dev_checkout_never_reexecs(central_store, execv_spy, tmp_path, monkeypatch):
    """Without the .vnx-install-mode=central marker the run is a dev checkout:
    no re-exec even when the pin names a different, installed version."""
    (central_store / "v1.3.0" / ".vnx-install-mode").unlink()
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "v1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_loop_guard_blocks_second_reexec(central_store, execv_spy, tmp_path, monkeypatch):
    """VNX_PIN_REEXECED already equal to the pin -> never exec again, even
    though the running version still differs (off-by-a-hair detection)."""
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "v1.2.0")
    monkeypatch.setenv(_reexec.REEXEC_ENV_FLAG, "v1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_loop_guard_normalizes_decorative_v(central_store, execv_spy, tmp_path, monkeypatch):
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "1.2.0")
    monkeypatch.setenv(_reexec.REEXEC_ENV_FLAG, "v1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


# ---------------------------------------------------------------------------
# Re-exec fires
# ---------------------------------------------------------------------------

def test_pin_different_installed_version_reexecs(central_store, execv_spy, tmp_path, monkeypatch):
    pinned = _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "v1.2.0")
    argv = ["status", "--project-dir", str(tmp_path), "--json"]
    _reexec.maybe_reexec_pinned(argv)

    assert len(execv_spy) == 1
    python, args = execv_spy[0]
    assert python == sys.executable
    assert args == [sys.executable, "-m", "vnx_cli.main", *argv]
    # Loop-guard armed + pinned install on PYTHONPATH for the exec'd process.
    assert os.environ[_reexec.REEXEC_ENV_FLAG] == "v1.2.0"
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(pinned)


def test_pin_without_v_resolves_v_prefixed_dir(central_store, execv_spy, tmp_path):
    """Pin '1.2.0' must find the 'v1.2.0' dir in the central store."""
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert len(execv_spy) == 1


def test_existing_pythonpath_preserved_after_pinned_paths(central_store, execv_spy, tmp_path, monkeypatch):
    pinned = _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "v1.2.0")
    monkeypatch.setenv("PYTHONPATH", "/opt/custom")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    parts = os.environ["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(pinned)
    assert parts[-1] == "/opt/custom"


def test_project_dir_equals_form_honored(central_store, execv_spy, tmp_path):
    """--project-dir=DIR form must be picked up from argv."""
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "v1.2.0")
    _reexec.maybe_reexec_pinned([f"--project-dir={tmp_path}"])
    assert len(execv_spy) == 1


def test_cwd_used_when_no_project_dir_arg(central_store, execv_spy, tmp_path, monkeypatch):
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "v1.2.0")
    monkeypatch.chdir(tmp_path)
    _reexec.maybe_reexec_pinned([])
    assert len(execv_spy) == 1


# ---------------------------------------------------------------------------
# Fail-open cases (warning + continue, never execv, never crash)
# ---------------------------------------------------------------------------

def test_fail_open_pinned_dir_missing(central_store, execv_spy, tmp_path, capsys):
    _pin(tmp_path, "v9.9.9")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    assert "WARNING" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["../evil", "bad;rm", "a b", "v1.2.0/..", ".."])
def test_fail_open_malformed_pin(central_store, execv_spy, tmp_path, capsys, bad):
    _pin(tmp_path, bad)
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    assert "WARNING" in capsys.readouterr().err


def test_fail_open_empty_pin_file(central_store, execv_spy, tmp_path):
    tmp_path.joinpath(".vnx-version").write_text("\n", encoding="utf-8")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_fail_open_pinned_dir_missing_vnx_cli(central_store, execv_spy, tmp_path, capsys):
    """A versions/<pin> dir without a vnx_cli package is not exec-able."""
    broken = central_store / "v1.2.0"
    broken.mkdir(parents=True)
    (broken / "VERSION").write_text("1.2.0\n", encoding="utf-8")
    _pin(tmp_path, "v1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    assert "WARNING" in capsys.readouterr().err


def test_fail_open_execv_oserror(central_store, tmp_path, monkeypatch, capsys):
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "v1.2.0")

    def _boom(path, args):
        raise OSError("exec format error")

    monkeypatch.setattr(os, "execv", _boom)
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])  # must not raise
    assert "WARNING" in capsys.readouterr().err


def test_fail_open_unexpected_exception(central_store, execv_spy, tmp_path, monkeypatch, capsys):
    """Any unexpected failure inside the check degrades to current version."""
    monkeypatch.setattr(
        _reexec, "_read_pin", lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])  # must not raise
    assert "WARNING" in capsys.readouterr().err


def test_fail_open_symlink_escape_refused(central_store, execv_spy, tmp_path, capsys):
    """A versions/<pin> symlink resolving OUTSIDE the versions root is refused."""
    outside = tmp_path / "elsewhere"
    (outside / "vnx_cli").mkdir(parents=True)
    (outside / "vnx_cli" / "__init__.py").write_text("", encoding="utf-8")
    (central_store / "v1.2.0").symlink_to(outside)
    _pin(tmp_path, "v1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    assert "WARNING" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

def test_main_invokes_reexec_first(monkeypatch):
    """main() must run the pin re-exec check before argparse dispatch."""
    import vnx_cli.main as main_mod

    calls = []
    monkeypatch.setattr(
        "vnx_cli._reexec.maybe_reexec_pinned", lambda: calls.append("reexec")
    )
    monkeypatch.setattr(sys, "argv", ["vnx", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main()
    assert excinfo.value.code == 0
    assert calls == ["reexec"]
