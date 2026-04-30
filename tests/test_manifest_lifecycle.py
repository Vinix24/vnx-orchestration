#!/usr/bin/env python3
"""CFX-7: subprocess manifest lifecycle active → completed/dead_letter cleanup.

After ``_promote_manifest`` moves the manifest out of
``dispatches/active/<id>/``, the originating directory must be removed so
``check_active_drain.py`` does not see it as an in-flight dispatch.  These
tests pin down the contract:

    A. successful dispatch → active/<id>/ removed, completed/<id>/manifest.json
       written, dispatches/active/ left empty
    B. failed dispatch (stage="dead_letter") → active/<id>/ removed,
       completed/<id>/ NEVER created, dead_letter/<id>/manifest.json written
    C. cleanup_worker_exit happy path leaves no stragglers in active/
    D. _safe_remove_active_dir refuses paths outside dispatches/active/
       (symlink, wrong parent, non-directory) — never recursively deletes
    E. re-running _promote_manifest on an already-cleaned dispatch is a no-op
       and leaves no orphans

The check_active_drain.py janitor remains a backstop for paths that do not
flow through _promote_manifest; one test asserts the happy-path leaves it
with zero orphans to drain.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS / "lib"
for _p in (str(SCRIPTS), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import subprocess_dispatch as sd  # noqa: E402

from check_active_drain import drain_active  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seed_active_dispatch(
    data_dir: Path,
    dispatch_id: str,
    *,
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Create active/<dispatch_id>/manifest.json (+ optional sibling files).

    Returns the active/<id>/ directory.
    """
    with patch.dict(os.environ, {"VNX_DATA_DIR": str(data_dir)}):
        manifest_path = sd._write_manifest(
            dispatch_id=dispatch_id,
            terminal_id="T1",
            model="sonnet",
            role="backend-developer",
            instruction="cfx-7 lifecycle test",
            commit_hash_before="0" * 16,
            branch="fix/cfx-7-manifest-lifecycle",
        )
    assert manifest_path is not None, "fixture: _write_manifest must succeed"
    active_dir = Path(manifest_path).parent

    if extra_files:
        for name, body in extra_files.items():
            (active_dir / name).write_text(body, encoding="utf-8")

    return active_dir


# ---------------------------------------------------------------------------
# Case A — happy path: successful dispatch removes active/<id>/
# ---------------------------------------------------------------------------

class TestSuccessfulPromotion:
    def test_active_dir_removed_after_completed_promotion(self, tmp_path: Path) -> None:
        dispatch_id = "20260430-cfx-7-success-A"
        active_dir = _seed_active_dispatch(tmp_path, dispatch_id)
        assert active_dir.exists()

        with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
            completed_path = sd._promote_manifest(dispatch_id, stage="completed")

        assert completed_path is not None
        assert Path(completed_path).exists()
        assert "completed" in str(completed_path)
        # The originating active dir is gone — not just empty, *gone*.
        assert not active_dir.exists(), "active/<id>/ must be removed after promotion"

    def test_active_dir_removed_even_with_sibling_files(self, tmp_path: Path) -> None:
        """Other components (vnx_starter, report_assembler) drop bundle.json
        and dispatch.md alongside manifest.json.  rmdir() cannot remove a
        non-empty directory; rmtree() must.
        """
        dispatch_id = "20260430-cfx-7-success-B"
        active_dir = _seed_active_dispatch(
            tmp_path,
            dispatch_id,
            extra_files={
                "bundle.json": "{}",
                "dispatch.md": "# CFX-7 sibling\n",
                "scratch.log": "noise\n",
            },
        )
        assert active_dir.exists()
        assert (active_dir / "bundle.json").exists()

        with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
            completed_path = sd._promote_manifest(dispatch_id, stage="completed")

        assert completed_path is not None
        assert not active_dir.exists(), "rmtree must purge sibling files too"

    def test_happy_path_leaves_zero_orphans_for_drain_janitor(
        self, tmp_path: Path
    ) -> None:
        """check_active_drain.py is a backstop, not the primary cleanup.
        The happy path must leave it with nothing to drain.
        """
        # Lay out the full .vnx-data skeleton drain_active() expects.
        data_dir = tmp_path / ".vnx-data"
        (data_dir / "dispatches" / "active").mkdir(parents=True)
        (data_dir / "dispatches" / "completed").mkdir(parents=True)
        (data_dir / "dispatches" / "dead_letter").mkdir(parents=True)
        (data_dir / "receipts" / "processed").mkdir(parents=True)

        dispatch_id = "20260430-cfx-7-no-orphans"
        _seed_active_dispatch(data_dir, dispatch_id)

        with patch.dict(os.environ, {"VNX_DATA_DIR": str(data_dir)}):
            sd._promote_manifest(dispatch_id, stage="completed")

        # The janitor sweeps "old" dispatches; the happy path should leave
        # nothing here, regardless of age threshold.
        results = drain_active(
            data_dir=data_dir,
            older_than_hours=0.0,  # treat anything as old
            dry_run=True,
        )
        assert results == [], (
            "happy-path promotion must leave check_active_drain with zero entries; "
            f"found {results!r}"
        )


