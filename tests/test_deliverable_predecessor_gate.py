"""ADR-096 B2: the door refuses a fire whose hard predecessor deliverable has no pr_merged receipt."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "scripts", ROOT / "scripts" / "lib"):
    sys.path.insert(0, str(_p))

import config_registry as cr
import config_runtime as crt
import config_store_db as cs
import dispatch_cli
import deliverable_predecessor_gate as dpg

PID, P, C = "vnx-dev", "dlv-pred00000001", "dlv-cand00000001"
MERGED = {"event_type": "pr_merged", "conclusion": "merged", "project_id": PID, "dispatch_id": P, "branch": f"dispatch/{P}"}
UNMERGED, RETIRED, UNKNOWN = dpg.REASON_UNMERGED, dpg.REASON_RETIRED, dpg.REASON_UNKNOWN


@pytest.fixture
def sd(tmp_path, monkeypatch):
    monkeypatch.setattr(crt, "get", lambda key: "refuse")
    (tmp_path / "t0_receipts.ndjson").write_text("")
    with sqlite3.connect(tmp_path / "runtime_coordination.db") as conn:
        conn.execute("CREATE TABLE dispatches (project_id, dispatch_id, state, output_kind)")
        conn.execute("CREATE TABLE deliverable_dependencies (project_id, deliverable_id, depends_on_id, kind, removed_at)")
    return tmp_path


def _dep(sd, kind="hard", state="running", output_kind="pr", removed_at=None, receipt=None, decision=None, log=None):
    with sqlite3.connect(sd / "runtime_coordination.db") as conn:
        conn.execute("INSERT INTO dispatches VALUES (?, ?, ?, ?)", (PID, P, state, output_kind))
        conn.execute("INSERT INTO deliverable_dependencies VALUES (?, ?, ?, ?, ?)", (PID, C, P, kind, removed_at))
    if receipt is not None:
        (sd / "t0_receipts.ndjson").write_text(json.dumps({k: v for k, v in {**MERGED, **receipt}.items() if v is not ...}) + "\n")
    if decision or log:
        (sd / "t0_decision_log.jsonl").write_text(log or json.dumps({
            "decision_type": "outcome_decision", "project_id": PID, "dispatch_id": P, "decision": decision}) + "\n")


def _door(sd, dispatch_id=C, **kw):
    return dispatch_cli._check_predecessor_verdict(dispatch_id=dispatch_id, project_id=PID, state_dir=sd, **kw)


def _ledger(sd):
    path = sd / dpg.LEDGER
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _reason(verdict):
    return next(r for r in (UNMERGED, RETIRED, UNKNOWN) if r in verdict.message) if verdict else None


def test_unmerged_hard_predecessor_is_refused_and_booked(sd):
    _dep(sd)
    v = _door(sd)
    assert v.severity == "blocking" and _reason(v) == UNMERGED and P in v.message
    [line] = _ledger(sd)
    assert line.pop("ts") and line == {"project_id": PID, "dispatch_id": C, "seq": 0, "candidate": C, "predecessor": P,
                                       "reason": UNMERGED, "mode": "refuse", "outcome": "refused", "override_reason": None}


R = dict(receipt={})
SCENARIOS = [
    (R, None), (dict(receipt={"dispatch_id": f"{P}-r2"}), None), (dict(receipt={"branch": "dispatch/dlv-other"}), UNMERGED),
    *[(dict(receipt={field: bad}), UNMERGED) for field in ("branch", "conclusion", "project_id", "dispatch_id")
      for bad in (..., None, 7, "other")],
    (dict(receipt={"event_type": "pr_opened_pr_merged"}), UNMERGED), (dict(receipt={"dispatch_id": f"{P}x"}), UNMERGED),
    (dict(R, output_kind="doc", decision="accept"), None), (dict(R, output_kind="post", decision="reject"), UNMERGED),
    (dict(R, output_kind="doc"), UNMERGED), (dict(R, output_kind=None), None), (dict(R, output_kind="weird"), UNKNOWN),
    (dict(R, output_kind=3), UNKNOWN), (dict(R, output_kind="doc", log="not json\n"), UNKNOWN),
    *[(dict(state=s), RETIRED) for s in ("withdrawn", "cancelled", "superseded", "retired")],
    (dict(state="bogus"), UNKNOWN), (dict(state=None), UNKNOWN), (dict(removed_at="2026-11-20T00:00:00Z"), None),
    (dict(kind=None), UNMERGED), (dict(kind="soft"), None),
]


@pytest.mark.parametrize("dep,expected", SCENARIOS)
def test_satisfied_only_on_positive_evidence(sd, dep, expected, capsys):
    _dep(sd, **dep)
    assert _reason(_door(sd)) == expected
    assert (P in capsys.readouterr().err) is (dep.get("kind") == "soft")  # soft: message only, no ledger line


@pytest.mark.parametrize("candidate", [f"{C}-r2", f"{C}-b", f"{C}-c-r2"])
def test_a_round_id_resolves_to_its_deliverable(sd, candidate):
    _dep(sd)
    assert _door(sd, dispatch_id=candidate).severity == "blocking" and _ledger(sd)[0]["candidate"] == C


@pytest.mark.parametrize("breakage,expected", [("missing", UNKNOWN), ("broken_line", UNKNOWN), ("dir", UNKNOWN),
                                               ("garbage_db", UNKNOWN), ("no_table", None), ("no_db", None)])
def test_unreadable_store_is_unknown(sd, breakage, expected):
    _dep(sd)
    receipts, db = sd / "t0_receipts.ndjson", sd / "runtime_coordination.db"
    (receipts if breakage in ("missing", "broken_line", "dir") else db).unlink()
    {"broken_line": lambda: receipts.write_text('{"event_type": "pr_merged", \n'), "dir": receipts.mkdir,
     "garbage_db": lambda: db.write_bytes(b"not a database" * 100),
     "no_table": lambda: sqlite3.connect(db).close()}.get(breakage, lambda: None)()
    assert _reason(_door(sd)) == expected


@pytest.mark.parametrize("mode,severity,outcome", [("warn", "warn", "warned"), ("refuse", "blocking", "refused"),
                                                    ("nonsense", "blocking", "refused"), (None, "blocking", "refused")])
def test_modes(sd, monkeypatch, mode, severity, outcome):
    monkeypatch.setattr(crt, "get", lambda key: mode)
    _dep(sd)
    assert _door(sd).severity == severity and _ledger(sd)[0]["outcome"] == outcome


def test_off_reads_nothing(sd, monkeypatch):
    monkeypatch.setattr(crt, "get", lambda key: "off")
    monkeypatch.setattr(dpg, "assess", lambda *a: pytest.fail("off must read nothing"))
    assert _door(sd) is None


@pytest.mark.parametrize("reason,severity", [("  merged as #3050 sha abc  ", "warn"), ("", "blocking"), ("  ", "blocking")])
def test_override_needs_a_reason_and_books_it(sd, reason, severity):
    _dep(sd)
    v = _door(sd, override_reason=reason)
    assert v.severity == severity and v.override_applied is bool(reason.strip())
    assert _ledger(sd)[0]["override_reason"] == (reason.strip() or None)
    if not reason.strip():  # the CLI refuses an empty reason before it reads the spec
        with pytest.raises(SystemExit, match="2"):
            dispatch_cli.main(["--spec-file", "/nonexistent.json", "--override-predecessor", reason])


def test_dry_run_writes_nothing_and_an_unbooked_override_is_refused(sd, monkeypatch):
    _dep(sd)
    assert _door(sd, dry_run=True).severity == "blocking" and not (sd / dpg.LEDGER).exists()
    monkeypatch.setattr(dpg, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert _door(sd, override_reason="why").severity == "blocking"


def test_registry_default_and_stored_value_through_config_runtime(sd, monkeypatch):
    monkeypatch.undo()
    for module, name, value in ((crt, "_wired_for", {}), (cr, "_db_resolver", None), (cr, "_default_project_id", None)):
        monkeypatch.setattr(module, name, value)
    monkeypatch.delenv(dpg.MODE_KEY, raising=False)
    e = cr.CONFIG_REGISTRY[dpg.MODE_KEY]
    assert (e.default, e.scope, e.allowed_values) == ("refuse", "project", dpg.MODES)
    assert crt.autowire(state_dir=sd, project_id=PID) and crt.get(dpg.MODE_KEY) == "refuse"
    with sqlite3.connect(sd / "runtime_coordination.db") as conn:
        cs.set_config(conn, PID, dpg.MODE_KEY, "warn", actor="op")
    assert crt.get(dpg.MODE_KEY) == "warn"


def test_the_door_runs_the_check_with_the_project_state_checks(sd, monkeypatch):
    _dep(sd)
    for name in ("_check_track_link_verdict", "_check_b3_cap_verdict"):
        monkeypatch.setattr(dispatch_cli, name, lambda spec, state_dir: None)
    spec = type("Spec", (), {"dispatch_id": f"{C}-r2", "project_id": PID})()
    [v] = dispatch_cli._project_state_verdicts(spec, state_dir=sd, dry_run=True)
    assert v.code == "deliverable-predecessor-unmerged" and v.severity == "blocking" and not _ledger(sd)


@pytest.mark.parametrize("output_kind", [None, "pr", "doc", "post"])
@pytest.mark.parametrize("state, reason", [
    ("cancelled", dpg.REASON_RETIRED), ("superseded", dpg.REASON_RETIRED), ("retired", dpg.REASON_RETIRED),
    ("withdrawn", dpg.REASON_RETIRED), ("no-such-state", dpg.REASON_UNKNOWN), (None, dpg.REASON_UNKNOWN),
])
def test_matching_proof_never_clears_a_retired_or_unknown_state(output_kind, state, reason):
    edge = dpg.Edge("pred", "hard", state, output_kind)
    findings = dpg.judge([edge], {"pred"}, {"pred"})  # a merge receipt and an accept both match
    assert findings == (dpg.Finding("pred", True, reason),)


@pytest.mark.parametrize("edge, merged, accepted, reason", [
    (dpg.Edge("pred", "hard", "completed", "weird"), {"pred"}, {"pred"}, dpg.REASON_UNKNOWN),
    (dpg.Edge("pred", "hard", "completed", "pr"), None, {"pred"}, dpg.REASON_UNKNOWN),
    (dpg.Edge("pred", "hard", "completed", "doc"), {"pred"}, None, dpg.REASON_UNKNOWN),
    (dpg.Edge("pred", "hard", "completed", "pr"), set(), set(), dpg.REASON_UNMERGED),
])
def test_unreadable_proof_or_unknown_kind_stays_blocking(edge, merged, accepted, reason):
    assert dpg.judge([edge], merged, accepted) == (dpg.Finding("pred", True, reason),)


def _parts(sd, pr_ref, receipts, state="completed", output_kind="pr", row=True):
    """P closed with ``pr_ref`` (OI-3054); ``receipts`` is (pr_number, branch) per merged pr_merged line."""
    with sqlite3.connect(sd / "runtime_coordination.db") as conn:
        conn.execute("ALTER TABLE dispatches ADD COLUMN pr_ref")
        if row:
            conn.execute("INSERT INTO dispatches VALUES (?, ?, ?, ?, ?)", (PID, P, state, output_kind, pr_ref))
        conn.execute("INSERT INTO deliverable_dependencies VALUES (?, ?, ?, 'hard', NULL)", (PID, C, P))
    (sd / "t0_receipts.ndjson").write_text("".join(json.dumps(
        {**MERGED, "pr_number": n, "branch": b, "dispatch_id": b.removeprefix("dispatch/")}) + "\n" for n, b in receipts))


N1 = [(3118, f"dispatch/{P}-a"), (3117, f"dispatch/{P}-b"), (3119, f"dispatch/{P}-c")]
PARTS = [
    ("#3118,#3117,#3119", N1, None, None),  # the N1 shape: three parts, all merged
    ("3118, 3117, #3119", N1, None, None),
    ("#3118,#3117,#3119", N1[:2], UNMERGED, "#3119"),
    ("#3118,#3117,#3119", N1[:2] + [(3119, "dispatch/dlv-other0000001-a")], UNKNOWN, "#3119"),
    ("#3118,#3117,#3119", N1 + [(3119, "dispatch/dlv-other0000001")], UNKNOWN, "#3119"),
    ("#3118", [(3118, f"dispatch/{P}x-a")], UNKNOWN, "#3118"),
    ("#3118", [(3118, f"dispatch/{P}-a-r2")], None, None),  # a round of a part resolves like _round_base
    ("#3118", [(3118, f"dispatch/{P}-r2")], None, None),
    ("#9999", N1, UNMERGED, "#9999"),  # a PR number no receipt carries
    (None, N1, UNMERGED, None),  # closed without a list: the old rule, no receipt on dispatch/P
    ("#3118,abc", N1, UNKNOWN, "unreadable"), ("", N1, UNKNOWN, "unreadable"), ("#0", N1, UNKNOWN, "unreadable"),
    ("3118.0", N1, UNKNOWN, "unreadable"), (3118, N1, UNKNOWN, "unreadable"),
]


@pytest.mark.parametrize("pr_ref,receipts,expected,named", PARTS)
def test_a_deliverable_shipped_in_parts_satisfies_through_its_closing_pr_list(sd, pr_ref, receipts, expected, named):
    _parts(sd, pr_ref, receipts)
    v = _door(sd)
    assert _reason(v) == expected and (named is None or named in v.message)


@pytest.mark.parametrize("change", [dict(state="running"), dict(state="ready"), dict(output_kind="doc")])
def test_the_pr_list_counts_only_on_a_closed_pr_deliverable(sd, change):
    _parts(sd, "#3118,#3117,#3119", N1, **change)
    assert _reason(_door(sd)) == UNMERGED


def test_receipts_of_another_project_or_not_merged_do_not_count(sd):
    _parts(sd, "#3118", [])
    rows = [{**MERGED, "pr_number": 3118, "branch": f"dispatch/{P}-a", **x}
            for x in ({"project_id": "other"}, {"conclusion": "closed"}, {"pr_number": "3118"}, {"pr_number": True})]
    (sd / "t0_receipts.ndjson").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert _reason(_door(sd)) == UNMERGED


def test_the_old_rule_alone_still_satisfies_a_closed_deliverable(sd):
    _parts(sd, "#3118,#3117", [(3118, f"dispatch/{P}")])
    assert _reason(_door(sd)) is None


def test_an_unreadable_receipt_line_or_a_missing_close_row_is_unknown_for_a_pr_list(sd, tmp_path_factory):
    _parts(sd, "#3118,#3117,#3119", N1)
    with open(sd / "t0_receipts.ndjson", "a") as fh:
        fh.write('{"event_type": "pr_merged", broken\n')
    assert _reason(_door(sd)) == UNKNOWN
    other = tmp_path_factory.mktemp("norow")
    (other / "t0_receipts.ndjson").write_text("")
    with sqlite3.connect(other / "runtime_coordination.db") as conn:
        conn.execute("CREATE TABLE dispatches (project_id, dispatch_id, state, output_kind)")
        conn.execute("CREATE TABLE deliverable_dependencies (project_id, deliverable_id, depends_on_id, kind, removed_at)")
    _parts(other, None, N1, row=False)
    assert _reason(_door(other)) == UNKNOWN
