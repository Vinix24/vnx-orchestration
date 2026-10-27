#!/usr/bin/env python3
"""Tests for ``store:legacy-xdg``: pre-#1437 XDG stores named by ``vnx doctor``.

Before PR #1437 (2026-10-08) ``vnx init`` created a project's store under
``${XDG_DATA_HOME:-~/.local/share}/vnx/<project_id>``. No resolver reads that
location any more, so an upgraded operator keeps an abandoned copy with
nothing pointing at it. ``vnx doctor`` must name that copy (WARN) when it is
the project's only store, and stay quiet (PASS) when it is absent, is only an
empty skeleton, or already coexists with the central ``~/.vnx-data/<id>``
store.

Every test redirects ``HOME`` and ``XDG_DATA_HOME`` at tmp dirs and clears the
ambient VNX data-dir vars, so no test reads the operator's real legacy stores.
"""

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from vnx_cli.commands.doctor import PASS, WARN, vnx_doctor

PROJECT_ID = "legacy-proj"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Hermetic HOME + XDG_DATA_HOME, and no ambient VNX data-dir overrides."""
    home, xdg = tmp_path / "home", tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    for key in (
        "VNX_DATA_DIR",
        "VNX_DATA_DIR_EXPLICIT",
        "VNX_DATA_HOME",
        "VNX_STATE_DIR",
        "VNX_PROJECT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    return SimpleNamespace(home=home, xdg=xdg)


def _project(tmp_path: Path, project_id: str = PROJECT_ID) -> Path:
    project = tmp_path / "project"
    (project / ".vnx").mkdir(parents=True)
    (project / ".vnx-project-id").write_text(f"{project_id}\n", encoding="utf-8")
    return project


def _legacy(xdg: Path, project_id: str = PROJECT_ID, with_db: bool = True) -> Path:
    store = xdg / "vnx" / project_id
    (store / "state").mkdir(parents=True)
    if with_db:
        (store / "state" / "runtime_coordination.db").write_bytes(b"")
    return store


def _central(home: Path, project_id: str = PROJECT_ID) -> Path:
    central = home / ".vnx-data" / project_id
    (central / "state").mkdir(parents=True)
    return central


def _check(project_id: str, central: Path):
    from vnx_cli.commands.doctor import _check_legacy_store

    return _check_legacy_store(project_id, central)


def _probe():
    """Import the probe lazily: it does not exist on the pre-fix tree."""
    import legacy_store_path

    return legacy_store_path


class TestDoctorReportsLegacyStore:
    def test_legacy_store_without_central_warns_through_the_cli(self, env, tmp_path, capsys):
        project = _project(tmp_path)
        legacy = _legacy(env.xdg)

        vnx_doctor(argparse.Namespace(project_dir=str(project), strict=False, json=True))
        payload = json.loads(capsys.readouterr().out)
        checks = {check["name"]: check for check in payload["checks"]}

        assert "store:legacy-xdg" in checks  # absent on the pre-fix tree
        check = checks["store:legacy-xdg"]
        assert check["status"] == WARN
        assert payload["summary"]["warn"] >= 1
        assert str(legacy) in check["detail"]
        assert "NOT used" in check["detail"]
        assert "vnx migrate --relocate-legacy" in check["detail"]
        assert "will be available" in check["detail"]

    def test_legacy_and_central_store_pass(self, env):
        legacy = _legacy(env.xdg)
        central = _central(env.home)

        check = _check(PROJECT_ID, central)

        assert check.name == "store:legacy-xdg"
        assert check.status == PASS
        assert str(legacy) in check.detail
        assert str(central) in check.detail

    def test_no_legacy_store_passes(self, env):
        check = _check(PROJECT_ID, env.home / ".vnx-data" / PROJECT_ID)

        assert check.status == PASS
        assert PROJECT_ID in check.detail
        assert "no legacy XDG store" in check.detail

    def test_empty_skeleton_passes(self, env):
        skeleton = _legacy(env.xdg, with_db=False)

        check = _check(PROJECT_ID, env.home / ".vnx-data" / PROJECT_ID)

        assert check.status == PASS
        assert str(skeleton) in check.detail
        assert "not a store" in check.detail

    def test_legacy_store_of_another_project_is_ignored(self, env):
        _legacy(env.xdg, "other-proj")

        check = _check(PROJECT_ID, env.home / ".vnx-data" / PROJECT_ID)

        assert check.status == PASS
        assert "no legacy XDG store" in check.detail
        assert "other-proj" not in check.detail


class TestLegacyStoreProbe:
    def test_xdg_data_home_is_honoured(self, env):
        store = _legacy(env.xdg)

        assert _probe().legacy_data_home() == env.xdg / "vnx"
        assert _probe().legacy_store_for(PROJECT_ID) == store

    def test_local_share_is_the_fallback(self, env, monkeypatch):
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        store = env.home / ".local" / "share" / "vnx" / PROJECT_ID
        (store / "state").mkdir(parents=True)
        (store / "state" / "runtime_coordination.db").write_bytes(b"")

        assert _probe().legacy_data_home() == env.home / ".local" / "share" / "vnx"
        assert _probe().legacy_store_for(PROJECT_ID) == store

    def test_iter_lists_exactly_the_fixture_dirs(self, env):
        zeta = _legacy(env.xdg, "zeta-proj")
        (zeta / "logs").mkdir()
        (zeta / "logs" / "a.log").write_bytes(b"x" * 7)
        _legacy(env.xdg, "alpha-proj", with_db=False)
        # A plain file in the data home is not a store dir and must not be listed.
        (env.xdg / "vnx" / "stray-file.txt").write_bytes(b"nope")

        stores = _probe().iter_legacy_stores()

        assert [s["project_id"] for s in stores] == ["alpha-proj", "zeta-proj"]
        assert stores[1]["path"] == zeta
        assert stores[1]["has_db"] is True
        assert stores[1]["size_bytes"] == 7
        assert isinstance(stores[1]["mtime"], float)
        assert stores[0]["has_db"] is False
        assert stores[0]["size_bytes"] == 0

    def test_skeleton_and_absent_home_yield_no_store(self, env, monkeypatch):
        _legacy(env.xdg, with_db=False)
        assert _probe().legacy_store_for(PROJECT_ID) is None

        monkeypatch.setenv("XDG_DATA_HOME", str(env.xdg / "absent"))
        assert _probe().iter_legacy_stores() == []

    @pytest.mark.parametrize("bad_id", ["", "../escape", "Upper", "a/b"])
    def test_unusable_project_ids_are_refused(self, env, bad_id):
        assert _probe().legacy_store_for(bad_id) is None