# ---------------------------------------------------------------------------
# Case B — failed dispatch: dead_letter, no completed/, no orphan
# ---------------------------------------------------------------------------

class TestFailedPromotion:
    def test_dead_letter_path_creates_no_completed_record(self, tmp_path: Path) -> None:
        dispatch_id = "20260430-cfx-7-failed-B"
        active_dir = _seed_active_dispatch(tmp_path, dispatch_id)

        with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
            dead_path = sd._promote_manifest(dispatch_id, stage="dead_letter")

        assert dead_path is not None
        dead_file = Path(dead_path)
        assert dead_file.exists()
        assert "dead_letter" in str(dead_file)

        completed_dir = tmp_path / "dispatches" / "completed" / dispatch_id
        assert not completed_dir.exists(), (
            "failed dispatches must never leave a completed/ artifact"
        )

        assert not active_dir.exists(), "active/<id>/ must also be cleaned for failures"

    def test_dead_letter_purges_sibling_files(self, tmp_path: Path) -> None:
        dispatch_id = "20260430-cfx-7-failed-siblings"
        active_dir = _seed_active_dispatch(
            tmp_path,
            dispatch_id,
            extra_files={"bundle.json": "{}", "dispatch.md": "# fail\n"},
        )

        with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
            sd._promote_manifest(dispatch_id, stage="dead_letter")

        assert not active_dir.exists()


# ---------------------------------------------------------------------------
# Case C — cleanup_worker_exit handles both paths
# ---------------------------------------------------------------------------

class TestCleanupWorkerExitIntegration:
    """cleanup_worker_exit() moves the *dispatch markdown file* (active/foo.md →
    completed/foo.md or rejected/<reason>/foo.md).  _promote_manifest() handles
    the *manifest dir* (active/<id>/manifest.json).  Both paths must converge
    so a successful subprocess dispatch ends with nothing in active/.
    """

    def _stage_dispatch_file(self, data_dir: Path, dispatch_id: str) -> Path:
        active_files = data_dir / "dispatches" / "active"
        active_files.mkdir(parents=True, exist_ok=True)
        path = active_files / f"{dispatch_id}.md"
        path.write_text(f"# {dispatch_id}\n", encoding="utf-8")
        return path

    def test_success_drains_both_manifest_dir_and_dispatch_file(
        self, tmp_path: Path
    ) -> None:
        from cleanup_worker_exit import cleanup_worker_exit

        data_dir = tmp_path / ".vnx-data"
        (data_dir / "dispatches" / "active").mkdir(parents=True)
        (data_dir / "dispatches" / "completed").mkdir(parents=True)
        (data_dir / "state").mkdir(parents=True)

        dispatch_id = "20260430-cfx-7-cleanup-success"
        # Manifest dir (subprocess path)
        active_manifest_dir = _seed_active_dispatch(data_dir, dispatch_id)
        # Markdown dispatch file (interactive path)
        dispatch_file = self._stage_dispatch_file(data_dir, dispatch_id)

        # Subprocess path runs _promote_manifest directly.
        with patch.dict(os.environ, {"VNX_DATA_DIR": str(data_dir)}):
            sd._promote_manifest(dispatch_id, stage="completed")

        # Interactive path runs cleanup_worker_exit which moves the .md file.
        with patch.dict(os.environ, {"VNX_DATA_DIR": str(data_dir)}):
            result = cleanup_worker_exit(
                terminal_id="T1",
                dispatch_id=dispatch_id,
                exit_status="success",
                dispatch_file=dispatch_file,
                state_dir=data_dir / "state",
            )

        assert not active_manifest_dir.exists()
        assert not dispatch_file.exists()
        assert result.dispatch_moved is not None
        assert "completed" in str(result.dispatch_moved)

    def test_failure_drains_manifest_to_dead_letter_and_file_to_rejected(
        self, tmp_path: Path
    ) -> None:
        from cleanup_worker_exit import cleanup_worker_exit

        data_dir = tmp_path / ".vnx-data"
        (data_dir / "dispatches" / "active").mkdir(parents=True)
        (data_dir / "dispatches" / "dead_letter").mkdir(parents=True)
        (data_dir / "state").mkdir(parents=True)

        dispatch_id = "20260430-cfx-7-cleanup-failure"
        active_manifest_dir = _seed_active_dispatch(data_dir, dispatch_id)
        dispatch_file = self._stage_dispatch_file(data_dir, dispatch_id)

        with patch.dict(os.environ, {"VNX_DATA_DIR": str(data_dir)}):
            sd._promote_manifest(dispatch_id, stage="dead_letter")

        with patch.dict(os.environ, {"VNX_DATA_DIR": str(data_dir)}):
            result = cleanup_worker_exit(
                terminal_id="T1",
                dispatch_id=dispatch_id,
                exit_status="failure",
                dispatch_file=dispatch_file,
                state_dir=data_dir / "state",
            )

        assert not active_manifest_dir.exists()
        assert not dispatch_file.exists()
        # The .md file should be in rejected/failure/, never completed/.
        assert result.dispatch_moved is not None
        assert "rejected" in str(result.dispatch_moved)
        assert not (data_dir / "dispatches" / "completed" / f"{dispatch_id}.md").exists()


