"""Permissions P8: the read-only posture report and the flip gate (decision B7, conditions a to e).

Every store is a ``tmp_path`` tree. The real central store is never read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "permission_posture_report.py"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import permission_harness_map as phm


def _ppr():
    """Imported on first use, so the subprocess test is a behaviour red and not an ImportError."""
    import permission_posture_report
    return permission_posture_report


PROFILE = (
    "version: 1\nprofiles:\n  backend-developer:\n"
    "    allowed_tools: [Read, Write, Edit, Bash]\n    denied_tools: [WebSearch]\n"
    "    file_write_scope: ['scripts/**']\n"
)
DECISION = {"decision": "flip-approved", "decided_by": "Vincent", "decided_at": "2026-11-09T07:00:00Z"}
ENV_FLAG = "VNX_ENFORCE_WORKER_PERMISSIONS"
TRACK_ID = "provider-agnostic-permissions"
DECISION_KEY = "flip_decision"


def _all_supported():
    return {lane: {d: (phm.SUPPORTED, "test") for d in phm.DIMENSIONS} for lane in phm.LANES}


def build(tmp_path, monkeypatch, *, table=None, fallback=False, streak=20, decision=DECISION,
          records=True, track=True, receipts=True, profiles=PROFILE):
    """A store that passes all five conditions; keyword arguments break one of them."""
    monkeypatch.setattr(phm, "_TABLE", table if table is not None else _all_supported())
    monkeypatch.delenv(ENV_FLAG, raising=False)
    root, state = tmp_path / "project", tmp_path / "state"
    (root / ".vnx").mkdir(parents=True)
    (root / ".vnx" / "worker_permissions.yaml").write_text(profiles)
    (root / "agents" / "backend-developer").mkdir(parents=True)
    (root / "agents" / "backend-developer" / "CLAUDE.md").write_text("# role\n")
    if fallback:
        (root / "agents" / "ghost-role").mkdir()
        (root / "agents" / "ghost-role" / "CLAUDE.md").write_text("# role\n")
    (state / "permission_records").mkdir(parents=True)
    if records:
        for lane in phm.LANES:
            rec = phm.material_record(lane, "ab" * 32, ["--flag"], f"d-{lane}")
            (state / "permission_records" / f"T1_d-{lane}.json").write_text(json.dumps(rec))
    if receipts:
        rows = [{"event_type": "permission_policy_checked", "dispatch_id": f"d-{i}",
                 "outcome": "match", "check_status": "GO"} for i in range(streak)]
        (state / "t0_receipts.ndjson").write_text("".join(json.dumps(r) + "\n" for r in rows))
    con = sqlite3.connect(state / "runtime_coordination.db")
    con.execute("CREATE TABLE tracks (track_id TEXT, project_id TEXT, metadata_json TEXT)")
    if track:
        meta = {} if decision is None else {DECISION_KEY: decision}
        con.execute("INSERT INTO tracks VALUES (?, ?, ?)", (TRACK_ID, "vnx-dev", json.dumps(meta)))
    con.commit()
    con.close()
    return state, root


def run(state, root, capsys, *extra):
    code = _ppr().main(["--json", "--state-dir", str(state), "--project-root", str(root),
                     "--project-id", "vnx-dev", *extra])
    return code, json.loads(capsys.readouterr().out)


def status(report, key):
    return next(c["status"] for c in report["conditions"] if c["key"] == key)


def test_report_lists_all_six_lanes(tmp_path, monkeypatch):
    state, root = build(tmp_path, monkeypatch, table=dict(phm._TABLE))
    proc = subprocess.run([sys.executable, str(SCRIPT), "--json", "--state-dir", str(state),
                           "--project-root", str(root), "--project-id", "vnx-dev"],
                          capture_output=True, text=True, env={**os.environ, ENV_FLAG: ""})
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr  # a report exists: 0 allowed, 1 blocked, never 2
    report = json.loads(proc.stdout)
    assert proc.returncode == 1  # this store has no operator decision, so the flip is blocked
    assert [r["lane"] for r in report["lanes"]] == list(phm.LANES) and len(report["lanes"]) == 6
    assert set(report["lanes"][0]["capabilities"]) == set(phm.DIMENSIONS)
    assert report["verdict"] == "blocked" and report["flip_ready"] is False


def test_empty_store_exits_2_as_a_subprocess(tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--json", "--state-dir", str(tmp_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 2 and json.loads(proc.stdout)["verdict"] == "unreadable"


def test_all_five_pass_gives_flip_allowed(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch)
    code, report = run(state, root, capsys)
    assert code == 0 and report["verdict"] == "flip-allowed" and report["flip_ready"] is True
    assert [status(report, k) for k in "abcde"] == ["pass"] * 5
    assert all(r["records"] == 1 and r["last_policy_state"] == "resolved" for r in report["lanes"])


def test_flip_blocked_on_fallback_role(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch, fallback=True)
    code, report = run(state, root, capsys)
    assert code == 1 and status(report, "b") == "fail" and report["verdict"] == "blocked"
    assert "ghost-role" in next(c["evidence"] for c in report["conditions"] if c["key"] == "b")


def _tampered(tmp_path, monkeypatch):
    state, root = build(tmp_path, monkeypatch)
    path = state / "permission_records" / "T1_d-kimi.json"
    rec = json.loads(path.read_text())
    rec["capabilities"]["mcp"]["level"] = "unsupported"
    path.write_text(json.dumps(rec))
    return state, root


def _one_unsupported(tmp_path, monkeypatch):
    table = _all_supported()
    table["kimi"]["filesystem_write"] = (phm.UNSUPPORTED, "test")
    return build(tmp_path, monkeypatch, table=table)


@pytest.mark.parametrize("key,make", [
    ("a", _tampered),
    ("b", lambda t, m: build(t, m, fallback=True)),
    ("c", _one_unsupported),
    ("d", lambda t, m: build(t, m, streak=19)),
    ("e", lambda t, m: build(t, m, decision=None)),
    ("e", lambda t, m: build(t, m, decision={**DECISION, "decision": "maybe"})),
])
def test_each_condition_failing_alone_blocks(tmp_path, monkeypatch, capsys, key, make):
    state, root = make(tmp_path, monkeypatch)
    code, report = run(state, root, capsys)
    assert code == 1 and report["verdict"] == "blocked"
    assert status(report, key) == "fail"
    assert [status(report, k) for k in "abcde" if k != key] == ["pass"] * 4


@pytest.mark.parametrize("key,kwargs", [
    ("a", {"records": False}),
    ("d", {"receipts": False}),
    ("e", {"track": False}),
])
def test_unmeasured_blocks(tmp_path, monkeypatch, capsys, key, kwargs):
    state, root = build(tmp_path, monkeypatch, **kwargs)
    code, report = run(state, root, capsys)
    assert code == 1 and status(report, key) == "unmeasured" and report["verdict"] == "blocked"


def test_no_roles_leaves_b_and_c_unmeasured(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch, profiles="version: 1\nprofiles: {}\n")
    (root / "agents" / "backend-developer" / "CLAUDE.md").unlink()
    code, report = run(state, root, capsys)
    assert code == 1 and status(report, "b") == status(report, "c") == "unmeasured"


@pytest.mark.parametrize("rows,expected", [
    ([{"dispatch_id": "d1", "outcome": "match", "check_status": "GO"}], 1),
    ([{"dispatch_id": "d1", "outcome": "match", "check_status": "GO"}] * 2, 1),
    ([{"dispatch_id": "d0", "outcome": "mismatch", "check_status": "GO"},
      {"dispatch_id": "d1", "outcome": "match", "check_status": "GO"}], 1),
    ([{"dispatch_id": "d1", "outcome": "match", "check_status": "GO"},
      {"dispatch_id": "d2", "outcome": "mismatch", "check_status": "GO"}], 0),
    ([{"dispatch_id": "d1", "outcome": "match", "check_status": "HOLD"}], 0),
    ([{"dispatch_id": "d1", "outcome": "match"}], 0),
    ([{"dispatch_id": "d1", "outcome": None, "check_status": "GO"}], 0),
    ([{"dispatch_id": "d1", "outcome": "matched", "check_status": "GO"}], 0),
    ([{"dispatch_id": None, "outcome": "match", "check_status": "GO"}], 0),
    ([{"outcome": "match", "check_status": "GO"}], 0),
    ([], 0),
])
def test_match_streak_counts_only_positive_evidence(rows, expected):
    assert _ppr().match_streak(rows) == expected


def test_unmatched_tail_receipt_breaks_the_run(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch, streak=25)
    with (state / "t0_receipts.ndjson").open("a") as fh:
        fh.write(json.dumps({"event_type": "permission_policy_checked", "dispatch_id": "d-x",
                             "outcome": "policy_absent", "check_status": "GO"}) + "\n")
    code, report = run(state, root, capsys)
    assert code == 1 and status(report, "d") == "fail"


def _corrupt_record(state, root):
    (state / "permission_records" / "T1_broken.json").write_text("{not json")


def _corrupt_receipt(state, root):
    with (state / "t0_receipts.ndjson").open("a") as fh:
        fh.write('{"event_type": "permission_policy_checked", broken\n')


def _corrupt_yaml(state, root):
    (root / ".vnx" / "worker_permissions.yaml").write_text("profiles: [unclosed")


def _no_db(state, root):
    (state / "runtime_coordination.db").unlink()


def _bad_metadata(state, root):
    con = sqlite3.connect(state / "runtime_coordination.db")
    con.execute("UPDATE tracks SET metadata_json = '{broken'")
    con.commit()
    con.close()


@pytest.mark.parametrize("corrupt", [_corrupt_record, _corrupt_receipt, _corrupt_yaml, _no_db, _bad_metadata])
def test_unreadable_input_exits_2(tmp_path, monkeypatch, capsys, corrupt):
    state, root = build(tmp_path, monkeypatch)
    corrupt(state, root)
    code, report = run(state, root, capsys)
    assert code == 2 and report["verdict"] == "unreadable" and report["error"]


def test_min_consecutive_below_one_exits_2(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch)
    assert run(state, root, capsys, "--min-consecutive", "0")[0] == 2


def _tree(base: Path) -> dict:
    return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(base.rglob("*")) if p.is_file()}


def test_run_leaves_the_store_byte_identical_and_never_sets_the_flag(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch)
    before = _tree(tmp_path)
    code, _ = run(state, root, capsys)
    assert code == 0 and _tree(tmp_path) == before
    assert ENV_FLAG not in os.environ


def test_script_has_no_env_or_file_writer_except_the_out_helper():
    source = SCRIPT.read_text()
    assert not re.search(r"os\.environ\[[^\]]+\]\s*=|os\.environ\.update|putenv|\.write_text|open\([^)]*['\"]w", source)


def test_out_file_gets_the_report_and_text_mode_names_the_verdict(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch)
    out = tmp_path / "out" / "posture.txt"
    out.parent.mkdir()
    code = _ppr().main(["--state-dir", str(state), "--project-root", str(root),
                     "--project-id", "vnx-dev", "--out", str(out)])
    printed = capsys.readouterr().out
    assert code == 0 and out.read_text() == printed and "verdict: flip-allowed" in printed


def _rewrite_records(state, **fields):
    for path in (state / "permission_records").glob("*.json"):
        rec = json.loads(path.read_text())
        rec.update(fields)
        path.write_text(json.dumps(rec))


def test_unresolved_records_on_all_lanes_do_not_qualify(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch)
    _rewrite_records(state, policy_digest=None, policy_state="unresolved")
    code, report = run(state, root, capsys)
    assert code == 1 and status(report, "a") == "fail" and report["verdict"] == "blocked"
    assert all(r["matches_matrix"] is True and r["qualified"] is False for r in report["lanes"])


def test_one_unresolved_lane_fails_a_and_is_named_apart_from_mismatches(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch)
    path = state / "permission_records" / "T1_d-kimi.json"
    rec = json.loads(path.read_text())
    rec.update(policy_digest=None, policy_state="unresolved")
    path.write_text(json.dumps(rec))
    code, report = run(state, root, capsys)
    evidence = next(c["evidence"] for c in report["conditions"] if c["key"] == "a")
    assert code == 1 and status(report, "a") == "fail"
    assert "mismatch with the matrix: []" in evidence and "invalid digest: ['kimi']" in evidence


@pytest.mark.parametrize("fields", [
    {"policy_digest": "abc"}, {"policy_digest": "AB" * 32}, {"policy_digest": 7}, {"policy_digest": "ab" * 32 + "\n"},
    {"policy_state": "pending"}, {"policy_state": None},
])
def test_resolved_without_a_valid_digest_or_state_fails_a(tmp_path, monkeypatch, capsys, fields):
    state, root = build(tmp_path, monkeypatch)
    _rewrite_records(state, **fields)
    assert status(run(state, root, capsys)[1], "a") == "fail"


def test_resolved_with_valid_digest_on_all_lanes_passes_a(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch)
    report = run(state, root, capsys)[1]
    assert status(report, "a") == "pass" and all(r["qualified"] is True for r in report["lanes"])


@pytest.mark.parametrize("who", ["automation", "vincent", " Vincent ", "", None, 7])
def test_decided_by_must_be_exactly_vincent(tmp_path, monkeypatch, capsys, who):
    state, root = build(tmp_path, monkeypatch, decision={**DECISION, "decided_by": who})
    code, report = run(state, root, capsys)
    assert code == 1 and status(report, "e") == "fail"
    assert "decided_by" in next(c["evidence"] for c in report["conditions"] if c["key"] == "e")


def _utc(delta):
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("when", [
    "not-a-date", "2026-11-09T07:00:00", "2026-11-09T07:00:00+01:00", "", None, 7, _utc(timedelta(days=1)),
])
def test_decided_at_must_be_a_past_utc_time(tmp_path, monkeypatch, capsys, when):
    state, root = build(tmp_path, monkeypatch, decision={**DECISION, "decided_at": when})
    code, report = run(state, root, capsys)
    assert code == 1 and status(report, "e") == "fail"
    assert "decided_at" in next(c["evidence"] for c in report["conditions"] if c["key"] == "e")


@pytest.mark.parametrize("when", ["2026-11-09T07:00:00Z", "2026-11-09T07:00:00+00:00", _utc(timedelta(minutes=-1))])
def test_vincent_with_past_utc_time_passes_e_and_all_five_allow(tmp_path, monkeypatch, capsys, when):
    state, root = build(tmp_path, monkeypatch, decision={**DECISION, "decided_at": when})
    code, report = run(state, root, capsys)
    assert code == 0 and [status(report, k) for k in "abcde"] == ["pass"] * 5 and report["verdict"] == "flip-allowed"


# Round 3: every source is absent (a named unmeasured), unreadable or malformed (exit 2), never empty.
LOCK = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits")


def _lock(path, mode=0o000):
    os.chmod(path, mode)
    return path


def _replace(path, kind):
    shutil.rmtree(path) if path.is_dir() else path.unlink()
    path.mkdir() if kind == "dir" else path.write_text("not a directory\n")


def _ghost(root):
    (root / "agents" / "ghost-role").mkdir()
    (root / "agents" / "ghost-role" / "CLAUDE.md").write_text("# role\n")
    return root / "agents" / "ghost-role"


def _append(state, text):
    with (state / "t0_receipts.ndjson").open("a") as fh:
        fh.write(text)


def test_unlistable_agents_registry_exits_2_and_names_it(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch, fallback=True)
    agents, real = root / "agents", Path.iterdir

    def iterdir(self):
        if self == agents:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    code, report = run(state, root, capsys)
    assert code == 2 and report["verdict"] == "unreadable" and str(agents) in report["error"]


@pytest.mark.parametrize("break_source", [
    pytest.param(lambda s, r: _lock(s / "permission_records", 0o300), marks=LOCK, id="records-unlistable"),
    pytest.param(lambda s, r: _replace(s / "permission_records", "file"), id="records-not-a-dir"),
    pytest.param(lambda s, r: (r / ".vnx" / "worker_permissions.yaml").unlink(), id="yaml-absent"),
    pytest.param(lambda s, r: _lock(r / ".vnx" / "worker_permissions.yaml"), marks=LOCK, id="yaml-unreadable"),
    pytest.param(lambda s, r: _replace(r / "agents", "file"), id="agents-not-a-dir"),
    pytest.param(lambda s, r: _lock(r / "agents", 0o300), marks=LOCK, id="agents-unlistable"),
    pytest.param(lambda s, r: _lock(_ghost(r), 0o600), marks=LOCK, id="role-dir-unenterable"),
    pytest.param(lambda s, r: (r / ".vnx" / "worker_permissions.yaml").write_text(
        PROFILE.replace("[Read, Write, Edit, Bash]", "Read")), id="profile-entry-malformed"),
    pytest.param(lambda s, r: (r / "agents" / "backend-developer" / "config.yaml").write_text("isolation: [x"),
                 id="isolation-broken"),
    pytest.param(lambda s, r: _lock(s / "t0_receipts.ndjson"), marks=LOCK, id="ledger-unreadable"),
    pytest.param(lambda s, r: _replace(s / "t0_receipts.ndjson", "dir"), id="ledger-is-a-dir"),
    pytest.param(lambda s, r: _append(s, '{"event_type": "permission_pol'), id="ledger-torn-line"),
    pytest.param(lambda s, r: _lock(s / "runtime_coordination.db"), marks=LOCK, id="db-unreadable"),
    pytest.param(lambda s, r: (s / "runtime_coordination.db").write_bytes(b"not a database" * 100), id="db-not-sqlite"),
])
def test_unreadable_or_malformed_source_exits_2(tmp_path, monkeypatch, capsys, break_source):
    state, root = build(tmp_path, monkeypatch)
    locked = break_source(state, root)
    try:
        code, report = run(state, root, capsys)
    finally:
        if isinstance(locked, Path):
            os.chmod(locked, 0o700)
    assert code == 2 and report["verdict"] == "unreadable" and report["error"]


@pytest.mark.parametrize("remove,keys,evidence", [
    (lambda s, r: shutil.rmtree(s / "permission_records"), "a", "no record yet"),
    (lambda s, r: shutil.rmtree(r / "agents"), "bc", "agents registry absent"),
])
def test_absent_source_is_a_named_unmeasured(tmp_path, monkeypatch, capsys, remove, keys, evidence):
    state, root = build(tmp_path, monkeypatch)
    remove(state, root)
    code, report = run(state, root, capsys)
    assert code == 1 and report["verdict"] == "blocked"
    for key in keys:
        assert status(report, key) == "unmeasured"
        assert evidence in next(c["evidence"] for c in report["conditions"] if c["key"] == key)


def test_receipt_with_an_escaped_event_name_still_breaks_the_run(tmp_path, monkeypatch, capsys):
    state, root = build(tmp_path, monkeypatch)
    _append(state, '{"event_type": "permission\\u005fpolicy_checked", "dispatch_id": "d-x", '
                   '"outcome": "mismatch", "check_status": "GO"}\n')
    code, report = run(state, root, capsys)
    assert code == 1 and status(report, "d") == "fail"
