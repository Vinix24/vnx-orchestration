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
    assert _scope_events(state_dir, "feat-a4") == []


# --------------------------------------------------------------------------
# Upward by itself: the dispatch door, through a real fire
# --------------------------------------------------------------------------

def _fire(tmp_path: Path, monkeypatch, paths: list[str], *, meta: dict | None, seed=False,
          resolve=False, dispatch_id="20261018-fire") -> tuple[int, Path, object]:
    """One real run_dispatch on a promoted bundle; the worker itself is a mock."""
    import dispatch_cli

    data_dir = tmp_path / "vnx-data"
    sd = _bootstrap(data_dir / "state")
    _make_track(sd, "feat-door", meta, seed=seed)
    if resolve:
        _resolve_plan_blocker(sd, "feat-door")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "advisory")

    bundle = data_dir / "dispatches" / "pending" / f"stage-{dispatch_id}"
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
        "isolation": "worktree", "track_id": "feat-door",
    }), encoding="utf-8")

    with patch("dispatch_cli._execute_claude_headless", return_value=0) as worker:
        rc = dispatch_cli.run_dispatch(spec_file)
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
    events = _scope_events(sd, "feat-door")
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
    assert _scope_events(sd, "feat-door") == []


# --------------------------------------------------------------------------
# Down only by hand
# --------------------------------------------------------------------------

def test_set_plan_scope_down_without_reason_is_refused(state_dir):
    _make_track(state_dir, "feat-down", {"plan_scope": "architecture"}, seed=True)
    rc, _, err = _cli(state_dir, "horizon", "set-plan-scope", "feat-down", "light")
    assert rc == 2
    assert "--reason is required" in err
    assert _meta(state_dir, "feat-down")["plan_scope"] == "architecture"
    assert _scope_events(state_dir, "feat-down") == []


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
    events = _scope_events(state_dir, "feat-down")
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
    _make_track(state_dir, "feat-odd", {"plan_scope": "heavy"})
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


def test_objective_sync_restores_plan_scope_after_the_seeder_rewrites_metadata(state_dir, tmp_path):
    """The ROADMAP seeder rebuilds metadata_json wholesale on an authored change
    (risk_class here); plan_scope, its rule and lane_hint must come back."""
    roadmap = tmp_path / "ROADMAP.yaml"
    roadmap.write_text(_ROADMAP.format(risk="high"), encoding="utf-8")
    rc, _, err = _cli(state_dir, "horizon", "sync", "--apply", "--roadmap", str(roadmap))
    assert rc == 0, err
    meta = _meta(state_dir, "feat-sync")
    meta.update({"plan_scope": "architecture", "plan_scope_rule": "door, dispatch d1: schema path x",
                 "lane_hint": "governed"})
    tracks.update_authored_fields(state_dir, "feat-sync", PID, metadata_json=json.dumps(meta))

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


@pytest.fixture
def gate(tmp_path, monkeypatch, state_dir):
    import plan_gate_panel as pgp
    import track_reconciler

    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_PANEL_RETRY", raising=False)
    monkeypatch.setattr(track_reconciler, "reconcile_track", lambda *a, **k: None)
    cfg = tmp_path / "plan_gate_panel.yaml"
    cfg.write_text(
        "version: 1\ndefault_seat: codex\nescalation_seat: opus\n"
        "tiebreaker:\n  max_rounds: 2\n  provider: deepseek_harness\n  model: deepseek-v4-pro\n"
        "seats:\n"
        "  - {label: opus, provider: claude, model_arg: opus}\n"
        "  - {label: codex, provider: codex, model_arg: gpt-test}\n",
        encoding="utf-8",
    )
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