# ---------------------------------------------------------------------------
# Case D — safety: never rmtree outside dispatches/active/
# ---------------------------------------------------------------------------

class TestSafeRemoveActiveDir:
    def test_refuses_path_outside_dispatches_active(self, tmp_path: Path) -> None:
        # Construct a directory whose parent is "active" but grandparent is
        # NOT "dispatches".  The helper must refuse.
        bad = tmp_path / "not_dispatches" / "active" / "spoof_id"
        bad.mkdir(parents=True)
        (bad / "important.txt").write_text("do not delete", encoding="utf-8")

        ok = sd._safe_remove_active_dir(bad)

        assert ok is False
        assert bad.exists(), "wrong-grandparent path must NOT be removed"
        assert (bad / "important.txt").read_text() == "do not delete"

    def test_refuses_path_with_wrong_parent(self, tmp_path: Path) -> None:
        bad = tmp_path / "dispatches" / "completed" / "spoof_id"
        bad.mkdir(parents=True)
        (bad / "manifest.json").write_text("{}", encoding="utf-8")

        ok = sd._safe_remove_active_dir(bad)

        assert ok is False
        assert bad.exists(), "completed/<id>/ must never be rmtree-d by this helper"

    def test_refuses_symlink_target(self, tmp_path: Path) -> None:
        # Real directory the symlink will point at — must survive.
        real = tmp_path / "real_payload"
        real.mkdir()
        (real / "secret.txt").write_text("keep me", encoding="utf-8")

        # Place the symlink under dispatches/active/ so name checks would
        # otherwise pass.  The is_symlink() guard must trip first.
        active = tmp_path / "dispatches" / "active"
        active.mkdir(parents=True)
        link = active / "linked_id"
        link.symlink_to(real, target_is_directory=True)

        ok = sd._safe_remove_active_dir(link)

        assert ok is False
        assert real.exists(), "symlink target must survive"
        assert (real / "secret.txt").read_text() == "keep me"
        # Best effort: link should still be present and unfollowed.
        assert link.is_symlink()

    def test_refuses_non_directory(self, tmp_path: Path) -> None:
        active = tmp_path / "dispatches" / "active"
        active.mkdir(parents=True)
        bogus = active / "stray_file"
        bogus.write_text("not a directory", encoding="utf-8")

        ok = sd._safe_remove_active_dir(bogus)

        assert ok is False
        assert bogus.exists()

    def test_missing_directory_is_idempotent_success(self, tmp_path: Path) -> None:
        ghost = tmp_path / "dispatches" / "active" / "never_existed"
        # Directory does not exist; helper must report success without error.
        assert sd._safe_remove_active_dir(ghost) is True


# ---------------------------------------------------------------------------
# Case E — idempotent: re-running cleanup is a no-op
# ---------------------------------------------------------------------------

class TestIdempotentCleanup:
    def test_double_promote_is_noop(self, tmp_path: Path) -> None:
        dispatch_id = "20260430-cfx-7-idempotent"
        active_dir = _seed_active_dispatch(tmp_path, dispatch_id)

        with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
            first = sd._promote_manifest(dispatch_id, stage="completed")
            second = sd._promote_manifest(dispatch_id, stage="completed")

        assert first is not None
        # Second call: manifest already moved → returns None, must NOT raise,
        # must NOT create duplicate state, must leave no active/<id>/ orphan.
        assert second is None
        assert not active_dir.exists()
        completed_file = tmp_path / "dispatches" / "completed" / dispatch_id / "manifest.json"
        assert completed_file.exists(), "completed manifest must remain after second call"

    def test_promote_after_stray_active_dir_recreated(self, tmp_path: Path) -> None:
        """Defence-in-depth: if something re-creates active/<id>/ AFTER a
        successful promotion (e.g. a late janitor write), a subsequent
        _promote_manifest call must still tidy it up rather than leaving an
        orphan.
        """
        dispatch_id = "20260430-cfx-7-stray"
        _seed_active_dispatch(tmp_path, dispatch_id)
        with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
            sd._promote_manifest(dispatch_id, stage="completed")

        stray = tmp_path / "dispatches" / "active" / dispatch_id
        stray.mkdir(parents=True)
        (stray / "stray.log").write_text("late writer", encoding="utf-8")

        with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
            result = sd._promote_manifest(dispatch_id, stage="completed")

        assert result is None  # no manifest to promote
        assert not stray.exists(), "idempotent cleanup must remove stray active dir"
