"""reconcile marks an approved edit applied only on its pr_merged receipt (ochtendronde D3c part B)."""

import json
import sqlite3
import sys
from contextlib import closing
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
    (root := tmp_path / "repo").joinpath("agents", "backend-developer").mkdir(parents=True)
    (root / TARGET).write_text("# Backend\n\n## Wat review het vaakst vindt\n\n- old rule\n\n## Other\n\nx\n")
    (state := tmp_path / "store" / "state").mkdir(parents=True)
    for name, value in [("PROJECT_ROOT", root), ("STATE_DIR", state), ("PENDING_PATH", state / "pending_edits.json"),
                        ("HISTORY_PATH", state / "edit_history.json"), ("_project_id", lambda: PID)]:
        monkeypatch.setattr(ase, name, value)
    ase._save_pending({"generated_at": "", "edits": [{
        "id": 1, "category": "role", "target": TARGET, "section": ase.ROLE_SECTION, "action": "append",
        "content": f"- {RULE}", "status": "pending", "evidence": "2 dispatches", "confidence": 0.8}]})
    return root, state, None


def _pending(state):
    return json.loads((state / "pending_edits.json").read_text())["edits"]


def _ledger(state, *lines):
    (state / "t0_receipts.ndjson").write_text("".join(
        (l if isinstance(l, str) else json.dumps(l)) + "\n" for l in lines))


def _merged(did, project=PID, pr=3060, sha="0123456789abcdef"):
    return {"event_type": "pr_merged", "conclusion": "merged", "dispatch_id": did, "project_id": project,
            "pr_number": pr, "merge_sha": sha}


def _approve(env):
    assert ase.cmd_accept("1") == 0
    return _pending(env[1])[0]


def test_reconcile_marks_applied_with_ref_remeasure_and_baseline(env):
    state = env[1]
    did = _approve(env)["dispatch_id"]
    _ledger(state, _merged("other-dispatch"), _merged(did))
    before = datetime.now(tz=timezone.utc)
    assert ase.cmd_reconcile() == 0 and _pending(state) == []
    (entry,) = json.loads((state / "edit_history.json").read_text())
    assert entry["status"] == "applied" and entry["applied_ref"] == "#3060@0123456789ab"
    applied, remeasure = (datetime.fromisoformat(entry[k].replace("Z", "+00:00")) for k in ("applied_at", "remeasure_at"))
    assert applied >= before - timedelta(seconds=1) and remeasure - applied == timedelta(days=14)
    base = entry["baseline"]
    assert base["window_days"] == 14 and base["status"] == "unknown" and base["count"] is None and base["reason"]
    assert ase.cmd_reconcile() == 0 and len(json.loads((state / "edit_history.json").read_text())) == 1  # idempotent


@pytest.mark.parametrize("ledger", ["none", "empty", "other_project", "other_dispatch", "not_merged", "no_sha"])
def test_reconcile_without_evidence_changes_nothing(env, ledger):
    root, state, store = env
    did = _approve(env)["dispatch_id"]
    lines = {"none": [], "empty": [], "other_project": [_merged(did, project="other")],
             "other_dispatch": [_merged(did + "x"), _merged("x" + did)],
             "not_merged": [{**_merged(did), "conclusion": "closed"}, {**_merged(did), "event_type": "x"}],
             "no_sha": [{**_merged(did), "merge_sha": None}]}[ledger]
    _ledger(state, *lines)
    ase.cmd_reconcile()
    assert _pending(state)[0]["status"] == "approved" and not (state / "edit_history.json").exists()


def test_reconcile_corrupt_or_missing_ledger_is_refused(env, capsys):
    root, state, store = env
    did = _approve(env)["dispatch_id"]
    (state / "t0_receipts.ndjson").unlink()  # accept wrote the approval receipt; now no ledger file at all
    assert ase.cmd_reconcile() == 1
    _ledger(state, json.dumps(_merged(did)), "{not json")
    assert ase.cmd_reconcile() == 1
    assert "unreadable" in capsys.readouterr().err
    assert _pending(state)[0]["status"] == "approved" and not (state / "edit_history.json").exists()


@pytest.mark.parametrize("outcome", ["rejected", "unreadable"])
def test_dispatch_outcome_failure_keeps_edit_approved_and_visible(env, monkeypatch, capsys, outcome):
    root, state, store = env
    did = _approve(env)["dispatch_id"]
    _ledger(state)

    def states(pid, wanted):
        if outcome == "unreadable":
            raise RuntimeError("ledger gone")
        return {did: ("rejected_dispatch", "bad lane")}
    monkeypatch.setattr(ase, "_dispatch_states", states)
    assert ase.cmd_reconcile() == (1 if outcome == "unreadable" else 0)
    edit = _pending(state)[0]
    if outcome == "unreadable":
        assert "outcomes unreadable" in capsys.readouterr().err
        assert edit["status"] == "approved" and "dispatch_state" not in edit
    else:
        assert (edit["status"], edit["dispatch_state"], edit["dispatch_state_reason"]) == (
            "approved", "rejected_dispatch", "bad lane")


