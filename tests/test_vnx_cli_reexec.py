#!/usr/bin/env python3
"""Tests for vnx_cli/_reexec.py — pip-CLI honors .vnx-version via re-exec.

Design-track ``pip-cli-honor-pin-via-reexec``. All tests run against a FAKE
central store under tmp_path; the operator's real ~/.vnx-system install and
.vnx-data runtime state are never touched.

Covers, in order: the walk-up pin/freeze lookup (OI-1170), floor semantics
(OI-1171 — the pin is a minimum, not an exact freeze), the
``.vnx-version-freeze`` escape hatch, and (in the last section) the
CLI-startup migration-staleness check added to ``vnx_cli/main.py`` alongside
it, reusing ``scripts/ledger_health.check_migration_staleness``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path so vnx_cli is importable without install
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from vnx_cli import _engine
from vnx_cli import _reexec
import ledger_health  # noqa: E402 — scripts/ inserted above


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


def _freeze(project_dir: Path, value: str) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".vnx-version-freeze").write_text(f"{value}\n", encoding="utf-8")
    return project_dir


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

def test_no_pin_no_freeze_no_reexec(central_store, execv_spy, tmp_path):
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_fail_open_empty_pin_file(central_store, execv_spy, tmp_path):
    tmp_path.joinpath(".vnx-version").write_text("\n", encoding="utf-8")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


@pytest.mark.parametrize("bad", ["../evil", "bad;rm", "a b", "v1.2.0/..", ".."])
def test_fail_open_malformed_pin(central_store, execv_spy, tmp_path, capsys, bad):
    _pin(tmp_path, bad)
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    assert "WARNING" in capsys.readouterr().err


def test_pin_not_semver_warns_and_noop(central_store, execv_spy, tmp_path, capsys):
    """A pin that passes the filename-safety grammar but isn't X.Y.Z (e.g. a
    typo'd label) cannot be enforced as a floor — warn and run current."""
    _pin(tmp_path, "banana")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "not a recognized" in err


# ---------------------------------------------------------------------------
# Floor semantics: current already satisfies the pin -> run current
# ---------------------------------------------------------------------------

