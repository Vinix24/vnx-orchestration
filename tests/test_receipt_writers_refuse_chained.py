"""The five writers that do not chain refuse a chained ledger, inside the append lock (ADR-083, H2)."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import ndjson_hash_chain as nhc


def _plain(rid: str) -> str:
    return json.dumps({"receipt_id": rid, "dispatch_id": "20260601-" + rid, "event_type": "task_complete"},
                      separators=(",", ":")) + "\n"


def _ledger(tmp_path: Path, chained: bool) -> Path:
    ledger = tmp_path / "state" / "t0_receipts.ndjson"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(_plain("r0") + _plain("r1"), encoding="utf-8")
    if chained:
        nhc.append_epoch_marker(ledger, 1)
        nhc.append_chained_entry(ledger, {"receipt_id": "r2", "event_type": "task_complete"})
    return ledger


def _run_pr_merged(tmp_path: Path, ledger: Path, monkeypatch) -> int:
    import backfill_pr_merged_receipts as bpm
    pr = {"number": 7, "title": "x", "headRefName": "b", "mergedAt": "2026-11-01T00:00:00Z"}
    monkeypatch.setattr(bpm, "_gh_list_merged_prs", lambda limit, since=None: [pr])
    monkeypatch.setattr(bpm, "_load_dispatch_register_events", lambda: [])
    monkeypatch.setattr(bpm, "_emit_register_event", lambda n: True)
    monkeypatch.setattr(bpm, "_resolve_receipts_path", lambda: tmp_path / "none.ndjson")
    monkeypatch.setattr(bpm, "_resolve_events_pr_merged_path", lambda: ledger)
    return bpm.main([])


def _run_wt(tmp_path: Path, ledger: Path, monkeypatch) -> int:
    from scripts.backfill_wt_receipts import main
    archive = tmp_path / "archive.ndjson"
    archive.write_text(_plain("r9"), encoding="utf-8")
    code = main(["--archive", str(archive), "--central", str(ledger), "--apply"])
    if code:
        assert not list(ledger.parent.glob("*.bak-backfill-*")), "backup taken before the refusal"
    return code


def _run_compact(tmp_path: Path, ledger: Path, monkeypatch) -> int:
    import compact_state
    monkeypatch.setattr(compact_state, "RECEIPTS_MAX_RECORDS", 2)
    dry = compact_state.compact_receipts(ledger.parent, dry_run=True)
    return compact_state.compact_receipts(ledger.parent) or dry


def _run_headless(tmp_path: Path, ledger: Path, monkeypatch) -> int:
    import backfill_headless_receipts as bhr
    monkeypatch.setattr(bhr, "T0_RECEIPTS_NDJSON", ledger)
    try:
        bhr._update_ndjson({}, {}, dry_run=False)
    except nhc.ChainedLedgerRefused:
        return 1
    return 0


def _run_merge_sh(tmp_path: Path, ledger: Path, monkeypatch) -> int:
    data, wt = ledger.parent.parent, tmp_path / "wt"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VNX_", "PROJECT_ROOT"))}
    env.update(VNX_DATA_DIR=str(data), VNX_STATE_DIR=str(data / "state"),
               VNX_REPORTS_DIR=str(data / "unified_reports"), VNX_DB_DIR=str(data / "database"))
    cmd = ["bash", str(ROOT / "scripts" / "vnx_worktree_merge_data.sh"), str(wt)]
    wt.mkdir()  # first run without worktree receipts: prove the main data dir is the test's own
    probe = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    assert f"→ {data}\n" in probe.stdout, probe.stdout
    (wt / "state").mkdir()
    (wt / "state" / "t0_receipts.ndjson").write_text(_plain("r9"), encoding="utf-8")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    assert "Done." in proc.stdout  # the other merge steps still run
    return int("[refused] Receipts:" in proc.stderr and "ADR-083" in proc.stderr)


WRITERS = [_run_pr_merged, _run_wt, _run_compact, _run_headless, _run_merge_sh]


@pytest.mark.parametrize("writer", WRITERS, ids=lambda w: w.__name__[5:])
def test_writer_leaves_a_chained_ledger_byte_for_byte(writer, tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, chained=True)
    before = ledger.read_bytes()
    assert writer(tmp_path, ledger, monkeypatch) != 0
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("writer", [_run_pr_merged, _run_wt, _run_merge_sh], ids=lambda w: w.__name__[5:])
def test_appender_still_appends_to_an_unchained_ledger(writer, tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, chained=False)
    before = ledger.read_bytes()
    assert writer(tmp_path, ledger, monkeypatch) == 0
    after = ledger.read_bytes()
    assert after.startswith(before) and len(after.splitlines()) == 3


def test_compact_still_rotates_an_unchained_ledger(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, chained=False)
    ledger.write_text(_plain("r0") + _plain("r1") + _plain("r2"), encoding="utf-8")
    assert _run_compact(tmp_path, ledger, monkeypatch) == 0
    assert ledger.read_text(encoding="utf-8") == _plain("r1") + _plain("r2")


def test_guard_refuses_an_ambiguous_tail(tmp_path):
    ledger = _ledger(tmp_path, chained=False)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write("{torn\n" * (nhc.TAIL_WINDOW // 6 + 10))
    with pytest.raises(nhc.ChainedLedgerRefused, match="ambiguous.*ADR-083"):
        with nhc.unchained_write(ledger):
            pytest.fail("the guarded block ran on an ambiguous ledger")


def test_guard_refuses_a_sealed_ledger_whose_tail_has_no_prev_hash(tmp_path):
    ledger = _ledger(tmp_path, chained=True)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(_plain("r3"))
    with pytest.raises(nhc.ChainedLedgerRefused, match="sealed"):
        with nhc.unchained_write(ledger):
            pytest.fail("the guarded block ran on a sealed ledger")


@pytest.mark.parametrize("chained", [None, False])
def test_guard_yields_with_the_ledger_lock_held(tmp_path, chained):
    ledger = tmp_path / "state" / "t0_receipts.ndjson" if chained is None else _ledger(tmp_path, chained)
    with nhc.unchained_write(ledger) as state:
        assert state == ("empty" if chained is None else "unchained")
        with nhc.ledger_lock_path(ledger).open("a+") as fh, pytest.raises(BlockingIOError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    with nhc.ledger_lock_path(ledger).open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
