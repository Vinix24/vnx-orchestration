#!/usr/bin/env python3
"""OI-941: STATE_DIR must resolve from cwd's .vnx-project-id, not module location.

Before the fix, ``open_items_manager.STATE_DIR`` was set at import time via
``ensure_env()`` which derives ``project_root`` from the module's own location
(the vnx-orchestration repo). This caused silent fallback to ``vnx-dev``
when running from a project directory that carries its own ``.vnx-project-id``
marker (e.g. mission-control, SEOcrawler, sales-copilot).

After the fix, ``STATE_DIR`` is resolved via ``vnx_paths.resolve_data_root(cwd)``
which reads ``.vnx-project-id`` from the current working directory and is
fail-closed (no silent ``vnx-dev`` fallback).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"


def _load_oim_in_cwd(cwd: Path) -> "module":
    """Import open_items_manager with *cwd* as the current working directory.

    Does NOT touch the environment — the caller must have already set up
    env vars via monkeypatch (setenv/delenv).  Uses a unique module name
    per test to avoid cross-test caching.
    """
    mod_name = f"open_items_manager_oi941_{cwd.name}"
    spec = importlib.util.spec_from_file_location(
        mod_name, SCRIPTS_DIR / "open_items_manager.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        del sys.modules[mod_name]
        raise
    return mod


# ---------------------------------------------------------------------------
# OI-941: STATE_DIR must honour the cwd project marker
# ---------------------------------------------------------------------------


def test_state_dir_resolves_from_cwd_project_id(tmp_path: Path, monkeypatch):
    """STATE_DIR resolves from cwd's .vnx-project-id, NOT from module location.

    The defect (OI-941): ``ensure_env()`` derives PROJECT_ROOT from the
    module's physical location (the vnx-orchestration repo), so
    VNX_STATE_DIR always resolves to ``~/.vnx-data/vnx-dev/state``
    regardless of CWD.  A call from mission-control silently writes
    open items into the vnx-dev ledger.

    After the fix, ``resolve_data_root(Path.cwd())`` reads the
    ``.vnx-project-id`` marker from CWD and resolves the state dir
    to the owning project.
    """
    # -- setup: a project directory with its own .vnx-project-id marker --
    project_dir = tmp_path / "test-project-oi941"
    project_dir.mkdir()
    (project_dir / ".vnx-project-id").write_text("test-project-oi941\n")

    # Pre-create a project-local .vnx-data/state so _resolve_state_root
    # branch 4 (existing dev checkout) returns it rather than falling
    # through to XDG.
    (project_dir / ".vnx-data" / "state").mkdir(parents=True)

    # Strip the conftest's module-scoped VNX_DATA_DIR_EXPLICIT / VNX_DATA_DIR
    # overrides so the resolution exercises the CWD-based path.
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.delenv("VNX_DATA_HOME", raising=False)
    monkeypatch.setenv("VNX_HOME", str(SCRIPTS_DIR.parent))
    monkeypatch.chdir(project_dir)

    mod = _load_oim_in_cwd(project_dir)

    expected = (project_dir / ".vnx-data" / "state").resolve()
    actual = mod.STATE_DIR.resolve()

    assert actual == expected, (
        f"OI-941 REGRESSION: STATE_DIR={actual}, expected={expected}\n"
        f"CWD={project_dir}\n"
        f".vnx-project-id={project_dir / '.vnx-project-id'}\n"
        f"STATE_DIR must resolve from cwd's .vnx-project-id marker, "
        f"not from the module's own repo location."
    )


def test_state_dir_not_vnx_dev_when_cwd_has_own_marker(
    tmp_path: Path, monkeypatch,
):
    """When cwd carries its own .vnx-project-id, STATE_DIR must NOT contain 'vnx-dev'.

    This is a regression-specific assertion: the defect triggered silent
    fallback to vnx-dev.  After the fix, a project with its own marker
    must never land in the vnx-dev state directory.
    """
    project_dir = tmp_path / "my-own-project"
    project_dir.mkdir()
    (project_dir / ".vnx-project-id").write_text("my-own-project\n")
    (project_dir / ".vnx-data" / "state").mkdir(parents=True)

    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.delenv("VNX_DATA_HOME", raising=False)
    monkeypatch.setenv("VNX_HOME", str(SCRIPTS_DIR.parent))
    monkeypatch.chdir(project_dir)

    mod = _load_oim_in_cwd(project_dir)

    state_dir_str = str(mod.STATE_DIR.resolve())
    assert "vnx-dev" not in state_dir_str, (
        f"OI-941 REGRESSION: STATE_DIR={state_dir_str} contains 'vnx-dev'. "
        f"CWD has .vnx-project-id=my-own-project. "
        f"STATE_DIR must NOT silently fall back to vnx-dev."
    )
    assert "my-own-project" in state_dir_str or str(project_dir) in state_dir_str, (
        f"OI-941: STATE_DIR={state_dir_str} should reflect the cwd project. "
        f"Expected path under {project_dir}."
    )


def test_state_dir_explicit_override_still_works(tmp_path: Path, monkeypatch):
    """VNX_DATA_DIR_EXPLICIT=1 + VNX_DATA_DIR still controls STATE_DIR.

    This ensures the fix doesn't break the existing test isolation pattern
    used by every other open_items_manager test.
    """
    project_dir = tmp_path / "explicit-project"
    project_dir.mkdir()
    (project_dir / ".vnx-project-id").write_text("explicit-project\n")

    explicit_data = tmp_path / "custom-data"
    (explicit_data / "state").mkdir(parents=True)

    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.delenv("VNX_DATA_HOME", raising=False)
    monkeypatch.setenv("VNX_HOME", str(SCRIPTS_DIR.parent))
    monkeypatch.setenv("VNX_DATA_DIR", str(explicit_data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.chdir(project_dir)

    mod = _load_oim_in_cwd(project_dir)

    expected = (explicit_data / "state").resolve()
    assert mod.STATE_DIR.resolve() == expected, (
        f"Explicit override broken: STATE_DIR={mod.STATE_DIR}, expected={expected}"
    )
