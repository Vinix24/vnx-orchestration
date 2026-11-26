"""Regression tests for OI-1342: _get_project_id() unification.

Both pr_queue_manager._get_project_id() and
headless_dispatch_writer._get_project_id() must delegate to
project_scope.resolve_stamp_project_id() so dispatch stamps are always
consistent regardless of which stamper is called.

Two scenarios:
  1. VNX_PROJECT_ID is set — all three return the explicit value.
  2. VNX_PROJECT_ID is unset — dispatch stampers refuse without a source.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("restore_project_scope")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
for _p in (SCRIPTS_DIR, SCRIPTS_LIB):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _reload_modules():
    """Reload all three modules so env-var changes take effect."""
    import project_scope
    import headless_dispatch_writer
    import pr_queue_manager

    importlib.reload(headless_dispatch_writer)
    importlib.reload(pr_queue_manager)

    return project_scope, headless_dispatch_writer, pr_queue_manager


ENV_KEY = "VNX_PROJECT_ID"


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    """Ensure VNX_PROJECT_ID is restored after each test."""
    monkeypatch.chdir(tmp_path)
    saved = os.environ.get(ENV_KEY)
    yield
    if saved is None:
        os.environ.pop(ENV_KEY, None)
    else:
        os.environ[ENV_KEY] = saved


class TestProjectIdWithEnvSet:
    """When VNX_PROJECT_ID is set, all stampers must return that exact value."""

    def test_project_scope_returns_env_value(self):
        os.environ[ENV_KEY] = "my-project"
        ps, _, _ = _reload_modules()
        assert ps.current_project_id() == "my-project"

    def test_headless_dispatch_writer_matches_project_scope(self):
        os.environ[ENV_KEY] = "my-project"
        ps, hdw, _ = _reload_modules()
        assert hdw._get_project_id() == ps.current_project_id()

    def test_pr_queue_manager_matches_project_scope(self):
        os.environ[ENV_KEY] = "my-project"
        ps, _, pqm = _reload_modules()
        assert pqm._get_project_id() == ps.current_project_id()

    def test_all_three_stampers_agree(self):
        os.environ[ENV_KEY] = "vnx-seo-v2"
        ps, hdw, pqm = _reload_modules()
        scope_val = ps.current_project_id()
        assert hdw._get_project_id() == scope_val
        assert pqm._get_project_id() == scope_val


class TestProjectIdWithEnvUnset:
    """Dispatch stampers refuse; the legacy scope helper changes in AG-1z."""

    def test_project_scope_returns_default(self):
        os.environ.pop(ENV_KEY, None)
        ps, _, _ = _reload_modules()
        assert ps.current_project_id() == "vnx-dev"

    def test_headless_dispatch_writer_matches_project_scope(self):
        os.environ.pop(ENV_KEY, None)
        ps, hdw, _ = _reload_modules()
        with pytest.raises(ps.TenantUnresolved, match="vnx init"):
            hdw._get_project_id()

    def test_pr_queue_manager_matches_project_scope(self):
        os.environ.pop(ENV_KEY, None)
        ps, _, pqm = _reload_modules()
        with pytest.raises(ps.TenantUnresolved, match="vnx init"):
            pqm._get_project_id()

    def test_all_three_stampers_agree_on_default(self):
        os.environ.pop(ENV_KEY, None)
        ps, hdw, pqm = _reload_modules()
        scope_val = ps.current_project_id()
        assert scope_val == "vnx-dev"
        for stamper in (hdw, pqm):
            with pytest.raises(ps.TenantUnresolved, match="vnx init"):
                stamper._get_project_id()
