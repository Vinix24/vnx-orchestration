"""tests/test_horizon_cli.py — `vnx horizon` pip-CLI command group (D1).

Verifies D1 of claudedocs/2026-07-05-horizon-planning-module-PLAN.md:

- `vnx horizon` wraps the existing planning_cli engine — no new logic.
- State resolves via the CENTRAL data root (`_engine.resolve_data_root`,
  matching `vnx track`/`vnx status`) — NOT `planning_cli._resolve_state_dir`'s
  repo-local degraded `<git-root>/.vnx-data/state` path.
- `--project-id` is required/resolved explicitly (ADR-007): a missing
  --project-id in an unresolvable context is rejected, never silently
  defaulted to 'vnx-dev'.
- `vnx objective` and `vnx deliverable` are full aliases of the same handler
  functions as `vnx horizon` / `vnx horizon deliverable`.
- `vnx horizon --help` lists the full subcommand surface.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
_SCRIPTS = REPO_ROOT / "scripts"
_MIGRATIONS = REPO_ROOT / "schemas" / "migrations"
for _p in (str(REPO_ROOT), str(_LIB), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import schema_migration  # noqa: E402

from vnx_cli import _engine  # noqa: E402
from vnx_cli.main import main as vnx_main  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _bootstrap_store(state_dir: Path) -> None:
    """Pre-migrate a runtime_coordination.db far enough for the planning
    surface (tracks/deliverables/plan-gate): 22 (track layer), 24 (tenant
    scoping), 27 (horizon + deliverables view), 28 (derived_status)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
        "state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT, "
        "priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT, "
        "attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "expires_after TEXT, metadata_json TEXT DEFAULT '{}', "
        "UNIQUE(dispatch_id, project_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coordination_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT, event_type TEXT, entity_type TEXT, entity_id TEXT, from_state TEXT, "
        "to_state TEXT, actor TEXT, reason TEXT, metadata_json TEXT, occurred_at TEXT, project_id TEXT)"
    )
    conn.commit()
    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    # Preflight normally owned by migrate_future_system.py: 0027's deliverables
    # VIEW selects dispatches.output_ref/output_kind, which the minimal
    # dispatches table above doesn't carry yet.
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.commit()
    for version, filename in [
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()


@pytest.fixture()
def isolated_env(monkeypatch):
    """Strip ambient VNX_* env so resolution is deterministic per-test."""
    for key in (
        "VNX_DATA_HOME", "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT",
        "VNX_PROJECT_ID", "VNX_STATE_DIR", "VNX_CANONICAL_ROOT", "PROJECT_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def project(tmp_path, isolated_env, monkeypatch):
    """An isolated, non-git project dir + a pre-migrated CENTRAL store wired
    via VNX_DATA_DIR_EXPLICIT (the highest-precedence override) — deliberately
    decoupled from --project-id resolution so store-location assertions and
    project-id assertions can be tested independently."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data_root = tmp_path / "central-data"
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
    state_dir = data_root / "state"
    _bootstrap_store(state_dir)
    # Pin CWD to the isolated (non-git) project dir so project_id auto-resolution
    # (which also consults CWD) never accidentally picks up this repo's own
    # git remote / any ancestor .vnx-project-id marker.
    monkeypatch.chdir(project_dir)
    return project_dir, state_dir


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["vnx", *argv])
    with pytest.raises(SystemExit) as exc:
        vnx_main()
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


# ---------------------------------------------------------------------------
# add / list round trip + central store resolution
# ---------------------------------------------------------------------------

def test_horizon_add_list_round_trip(project, monkeypatch, capsys):
    project_dir, state_dir = project

    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "add", "feat-h1", "Feature H1", "shipped",
        "--project-id", "horizon-test", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "list", "--project-id", "horizon-test",
        "--project-dir", str(project_dir), "--json",
    ])
    assert rc == 0, err
    data = json.loads(out)
    assert {d["track_id"] for d in data} == {"feat-h1"}


def test_horizon_resolves_central_store_not_repo_local(project, monkeypatch, capsys):
    project_dir, state_dir = project

    expected_state_dir = _engine.resolve_data_root(project_dir) / "state"
    assert expected_state_dir == state_dir

    rc, _, err = _run(monkeypatch, capsys, [
        "horizon", "add", "feat-h2", "Feature H2", "shipped",
        "--project-id", "horizon-test", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    # The track landed in the pre-migrated CENTRAL store, not a repo-local
    # `<project_dir>/.vnx-data/state` (which was never created) or this
    # checkout's own `.vnx-data/state` (planning_cli's degraded default).
    assert not (project_dir / ".vnx-data").exists()
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        row = conn.execute(
            "SELECT track_id FROM tracks WHERE track_id = ? AND project_id = ?",
            ("feat-h2", "horizon-test"),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


# ---------------------------------------------------------------------------
# project-id resolution (ADR-007) — never a silent 'vnx-dev' fallback
# ---------------------------------------------------------------------------

def test_horizon_missing_project_id_is_rejected_not_defaulted(project, monkeypatch, capsys):
    project_dir, state_dir = project

    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "list", "--project-dir", str(project_dir),
    ])
    assert rc == 2
    assert "vnx-dev" not in err
    assert "project-id" in err or "project_id" in err

    # No 'vnx-dev' row was ever created/queried against the store.
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE project_id = 'vnx-dev'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_horizon_resolves_project_id_from_marker_when_unambiguous(project, monkeypatch, capsys):
    project_dir, state_dir = project
    (project_dir / ".vnx-project-id").write_text("marker-proj\n", encoding="utf-8")

    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "add", "feat-marker", "Marker Feature", "shipped",
        "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        row = conn.execute(
            "SELECT project_id FROM tracks WHERE track_id = 'feat-marker'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "marker-proj"


# ---------------------------------------------------------------------------
# objective / deliverable aliases hit the SAME handlers as horizon
# ---------------------------------------------------------------------------

def test_objective_alias_reads_same_store_as_horizon(project, monkeypatch, capsys):
    project_dir, state_dir = project

    rc, _, err = _run(monkeypatch, capsys, [
        "horizon", "add", "feat-alias1", "Alias via horizon", "shipped",
        "--project-id", "horizon-test", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    rc, out, err = _run(monkeypatch, capsys, [
        "objective", "list", "--project-id", "horizon-test",
        "--project-dir", str(project_dir), "--json",
    ])
    assert rc == 0, err
    assert "feat-alias1" in {d["track_id"] for d in json.loads(out)}

    rc, _, err = _run(monkeypatch, capsys, [
        "objective", "add", "feat-alias2", "Alias via objective", "shipped",
        "--project-id", "horizon-test", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "list", "--project-id", "horizon-test",
        "--project-dir", str(project_dir), "--json",
    ])
    assert rc == 0, err
    ids = {d["track_id"] for d in json.loads(out)}
    assert {"feat-alias1", "feat-alias2"} <= ids


def test_deliverable_alias_reads_same_store_as_horizon_deliverable(project, monkeypatch, capsys):
    project_dir, state_dir = project

    rc, _, err = _run(monkeypatch, capsys, [
        "horizon", "add", "feat-dlv", "Deliverable host", "shipped",
        "--project-id", "horizon-test", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    rc, out, err = _run(monkeypatch, capsys, [
        "deliverable", "add", "--objective", "feat-dlv", "--output-kind", "doc",
        "--title", "via top-level alias",
        "--project-id", "horizon-test", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "deliverable", "list", "--objective", "feat-dlv",
        "--project-id", "horizon-test", "--project-dir", str(project_dir), "--json",
    ])
    assert rc == 0, err
    records = json.loads(out)
    assert any(r.get("track") == "feat-dlv" for r in records)

    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "deliverable", "add", "--objective", "feat-dlv", "--output-kind", "post",
        "--title", "via nested horizon",
        "--project-id", "horizon-test", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    rc, out, err = _run(monkeypatch, capsys, [
        "deliverable", "list", "--objective", "feat-dlv",
        "--project-id", "horizon-test", "--project-dir", str(project_dir), "--json",
    ])
    assert rc == 0, err
    records = json.loads(out)
    assert len(records) == 2


# ---------------------------------------------------------------------------
# --help lists the full surface
# ---------------------------------------------------------------------------

def test_horizon_help_lists_full_surface(monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, ["horizon", "--help"])
    assert rc == 0
    for verb in (
        "add", "list", "show", "sync", "drift", "reconcile",
        "reconcile-review", "reconcile-streak", "close", "reopen",
        "deliverable", "plan-gate",
    ):
        assert verb in out, f"missing verb in `vnx horizon --help`: {verb}"


def test_horizon_deliverable_and_plan_gate_help_list_full_surface(monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, ["horizon", "deliverable", "--help"])
    assert rc == 0
    for verb in ("add", "list", "promote"):
        assert verb in out

    rc, out, _ = _run(monkeypatch, capsys, ["horizon", "plan-gate", "--help"])
    assert rc == 0
    for verb in ("seed", "run", "status"):
        assert verb in out


def test_objective_and_deliverable_alias_help(monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, ["objective", "--help"])
    assert rc == 0
    for verb in (
        "add", "list", "show", "sync", "drift", "reconcile",
        "reconcile-review", "reconcile-streak", "close", "reopen",
    ):
        assert verb in out
    # objective is the OBJECTIVE-domain alias only — deliverable/plan-gate stay
    # on their own top-level surfaces, matching bin/vnx's existing split.
    assert "deliverable" not in out
    assert "plan-gate" not in out

    rc, out, _ = _run(monkeypatch, capsys, ["deliverable", "--help"])
    assert rc == 0
    for verb in ("add", "list", "promote"):
        assert verb in out


# ---------------------------------------------------------------------------
# repo_root defaulting + drift no-crash (central-mode-path-correctness, round 3)
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from vnx_cli.commands import horizon as _horizon  # noqa: E402


def test_default_repo_root_sets_from_project_dir(tmp_path):
    args = SimpleNamespace(project_dir=str(tmp_path))
    _horizon._default_repo_root(args)
    assert args.repo_root == str(tmp_path.resolve())


def test_default_repo_root_preserves_explicit(tmp_path):
    args = SimpleNamespace(project_dir=str(tmp_path), repo_root="/explicit/root")
    _horizon._default_repo_root(args)
    assert args.repo_root == "/explicit/root"


def test_horizon_drift_no_repo_root_crash(project, monkeypatch, capsys):
    """`vnx horizon drift` must not raise 'Namespace has no attribute repo_root'
    nor emit 'cannot resolve repo root' — the pip CLI defaults repo_root and the
    reconciler tolerates it."""
    project_dir, _ = project
    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "drift",
        "--project-id", "horizon-test", "--project-dir", str(project_dir),
    ])
    assert "has no attribute 'repo_root'" not in err
    assert "cannot resolve repo root" not in err
    assert rc == 0


def test_horizon_reconcile_no_repo_root_crash(project, monkeypatch, capsys):
    project_dir, _ = project
    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "reconcile",
        "--project-id", "horizon-test", "--project-dir", str(project_dir),
    ])
    assert "has no attribute 'repo_root'" not in err
    assert "cannot resolve repo root" not in err
    # reconcile returns 0 (nothing to close) or 3 (gh absent) — never a crash.
    assert rc in (0, 3)
