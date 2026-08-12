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
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
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


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """A home directory confined entirely to tmp_path, so pin-walk-up
    boundary tests never escape into the real filesystem (the walk climbs
    toward $HOME — see _reexec._find_pin_dir — and the real $HOME on the
    machine running these tests is not a hermetic sandbox)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


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
    assert args == [sys.executable, "-P", "-m", "vnx_cli.main", *argv]
    # Loop-guard armed + pinned install on PYTHONPATH for the exec'd process.
    assert os.environ[_reexec.REEXEC_ENV_FLAG] == "v1.2.0"
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(pinned)


def test_reexec_sets_safepath_against_cwd_shadow(central_store, execv_spy, tmp_path):
    """cwd-shadow hardening: `python -m` prepends cwd to sys.path ahead of
    PYTHONPATH, so a cwd-local `vnx_cli/` would shadow the pinned install.
    The re-exec must set PYTHONSAFEPATH=1 in the environment AND pass the
    explicit `-P` flag BEFORE `-m` so the pinned install always wins."""
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "v1.2.0")
    argv = ["--project-dir", str(tmp_path)]
    _reexec.maybe_reexec_pinned(argv)

    assert len(execv_spy) == 1
    _, args = execv_spy[0]
    assert os.environ["PYTHONSAFEPATH"] == "1"
    assert "-P" in args and "-m" in args
    assert args.index("-P") < args.index("-m")


def test_pin_without_v_resolves_v_prefixed_dir(central_store, execv_spy, tmp_path):
    """Pin '1.2.0' must find the 'v1.2.0' dir in the central store."""
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert len(execv_spy) == 1


def test_pin_not_satisfied_by_same_version_different_install(
    central_store, execv_spy, tmp_path, monkeypatch
):
    """OI-892: a rolling dir claiming the SAME VERSION must not impersonate the
    pinned install.

    The running engine root is ``versions/edge``, whose VERSION file says
    1.3.0 but which carries a different commit than the real
    ``versions/v1.3.0`` (the exact doubleganger measured during the v1.4.0
    cut). A pin on v1.3.0 must re-exec to ``versions/v1.3.0``, not trust
    edge's VERSION string. RED on the old code: it compared VERSION strings
    and returned early.
    """
    pinned = central_store / "v1.3.0"
    (pinned / "COMMIT_SHA").write_text("6129a327\n", encoding="utf-8")

    imposter = central_store / "edge"
    (imposter / "vnx_cli").mkdir(parents=True)
    (imposter / "vnx_cli" / "__init__.py").write_text("", encoding="utf-8")
    (imposter / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    (imposter / "VERSION").write_text("1.3.0\n", encoding="utf-8")  # claims the SAME version
    (imposter / "COMMIT_SHA").write_text("d74e0691\n", encoding="utf-8")

    monkeypatch.setattr(_engine, "engine_root", lambda: imposter)
    _pin(tmp_path, "v1.3.0")

    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])

    assert len(execv_spy) == 1
    _, args = execv_spy[0]
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(pinned)


def test_pin_v140_with_running_v141_reexecs(central_store, execv_spy, tmp_path):
    """OI-892/OI-914: a v1.4.0 pin with a v1.4.1 engine running must re-exec
    to v1.4.0 — the exact drift measured on all three consumers after the
    v1.4.1 rollout (`cat .vnx-version` said v1.4.0 while v1.4.1 loaded)."""
    pinned = _add_version(central_store, "v1.4.0", "1.4.0")
    _add_version(central_store, "v1.4.1", "1.4.1")
    _pin(tmp_path, "v1.4.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])

    assert len(execv_spy) == 1
    _, args = execv_spy[0]
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(pinned)


def test_running_edge_reports_dir_name_not_stale_version(central_store, tmp_path):
    """OI-892: a rolling dir (edge) whose VERSION lags is reported by its dir
    name, never by a stale VERSION — it cannot present itself as a released
    version."""
    edge = central_store / "edge"
    edge.mkdir()
    (edge / "VERSION").write_text("1.3.0\n", encoding="utf-8")
    assert _reexec._running_version(edge) == "edge"


def test_running_version_reads_version_file(central_store):
    """A released version dir is still reported by its VERSION file."""
    assert _reexec._running_version(central_store / "v1.3.0") == "1.3.0"


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


def test_fallback_warning_names_resolved_current_not_label(
    central_store, execv_spy, tmp_path, capsys
):
    """OI-1070: when the pin is not installed, the fallback warning must name the
    version the ``current`` symlink ACTUALLY resolves to, not a VERSION label.

    Setup: the running engine root is ``versions/v1.3.0`` (VERSION label
    ``1.3.0``), but ``current`` points at a DIFFERENT install ``versions/v1.4.4``
    (VERSION label ``1.4.4``). A pin on a non-existent ``v9.9.9`` must report the
    resolved ``current`` target (``1.4.4``), never the running engine's label
    (``1.3.0``). RED on the old code: it read the VERSION label of the running
    engine root and printed ``1.3.0``.
    """
    # A second installed version that ``current`` will point at.
    real_current = _add_version(central_store, "v1.4.4", "1.4.4")
    # ``current`` symlink resolves to v1.4.4 (different from the running
    # engine root v1.3.0 whose VERSION label is 1.3.0).
    store_root = central_store.parent
    (store_root / "current").symlink_to(real_current)

    _pin(tmp_path, "v9.9.9")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])

    assert execv_spy == []  # no re-exec: pin not installed, fell open
    err = capsys.readouterr().err
    assert "is not installed" in err
    assert "1.4.4" in err  # the resolved current target, not the label
    assert "1.3.0" not in err  # the stale running-engine label must NOT appear


def test_fallback_warning_unknown_when_no_current_symlink(
    central_store, execv_spy, tmp_path, capsys
):
    """No ``current`` symlink -> the warning names 'unknown', not a label."""
    _pin(tmp_path, "v9.9.9")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    err = capsys.readouterr().err
    assert "is not installed" in err
    assert "unknown" in err
    # The running engine's VERSION label (1.3.0) must not be reported as the
    # "current version" when there is no current symlink to resolve.
    assert "1.3.0" not in err


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
# Pin walk-up (OI-1170): the pin used to be looked up ONLY in the literal
# project_dir/cwd. A T0 terminal running from a nested submap (e.g.
# .claude/terminals/T0) would find nothing there and silently run whatever
# version happened to be `current`, disagreeing with `vnx --version` run
# from the project root. These five cases are each written to fail on the
# pre-fix code (see docstrings) and pass after the walk-up fix.
# ---------------------------------------------------------------------------

def test_pin_found_by_walking_up_from_submap(central_store, execv_spy, fake_home):
    """Case 1: pin lives at the project root; the command runs from a nested
    submap (mirrors a T0 terminal under .claude/terminals/T0). RED on the
    pre-fix code: `_read_pin` only checked the literal project_dir, found
    nothing in the submap, and returned early with zero re-exec."""
    pinned = _add_version(central_store, "v1.2.0", "1.2.0")
    project = fake_home / "project"
    _pin(project, "v1.2.0")
    submap = project / ".claude" / "terminals" / "T0"
    submap.mkdir(parents=True)

    _reexec.maybe_reexec_pinned(["--project-dir", str(submap)])

    assert len(execv_spy) == 1
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(pinned)


def test_project_dir_pin_wins_over_ancestor_pin(central_store, execv_spy, fake_home):
    """Case 2 (regression pin): an explicit --project-dir naming a directory
    that HAS its own pin must be honored as-is — an ancestor's different pin
    must never override the closer, explicitly-named one. Behavior must be
    unchanged by the walk-up fix whenever --project-dir already points
    straight at the pin."""
    _add_version(central_store, "v1.1.0", "1.1.0")
    inner = _add_version(central_store, "v1.2.0", "1.2.0")
    project = fake_home / "project"
    _pin(project, "v1.1.0")
    sub = project / "sub"
    _pin(sub, "v1.2.0")

    _reexec.maybe_reexec_pinned(["--project-dir", str(sub)])

    assert len(execv_spy) == 1
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(inner)


def test_no_pin_anywhere_within_boundary_no_reexec(central_store, execv_spy, fake_home):
    """Case 3 (regression pin): no .vnx-version anywhere from the submap up
    to the $HOME boundary -> no re-exec, same as the pre-walk-up behavior for
    a directory with no pin. Also proves the walk does not manufacture a
    match out of nothing once it is allowed to climb."""
    project = fake_home / "project"
    submap = project / ".claude" / "terminals" / "T0"
    submap.mkdir(parents=True)

    _reexec.maybe_reexec_pinned(["--project-dir", str(submap)])

    assert execv_spy == []


def test_dev_checkout_warns_on_pin_divergence(central_store, execv_spy, fake_home, capsys):
    """Case 4: a pin IS found (via walk-up) but the running install is a dev
    checkout (no .vnx-install-mode=central marker), so no re-exec can honor
    it and the running version differs from the pin. RED on the pre-fix
    code for two independent reasons: the walk-up did not exist (so the pin
    was never found from the submap at all), AND the dev-checkout branch
    printed nothing even when it did have a pin in hand — this was the one
    branch where 'pin found, running version diverges' produced zero
    output."""
    (central_store / "v1.3.0" / ".vnx-install-mode").unlink()
    project = fake_home / "project"
    _pin(project, "v1.2.0")
    submap = project / "sub"
    submap.mkdir()

    _reexec.maybe_reexec_pinned(["--project-dir", str(submap)])

    assert execv_spy == []
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "v1.2.0" in err
    assert str(project / ".vnx-version") in err


def test_malformed_pin_found_via_walkup_fails_open_with_warning(
    central_store, execv_spy, fake_home, capsys
):
    """Case 5: an unreadable/malformed pin keeps the existing fail-open
    behavior (no re-exec, no crash) plus its warning — now also reachable
    when the malformed pin is discovered via walk-up rather than sitting
    directly in the literal project_dir. RED on the pre-fix code: the
    submap-relative lookup found no file at all, so neither the fail-open
    path nor its warning ever ran."""
    project = fake_home / "project"
    _pin(project, "bad;rm")
    submap = project / ".claude" / "terminals" / "T0"
    submap.mkdir(parents=True)

    _reexec.maybe_reexec_pinned(["--project-dir", str(submap)])

    assert execv_spy == []
    assert "WARNING" in capsys.readouterr().err


def test_walkup_stops_before_home_directory(central_store, execv_spy, fake_home):
    """The walk must never consider $HOME itself (or above it) as a pin
    source — a pin sitting there would silently apply to every project on
    the machine. A pin placed AT fake_home is invisible to a project nested
    under it."""
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(fake_home, "v1.2.0")
    submap = fake_home / "project" / "sub"
    submap.mkdir(parents=True)

    _reexec.maybe_reexec_pinned(["--project-dir", str(submap)])

    assert execv_spy == []


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