def _db(path, rows):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE antipatterns (project_id TEXT, title TEXT, description TEXT, "
                     "source_dispatch_ids TEXT, last_seen TEXT)")
        conn.executemany("INSERT INTO antipatterns VALUES (?,?,?,?,?)", rows)


NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)
EDIT = {"content": f"- {RULE}"}


def _receipts(path, *items):  # items: (dispatch_id, timestamp[, project])
    path.write_text("".join(json.dumps({"dispatch_id": i[0], "timestamp": i[1], "project_id": i[2] if len(i) > 2 else PID})
                            + "\n" for i in items))
    return path


def _baseline(tmp_path, rows, *receipts, ledger=True):
    _db(tmp_path / "q.db", rows)
    led = _receipts(tmp_path / "l.ndjson", *receipts) if ledger else tmp_path / "missing.ndjson"
    return sed.baseline_for(EDIT, PID, tmp_path / "q.db", NOW, led)


def _row(ids, seen="2026-11-18 08:00:00", title="t", project=PID):
    return (project, title, RULE, ids, seen)


@pytest.mark.parametrize("rows,receipts,count", [
    ([_row('["d1","d2","d1"]', "2026-11-10 08:00:00"), _row('["d3"]'), _row('["d9"]', "2026-10-01 08:00:00"),
      _row('["d8"]', "2026-11-10 08:00:00", project="other")],
     [("d1", "2026-11-09T08:00:00Z"), ("d1", "2026-11-12T08:00:00Z"), ("d2", "2026-11-10T08:00:00+00:00"),
      ("d3", "2026-11-18T08:00:00Z")], 3),
    ([_row('["old"]')], [("old", "2026-10-20T08:00:00Z")], 0),  # recently seen row, old dispatch
    ([_row('["d1","d2"]')],  # earliest receipt of d1 is old; d2 receipt of another project is ignored
     [("d1", "2026-11-17T08:00:00Z"), ("d1", "2026-10-01T08:00:00Z"), ("d2", "2026-11-17T08:00:00Z", "other"),
      ("d2", "2026-11-16T08:00:00Z")], 1),
    ([_row('["d1"]', "2026-11-10 08:00:00"), _row('["d1"]', "2026-11-12 08:00:00", title="tt")],
     [("d1", "2026-11-09T08:00:00Z")], 1),  # two rows sharing a dispatch count it once
], ids=["distinct_in_window", "old_dispatch_of_recent_row", "earliest_receipt_exact_project", "shared_dispatch_once"])
def test_baseline_counts_dispatches_in_window(tmp_path, rows, receipts, count):
    out = _baseline(tmp_path, rows, *receipts)
    assert out["status"] == "ok" and out["count"] == count and out["window_days"] == 14


def test_id_without_any_receipt_is_unknown_naming_the_missing_count(tmp_path):
    out = _baseline(tmp_path, [_row('["d1","ghost","d1x"]')], ("d1", "2026-11-17T08:00:00Z"), ("d1x", "bad"))
    assert out["status"] == "unknown" and out["count"] is None and "2 of 3" in out["reason"]


@pytest.mark.parametrize("last_seen", ["not a date", "", None])
def test_unparsable_last_seen_is_unknown(tmp_path, last_seen):
    out = _baseline(tmp_path, [_row('["d1"]', last_seen)], ("d1", "2026-11-17T08:00:00Z"))
    assert out["status"] == "unknown" and out["count"] is None and "last_seen" in out["reason"]


def test_unreadable_or_absent_ledger_is_unknown(tmp_path):
    out = _baseline(tmp_path, [_row('["d1"]')], ledger=False)
    assert out["status"] == "unknown" and "ledger unreadable" in out["reason"]
    (tmp_path / "l.ndjson").write_text("{not json\n")
    out = sed.baseline_for(EDIT, PID, tmp_path / "q.db", NOW, tmp_path / "l.ndjson")
    assert out["status"] == "unknown" and "ledger unreadable" in out["reason"]
    out = sed.baseline_for(EDIT, PID, tmp_path / "q.db", NOW)
    assert out["status"] == "unknown" and "no receipt ledger" in out["reason"]


@pytest.mark.parametrize("rows,reason", [([], "no antipattern row"), (None, "not found"),
                                         ([_row("not json", "2026-11-10 08:00:00")], "source_dispatch_ids unreadable")])
def test_baseline_unknown_is_never_zero(tmp_path, rows, reason):
    if rows is not None:
        _db(tmp_path / "q.db", rows)
    out = sed.baseline_for(EDIT, PID, tmp_path / "q.db", NOW)
    assert out["status"] == "unknown" and out["count"] is None and reason in out["reason"]


def test_empty_edit_source_is_unknown(tmp_path):
    assert sed.baseline_for({"content": ""}, PID, tmp_path / "none.db", NOW)["count"] is None
