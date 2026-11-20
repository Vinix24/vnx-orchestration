"""P1b: ``horizon plan-scope review`` and its approval-gated confirm, on a real migrated store."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_scope import (  # noqa: E402
    PID, _add_deliverable, _bootstrap, _cli, _make_track, _meta, _open_plan_blocker, _plan_rows,
)

import plan_scope  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    sd = _bootstrap(tmp_path / "data" / "state")
    for track, scope in (("t-refactor", "architecture"), ("t-security", "architecture"),
                         ("t-nopaths", "architecture"), ("t-light", "architecture"), ("t-done", "light")):
        _make_track(sd, track, {"plan_scope": scope}, seed=scope == "architecture")
    _add_deliverable(sd, "t-refactor", "r", "--task-class", "03_refactoring",
                     "--dispatch-paths", "scripts/lib/a.py")
    _add_deliverable(sd, "t-security", "s", "--task-class", "01_code_generation",
                     "--dispatch-paths", "schemas/migrations/0099_x.sql")
    _add_deliverable(sd, "t-nopaths", "n", "--task-class", "01_code_generation")
    _add_deliverable(sd, "t-light", "l", "--task-class", "01_code_generation",
                     "--dispatch-paths", "scripts/lib/plain.py")
    return sd


def _review(sd, *extra):
    rc, out, err = _cli(sd, "horizon", "plan-scope", "review", "--json", *extra)
    return rc, (json.loads(out) if out.strip() else None), err


def _by_track(payload):
    return {t["track_id"]: t for t in payload["tracks"]}


def test_review_proposes_per_track_with_reason(store):
    rc, payload, _ = _review(store)
    assert rc == 0
    tracks = _by_track(payload)
    assert set(tracks) == {"t-refactor", "t-security", "t-nopaths", "t-light"}  # t-done has no OI-PLAN
    assert tracks["t-refactor"]["proposal"] == "architecture" and "03_refactoring" in tracks["t-refactor"]["reason"]
    assert tracks["t-security"]["proposal"] == "architecture" and "schemas/migrations/0099_x.sql" in tracks["t-security"]["reason"]
    assert tracks["t-nopaths"]["proposal"] == "architecture" and "dispatch_paths missing" in tracks["t-nopaths"]["reason"]
    assert tracks["t-light"]["proposal"] == "light"
    assert (tracks["t-light"]["source"], tracks["t-light"]["plan_scope"]) == ("stored", "architecture")
    assert set(tracks["t-light"]) == {"track_id", "horizon", "plan_scope", "source", "proposal", "reason"}


def test_review_text_output_is_read_only(store):
    before = _plan_rows(store, "t-light")
    rc, out, _ = _cli(store, "horizon", "plan-scope", "review")
    assert rc == 0 and "proposal=light" in out and _plan_rows(store, "t-light") == before


@pytest.mark.parametrize("flags", [("--confirm", "t-light"), ("--confirm-all",)])
def test_confirm_without_approval_id_refuses_and_writes_nothing(store, flags):
    rc, _, err = _cli(store, "horizon", "plan-scope", "review", *flags)
    assert rc == 2 and "--approval-id" in err
    assert _open_plan_blocker(store, "t-light") and _meta(store, "t-light")["plan_scope"] == "architecture"


def test_confirm_light_stores_scope_and_resolves_oi(store):
    rc, payload, err = _review(store, "--confirm", "t-light", "--approval-id", "APR-7")
    assert rc == 0, err and [r["outcome"] for r in payload["confirm"]] == ["confirmed"]
    assert _meta(store, "t-light")["plan_scope"] == "light"
    assert not _open_plan_blocker(store, "t-light")
    assert _open_plan_blocker(store, "t-refactor")


def test_confirm_architecture_keeps_oi_open(store):
    rc, payload, _ = _review(store, "--confirm", "t-refactor", "--approval-id", "APR-7")
    assert rc == 0 and payload["confirm"][0]["outcome"] == "confirmed"
    assert _meta(store, "t-refactor")["plan_scope"] == "architecture"
    assert _open_plan_blocker(store, "t-refactor")


def test_confirm_unknown_track_refuses_everything(store):
    rc, _, err = _cli(store, "horizon", "plan-scope", "review", "--confirm", "t-light,t-done", "--approval-id", "A")
    assert rc == 2 and "t-done" in err
    assert _open_plan_blocker(store, "t-light")


def test_confirm_all_skips_a_track_whose_proposal_changed(store, monkeypatch):
    import planning_cli
    real = planning_cli._plan_scope_deliverables
    calls = {"n": 0}

    def flaky(state_dir, project_id, track_id):
        if track_id == "t-light":
            calls["n"] += 1
            if calls["n"] > 1:  # the re-read at confirm time sees a refactor deliverable
                return [{"ref": "x", "task_class": "03_refactoring", "dispatch_paths": ["a.py"]}]
        return real(state_dir, project_id, track_id)

    monkeypatch.setattr(planning_cli, "_plan_scope_deliverables", flaky)
    rc, payload, _ = _review(store, "--confirm-all", "--approval-id", "APR-9")
    assert rc == 0
    outcomes = {r["track_id"]: r["outcome"] for r in payload["confirm"]}
    assert outcomes["t-light"] == "skipped" and outcomes["t-refactor"] == "confirmed"
    assert _open_plan_blocker(store, "t-light")


@pytest.mark.parametrize("deliverables", [
    [], [None], [{"ref": "d"}], [{"ref": "d", "dispatch_paths": None}], [{"ref": "d", "dispatch_paths": "a.py"}],
    [{"ref": "d", "dispatch_paths": [3]}], [{"ref": "d", "dispatch_paths": []}], [{"ref": "d", "dispatch_paths": [""]}],
])
def test_propose_stays_architecture_when_unreadable(deliverables):
    assert plan_scope.propose(deliverables).scope == "architecture"


def test_propose_unknown_task_class_is_not_a_refactor():
    assert plan_scope.propose([{"ref": "d", "task_class": "99_x", "dispatch_paths": ["scripts/lib/p.py"]}]).scope == "light"


def test_unreadable_metadata_json_stays_architecture(store):
    import sqlite3
    conn = sqlite3.connect(str(store / "runtime_coordination.db"))
    conn.execute("UPDATE tracks SET metadata_json = '{broken' WHERE track_id = 't-light'")
    conn.commit()
    conn.close()
    _, payload, _ = _review(store)
    row = _by_track(payload)["t-light"]
    assert row["source"] == "unreadable" and row["plan_scope"] == "architecture"