def test_pin_below_running_no_reexec(central_store, execv_spy, tmp_path):
    """The common post-dispatch case: a project pinned to an older floor is
    pulled FORWARD onto whatever is current, not frozen on the floor."""
    _pin(tmp_path, "1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_pin_equals_running_no_reexec(central_store, execv_spy, tmp_path):
    _pin(tmp_path, "1.3.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_pin_equals_running_with_decorative_v_no_reexec(central_store, execv_spy, tmp_path):
    _pin(tmp_path, "v1.3.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


# ---------------------------------------------------------------------------
# Floor semantics: current is BELOW the pin -> re-exec UP if installed
# ---------------------------------------------------------------------------

def test_pin_above_running_reexecs_to_pinned_when_installed(central_store, execv_spy, tmp_path):
    pinned = _add_version(central_store, "v1.4.0", "1.4.0")
    _pin(tmp_path, "1.4.0")
    argv = ["status", "--project-dir", str(tmp_path), "--json"]
    _reexec.maybe_reexec_pinned(argv)

    assert len(execv_spy) == 1
    python, args = execv_spy[0]
    assert python == sys.executable
    assert args == [sys.executable, "-P", "-m", "vnx_cli.main", *argv]
    assert os.environ[_reexec.REEXEC_ENV_FLAG] == "1.4.0"
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(pinned)


def test_pin_above_running_v_prefix_resolves(central_store, execv_spy, tmp_path):
    _add_version(central_store, "v1.4.0", "1.4.0")
    _pin(tmp_path, "v1.4.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert len(execv_spy) == 1


def test_reexec_sets_safepath_against_cwd_shadow(central_store, execv_spy, tmp_path):
    """cwd-shadow hardening: `python -m` prepends cwd to sys.path ahead of
    PYTHONPATH, so a cwd-local `vnx_cli/` would shadow the pinned install.
    The re-exec must set PYTHONSAFEPATH=1 in the environment AND pass the
    explicit `-P` flag BEFORE `-m` so the pinned install always wins."""
    _add_version(central_store, "v1.4.0", "1.4.0")
    _pin(tmp_path, "1.4.0")
    argv = ["--project-dir", str(tmp_path)]
    _reexec.maybe_reexec_pinned(argv)

    assert len(execv_spy) == 1
    _, args = execv_spy[0]
    assert os.environ["PYTHONSAFEPATH"] == "1"
    assert "-P" in args and "-m" in args
    assert args.index("-P") < args.index("-m")


def test_existing_pythonpath_preserved_after_pinned_paths(central_store, execv_spy, tmp_path, monkeypatch):
    pinned = _add_version(central_store, "v1.4.0", "1.4.0")
    _pin(tmp_path, "1.4.0")
    monkeypatch.setenv("PYTHONPATH", "/opt/custom")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    parts = os.environ["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(pinned)
    assert parts[-1] == "/opt/custom"


def test_loop_guard_blocks_second_reexec_on_floor_violation(central_store, execv_spy, tmp_path, monkeypatch):
    """VNX_PIN_REEXECED already equal to the floor pin -> never exec again,
    even though the running version still sits below it (off-by-a-hair
    detection surviving into the re-exec'd process)."""
    _add_version(central_store, "v1.4.0", "1.4.0")
    _pin(tmp_path, "1.4.0")
    monkeypatch.setenv(_reexec.REEXEC_ENV_FLAG, "1.4.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


# ---------------------------------------------------------------------------
# Floor semantics: unmet, and NOTHING installed satisfies it -> loud failure
# ---------------------------------------------------------------------------

def test_pin_above_running_not_installed_raises_systemexit(central_store, execv_spy, tmp_path):
    _pin(tmp_path, "9.9.9")
    with pytest.raises(SystemExit) as excinfo:
        _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    msg = str(excinfo.value)
    assert "9.9.9" in msg
    assert "1.3.0" in msg  # the running version that failed to satisfy the floor
    assert "ERROR" in msg


def test_pin_above_running_not_installed_message_names_remediation(central_store, tmp_path):
    _pin(tmp_path, "9.9.9")
    with pytest.raises(SystemExit) as excinfo:
        _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    msg = str(excinfo.value)
    assert "vnx release publish" in msg
    assert ".vnx-version-freeze" in msg  # the escape hatch is named as an alternative


# ---------------------------------------------------------------------------
# Dev checkouts: floor cannot be enforced by re-exec, only warned about
# ---------------------------------------------------------------------------

def test_dev_checkout_floor_unmet_warns_no_exit(central_store, execv_spy, tmp_path, capsys):
    (central_store / "v1.3.0" / ".vnx-install-mode").unlink()
    _pin(tmp_path, "9.9.9")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])  # must not raise
    assert execv_spy == []
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "dev checkout" in err
    assert "floor" in err


def test_dev_checkout_floor_satisfied_no_warning(central_store, execv_spy, tmp_path, capsys):
    (central_store / "v1.3.0" / ".vnx-install-mode").unlink()
    _pin(tmp_path, "1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Rolling installs (edge/latest) are exempt from floor enforcement
# ---------------------------------------------------------------------------

def test_rolling_dir_exempt_from_floor(central_store, execv_spy, tmp_path, monkeypatch):
    """A running ``edge`` install cannot be numerically compared to a floor
    (its identity is a dir name, not an X.Y.Z) — exempt, not a violation,
    even though its stale VERSION label would fail a naive comparison."""
    edge = central_store / "edge"
    (edge / "vnx_cli").mkdir(parents=True)
    (edge / "vnx_cli" / "__init__.py").write_text("", encoding="utf-8")
    (edge / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    (edge / "VERSION").write_text("1.3.0\n", encoding="utf-8")
    monkeypatch.setattr(_engine, "engine_root", lambda: edge)

    _pin(tmp_path, "9.9.9")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])  # must not raise
    assert execv_spy == []


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


# ---------------------------------------------------------------------------
# _parse_version
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.4.5", (1, 4, 5, 0)),
        ("v1.4.5", (1, 4, 5, 0)),
        ("1.4.5-rc1", (1, 4, 5, -1)),
        ("10.20.30", (10, 20, 30, 0)),
    ],
)
def test_parse_version_recognized(value, expected):
    assert _reexec._parse_version(value) == expected


@pytest.mark.parametrize("value", ["edge", "latest", "banana", "1.4", "1.4.x"])
def test_parse_version_unrecognized(value):
    assert _reexec._parse_version(value) is None


def test_parse_version_prerelease_sorts_below_final():
    assert _reexec._parse_version("1.4.0-rc1") < _reexec._parse_version("1.4.0")


def test_parse_version_ordering_is_numeric_not_lexicographic():
    assert _reexec._parse_version("1.10.0") > _reexec._parse_version("1.9.0")


# ---------------------------------------------------------------------------
# Escape hatch: .vnx-version-freeze (old exact-pin behavior, fully fail-open)
# ---------------------------------------------------------------------------

def test_freeze_reexecs_to_exact_older_version(central_store, execv_spy, tmp_path):
    """The escape hatch can go DOWN even though current already satisfies
    any floor — proving it bypasses floor semantics entirely."""
    frozen = _add_version(central_store, "v1.2.0", "1.2.0")
    _freeze(tmp_path, "1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])

    assert len(execv_spy) == 1
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(frozen)
    assert os.environ[_reexec.REEXEC_ENV_FLAG] == "1.2.0"


def test_freeze_takes_priority_over_satisfied_pin(central_store, execv_spy, tmp_path):
    """A pin that WOULD be satisfied by current (no-op on its own) is
    overridden entirely when a freeze is also present."""
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "1.0.0")  # satisfied by current on its own — would no-op
    _freeze(tmp_path, "1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert len(execv_spy) == 1


def test_freeze_equals_running_no_reexec(central_store, execv_spy, tmp_path):
    _freeze(tmp_path, "1.3.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_freeze_not_installed_fails_open_not_systemexit(central_store, execv_spy, tmp_path, capsys):
    """Unlike an unmet floor, an unmet freeze is fully fail-open — it is a
    deliberate, already-explicit operator override, not the default
    guarantee, so it degrades to running current instead of blocking."""
    _freeze(tmp_path, "9.9.9")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])  # must not raise
    assert execv_spy == []
    assert "WARNING" in capsys.readouterr().err


def test_freeze_dev_checkout_diverged_warns(central_store, execv_spy, tmp_path, capsys):
    (central_store / "v1.3.0" / ".vnx-install-mode").unlink()
    _add_version(central_store, "v1.2.0", "1.2.0")
    _freeze(tmp_path, "1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "freeze" in err


def test_freeze_loop_guard_blocks_second_reexec(central_store, execv_spy, tmp_path, monkeypatch):
    _add_version(central_store, "v1.2.0", "1.2.0")
    _freeze(tmp_path, "1.2.0")
    monkeypatch.setenv(_reexec.REEXEC_ENV_FLAG, "1.2.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []


def test_malformed_freeze_falls_through_to_pin(central_store, execv_spy, tmp_path, capsys):
    """A malformed freeze is treated as absent (warn + continue), not as a
    reason to skip the floor pin entirely."""
    _freeze(tmp_path, "bad;rm")
    _pin(tmp_path, "1.2.0")  # satisfied by current -> no-op
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])
    assert execv_spy == []
    assert "WARNING" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Walk-up: nearest ancestor holding .vnx-version / .vnx-version-freeze wins
# ---------------------------------------------------------------------------

def test_pin_found_in_parent_directory(central_store, execv_spy, tmp_path):
    _add_version(central_store, "v1.4.0", "1.4.0")
    _pin(tmp_path, "1.4.0")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    _reexec.maybe_reexec_pinned(["--project-dir", str(nested)])
    assert len(execv_spy) == 1


def test_freeze_found_in_parent_directory(central_store, execv_spy, tmp_path):
    frozen = _add_version(central_store, "v1.2.0", "1.2.0")
    _freeze(tmp_path, "1.2.0")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    _reexec.maybe_reexec_pinned(["--project-dir", str(nested)])
    assert len(execv_spy) == 1
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(frozen)


def test_nearest_ancestor_pin_wins_over_farther_one(central_store, execv_spy, tmp_path):
    _add_version(central_store, "v1.4.0", "1.4.0")
    _add_version(central_store, "v1.2.0", "1.2.0")
    _pin(tmp_path, "1.4.0")  # farther ancestor
    nested = tmp_path / "a"
    nested.mkdir()
    _pin(nested, "1.2.0")  # nearest ancestor — must win
    deeper = nested / "b"
    deeper.mkdir()
    _reexec.maybe_reexec_pinned(["--project-dir", str(deeper)])
    assert execv_spy == []  # 1.2.0 satisfied by running 1.3.0 — the nearer pin was honored


def test_walk_bounded_at_home_directory(central_store, execv_spy, tmp_path, monkeypatch):
    """A pin file living AT the resolved home directory is never picked up
    for a search starting below it — the walk is bounded exclusive of home,
    so a stray pin above a project can never silently version-pin every
    project a user happens to run vnx from."""
    fake_home = tmp_path / "home"
    project = fake_home / "projects" / "myproj"
    project.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    _pin(fake_home, "9.9.9")  # would be a floor violation if honored
    _reexec.maybe_reexec_pinned(["--project-dir", str(project)])  # must not raise
    assert execv_spy == []


def test_walk_finds_pin_below_home_boundary(central_store, execv_spy, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    projects = fake_home / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    _add_version(central_store, "v1.4.0", "1.4.0")
    _pin(projects, "1.4.0")  # below home — must be found
    nested = projects / "myproj"
    nested.mkdir()
    _reexec.maybe_reexec_pinned(["--project-dir", str(nested)])
    assert len(execv_spy) == 1


# ---------------------------------------------------------------------------
# Fail-open cases carried over from the exact-pin era, re-verified under
# floor semantics (pin set ABOVE running so the floor path is actually
# exercised instead of short-circuiting on "already satisfied")
# ---------------------------------------------------------------------------

def test_fail_open_pinned_dir_missing_vnx_cli(central_store, execv_spy, tmp_path, capsys):
    """A versions/<pin> dir without a vnx_cli package is not exec-able —
    this is a found-but-BROKEN install, a different (softer) failure class
    than a genuinely absent one, so it still fails open rather than exiting."""
    broken = central_store / "v1.4.0"
    broken.mkdir(parents=True)
    (broken / "VERSION").write_text("1.4.0\n", encoding="utf-8")
    _pin(tmp_path, "1.4.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])  # must not raise
    assert execv_spy == []
    assert "WARNING" in capsys.readouterr().err


def test_fail_open_execv_oserror(central_store, tmp_path, monkeypatch, capsys):
    _add_version(central_store, "v1.4.0", "1.4.0")
    _pin(tmp_path, "1.4.0")

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
    """A versions/<pin> symlink resolving OUTSIDE the versions root is
    refused — a FOUND-but-refused install, not a genuinely absent one, so
    this must fail open (warn + continue), never the loud SystemExit an
    absent floor version gets."""
    outside = tmp_path / "elsewhere"
    (outside / "vnx_cli").mkdir(parents=True)
    (outside / "vnx_cli" / "__init__.py").write_text("", encoding="utf-8")
    (central_store / "v1.4.0").symlink_to(outside)
    _pin(tmp_path, "1.4.0")
    _reexec.maybe_reexec_pinned(["--project-dir", str(tmp_path)])  # must not raise
    assert execv_spy == []
    assert "WARNING" in capsys.readouterr().err


def test_project_dir_equals_form_honored(central_store, execv_spy, tmp_path):
    """--project-dir=DIR form must be picked up from argv."""
    _add_version(central_store, "v1.4.0", "1.4.0")
    _pin(tmp_path, "1.4.0")
    _reexec.maybe_reexec_pinned([f"--project-dir={tmp_path}"])
    assert len(execv_spy) == 1


def test_cwd_used_when_no_project_dir_arg(central_store, execv_spy, tmp_path, monkeypatch):
    _add_version(central_store, "v1.4.0", "1.4.0")
    _pin(tmp_path, "1.4.0")
    monkeypatch.chdir(tmp_path)
    _reexec.maybe_reexec_pinned([])
    assert len(execv_spy) == 1


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

def test_main_invokes_reexec_before_schema_check_before_dispatch(monkeypatch):
    """main() must run the pin re-exec check, then the schema-staleness
    check, before argparse dispatch — in that order."""
    import vnx_cli.main as main_mod

    calls = []
    monkeypatch.setattr(
        "vnx_cli._reexec.maybe_reexec_pinned", lambda: calls.append("reexec")
    )
    monkeypatch.setattr(
        main_mod, "_check_migration_staleness_startup", lambda: calls.append("schema")
    )
    monkeypatch.setattr(sys, "argv", ["vnx", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main()
    assert excinfo.value.code == 0
    assert calls == ["reexec", "schema"]


# ---------------------------------------------------------------------------
# vnx_cli/main.py::_check_migration_staleness_startup — Part 2
# ---------------------------------------------------------------------------

@pytest.fixture()
def schema_check(monkeypatch, tmp_path):
    """Route the schema-staleness check at a fake data root without touching
    real state, and capture what ledger_health.check_migration_staleness was
    asked (so a test can stub its return value)."""
    import vnx_cli.main as main_mod

    data_root = tmp_path / "data-root"
    monkeypatch.setattr(_engine, "resolve_data_root", lambda project_dir: data_root)
    return main_mod, data_root


def _stub_staleness(monkeypatch, result: dict) -> None:
    monkeypatch.setattr(ledger_health, "check_migration_staleness", lambda state_dir: result)


def test_schema_check_noop_when_db_absent(schema_check, monkeypatch, capsys):
    main_mod, _ = schema_check
    _stub_staleness(monkeypatch, {"status": ledger_health.STATUS_OK, "db_exists": False})
    main_mod._check_migration_staleness_startup([])  # must not raise
    assert capsys.readouterr().err == ""


def test_schema_check_noop_when_skipped_unverified(schema_check, monkeypatch, capsys):
    main_mod, _ = schema_check
    _stub_staleness(
        monkeypatch,
        {"status": ledger_health.SKIPPED_UNVERIFIED, "db_exists": True, "reason": "corrupt"},
    )
    main_mod._check_migration_staleness_startup([])  # must not raise
    assert capsys.readouterr().err == ""


def test_schema_check_noop_when_in_sync(schema_check, monkeypatch, capsys):
    main_mod, _ = schema_check
    _stub_staleness(
        monkeypatch,
        {
            "status": ledger_health.STATUS_OK,
            "db_exists": True,
            "current_user_version": 32,
            "highest_available_migration": 32,
        },
    )
    main_mod._check_migration_staleness_startup([])
    assert capsys.readouterr().err == ""


def test_schema_check_warns_when_behind(schema_check, monkeypatch, capsys):
    main_mod, data_root = schema_check
    _stub_staleness(
        monkeypatch,
        {
            "status": ledger_health.STATUS_FINDING,
            "db_exists": True,
            "current_user_version": 5,
            "highest_available_migration": 8,
        },
    )
    main_mod._check_migration_staleness_startup([])  # must not raise
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "vnx migrate" in err
    assert "3 migration" in err
    assert str(data_root / "state" / ledger_health.RUNTIME_DB_NAME) in err


def test_schema_check_raises_when_ahead(schema_check, monkeypatch):
    main_mod, _ = schema_check
    _stub_staleness(
        monkeypatch,
        {
            "status": ledger_health.STATUS_OK,
            "db_exists": True,
            "current_user_version": 10,
            "highest_available_migration": 8,
        },
    )
    with pytest.raises(SystemExit) as excinfo:
        main_mod._check_migration_staleness_startup([])
    msg = str(excinfo.value)
    assert "ERROR" in msg
    assert "OLDER" in msg
    assert "10" in msg and "0008" in msg


def test_schema_check_fail_open_on_resolve_exception(monkeypatch, capsys):
    import vnx_cli.main as main_mod

    def _boom(project_dir):
        raise RuntimeError("cannot resolve")

    monkeypatch.setattr(_engine, "resolve_data_root", _boom)
    main_mod._check_migration_staleness_startup([])  # must not raise
    assert capsys.readouterr().err == ""


def test_schema_check_fail_open_on_checker_exception(schema_check, monkeypatch, capsys):
    main_mod, _ = schema_check

    def _boom(state_dir):
        raise RuntimeError("db locked")

    monkeypatch.setattr(ledger_health, "check_migration_staleness", _boom)
    main_mod._check_migration_staleness_startup([])  # must not raise
    assert capsys.readouterr().err == ""
