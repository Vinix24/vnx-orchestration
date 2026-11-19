"""reconcile marks an approved edit applied only on its pr_merged receipt (ochtendronde D3c part B)."""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import apply_suggested_edits as ase
import suggested_edit_dispatch as sed

PID = "vnx-dev"
RULE = "Reviewers keep finding a missing fail-closed check on evidence reads"
TARGET = "agents/backend-developer/CLAUDE.md"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "agents" / "backend-developer").mkdir(parents=True)
    (root / TARGET).write_text("# Backend\n\n## Wat review het vaakst vindt\n\n- old rule\n\n## Other\n\nx\n")
    store = tmp_path / "store"
    state = store / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(ase, "PROJECT_ROOT", root)
    monkeypatch.setattr(ase, "STATE_DIR", state)
    monkeypatch.setattr(ase, "PENDING_PATH", state / "pending_edits.json")
    monkeypatch.setattr(ase, "HISTORY_PATH", state / "edit_history.json")
    monkeypatch.setattr(ase, "_project_id", lambda: PID)
    edit = {"id": 1, "category": "role", "target": TARGET, "section": ase.ROLE_SECTION, "action": "append",
            "content": f"- {RULE}", "status": "pending", "evidence": "2 dispatches", "confidence": 0.8}
    ase._save_pending({"generated_at": "", "edits": [edit]})
    return root, state, store


def _pending(state):
    return json.loads((state / "pending_edits.json").read_text())["edits"]


def _ledger(state, *lines):
    (state / "t0_receipts.ndjson").write_text("".join(
        (l if isinstance(l, str) else json.dumps(l)) + "\n" for l in lines))


def _merged(did, project=PID, pr=3060, sha="0123456789abcdef"):
    return {"event_type": "pr_merged", "conclusion": "merged", "dispatch_id": did,
            "project_id": project, "pr_number": pr, "merge_sha": sha}


def _approve(env):
    assert ase.cmd_accept("1") == 0
    return _pending(env[1])[0]



def test_reconcile_marks_applied_with_ref_remeasure_and_baseline(env):
    root, state, store = env
    did = _approve(env)["dispatch_id"]
    _ledger(state, _merged("other-dispatch"), _merged(did))
    before = datetime.now(tz=timezone.utc)
    assert ase.cmd_reconcile() == 0
    assert _pending(state) == []
    (entry,) = json.loads((state / "edit_history.json").read_text())
    assert entry["status"] == "applied" and entry["applied_ref"] == "#3060@0123456789ab"
    applied = datetime.fromisoformat(entry["applied_at"].replace("Z", "+00:00"))
    assert applied >= before - timedelta(seconds=1)
    assert datetime.fromisoformat(entry["remeasure_at"].replace("Z", "+00:00")) - applied == timedelta(days=14)
    assert entry["baseline"]["window_days"] == 14 and entry["baseline"]["status"] == "unknown"
    assert entry["baseline"]["count"] is None and entry["baseline"]["reason"]
    assert ase.cmd_reconcile() == 0  # idempotent
    assert len(json.loads((state / "edit_history.json").read_text())) == 1


@pytest.mark.parametrize("ledger", ["none", "empty", "other_project", "other_dispatch", "not_merged", "no_sha"])
def test_reconcile_without_evidence_changes_nothing(env, ledger):
    root, state, store = env
    did = _approve(env)["dispatch_id"]
    lines = {"none": [], "empty": [],
             "other_project": [_merged(did, project="other")],
             "other_dispatch": [_merged(did + "x"), _merged("x" + did)],
             "not_merged": [{**_merged(did), "conclusion": "closed"}, {**_merged(did), "event_type": "x"}],
             "no_sha": [{**_merged(did), "merge_sha": None}]}[ledger]
    _ledger(state, *lines)
    ase.cmd_reconcile()
    assert _pending(state)[0]["status"] == "approved"
    assert not (state / "edit_history.json").exists()


def test_reconcile_corrupt_or_missing_ledger_is_refused(env, capsys):
    root, state, store = env
    did = _approve(env)["dispatch_id"]
    (state / "t0_receipts.ndjson").unlink()  # accept wrote the approval receipt; now no ledger file at all
    assert ase.cmd_reconcile() == 1
    _ledger(state, json.dumps(_merged(did)), "{not json")
    assert ase.cmd_reconcile() == 1
    assert "unreadable" in capsys.readouterr().err
    assert _pending(state)[0]["status"] == "approved" and not (state / "edit_history.json").exists()


def test_failed_dispatch_leaves_edit_approved_with_state(env, monkeypatch):
    root, state, store = env
    did = _approve(env)["dispatch_id"]
    _ledger(state)
    monkeypatch.setattr(ase, "_dispatch_states", lambda pid, wanted: {did: ("rejected_dispatch", "bad lane")})
    assert ase.cmd_reconcile() == 0
    edit = _pending(state)[0]
    assert edit["status"] == "approved" and edit["dispatch_state"] == "rejected_dispatch"
    assert edit["dispatch_state_reason"] == "bad lane"


def test_unreadable_outcomes_is_visible_and_keeps_edit(env, monkeypatch, capsys):
    root, state, store = env
    _approve(env)
    _ledger(state)

    def boom(pid, wanted):
        raise RuntimeError("ledger gone")
    monkeypatch.setattr(ase, "_dispatch_states", boom)
    assert ase.cmd_reconcile() == 1
    assert "outcomes unreadable" in capsys.readouterr().err
    assert _pending(state)[0]["status"] == "approved"


def _db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE antipatterns (project_id TEXT, title TEXT, description TEXT, "
                 "source_dispatch_ids TEXT, last_seen TEXT)")
    conn.executemany("INSERT INTO antipatterns VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_baseline_counts_distinct_dispatches_in_window(tmp_path):
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    db = tmp_path / "q.db"
    _db(db, [(PID, "t", RULE, '["d1","d2","d1"]', "2026-11-10 08:00:00"),
             (PID, "t", RULE, '["d3"]', "2026-11-18 08:00:00"),
             (PID, "t", RULE, '["d9"]', "2026-10-01 08:00:00"),
             ("other", "t", RULE, '["d8"]', "2026-11-10 08:00:00")])
    out = sed.baseline_for({"content": f"- {RULE}"}, PID, db, now)
    assert out["status"] == "ok" and out["count"] == 3 and out["window_days"] == 14


@pytest.mark.parametrize("rows,reason", [
    ([], "no antipattern row"),
    ([(PID, "t", RULE, "not json", "2026-11-10 08:00:00")], "source_dispatch_ids unreadable"),
])
def test_baseline_unknown_is_never_zero(tmp_path, rows, reason):
    db = tmp_path / "q.db"
    _db(db, rows)
    out = sed.baseline_for({"content": f"- {RULE}"}, PID, db, datetime(2026, 11, 19, tzinfo=timezone.utc))
    assert out["status"] == "unknown" and out["count"] is None and reason in out["reason"]


def test_baseline_missing_db_and_empty_source_are_unknown(tmp_path):
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    assert sed.baseline_for({"content": f"- {RULE}"}, PID, tmp_path / "none.db", now)["status"] == "unknown"
    assert sed.baseline_for({"content": ""}, PID, tmp_path / "none.db", now)["count"] is None
