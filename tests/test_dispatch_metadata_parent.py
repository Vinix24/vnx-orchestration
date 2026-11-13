"""ADR-071 besluit 2: every lane writes a dispatch_metadata row at its start, and
parent_dispatch is fill-once. tmp_path stores only; no live store is opened."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "scripts" / "lib", REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dispatch_envelope  # noqa: E402
import quality_db_init  # noqa: E402
from dispatch_envelope import _AdapterResult, run_envelope_headless_plan  # noqa: E402
from dispatch_internal import issue_permit  # noqa: E402
from dispatch_metadata_db import stamp_lane_start, upsert_dispatch_provider_row  # noqa: E402
from dispatch_plan import RuntimeSnapshot, compile_plan  # noqa: E402
from dispatch_spec import DispatchSpec, Provider, ValidatedSpec  # noqa: E402


def _state(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    assert quality_db_init.bootstrap_qi_db(
        state_dir / "quality_intelligence.db", REPO / "schemas" / "quality_intelligence.sql"
    )
    return state_dir


def _rows(state_dir: Path, dispatch_id: str) -> list:
    conn = sqlite3.connect(str(state_dir / "quality_intelligence.db"))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM dispatch_metadata WHERE dispatch_id = ?", (dispatch_id,)
        ).fetchall()
    finally:
        conn.close()


def _headless_plan(tmp_path: Path, parent: str):
    instr = tmp_path / "instruction.md"
    instr.write_text("# Test instruction\n", encoding="utf-8")
    spec = DispatchSpec(
        schema_version=1, project_id="vnx-dev", dispatch_id="dlv-parent-1",
        staging_id="staging-parent", instruction_file=instr, role="backend-developer",
        target_slot="T1", gate="codex_gate", dispatch_paths=(), provider=Provider.CLAUDE,
        model="sonnet", pr_id="77", allow_headless=True, headless_reason="test",
    )
    text = "# Test instruction\n"
    vspec = ValidatedSpec(
        spec=spec, instruction_text=text, normalized_paths=(),
        instruction_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    slots = ["T0", "T1", "T2", "T3"]
    plan = compile_plan(vspec, RuntimeSnapshot(
        staging_promoted=True, target_health={s: "healthy" for s in slots},
        target_capable={s: True for s in slots}, parent_dispatch=parent,
    ))
    assert plan.lane == "claude_headless" and plan.parent_dispatch == parent
    return plan


def _run_headless(tmp_path: Path, state_dir: Path, parent: str) -> None:
    plan = _headless_plan(tmp_path, parent)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    receipt = state_dir / "t0_receipts.ndjson"
    receipt.touch()
    with patch.object(dispatch_envelope.ClaudeSubprocessAdapter, "run",
                      return_value=_AdapterResult(returncode=0, completion_text="done", status="success")), \
         patch("dispatch_worktree_isolation.resolve_consumer_project_root",
               return_value=tmp_path / "consumer-root"), \
         patch("dispatch_worktree_isolation.create_dispatch_worktree",
               return_value=tmp_path / "fake-wt"), \
         patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
         patch("dispatch_envelope._govern", return_value=(None, receipt)):
        run_envelope_headless_plan(
            plan, issue_permit(plan), state_dir=state_dir, data_dir=data_dir, role=plan.role)


def test_headless_start_writes_one_row_with_parent(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    _run_headless(tmp_path, state_dir, "dlv-origin-1")
    rows = _rows(state_dir, "dlv-parent-1")
    assert len(rows) == 1
    assert rows[0]["parent_dispatch"] == "dlv-origin-1"
    assert (rows[0]["provider"], rows[0]["model"], rows[0]["pr_id"]) == ("claude", "sonnet", "77")


def test_subprocess_start_writes_one_row_with_parent(tmp_path: Path, monkeypatch) -> None:
    import subprocess_dispatch as sd
    from subprocess_dispatch_internals import recovery

    state_dir = _state(tmp_path)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(state_dir.parent))
    ok = SimpleNamespace(success=True)
    with patch.object(sd, "_get_commit_hash", return_value="abc"), \
         patch.object(sd, "_get_dirty_files", return_value=set()), \
         patch.object(sd, "WorkerHealthMonitor"), \
         patch.object(recovery, "_attempt_delivery", return_value=ok), \
         patch.object(recovery, "_handle_success"):
        assert sd.deliver_with_recovery(
            "T2", "do it", "sonnet", "dlv-sub-1", role="backend-developer",
            pr_id="88", parent_dispatch="dlv-origin-2",
        )
    rows = _rows(state_dir, "dlv-sub-1")
    assert len(rows) == 1
    assert rows[0]["parent_dispatch"] == "dlv-origin-2"
    assert (rows[0]["terminal"], rows[0]["track"], rows[0]["provider"]) == ("T2", "B", "claude")


def test_parent_is_fill_once(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    kw = dict(dispatch_id="dlv-once", terminal="T1", provider="claude", model="sonnet")
    assert stamp_lane_start(state_dir, parent_dispatch="dlv-first", **kw)
    assert stamp_lane_start(state_dir, parent_dispatch="dlv-second", **kw)
    assert _rows(state_dir, "dlv-once")[0]["parent_dispatch"] == "dlv-first"
    assert stamp_lane_start(state_dir, **kw)
    assert stamp_lane_start(state_dir, parent_dispatch="  ", **kw)
    rows = _rows(state_dir, "dlv-once")
    assert len(rows) == 1 and rows[0]["parent_dispatch"] == "dlv-first"


def test_parent_fills_an_empty_slot_on_an_existing_row(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    kw = dict(dispatch_id="dlv-late", terminal="T1", provider="claude", model="sonnet")
    stamp_lane_start(state_dir, **kw)
    assert _rows(state_dir, "dlv-late")[0]["parent_dispatch"] is None
    stamp_lane_start(state_dir, parent_dispatch="dlv-origin", **kw)
    assert _rows(state_dir, "dlv-late")[0]["parent_dispatch"] == "dlv-origin"


def test_existing_gate_row_keeps_its_shape(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    db = state_dir / "quality_intelligence.db"
    assert upsert_dispatch_provider_row(
        db, dispatch_id="dlv-gate", terminal="T3", provider="codex", model="gpt-x",
        track="C", role="reviewer", gate="codex_gate", pr_id="5", outcome_status="done",
        report_path="/tmp/r.md",
    )
    before = dict(_rows(state_dir, "dlv-gate")[0])
    stamp_lane_start(state_dir, dispatch_id="dlv-gate", terminal="T1", provider="claude",
                     model="sonnet", role="other", pr_id="9", gate="g2")
    after = dict(_rows(state_dir, "dlv-gate")[0])
    # provider is the writer's authoritative stamp (unchanged behaviour); everything else is kept.
    after_cmp = {k: v for k, v in after.items() if k != "provider"}
    before_cmp = {k: v for k, v in before.items() if k != "provider"}
    assert after_cmp == before_cmp
    stamp_lane_start(state_dir, dispatch_id="dlv-gate", terminal="T1", provider="codex",
                     parent_dispatch="dlv-origin")
    final = dict(_rows(state_dir, "dlv-gate")[0])
    assert final["parent_dispatch"] == "dlv-origin"
    assert {k: v for k, v in final.items() if k != "parent_dispatch"} == {
        k: v for k, v in before.items() if k != "parent_dispatch"}


def test_missing_db_does_not_break_the_lane_start(tmp_path: Path) -> None:
    empty = tmp_path / "no-state"
    empty.mkdir()
    assert stamp_lane_start(empty, dispatch_id="d", terminal="T1", provider="claude",
                            parent_dispatch="p") is False
    assert not (empty / "quality_intelligence.db").exists()


def test_missing_db_headless_start_still_succeeds(tmp_path: Path) -> None:
    state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    _run_headless(tmp_path, state_dir, "dlv-origin-1")
    assert not (state_dir / "quality_intelligence.db").exists()


def test_empty_required_field_is_swallowed_not_raised(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    assert stamp_lane_start(state_dir, dispatch_id="", terminal="T1", provider="claude") is False
    assert _rows(state_dir, "") == []


# --- difficulty_json (herwerk-per-model-en-reviewer): migration v37 and the fill-once writer ---

def _v36_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    before = {k: v for k, v in quality_db_init.MIGRATIONS.items() if k < 37}
    with patch.dict(quality_db_init.MIGRATIONS, before, clear=True):
        assert quality_db_init.bootstrap_qi_db(
            state_dir / "quality_intelligence.db", REPO / "schemas" / "quality_intelligence.sql")
    return state_dir


def _cols(state_dir: Path) -> set:
    conn = sqlite3.connect(str(state_dir / "quality_intelligence.db"))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)")}
    finally:
        conn.close()


def test_v37_adds_the_column_keeps_rows_and_is_idempotent(tmp_path: Path) -> None:
    state_dir = _v36_state(tmp_path)
    assert "difficulty_json" not in _cols(state_dir)
    assert stamp_lane_start(state_dir, dispatch_id="dlv-old", terminal="T1", provider="claude")
    conn = sqlite3.connect(str(state_dir / "quality_intelligence.db"))
    try:
        quality_db_init._migrate_v37(conn)
        quality_db_init._migrate_v37(conn)
        conn.commit()
    finally:
        conn.close()
    assert "difficulty_json" in _cols(state_dir)
    rows = _rows(state_dir, "dlv-old")
    assert len(rows) == 1 and rows[0]["difficulty_json"] is None


def test_difficulty_is_stored_and_fill_once(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    kw = dict(dispatch_id="dlv-diff", terminal="T1", provider="claude", model="sonnet")
    assert stamp_lane_start(state_dir, difficulty_json='{"file_count": 3}', **kw)
    assert stamp_lane_start(state_dir, difficulty_json='{"file_count": 9}', **kw)
    assert stamp_lane_start(state_dir, **kw)
    assert _rows(state_dir, "dlv-diff")[0]["difficulty_json"] == '{"file_count": 3}'


def test_difficulty_fills_an_empty_slot_on_an_existing_row(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    kw = dict(dispatch_id="dlv-late", terminal="T1", provider="claude")
    assert stamp_lane_start(state_dir, **kw)
    assert stamp_lane_start(state_dir, difficulty_json='{"file_count": 1}', **kw)
    assert _rows(state_dir, "dlv-late")[0]["difficulty_json"] == '{"file_count": 1}'


def test_store_without_the_column_takes_the_row_without_it(tmp_path: Path) -> None:
    state_dir = _v36_state(tmp_path)
    assert stamp_lane_start(state_dir, dispatch_id="dlv-v36", terminal="T1", provider="claude",
                            difficulty_json='{"file_count": 2}') is True
    assert len(_rows(state_dir, "dlv-v36")) == 1
    assert "difficulty_json" not in _cols(state_dir)
