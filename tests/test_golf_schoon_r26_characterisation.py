"""Characterisation tests for golf-schoon R26 (dlv-702b81a9f505).

Pins the observable behaviour of
``scripts/backfill_dispatch_track_linkage.py::apply_to_live`` before and after
an extract-method refactor: stdout, stderr, exit codes, the backup-before-write
order, the rows it writes or rolls back, and the audit NDJSON record, on a real
temp SQLite file.

The module is loaded by path under unique names (no basename collision risk).
"""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
for _p in (_REPO / "scripts", _REPO / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load("_r26_backfill", _REPO / "scripts" / "backfill_dispatch_track_linkage.py")
# Fixture builder (schema rendered from schema_manifest) of the existing test file.
_fx = _load("_r26_backfill_fixture", _REPO / "tests" / "test_backfill_dispatch_track_linkage.py")

PROJECT_ID = "vnx-dev"

# ---------------------------------------------------------------------------
# backfill_dispatch_track_linkage.apply_to_live
# ---------------------------------------------------------------------------

_STAMP = "20260101T000000Z"
_ISO = "2026-01-01T00:00:00.000Z"


@pytest.fixture()
def live(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    db = state / bf.DB_FILENAME
    _fx._make_db(db)
    roadmap = tmp_path / "ROADMAP.yaml"
    roadmap.write_text(_fx.ROADMAP_YAML, encoding="utf-8")
    monkeypatch.setattr(bf, "_utc_stamp", lambda: _STAMP)
    monkeypatch.setattr(bf, "_utc_iso", lambda: _ISO)
    return {"db": db, "roadmap": roadmap, "tmp": str(tmp_path)}


def _norm(text: str, tmp: str) -> str:
    text = text.replace(tmp, "<TMP>")
    return re.sub(r"Backup written \(\d+ bytes\)", "Backup written (N bytes)", text)


def _rows(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        return {
            "disp": sorted(conn.execute("SELECT dispatch_id, track FROM dispatches").fetchall(),
                           key=lambda r: r[0]),
            "tracks": sorted(conn.execute("SELECT track_id, pr_ref, phase FROM tracks").fetchall()),
            "events": sorted(conn.execute(
                "SELECT event_id, entity_id, reason, metadata_json, occurred_at "
                "FROM coordination_events").fetchall()),
        }
    finally:
        conn.close()


_APPLY_TAIL = """  Rowcount assertion: dispatches 4->4, tracks 3->3  [ok]
  Non-null track preservation: [ok]
  Integrity check (live): [ok]

  Recomputing derived_status via reconcile_all_tracks ...
    reconciled 3 tracks — derived=done: 2, drifted: 0

  Backfill complete. Backup: <TMP>/state/runtime_coordination.db.bak-linkage-20260101T000000Z
"""


def test_apply_happy_path_output_rows_audit(live, capsys):
    before = _rows(live["db"])
    assert bf.apply_to_live(live["db"], live["roadmap"], PROJECT_ID) is None
    cap = capsys.readouterr()
    out = _norm(cap.out, live["tmp"])
    assert cap.err == ""
    assert out.startswith("\n  --APPLY MODE — backfilling live DB: <TMP>/state/runtime_coordination.db\n")
    assert "LINKAGE DIAGNOSIS — LIVE (BEFORE)" in out
    assert ("  Writing backup: <TMP>/state/runtime_coordination.db.bak-linkage-20260101T000000Z\n"
            "  Backup written (N bytes)\n") in out
    assert out.index("Writing backup") < out.index("BACKFILL REPORT")
    assert out.endswith(_APPLY_TAIL)
    # backup taken BEFORE the backfill: it holds the pre-state rows
    backup = live["db"].parent / f"{bf.DB_FILENAME}.bak-linkage-{_STAMP}"
    assert _rows(backup) == before
    after = _rows(live["db"])
    assert after["disp"] == [("d1-readme", "launch-readme"), ("d2-renames", "launch-renames"),
                             ("d3-readme-lane", "C"), ("d4-open", None)]
    assert [e[0] for e in after["events"]] == ["bf-prmerged-vnx-dev-d1-readme",
                                               "bf-prmerged-vnx-dev-d2-renames"]
    audit = (live["db"].parent / "backfill_linkage_audit.ndjson").read_text().splitlines()
    assert len(audit) == 1
    assert json.loads(audit[0]) == {
        "ts": _ISO, "op": "backfill_dispatch_track_linkage_apply",
        "db_path": str(live["db"]), "project_id": PROJECT_ID, "dispatches_relinked": 2,
        "track_pr_ref_set": ["launch-renames"], "pr_merged_events_added": 2,
        "backup_path": str(backup),
    }
    assert list(json.loads(audit[0])) == ["ts", "op", "db_path", "project_id", "dispatches_relinked",
                                          "track_pr_ref_set", "pr_merged_events_added", "backup_path"]


def _wrap_backfill(monkeypatch, after_sql: str | None = None, errors=None, exc=None):
    real = bf.apply_backfill

    def wrapped(conn, pr_map, project_id, occurred_at):
        if exc is not None:
            raise exc
        report = real(conn, pr_map, project_id, occurred_at)
        if after_sql:
            conn.execute(after_sql)
        if errors:
            report["errors"] = errors
        return report

    monkeypatch.setattr(bf, "apply_backfill", wrapped)


_FAIL_CASES = {
    "errors": ({"errors": ["boom one", "boom two"]},
               "\n  [FATAL] Backfill error(s). Transaction rolled back. Live DB NOT modified.\n"
               "    - boom one\n    - boom two\n"),
    "track_changed": ({"after_sql": "UPDATE dispatches SET track='Z' WHERE dispatch_id='d3-readme-lane'"},
                      "\n  [FATAL] A dispatch lost or changed its existing non-null track. "
                      "Transaction rolled back. Live DB NOT modified.\n"),
    "rowcount": ({"after_sql": "INSERT INTO tracks (track_id, project_id, title) VALUES ('x', 'vnx-dev', 'x')"},
                 "\n  [FATAL] Rowcount changed (dispatches 4->4, tracks 3->4). Backfill is additive to "
                 "coordination_events only. Rolled back.\n"),
}


@pytest.mark.parametrize("case", sorted(_FAIL_CASES))
def test_apply_fatal_paths_roll_back_and_exit_1(live, monkeypatch, capsys, case):
    kwargs, expected_err = _FAIL_CASES[case]
    _wrap_backfill(monkeypatch, **kwargs)
    before = _rows(live["db"])
    with pytest.raises(SystemExit) as info:
        bf.apply_to_live(live["db"], live["roadmap"], PROJECT_ID)
    assert info.value.code == 1
    cap = capsys.readouterr()
    assert cap.err == expected_err
    assert "BACKFILL REPORT" not in cap.out
    assert _rows(live["db"]) == before
    assert (live["db"].parent / f"{bf.DB_FILENAME}.bak-linkage-{_STAMP}").exists()
    assert not (live["db"].parent / "backfill_linkage_audit.ndjson").exists()


def test_apply_integrity_failure_names_backup(live, monkeypatch, capsys):
    monkeypatch.setattr(bf, "_integrity_check", lambda conn: ["row 1 bad", "row 2 bad"])
    before = _rows(live["db"])
    with pytest.raises(SystemExit) as info:
        bf.apply_to_live(live["db"], live["roadmap"], PROJECT_ID)
    assert info.value.code == 1
    err = _norm(capsys.readouterr().err, live["tmp"])
    assert err == ("\n  [FATAL] integrity_check failed pre-commit. Rolled back. Backup at: "
                   "<TMP>/state/runtime_coordination.db.bak-linkage-20260101T000000Z\n"
                   "    row 1 bad\n    row 2 bad\n")
    assert _rows(live["db"]) == before


def test_apply_exception_path_as_shipped(live, monkeypatch, capsys):
    """As shipped: the except branch closes the connection, then the finally
    branch reads ``conn.in_transaction`` on it, so the caller gets
    sqlite3.ProgrammingError with the original exception as __context__."""
    _wrap_backfill(monkeypatch, exc=KeyError("kaput"))
    before = _rows(live["db"])
    with pytest.raises(sqlite3.ProgrammingError, match="closed database") as info:
        bf.apply_to_live(live["db"], live["roadmap"], PROJECT_ID)
    assert isinstance(info.value.__context__, KeyError)
    assert capsys.readouterr().err == "\n  [ERROR] Backfill failed — transaction rolled back.\n"
    assert _rows(live["db"]) == before


def test_apply_reconcile_runtime_error_warns_and_continues(live, monkeypatch, capsys):
    def boom(state_dir, project_id):
        raise RuntimeError("no reconciler")

    monkeypatch.setattr(bf.track_reconciler, "reconcile_all_tracks", boom)
    bf.apply_to_live(live["db"], live["roadmap"], PROJECT_ID)
    out = capsys.readouterr().out
    assert ("\n  Recomputing derived_status via reconcile_all_tracks ...\n"
            "    [WARNING] reconcile skipped: no reconciler\n\n  Backfill complete.") in out
    assert (live["db"].parent / "backfill_linkage_audit.ndjson").exists()


def test_apply_audit_write_failure_warns_on_stderr(live, capsys):
    (live["db"].parent / "backfill_linkage_audit.ndjson").mkdir()
    bf.apply_to_live(live["db"], live["roadmap"], PROJECT_ID)
    cap = capsys.readouterr()
    assert cap.err.startswith("  [WARNING] Failed to write audit record: ")
    assert cap.out.rstrip().endswith("bak-linkage-20260101T000000Z")
