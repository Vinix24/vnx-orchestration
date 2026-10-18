"""P1 of track plan-gate-alleen-voor-architectuur: one plan scope per track, only upward by itself.

A track carries ``plan_scope`` (architecture | light) in tracks.metadata_json,
next to ``lane_hint``. ``horizon add`` requires it; the fourth deliverable and
an architecture-class build dispatch at the door move a light track up and
seed ``OI-PLAN-<track>``; only ``set-plan-scope ... --reason`` moves it down.
Old tracks without the key read from their open OI-PLAN. A plan-gate run whose
paths fall in the schema or security class gets both seats from the start.

Everything below drives the real code on a real migrated store: the CLI via
``planning_cli.main``, the door via a real ``run_dispatch`` fire, the plan-gate
via ``run_plan_gate_for_track`` with only the model lane faked. Tracks are made
through the DAL (``tracks.create_track``), which the old code has too, and a
helper this PR adds is reached through ``_new``, so a run on the old code fails
on an assertion about behaviour, never on an import or a missing attribute.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
for _p in (str(_REPO / "scripts" / "lib"), str(_REPO / "scripts"), str(_REPO / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import schema_migration
import tracks
import planning_cli
from fixtures.dispatches_schema_fixture import ensure_dispatches_columns

_MIGRATIONS = _REPO / "schemas" / "migrations"
PID = "vnx-dev"


def _missing(*_args, **_kwargs):
    return "<not on this code>"


def _new(module_name: str, attr: str):
    """A helper this PR adds, or a stand-in whose result fails the assertion."""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return _missing
    return getattr(module, attr, _missing)


def _bootstrap(state_dir: Path) -> Path:
    """A store with the track layer, the deliverable view and resolvable blockers."""
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
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
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
        (33, "0033_track_decision_ref.sql"),
    ]:
        schema_migration.apply_script_if_below(
            conn, version, (_MIGRATIONS / filename).read_text(encoding="utf-8"),
        )
        conn.commit()
    # A migration above rebuilds dispatches; put back every canonical column
    # (output_kind/output_ref among them) the deliverable plane reads.
    ensure_dispatches_columns(conn)
    conn.commit()
    conn.close()
    return state_dir


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    return _bootstrap(tmp_path / "data" / "state")


@pytest.fixture(autouse=True)
def _restore_environ():
    """run_dispatch exports VNX_CURRENT_* into os.environ; keep it inside this test."""
    before = dict(os.environ)
    yield
    for key in set(os.environ) - set(before):
        del os.environ[key]
    os.environ.update(before)


def _cli(state_dir: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = planning_cli.main([*argv, "--project-id", PID, "--state-dir", str(state_dir)])
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def _make_track(state_dir: Path, track_id: str, meta: dict | None = None, *, seed: bool = False) -> None:
    """A track made through the DAL, with whatever metadata the test needs."""
    tracks.create_track(
        state_dir, track_id, PID, f"Track {track_id}", "shipped",
        metadata_json=json.dumps(meta) if meta is not None else None,
    )
    if seed:
        assert planning_cli._seed_plan_blocker(state_dir, track_id, PID)


def _meta(state_dir: Path, track_id: str) -> dict:
    return json.loads(tracks.get_track(state_dir, track_id, PID)["metadata_json"] or "{}")


def _plan_rows(state_dir: Path, track_id: str) -> list[tuple]:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        return conn.execute(
            "SELECT oi_id, resolved_at FROM track_open_items "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
            (track_id, PID, f"OI-PLAN-{track_id}"),
        ).fetchall()
    finally:
        conn.close()


def _open_plan_blocker(state_dir: Path, track_id: str) -> bool:
    return any(resolved is None for _oi, resolved in _plan_rows(state_dir, track_id))


def _resolve_plan_blocker(state_dir: Path, track_id: str) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "UPDATE track_open_items SET resolved_at = '2026-10-18T00:00:00Z', "
        "resolution_reason = '[panel] PASS' WHERE track_id = ? AND oi_id = ?",
        (track_id, f"OI-PLAN-{track_id}"),
    )
    conn.commit()
    conn.close()


def _scope_events(state_dir: Path, track_id: str) -> list[dict]:
    path = state_dir.parent / "events" / "track_events.ndjson"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [e for e in events if e["event_type"] == "track_plan_scope_set" and e["track_id"] == track_id]


def _scope_changes(state_dir: Path, track_id: str) -> list[dict]:
    """The scope events after birth: a track born with a scope records it as old 'none'."""
    return [e for e in _scope_events(state_dir, track_id) if e["details"]["old"] != "none"]


def _fail_plan_seeds(state_dir: Path) -> None:
    """Make every OI-PLAN seed write fail at the storage level, whichever code writes it."""
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "CREATE TRIGGER fail_plan_seed BEFORE INSERT ON track_open_items "
        "WHEN NEW.oi_id LIKE 'OI-PLAN-%' "
        "BEGIN SELECT RAISE(ABORT, 'injected: the OI-PLAN seed write fails'); END"
    )
    conn.commit()
    conn.close()


def _heal_plan_seeds(state_dir: Path) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("DROP TRIGGER fail_plan_seed")
    conn.commit()
    conn.close()


def _show(state_dir: Path, track_id: str) -> dict:
    rc, out, err = _cli(state_dir, "horizon", "show", track_id, "--json")
    assert rc == 0, err
    return json.loads(out)


def _add_deliverable(state_dir: Path, track_id: str, title: str, *extra: str) -> tuple[int, str, str]:
    return _cli(state_dir, "deliverable", "add", "--objective", track_id, "--output-kind", "pr",
                "--title", title, *extra)


# --------------------------------------------------------------------------
# horizon add: --plan-scope is required
# --------------------------------------------------------------------------

def test_horizon_add_without_plan_scope_is_refused_and_creates_nothing(state_dir):
    rc, _, err = _cli(state_dir, "horizon", "add", "feat-none", "No scope", "shipped")
    assert rc != 0
    assert "--plan-scope" in err
    assert "architecture" in err and "light" in err
    assert tracks.get_track(state_dir, "feat-none", PID) is None


def test_horizon_add_light_seeds_no_plan_gate(state_dir):
    rc, _, err = _cli(state_dir, "horizon", "add", "feat-light", "Light", "shipped",
                      "--plan-scope", "light")
    assert rc == 0, err
    assert _meta(state_dir, "feat-light").get("plan_scope") == "light"
    assert _plan_rows(state_dir, "feat-light") == []


def test_horizon_add_architecture_seeds_the_plan_gate(state_dir):
    rc, _, err = _cli(state_dir, "horizon", "add", "feat-arch", "Arch", "shipped",
                      "--plan-scope", "architecture")
    assert rc == 0, err
    assert _meta(state_dir, "feat-arch").get("plan_scope") == "architecture"
    assert _open_plan_blocker(state_dir, "feat-arch")


def test_horizon_add_rejects_an_unknown_plan_scope(state_dir):
    rc, _, _ = _cli(state_dir, "horizon", "add", "feat-x", "X", "shipped", "--plan-scope", "heavy")
    assert rc != 0
    assert tracks.get_track(state_dir, "feat-x", PID) is None


# --------------------------------------------------------------------------
# Upward by itself: the fourth deliverable
# --------------------------------------------------------------------------

def test_fourth_deliverable_moves_a_light_track_up_and_seeds_oi_plan(state_dir):
    _make_track(state_dir, "feat-four", {"plan_scope": "light"})
    for n in range(1, 4):
        rc, out, err = _add_deliverable(state_dir, "feat-four", f"PR {n}")
        assert rc == 0, err
        assert "plan-scope" not in out
    assert _meta(state_dir, "feat-four")["plan_scope"] == "light"
    assert _plan_rows(state_dir, "feat-four") == []

    rc, out, err = _add_deliverable(state_dir, "feat-four", "PR 4")
    assert rc == 0, err
    scope_lines = [line for line in out.splitlines() if line.startswith("plan-scope:")]
    assert len(scope_lines) == 1, out
    assert "light -> architecture" in scope_lines[0]
    assert "OI-PLAN-feat-four seeded" in scope_lines[0]
    meta = _meta(state_dir, "feat-four")
    assert meta["plan_scope"] == "architecture"
    assert "4 deliverables" in meta.get("plan_scope_rule", "")
    assert _open_plan_blocker(state_dir, "feat-four")


def test_fourth_deliverable_leaves_an_architecture_track_alone(state_dir):
    _make_track(state_dir, "feat-a4", {"plan_scope": "architecture"}, seed=True)
    for n in range(1, 5):
        rc, out, err = _add_deliverable(state_dir, "feat-a4", f"PR {n}")
        assert rc == 0, err
        assert "plan-scope" not in out
    assert _meta(state_dir, "feat-a4")["plan_scope"] == "architecture"
    assert _scope_changes(state_dir, "feat-a4") == []


# --------------------------------------------------------------------------
# Upward by itself: the dispatch door, through a real fire
# --------------------------------------------------------------------------

def _door_store(tmp_path: Path, monkeypatch) -> Path:
    """The store a real door fire writes to, with no track in it yet."""
    data_dir = tmp_path / "vnx-data"
    sd = _bootstrap(data_dir / "state")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "advisory")
    return sd


def _fire_at(sd: Path, paths: list[str], dispatch_id: str, track_id: str = "feat-door") -> tuple[int, object]:
    """One real run_dispatch on a promoted bundle; the worker itself is a mock."""
    import dispatch_cli

    bundle = sd.parent / "dispatches" / "pending" / f"stage-{dispatch_id}"
    bundle.mkdir(parents=True)
    instruction = bundle / "instruction.md"
    instruction.write_text("# Dispatch\n\nChange the code.\n", encoding="utf-8")
    spec_file = bundle / "dispatch-spec.json"
    spec_file.write_text(json.dumps({
        "schema_version": 1, "project_id": PID, "dispatch_id": dispatch_id,
        "staging_id": f"stage-{dispatch_id}", "instruction_file": str(instruction),
        "role": "backend-developer", "target_slot": "T0", "gate": "codex_gate",
        "dispatch_paths": [{"path": p, "access": "read_write"} for p in paths],
        "provider": "claude", "model": None, "deadline_seconds": 3600,
        "isolation": "worktree", "track_id": track_id,
    }), encoding="utf-8")

    with patch("dispatch_cli._execute_claude_headless", return_value=0) as worker:
        rc = dispatch_cli.run_dispatch(spec_file)
    return rc, worker


def _fire(tmp_path: Path, monkeypatch, paths: list[str], *, meta: dict | None, seed=False,
          resolve=False, dispatch_id="20261018-fire") -> tuple[int, Path, object]:
    """One track, then one real fire on it."""
    sd = _door_store(tmp_path, monkeypatch)
    _make_track(sd, "feat-door", meta, seed=seed)
    if resolve:
        _resolve_plan_blocker(sd, "feat-door")
    rc, worker = _fire_at(sd, paths, dispatch_id)
    return rc, sd, worker


@pytest.mark.parametrize("paths, rule_part", [
    (["scripts/lib/provider_spawns/kimi_spawn.py"], "security path"),
    (["schemas/migrations/0099_new_table.sql"], "schema path"),
    (["scripts/lib/dispatch_cli.py"], "variant coding-strict"),
    ([], "without dispatch_paths"),
], ids=["security", "schema", "coding-strict", "no-paths"])
def test_door_moves_a_light_track_up_and_still_dispatches(tmp_path, monkeypatch, capsys, paths, rule_part):
    rc, sd, worker = _fire(tmp_path, monkeypatch, paths, meta={"plan_scope": "light"})

    assert rc == 0, "the door refuses nothing in P1"
    worker.assert_called_once()
    meta = _meta(sd, "feat-door")
    assert meta["plan_scope"] == "architecture"
    assert rule_part in meta.get("plan_scope_rule", "")
    assert "dispatch 20261018-fire" in meta.get("plan_scope_rule", "")
    assert _open_plan_blocker(sd, "feat-door")
    lines = [ln for ln in capsys.readouterr().err.splitlines() if "plan-scope:" in ln]
    assert len(lines) == 1
    assert "feat-door light -> architecture" in lines[0]
    assert "The dispatch continues" in lines[0]


def test_door_leaves_a_light_track_light_on_a_light_change(tmp_path, monkeypatch, capsys):
    rc, sd, worker = _fire(
        tmp_path, monkeypatch, ["docs/core/HORIZON_LIFECYCLE.md", "scripts/lib/report_writer.py"],
        meta={"plan_scope": "light"},
    )
    assert rc == 0
    worker.assert_called_once()
    assert _meta(sd, "feat-door")["plan_scope"] == "light"
    assert _plan_rows(sd, "feat-door") == []
    assert "plan-scope" not in capsys.readouterr().err


def test_door_moves_an_old_track_up_without_reopening_its_passed_gate(tmp_path, monkeypatch):
    """An old track whose gate passed reads light (no OPEN OI-PLAN). Moving it up
    must not clear resolved_at: that would reopen a passed gate."""
    rc, sd, _ = _fire(tmp_path, monkeypatch, ["scripts/lib/dispatch_cli.py"],
                      meta=None, seed=True, resolve=True)
    assert rc == 0
    assert _meta(sd, "feat-door").get("plan_scope") == "architecture"
    assert _plan_rows(sd, "feat-door"), "the resolved row is still there"
    assert not _open_plan_blocker(sd, "feat-door")


def test_door_upward_move_writes_one_event_with_old_new_and_reason(tmp_path, monkeypatch):
    rc, sd, _ = _fire(tmp_path, monkeypatch, [".github/workflows/vnx-ci.yml"], meta={"plan_scope": "light"})
    assert rc == 0
    events = _scope_changes(sd, "feat-door")
    assert len(events) == 1
    details = events[0]["details"]
    assert (details["old"], details["new"]) == ("light", "architecture")
    assert "security path .github/workflows/vnx-ci.yml" in details["reason"]
    assert details["rule"] == details["reason"]


def test_door_leaves_an_architecture_track_alone(tmp_path, monkeypatch):
    rc, sd, _ = _fire(tmp_path, monkeypatch, ["scripts/lib/dispatch_cli.py"],
                      meta={"plan_scope": "architecture"}, seed=True)
    assert rc == 0
    assert _meta(sd, "feat-door")["plan_scope"] == "architecture"
    assert _scope_changes(sd, "feat-door") == []


# --------------------------------------------------------------------------
# Down only by hand
# --------------------------------------------------------------------------

def test_set_plan_scope_down_without_reason_is_refused(state_dir):
    _make_track(state_dir, "feat-down", {"plan_scope": "architecture"}, seed=True)
    rc, _, err = _cli(state_dir, "horizon", "set-plan-scope", "feat-down", "light")
    assert rc == 2
    assert "--reason is required" in err
    assert _meta(state_dir, "feat-down")["plan_scope"] == "architecture"
    assert _scope_changes(state_dir, "feat-down") == []


def test_set_plan_scope_down_with_reason_is_written_and_leaves_oi_plan(state_dir):
    _make_track(state_dir, "feat-down", {"plan_scope": "architecture"}, seed=True)
    rc, out, err = _cli(state_dir, "horizon", "set-plan-scope", "feat-down", "light",
                        "--reason", "one small PR, no gate or schema change")
    assert rc == 0, err
    assert "architecture -> light" in out
    meta = _meta(state_dir, "feat-down")
    assert meta["plan_scope"] == "light"
    assert "plan_scope_rule" not in meta
    assert _open_plan_blocker(state_dir, "feat-down"), "lowering never closes or attests OI-PLAN"
    events = _scope_changes(state_dir, "feat-down")
    assert len(events) == 1
    assert events[0]["details"] == {
        "old": "architecture", "new": "light",
        "reason": "one small PR, no gate or schema change", "rule": None,
    }
    assert events[0]["actor"] == "operator"


def test_set_plan_scope_on_a_missing_track_is_refused(state_dir):
    rc, _, err = _cli(state_dir, "horizon", "set-plan-scope", "nope", "light", "--reason", "x")
    assert rc == 2
    assert "track not found" in err


# --------------------------------------------------------------------------
# Old tracks: the transition reading, which writes nothing
# --------------------------------------------------------------------------

def test_old_track_with_open_oi_plan_reads_architecture(state_dir):
    _make_track(state_dir, "feat-legacy", None, seed=True)
    before = tracks.get_track(state_dir, "feat-legacy", PID)["metadata_json"]

    shown = _show(state_dir, "feat-legacy")
    assert (shown.get("plan_scope"), shown.get("plan_scope_source")) == ("architecture", "transition")
    assert tracks.get_track(state_dir, "feat-legacy", PID)["metadata_json"] == before
    assert _scope_events(state_dir, "feat-legacy") == []


def test_old_track_without_open_oi_plan_reads_light_and_nothing_is_written(state_dir):
    _make_track(state_dir, "feat-legacy", None)
    before = tracks.get_track(state_dir, "feat-legacy", PID)["metadata_json"]

    shown = _show(state_dir, "feat-legacy")
    assert (shown.get("plan_scope"), shown.get("plan_scope_source")) == ("light", "transition")
    rc, out, err = _cli(state_dir, "horizon", "list", "--json")
    assert rc == 0, err
    rows = [r for r in json.loads(out) if r["track_id"] == "feat-legacy"]
    assert [r.get("plan_scope") for r in rows] == ["light"]
    assert tracks.get_track(state_dir, "feat-legacy", PID)["metadata_json"] == before
    assert _scope_events(state_dir, "feat-legacy") == []


def test_an_unknown_stored_scope_reads_architecture(state_dir):
    """No writer stores an unknown value, so the test puts one in the row itself."""
    _make_track(state_dir, "feat-odd", None)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("UPDATE tracks SET metadata_json = ? WHERE track_id = ?",
                 (json.dumps({"plan_scope": "heavy"}), "feat-odd"))
    conn.commit()
    conn.close()
    shown = _show(state_dir, "feat-odd")
    assert (shown.get("plan_scope"), shown.get("plan_scope_source")) == ("architecture", "unreadable")


# --------------------------------------------------------------------------
# show and list
# --------------------------------------------------------------------------

def test_show_names_the_scope_and_the_rule_that_set_it(state_dir):
    _make_track(state_dir, "feat-show", {"plan_scope": "light"})
    for n in range(1, 5):
        _add_deliverable(state_dir, "feat-show", f"PR {n}")
    shown = _show(state_dir, "feat-show")
    assert shown.get("plan_scope") == "architecture"
    assert "4 deliverables" in (shown.get("plan_scope_rule") or "")
    rc, out, _ = _cli(state_dir, "horizon", "show", "feat-show")
    assert rc == 0
    lines = [ln for ln in out.splitlines() if "plan_scope:" in ln]
    assert len(lines) == 1
    assert "architecture (rule: deliverable add: 4 deliverables" in lines[0]


def test_show_says_set_by_hand_and_list_keeps_its_columns_with_a_tag(state_dir):
    _make_track(state_dir, "feat-tag", {"plan_scope": "light"})
    rc, out, _ = _cli(state_dir, "horizon", "show", "feat-tag")
    assert "plan_scope: light (set by hand)" in out
    rc, out, _ = _cli(state_dir, "horizon", "list")
    assert rc == 0
    lines = [ln for ln in out.splitlines() if "feat-tag" in ln]
    assert len(lines) == 1
    assert "[queued ]" in lines[0] and "Track feat-tag" in lines[0]
    assert lines[0].rstrip().endswith("scope: light")


# --------------------------------------------------------------------------
# plan_scope and lane_hint survive each other's writes
# --------------------------------------------------------------------------

def test_plan_scope_and_lane_hint_survive_each_others_writes(state_dir):
    _make_track(state_dir, "feat-both", {"plan_scope": "light", "lane_hint": "governed"})

    rc, _, err = _cli(state_dir, "horizon", "set-lane-hint", "feat-both", "direct")
    assert rc == 0, err
    assert _meta(state_dir, "feat-both") == {"lane_hint": "direct", "plan_scope": "light"}

    rc, _, err = _cli(state_dir, "horizon", "set-plan-scope", "feat-both", "architecture",
                      "--reason", "touches the merge gate")
    assert rc == 0, err
    assert _meta(state_dir, "feat-both") == {"lane_hint": "direct", "plan_scope": "architecture"}


def test_add_with_both_flags_stores_both(state_dir):
    rc, _, err = _cli(state_dir, "horizon", "add", "feat-flags", "Flags", "shipped",
                      "--plan-scope", "light", "--lane-hint", "governed")
    assert rc == 0, err
    assert _meta(state_dir, "feat-flags") == {"lane_hint": "governed", "plan_scope": "light"}


_ROADMAP = """
roadmap_id: test-roadmap
title: Test
features:
  - feature_id: feat-sync
    title: Feature Sync
    risk_class: {risk}
    depends_on: []
    milestone: "1.0"
    status: planned
