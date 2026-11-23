"""ADR-078 M4 test duties: the memory scan inside the T0 state build."""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))
import build_t0_state as bts
import memory_ledger as ml
import pr_queue_state


def _note(extra=""):
    return "---\nname: n\ndescription: d\ntype: feedback\n%s---\nbody\n" % extra


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "proj")
    monkeypatch.setattr(pr_queue_state, "_get_open_prs", lambda project_root: ([], None))
    monkeypatch.setattr(pr_queue_state, "_get_merged_today", lambda project_root: ([], None, False))
    mem = tmp_path / "memory"
    mem.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    dispatches = tmp_path / "dispatches"
    for sub in ("pending", "active", "conflicts"):
        (dispatches / sub).mkdir(parents=True)
    return mem, state, dispatches


def _bind(mem, state):
    (state / ml.SOURCES_FILE).write_text(json.dumps(
        {"schema": ml.SOURCES_SCHEMA, "project_id": "proj", "memory_dirs": [str(mem)]}))


def test_bound_folder_shows_ok_with_counts(env):
    mem, state, _ = env
    (mem / "a.md").write_text(_note())
    (mem / "old.md").write_text(_note("invalid_at: 2020-01-01\n"))
    (mem / "new.md").write_text(_note("superseded_by: a.md\n"))
    _bind(mem, state)
    block = bts._build_memory_ledger(state)
    assert block == {"status": "ok", "reason": "ok", "notes": 3, "written": 3,
                     "unknown": 0, "out_of_force": 2}
    again = bts._build_memory_ledger(state)
    assert again["written"] == 0 and again["notes"] == 3


def test_unreadable_note_counts_as_unknown(env):
    mem, state, _ = env
    (mem / "a.md").write_text(_note())
    (mem / "dead.md").symlink_to(mem / "missing.md")  # cannot be read: the scan says so
    _bind(mem, state)
    block = bts._build_memory_ledger(state)
    assert block["status"] == "ok" and block["unknown"] == 1 and block["notes"] == 1


def test_scan_that_raises_gives_error_and_state_still_builds(env, monkeypatch):
    mem, state, dispatches = env
    _bind(mem, state)

    def boom(*_a, **_k):
        raise RuntimeError("x" * 500)
    monkeypatch.setattr(ml, "scan", boom)
    block = bts._build_memory_ledger(state)
    assert block["status"] == "error" and block["reason"].startswith("RuntimeError: xxx")
    assert len(block["reason"]) == 200 and set(block) == {"status", "reason"}
    doc = bts.build_t0_state(state_dir=state, dispatch_dir=dispatches)
    assert doc["memory_ledger"]["status"] == "error"
    bts._write_atomic(state / "t0_state.json", doc)
    assert json.loads((state / "t0_state.json").read_text())["memory_ledger"]["status"] == "error"


def test_budget_runs_out_then_next_build_continues(env, monkeypatch):
    mem, state, _ = env
    for name in ("a.md", "b.md", "c.md"):
        (mem / name).write_text(_note())
    _bind(mem, state)
    ticks = iter([0.0, 0.0, 5.0])
    real = ml.scan
    monkeypatch.setattr(
        ml, "scan",
        lambda sd, budget=None: real(sd, budget=budget, clock=lambda: next(ticks, 9.0)))
    first = bts._build_memory_ledger(state)
    assert first["status"] == "partial" and first["reason"] == "budget" and first["notes"] == 1
    monkeypatch.setattr(ml, "scan", real)
    second = bts._build_memory_ledger(state)
    assert second["status"] == "ok" and second["notes"] == 3


def test_unbound_has_no_count_fields(env):
    _, state, _ = env
    block = bts._build_memory_ledger(state)
    assert block["status"] == "unbound" and set(block) == {"status", "reason"}
