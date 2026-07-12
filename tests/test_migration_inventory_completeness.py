#!/usr/bin/env python3
"""Tests for migration_inventory.verify_complete() — the fs<->git completeness oracle.

Dispatch-ID: 20260712-183939-cockpit-pr8

The oracle compares the filesystem-glob enumeration of `<root>/schemas/migrations/*.sql`
against `git ls-files` for the SAME root. Both universes must be rooted at the same
migrations dir, or the comparison is meaningless (codex finding). The negative case
runs against a MONKEYPATCHED TEMP root — never the real repo dir — so it can safely
mutate the working tree (add an untracked file) without polluting this checkout.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import migration_inventory as mi  # noqa: E402


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _seed_temp_repo(root: Path) -> None:
    (root / "schemas" / "migrations").mkdir(parents=True)
    (root / "schemas" / "migrations" / "0001_seed.sql").write_text(
        "CREATE TABLE dispatches (id INTEGER PRIMARY KEY, project_id TEXT NOT NULL);\n"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")


def test_verify_complete_passes_on_the_real_tree():
    result = mi.verify_complete(REPO)
    assert result.ok, result.violations
    assert result.violations == ()


def test_verify_complete_fails_on_untracked_sql_in_a_monkeypatched_temp_root(tmp_path, monkeypatch):
    _seed_temp_repo(tmp_path)

    # Baseline: fs and git agree over this shared temp root.
    baseline = mi.verify_complete(tmp_path)
    assert baseline.ok, baseline.violations

    # Introduce a disk<->git divergence: a SQL file on disk that was never
    # `git add`-ed. This must never touch the real repo — the migrations root
    # is monkeypatched to the temp dir for the whole call chain, including the
    # root=None default-resolution path.
    (tmp_path / "schemas" / "migrations" / "0002_untracked.sql").write_text(
        "CREATE TABLE terminal_leases (id INTEGER PRIMARY KEY, project_id TEXT NOT NULL);\n"
    )

    monkeypatch.setattr(mi, "resolve_project_root", lambda *_a, **_kw: tmp_path)

    result = mi.verify_complete()
    assert not result.ok
    assert any("0002_untracked.sql" in v for v in result.violations)

    # The real repo's migrations dir must be untouched by the negative case.
    real_status = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "schemas/migrations"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert real_status == ""


def test_verify_complete_fails_when_tracked_file_missing_from_disk(tmp_path):
    _seed_temp_repo(tmp_path)
    (tmp_path / "schemas" / "migrations" / "0001_seed.sql").unlink()

    result = mi.verify_complete(tmp_path)
    assert not result.ok
    assert any("0001_seed.sql" in v for v in result.violations)