"""


def test_objective_sync_keeps_plan_scope_when_the_seeder_rewrites_metadata(state_dir, tmp_path):
    """The ROADMAP seeder rebuilds metadata_json wholesale on an authored change
    (risk_class here); plan_scope, its rule and lane_hint stay as they are."""
    import plan_scope

    roadmap = tmp_path / "ROADMAP.yaml"
    roadmap.write_text(_ROADMAP.format(risk="high"), encoding="utf-8")
    rc, _, err = _cli(state_dir, "horizon", "sync", "--apply", "--roadmap", str(roadmap))
    assert rc == 0, err
    assert _cli(state_dir, "horizon", "set-lane-hint", "feat-sync", "governed")[0] == 0
    plan_scope.raise_to_architecture(state_dir, "feat-sync", PID, rule="door, dispatch d1: schema path x")

    roadmap.write_text(_ROADMAP.format(risk="low"), encoding="utf-8")
    rc, _, err = _cli(state_dir, "horizon", "sync", "--apply", "--roadmap", str(roadmap))
    assert rc == 0, err
    after = _meta(state_dir, "feat-sync")
    assert after["risk_class"] == "low", "the seeder did rewrite metadata_json"
    assert after.get("plan_scope") == "architecture"
    assert after.get("plan_scope_rule") == "door, dispatch d1: schema path x"
    assert after.get("lane_hint") == "governed"


# --------------------------------------------------------------------------
# The security path category, pinned per kind with a negative per kind
# --------------------------------------------------------------------------

def test_security_path_lists_are_pinned():
    import smart_router

    assert getattr(smart_router, "_SECURITY_PREFIXES", None) == (
        ("scripts/lib/provider_spawns/", "provider-spawns"),
        (".claude/settings", "claude-settings"),
        (".github/workflows/", "ci-workflows"),
    )
    assert getattr(smart_router, "_SECURITY_DIR_NAMES", None) == (("hooks", "hooks"),)
    assert getattr(smart_router, "_SECURITY_NAME_MARKERS", None) == (
        ("permission", "permission-module"),
        ("sandbox", "sandbox-module"),
        ("auth", "credential-name"),
        ("secret", "credential-name"),
        ("token", "credential-name"),
        ("credential", "credential-name"),
    )


@pytest.mark.parametrize("kind, positives, negative", [
    ("provider-spawns", ["scripts/lib/provider_spawns/kimi_spawn.py", "./scripts/lib/provider_spawns/"],
     "scripts/lib/provider_registry.py"),
    ("claude-settings", [".claude/settings.json", ".claude/settings.local.json", "./.claude/settings.json"],
     ".claude/terminals/T0/CLAUDE.md"),
    ("ci-workflows", [".github/workflows/vnx-ci.yml"], ".github/ISSUE_TEMPLATE/bug.md"),
    ("hooks", ["hooks/pre-commit", ".claude/hooks/session_start.sh", "scripts/hooks/guard.py"],
     "scripts/lib/hook_utils.py"),
    ("permission-module", ["scripts/vnx_worker_permissions_merge.py"], "scripts/lib/report_writer.py"),
    ("sandbox-module", ["scripts/lib/sandbox_runner.py"], "scripts/lib/runner.py"),
    ("credential-name", ["scripts/lib/oauth_refresh.py", "configs/secrets.example.yaml",
                         "scripts/lib/token_budget.py", "scripts/credential_store.sh"],
     "scripts/lib/receipt_reader.py"),
])
def test_security_path_kind_per_kind(kind, positives, negative):
    security_path_kind = _new("smart_router", "security_path_kind")
    category_of = _new("smart_router", "plan_scope_path_category")
    for path in positives:
        assert security_path_kind(path) == kind, path
        assert category_of(path) == "security", path
    assert security_path_kind(negative) is None
    assert category_of(negative) != "security"


@pytest.mark.parametrize("category, positives, negative", [
    ("schema", ["scripts/migrations/0040_x.sql", "schemas/migrations/0041_y.sql"],
     "schemas/runtime_coordination.sql"),
    ("gate-merge", ["scripts/pr_merge.py", "scripts/lib/plan_gate_panel.py", "scripts/lib/verify_pr.py"],
     "scripts/lib/report_writer.py"),
    ("store-resolution", ["scripts/lib/vnx_paths.py", "scripts/lib/project_root.py", "vnx_cli/_engine.py"],
     "vnx_cli/main.py"),
])
def test_plan_scope_path_category_per_category(category, positives, negative):
    category_of = _new("smart_router", "plan_scope_path_category")
    for path in positives:
        assert category_of(path) == category, path
    assert category_of(negative) is None


def test_dispatch_rule_is_none_for_a_light_change_and_names_the_rule_otherwise():
    dispatch_rule = _new("plan_scope", "dispatch_rule")
    assert dispatch_rule(["docs/core/HORIZON_PLANNING.md"]) is None
    assert dispatch_rule(["scripts/lib/report_writer.py", "tests/test_x.py"]) is None
    assert dispatch_rule([]) == "build dispatch without dispatch_paths (closed on doubt)"
    assert dispatch_rule(["scripts/lib/gate_executor.py"]).startswith("variant coding-strict")
    assert dispatch_rule(["vnx_cli/_engine.py"]) == "store-resolution path vnx_cli/_engine.py"


# --------------------------------------------------------------------------
# The plan-gate: both seats from the start for schema and security
# --------------------------------------------------------------------------

def _seat_report(verdict: str) -> str:
    import plan_gate_panel as pgp

    body = json.dumps({"verdict": verdict, "blocking_findings": [], "rationale": "r"})
    return f"Review.\n```{pgp.VERDICT_FENCE}\n{body}\n```\n"


class _Lanes:
    def __init__(self, seats: dict):
        self.seats = seats
        self.providers: list = []

    def __call__(self, provider, model_arg, instruction, dispatch_id):
        self.providers.append(provider)
        return self.seats[provider]


_PANEL_CFG = (
    "version: 1\ndefault_seat: codex\nescalation_seat: opus\n"
    "tiebreaker:\n  max_rounds: 2\n  provider: deepseek_harness\n  model: deepseek-v4-pro\n"
    "seats:\n"
    "  - {label: opus, provider: claude, model_arg: opus}\n"
    "  - {label: codex, provider: codex, model_arg: gpt-test}\n"
)


@pytest.fixture
def gate(tmp_path, monkeypatch, state_dir):
    import plan_gate_panel as pgp
    import track_reconciler

    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_PANEL_RETRY", raising=False)
    monkeypatch.setattr(track_reconciler, "reconcile_track", lambda *a, **k: None)
    cfg = tmp_path / "plan_gate_panel.yaml"
    cfg.write_text(_PANEL_CFG, encoding="utf-8")
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: cfg)
    ledger = tmp_path / ".vnx-attest" / "plan-gate-seats.ndjson"
    monkeypatch.setattr(pgp, "_resolve_seat_ledger_path", lambda data_dir: ledger)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)
    _make_track(state_dir, "feat-gate", {"plan_scope": "architecture"}, seed=True)

    def _run(lanes: _Lanes, **kw):
        monkeypatch.setattr(pgp, "_make_default_dispatcher", lambda *a, **k: lanes)
        doc = tmp_path / "plan.md"
        doc.write_text("## Problem\nA plan.\n", encoding="utf-8")
        return planning_cli.run_plan_gate_for_track(
            state_dir=state_dir, track_id="feat-gate", project_id=PID, doc=str(doc), **kw,
        )

    return _run


def test_plan_gate_with_a_security_path_runs_both_seats_from_the_start(gate):
    lanes = _Lanes({"codex": _seat_report("pass"), "claude": _seat_report("pass")})
    result = gate(lanes, dispatch_paths=".github/workflows/vnx-ci.yml")
    assert lanes.providers == ["codex", "claude"], "no BLOCK, yet both seats read the plan"
    assert result.decision == "PASS"
    assert "both seats from the start (security path .github/workflows/vnx-ci.yml)" in result.gov_trace


def test_plan_gate_with_a_schema_path_needs_both_voices(gate):
    lanes = _Lanes({"codex": _seat_report("pass"), "claude": "a prose review with no verdict"})
    result = gate(lanes, dispatch_paths="scripts/migrations/0040_new.sql")
    assert lanes.providers[:2] == ["codex", "claude"]
    assert result.decision != "PASS", "a schema plan does not pass on one readable voice"


def test_plan_gate_with_a_plain_path_runs_one_seat(gate):
    lanes = _Lanes({"codex": _seat_report("pass"), "claude": _seat_report("pass")})
    result = gate(lanes, dispatch_paths="scripts/lib/report_writer.py")
    assert lanes.providers == ["codex"]
    assert result.decision == "PASS"


def test_plan_gate_reads_deliverable_paths_when_the_run_names_none(gate, state_dir):
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track, metadata_json, "
        "output_kind, output_ref) VALUES ('dlv-mig', ?, 'proposed', 'feat-gate', ?, 'pr', 'pr:dlv-mig')",
        (PID, json.dumps({"title": "Migration", "deliverable": True,
                          "dispatch_paths": ["schemas/migrations/0042_x.sql", "scripts/x.py"]})),
    )
    conn.commit()
    conn.close()
    lanes = _Lanes({"codex": _seat_report("pass"), "claude": _seat_report("pass")})
    result = gate(lanes)
    assert lanes.providers == ["codex", "claude"]
    assert "schema path schemas/migrations/0042_x.sql" in (result.gov_trace or "")


def test_deliverable_add_and_set_store_dispatch_paths(state_dir):
    _make_track(state_dir, "feat-dp", {"plan_scope": "architecture"}, seed=True)
    rc, out, err = _add_deliverable(state_dir, "feat-dp", "Spawn hardening",
                                    "--dispatch-paths", "scripts/lib/provider_spawns/a.py, scripts/b.py")
    assert rc == 0, err
    dispatch_id = out.split("Deliverable created: ", 1)[1].split()[0]
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    stored = json.loads(conn.execute(
        "SELECT metadata_json FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()[0])
    conn.close()
    assert stored.get("dispatch_paths") == ["scripts/lib/provider_spawns/a.py", "scripts/b.py"]

    rc, out, err = _cli(state_dir, "deliverable", "set", dispatch_id, "--dispatch-paths", "hooks/x.sh")
    assert rc == 0, err
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    stored = json.loads(conn.execute(
        "SELECT metadata_json FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()[0])
    conn.close()
    assert stored["dispatch_paths"] == ["hooks/x.sh"]
    assert stored["title"] == "Spawn hardening"


def test_second_seat_decision_function():
    second_seat_from_start = _new("plan_gate_panel", "second_seat_from_start")
    assert second_seat_from_start(None) is None
    assert second_seat_from_start(["scripts/lib/dispatch_cli.py"]) is None
    assert second_seat_from_start(["hooks/x.sh"]) == "security path hooks/x.sh"
    assert second_seat_from_start(["scripts/migrations/0001.sql"]) == (
        "schema path scripts/migrations/0001.sql"
    )


# --------------------------------------------------------------------------
# One classification
# --------------------------------------------------------------------------

def test_the_second_scope_classifier_is_gone():
    import plan_gate_enforcement

    assert not hasattr(plan_gate_enforcement, "plan_gate_scope")
    assert not hasattr(plan_gate_enforcement, "LIGHT_PANEL_LABELS")


# --------------------------------------------------------------------------
# Raising the scope and seeding OI-PLAN are one step
# --------------------------------------------------------------------------

_ARCH_PATH = ["scripts/lib/provider_spawns/kimi_spawn.py"]


def test_a_failed_seed_never_leaves_architecture_by_rule_without_oi_plan(tmp_path, monkeypatch):
    sd = _door_store(tmp_path, monkeypatch)
    _make_track(sd, "feat-door", {"plan_scope": "light"})
    _fail_plan_seeds(sd)

    rc, worker = _fire_at(sd, _ARCH_PATH, "20261018-fail")

    assert rc == 0, "the door refuses nothing in P1, a failed seed included"
    worker.assert_called_once()
    meta = _meta(sd, "feat-door")
    assert _plan_rows(sd, "feat-door") == []
    assert meta.get("plan_scope") == "light", "the raise rolled back with the seed"
    assert "plan_scope_rule" not in meta
    assert _scope_changes(sd, "feat-door") == [], "no ledger line for a raise that did not happen"


def test_a_failed_ledger_write_leaves_neither_the_scope_nor_the_blocker(state_dir, monkeypatch):
    import plan_scope
    import state_writer

    _make_track(state_dir, "feat-ledger", {"plan_scope": "light"})

    def refuse(*_args, **_kwargs):
        raise OSError("injected: the ledger write fails")

    monkeypatch.setattr(state_writer, "append_locked", refuse)
    with pytest.raises(OSError, match="injected"):
        plan_scope.raise_to_architecture(state_dir, "feat-ledger", PID, rule="4 deliverables (limit 4)")
    monkeypatch.undo()

    assert _meta(state_dir, "feat-ledger") == {"plan_scope": "light"}
    assert _plan_rows(state_dir, "feat-ledger") == []


def test_door_retry_after_a_failed_seed_ends_with_an_open_oi_plan(tmp_path, monkeypatch):
    sd = _door_store(tmp_path, monkeypatch)
    _make_track(sd, "feat-door", {"plan_scope": "light"})
    _fail_plan_seeds(sd)
    assert _fire_at(sd, _ARCH_PATH, "20261018-fail")[0] == 0
    _heal_plan_seeds(sd)

    rc, _ = _fire_at(sd, _ARCH_PATH, "20261018-retry")

    assert rc == 0
    assert _meta(sd, "feat-door")["plan_scope"] == "architecture"
    assert _open_plan_blocker(sd, "feat-door")


def test_set_plan_scope_retry_after_a_failed_seed_ends_with_an_open_oi_plan(tmp_path, monkeypatch):
    sd = _door_store(tmp_path, monkeypatch)
    _make_track(sd, "feat-door", {"plan_scope": "light"})
    _fail_plan_seeds(sd)
    assert _fire_at(sd, _ARCH_PATH, "20261018-fail")[0] == 0
    _heal_plan_seeds(sd)

    rc, out, err = _cli(sd, "horizon", "set-plan-scope", "feat-door", "architecture",
                        "--reason", "spawn code changes")

    assert rc == 0, err
    assert _meta(sd, "feat-door")["plan_scope"] == "architecture"
    assert _open_plan_blocker(sd, "feat-door")


def _born_architecture_without_oi_plan(sd: Path, track_id: str) -> None:
    """``horizon add --plan-scope architecture`` whose seed write failed."""
    _fail_plan_seeds(sd)
    rc, _, err = _cli(sd, "horizon", "add", track_id, "Born", "shipped", "--plan-scope", "architecture")
    assert rc == 0, err
    _heal_plan_seeds(sd)
    assert _meta(sd, track_id)["plan_scope"] == "architecture"
    assert _plan_rows(sd, track_id) == []


@pytest.mark.parametrize("paths", [_ARCH_PATH, ["docs/core/HORIZON_LIFECYCLE.md"]],
                         ids=["architecture-change", "light-change"])
def test_next_door_pass_seeds_oi_plan_on_an_architecture_track_that_has_none(
    tmp_path, monkeypatch, capsys, paths,
):
    sd = _door_store(tmp_path, monkeypatch)
    _born_architecture_without_oi_plan(sd, "feat-born")
    capsys.readouterr()

    rc, worker = _fire_at(sd, paths, "20261018-recover", track_id="feat-born")

    assert rc == 0
    worker.assert_called_once()
    assert _open_plan_blocker(sd, "feat-born")
    lines = [ln for ln in capsys.readouterr().err.splitlines() if "plan-scope:" in ln]
    assert len(lines) == 1
    assert "OI-PLAN-feat-born seeded" in lines[0]

    rc, _ = _fire_at(sd, paths, "20261018-again", track_id="feat-born")
    assert rc == 0
    assert len(_plan_rows(sd, "feat-born")) == 1, "seeding a missing blocker is idempotent"
    assert "plan-scope:" not in capsys.readouterr().err


def test_set_plan_scope_seeds_oi_plan_on_an_architecture_track_that_has_none(state_dir):
    _born_architecture_without_oi_plan(state_dir, "feat-born")

    rc, out, err = _cli(state_dir, "horizon", "set-plan-scope", "feat-born", "architecture",
                        "--reason", "confirm the scope")

    assert rc == 0, err
    assert _open_plan_blocker(state_dir, "feat-born")
    assert "OI-PLAN-feat-born seeded" in out


def test_a_resolved_oi_plan_on_an_architecture_track_stays_resolved(tmp_path, monkeypatch):
    sd = _door_store(tmp_path, monkeypatch)
    _make_track(sd, "feat-door", {"plan_scope": "architecture"}, seed=True)
    _resolve_plan_blocker(sd, "feat-door")

    assert _fire_at(sd, _ARCH_PATH, "20261018-pass")[0] == 0
    rc, _, err = _cli(sd, "horizon", "set-plan-scope", "feat-door", "architecture",
                      "--reason", "still architecture")
    assert rc == 0, err

    rows = _plan_rows(sd, "feat-door")
    assert len(rows) == 1
    assert rows[0][1] is not None, "a resolved OI-PLAN is never reopened"


# --------------------------------------------------------------------------
# objective sync and set-lane-hint never put back a stale scope or lane_hint
# --------------------------------------------------------------------------

_RAISE_RULE = "door, dispatch d-concurrent: schema path schemas/migrations/0099_x.sql"


def _synced_track(state_dir: Path, roadmap: Path) -> None:
    """feat-sync from the ROADMAP, then a stored light scope and lane_hint governed."""
    roadmap.write_text(_ROADMAP.format(risk="high"), encoding="utf-8")
    assert _cli(state_dir, "horizon", "sync", "--apply", "--roadmap", str(roadmap))[0] == 0
    assert _cli(state_dir, "horizon", "set-plan-scope", "feat-sync", "light",
                "--reason", "one small PR")[0] == 0
    assert _cli(state_dir, "horizon", "set-lane-hint", "feat-sync", "governed")[0] == 0
    roadmap.write_text(_ROADMAP.format(risk="low"), encoding="utf-8")


def _concurrent(state_dir: Path, write: str):
    import plan_scope

    def raise_scope():
        plan_scope.raise_to_architecture(state_dir, "feat-sync", PID, rule=_RAISE_RULE)

    def set_lane():
        assert _cli(state_dir, "horizon", "set-lane-hint", "feat-sync", "direct")[0] == 0

    return {"scope": raise_scope, "lane": set_lane}[write]


def _hook_once(monkeypatch, owner, attr: str, before) -> None:
    """Run ``before`` once, right before the first call to ``owner.attr``."""
    real = getattr(owner, attr)
    fired: list[bool] = []

    def hooked(*args, **kwargs):
        if not fired:
            fired.append(True)
            before()
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, attr, hooked)


@pytest.mark.parametrize("write", ["scope", "lane"])
@pytest.mark.parametrize("hook_point", ["before-the-seeder-runs", "between-the-seeders-read-and-write"])
def test_objective_sync_keeps_a_planning_key_written_while_it_runs(
    state_dir, tmp_path, monkeypatch, write, hook_point,
):
    roadmap = tmp_path / "ROADMAP.yaml"
    _synced_track(state_dir, roadmap)
    other = _concurrent(state_dir, write)
    if hook_point == "before-the-seeder-runs":
        _hook_once(monkeypatch, planning_cli.seeder, "seed", other)
    else:
        _hook_once(monkeypatch, tracks, "update_authored_fields", other)

    rc, _, err = _cli(state_dir, "horizon", "sync", "--apply", "--roadmap", str(roadmap))

    assert rc == 0, err
    meta = _meta(state_dir, "feat-sync")
    assert meta["risk_class"] == "low", "the seeder did rewrite metadata_json"
    latest = _scope_events(state_dir, "feat-sync")[-1]["details"]["new"]
    if write == "scope":
        assert meta.get("plan_scope") == "architecture"
        assert meta.get("plan_scope_rule") == _RAISE_RULE
        assert latest == "architecture", "store and ledger agree"
    else:
        assert meta.get("lane_hint") == "direct"
        assert (meta.get("plan_scope"), latest) == ("light", "light")


def test_set_lane_hint_keeps_a_scope_raised_after_it_read_the_track(state_dir, monkeypatch):
    import plan_scope

    _make_track(state_dir, "feat-race", {"plan_scope": "light", "lane_hint": "governed"})
    real_get = tracks.get_track
    fired: list[bool] = []

    def read_then_raise(*args, **kwargs):
        row = real_get(*args, **kwargs)
        if not fired:
            fired.append(True)
            plan_scope.raise_to_architecture(state_dir, "feat-race", PID, rule=_RAISE_RULE)
        return row

    monkeypatch.setattr(tracks, "get_track", read_then_raise)
    rc, _, err = _cli(state_dir, "horizon", "set-lane-hint", "feat-race", "direct")

    assert rc == 0, err
    assert fired
    meta = _meta(state_dir, "feat-race")
    assert meta.get("lane_hint") == "direct"
    assert meta.get("plan_scope") == "architecture", "a lane_hint write never lowers the scope"


def test_update_authored_fields_never_writes_the_planning_keys(state_dir):
    _make_track(state_dir, "feat-auth", {"plan_scope": "architecture", "lane_hint": "governed",
                                         "risk_class": "high"})

    tracks.update_authored_fields(state_dir, "feat-auth", PID, metadata_json=json.dumps(
        {"risk_class": "low", "plan_scope": "light", "lane_hint": "direct"}))

    assert _meta(state_dir, "feat-auth") == {
        "risk_class": "low", "plan_scope": "architecture", "lane_hint": "governed",
    }


# --------------------------------------------------------------------------
# The initial scope is a decision in the ledger (ADR-005)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("scope", ["light", "architecture"])
def test_horizon_add_records_the_initial_scope_as_one_event(state_dir, scope):
    rc, _, err = _cli(state_dir, "horizon", "add", "feat-born", "Born", "shipped", "--plan-scope", scope)

    assert rc == 0, err
    events = _scope_events(state_dir, "feat-born")
    assert len(events) == 1
    details = events[0]["details"]
    assert (details["old"], details["new"], details["rule"]) == ("none", scope, "horizon add")
    assert details["reason"].strip()


def test_create_track_refuses_an_unknown_plan_scope_and_writes_nothing(state_dir):
    with pytest.raises(ValueError, match="plan_scope"):
        tracks.create_track(state_dir, "feat-odd", PID, "Odd", "shipped",
                            metadata_json=json.dumps({"plan_scope": "heavy"}))

    assert tracks.get_track(state_dir, "feat-odd", PID) is None
    assert _scope_events(state_dir, "feat-odd") == []


# --------------------------------------------------------------------------
# One normalisation before every classification (round 3)
# --------------------------------------------------------------------------

# An unclean spelling of a path and the clean path it names: an internal "./",
# a repeated separator, a leading "./", a ".." segment and backslashes.
_UNCLEAN_SECOND_SEAT_PATHS = [
    (".github/./workflows/vnx-ci.yml", ".github/workflows/vnx-ci.yml"),
    (".github//workflows/vnx-ci.yml", ".github/workflows/vnx-ci.yml"),
    ("./.github/workflows/vnx-ci.yml", ".github/workflows/vnx-ci.yml"),
    ("scripts/lib/../lib/provider_spawns/kimi_spawn.py", "scripts/lib/provider_spawns/kimi_spawn.py"),
    (".claude/./settings.json", ".claude/settings.json"),
    ("schemas//migrations/0041_y.sql", "schemas/migrations/0041_y.sql"),
    ("scripts\\migrations\\0040_x.sql", "scripts/migrations/0040_x.sql"),
]
_UNCLEAN_PATHS = _UNCLEAN_SECOND_SEAT_PATHS + [
    ("scripts/lib/./dispatch_cli.py", "scripts/lib/dispatch_cli.py"),
]
_OUTSIDE_PATHS = ["../outside/x.py", "scripts/../../outside/x.py", "..", "/tmp/outside/x.py",
                  "C:\\outside\\x.py"]


@pytest.mark.parametrize("unclean, clean", _UNCLEAN_PATHS, ids=[u for u, _ in _UNCLEAN_PATHS])
def test_an_unclean_path_classifies_as_its_clean_path(tmp_path, unclean, clean):
    import plan_gate_panel as pgp

    dispatch_rule = _new("plan_scope", "dispatch_rule")
    category_of = _new("smart_router", "plan_scope_path_category")
    kind_of = _new("smart_router", "security_path_kind")
    cfg = tmp_path / "plan_gate_panel.yaml"
    cfg.write_text(_PANEL_CFG, encoding="utf-8")

    assert dispatch_rule([clean]) is not None, "the clean path is an architecture path"
    assert dispatch_rule([unclean]) == dispatch_rule([clean])
    assert category_of(unclean) == category_of(clean)
    assert kind_of(unclean) == kind_of(clean)
    assert pgp.compose_panel(dispatch_paths=[unclean], config_path=cfg) == (
        pgp.compose_panel(dispatch_paths=[clean], config_path=cfg)
    )


@pytest.mark.parametrize("unclean, clean", _UNCLEAN_SECOND_SEAT_PATHS,
                         ids=[u for u, _ in _UNCLEAN_SECOND_SEAT_PATHS])
def test_plan_gate_on_an_unclean_path_runs_both_seats_and_one_voice_clears_nothing(
    gate, state_dir, unclean, clean,
):
    lanes = _Lanes({"codex": _seat_report("pass"), "claude": "a prose review with no verdict"})
    result = gate(lanes, dispatch_paths=unclean)

    assert lanes.providers[:2] == ["codex", "claude"], "both seats read the plan from the start"
    assert result.decision != "PASS", "one readable voice does not pass this plan"
    assert _open_plan_blocker(state_dir, "feat-gate"), "OI-PLAN stays open"
    assert "both seats from the start (" in result.gov_trace
    assert f" path {clean})" in result.gov_trace


@pytest.mark.parametrize("unclean, clean", _UNCLEAN_SECOND_SEAT_PATHS[:3] + _UNCLEAN_SECOND_SEAT_PATHS[4:5],
                         ids=["internal-dot", "double-slash", "leading-dot", "claude-settings"])
def test_door_reads_an_unclean_path_as_its_clean_path(tmp_path, monkeypatch, unclean, clean):
    rc, sd, worker = _fire(tmp_path, monkeypatch, [unclean], meta={"plan_scope": "light"})

    assert rc == 0
    worker.assert_called_once()
    meta = _meta(sd, "feat-door")
    assert meta["plan_scope"] == "architecture"
    assert f"security path {clean}" in meta["plan_scope_rule"]
    assert _open_plan_blocker(sd, "feat-door")


@pytest.mark.parametrize("outside", _OUTSIDE_PATHS)
def test_a_path_that_leaves_the_repository_classifies_as_architecture(tmp_path, outside):
    import plan_gate_panel as pgp

    dispatch_rule = _new("plan_scope", "dispatch_rule")
    category_of = _new("smart_router", "plan_scope_path_category")
    cfg = tmp_path / "plan_gate_panel.yaml"
    cfg.write_text(_PANEL_CFG, encoding="utf-8")

    assert category_of(outside) == "outside-repo"
    assert (dispatch_rule([outside]) or "").startswith("outside-repo path ")
    assert dispatch_rule(["docs/core/HORIZON_PLANNING.md", outside]) is not None
    composition = pgp.compose_panel(dispatch_paths=[outside], config_path=cfg)
    assert [s["label"] for s in composition.seats] == ["codex", "opus"]
    assert composition.quorum == 2


@pytest.mark.parametrize("outside", ["../outside/x.py", "/tmp/outside/x.py"])
def test_plan_gate_on_a_path_outside_the_repository_runs_both_seats(gate, state_dir, outside):
    lanes = _Lanes({"codex": _seat_report("pass"), "claude": "a prose review with no verdict"})
    result = gate(lanes, dispatch_paths=outside)

    assert lanes.providers[:2] == ["codex", "claude"]
    assert result.decision != "PASS"
    assert _open_plan_blocker(state_dir, "feat-gate")
