"""The seat-rule key (track ``tweede-stoel-op-signaal``, PR 4).

Plan: ``claudedocs/specs/tweede-stoel-op-signaal/plan.md`` (Overgang van
verplichtingen, Deliverable 4). One key, read in one place:
``VNX_REVIEW_SEAT_RULE`` through ``config_runtime`` (the reader the review
stack already uses). Unset or ``first_seat_or_every_seat_by_risk`` keeps the
old rule; ``first_seat_or_every_seat_on_signal`` turns the signal rule on; any
other value fails closed to the old rule and its name lands on the record.

Pinned here: with the key off the door's decision is the one on main on every
input (the golden and the R3b/R5b suites stay green unchanged); with the key on
a clean codex PASS needs codex only, every signal asks the second seat by name,
every ``UNKNOWN``/fail-closed case keeps every seat, a seat-2 FAIL on the head
blocks, an invalid value keeps the old rule and is named, and track closure
judges a receipt under the rule its own record names — never under the key.

Runner transition (plan item 3, the ``retired``/``no_signal`` obligation) is a
separate follow-up; see the PR report's Open Items.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import closure_verifier
import gate_obligations
import pr_merge
import review_risk_class
import review_seat_evaluation as rse
from review_seat_support import pin_review_stack

PR = 2486
PR_ID = str(PR)
BRANCH = "dispatch/dlv-ff14320cb80e"
DISPATCH = "dlv-ff14320cb80e"
#: Sample residue 4: the deterministic sample does NOT fire on this head.
HEAD = "9f3c2a1b7e4d5c6a8b0f1e2d3c4b5a6978879605"
#: Sample residue 0: the deterministic sample fires on this head.
SAMPLE_HEAD = "c" * 40
STACK = ("codex_gate", "kimi_gate")
SIGNAL_SOURCE = "VNX_REVIEW_SEAT_RULE"
CLEAN_REPORT = "# review\n\nThe reviewer read the whole diff and found nothing that holds the merge.\n"
FAIL_REPORT = (
    "# review\n\nThe change breaks every caller.\n\n```json\n"
    '{"verdict": "fail", "findings": [{"severity": "blocking", "message": "breaks every caller"}]}\n```\n'
)


# ---------------------------------------------------------------------------
# A throwaway project store
# ---------------------------------------------------------------------------


def _store(tmp_path: Path, monkeypatch, head: str = HEAD) -> Dict[str, Any]:
    data_dir = tmp_path / "data"
    state = data_dir / "state"
    for directory in (
        state / "review_gates" / "results",
        state / "review_gates" / "requests",
        data_dir / "unified_reports",
        data_dir / "dispatches",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for key, value in {
        "PROJECT_ROOT": tmp_path, "VNX_HOME": VNX_ROOT, "VNX_DATA_DIR": data_dir,
        "VNX_STATE_DIR": state, "VNX_DISPATCH_DIR": data_dir / "dispatches",
        "VNX_LOGS_DIR": data_dir / "logs", "VNX_PIDS_DIR": data_dir / "pids",
        "VNX_LOCKS_DIR": data_dir / "locks", "VNX_REPORTS_DIR": data_dir / "unified_reports",
        "VNX_DB_DIR": data_dir / "database",
    }.items():
        monkeypatch.setenv(key, str(value))
    pin_review_stack(monkeypatch, ",".join(STACK))
    monkeypatch.delenv(SIGNAL_SOURCE, raising=False)
    return {
        "head": head,
        "state": state,
        "reviews": state / "review_gates",
        "results": state / "review_gates" / "results",
        "reports": data_dir / "unified_reports",
        "receipts": state / "t0_receipts.ndjson",
    }


def _pr_data(store) -> Dict[str, Any]:
    return {
        "number": PR, "title": "seat rule key", "state": "OPEN", "isDraft": False,
        "headRefName": BRANCH, "baseRefName": "main", "headRefOid": store["head"],
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
    }


def _go() -> Dict[str, Any]:
    return {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}


def _report(store, name: str, body: str = CLEAN_REPORT) -> str:
    path = store["reports"] / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def _record(store, gate: str, *, status: str = "pass", report_body: str = CLEAN_REPORT) -> Dict[str, Any]:
    return {
        "gate": gate, "pr_id": PR_ID, "pr_number": PR, "status": status,
        "commit_sha": store["head"], "branch": BRANCH, "contract_hash": f"sha256:{gate}",
        "report_path": _report(store, f"{gate}.md", report_body), "blocking_findings": [],
        "advisory_findings": [], "evidence_source": "live", "test_run": False,
    }


def _write(store, gate: str, **kwargs: Any) -> None:
    path = store["results"] / f"pr-{PR}-{gate}.json"
    path.write_text(json.dumps(_record(store, gate, **kwargs)), encoding="utf-8")


def _ledger_row(store, seat: str, *, warnings: Optional[int], notices: Optional[int], verdict: str = "pass") -> Dict[str, Any]:
    import review_rounds

    row: Dict[str, Any] = {
        "project_id": review_rounds.store_project_id(store["reviews"]),
        "pr_number": PR, "head_sha": store["head"], "seat": seat, "reviewer": seat,
        "takeover_from": "", "verdict": verdict, "blocking_count": 0,
        "report_path": str(store["reports"] / f"{seat}.md"), "dispatch_id": f"d-{seat}",
        "recorded_at": "2026-11-03T10:00:00Z", "branch": BRANCH, "source": "gate",
    }
    if warnings is not None:
        row["warning_count"] = warnings
    if notices is not None:
        row["prescan_notice_count"] = notices
    return row


def _write_ledger(
    store, codex: Optional[Tuple[Optional[int], Optional[int]]] = (0, 0), *, verdict: str = "pass",
) -> None:
    import review_rounds

    opened = {
        "kind": "opened", "project_id": review_rounds.store_project_id(store["reviews"]),
        "pr_number": PR, "basis": "new", "evidence": 0,
        "recorded_at": "2026-11-03T09:00:00Z", "source": "gate",
    }
    rows = [opened]
    if codex is not None:
        rows.append(_ledger_row(store, "codex_gate", warnings=codex[0], notices=codex[1], verdict=verdict))
    rows.append(_ledger_row(store, "kimi_gate", warnings=None, notices=None))
    (store["reviews"] / "rounds.ndjson").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8",
    )


def _pin_risk(
    monkeypatch, head: str, *, risk_class: str = "medium", paths: Optional[List[tuple]] = None,
    irreversible: Any = False, derived: Optional[str] = None,
) -> None:
    def fake(*args: Any, **kwargs: Any) -> Any:
        return review_risk_class.RiskClass(
            risk_class=risk_class, derived=derived or risk_class, reason="fixture",
            deciding=tuple(paths or ()), irreversible=irreversible, head_sha=head,
            merge_base="a" * 40, base_ref="main", rule_version="fixture", files=1,
        )

    monkeypatch.setattr(review_risk_class, "merge_head_risk_class", fake)


def _stub_door_edges(monkeypatch, store) -> None:
    import file_scope_overlap

    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (_go(), _pr_data(store)))
    monkeypatch.setattr(pr_merge, "_query_pr", lambda n: _pr_data(store))
    monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: _go())
    monkeypatch.setattr(pr_merge, "_run_contract_invalid_gate", lambda *a, **k: _go())
    monkeypatch.setattr(pr_merge, "_run_branch_protection_gate", lambda *a, **k: _go())
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)
    monkeypatch.setattr(file_scope_overlap, "warn_overlaps", lambda *a, **k: [])


def _obligation(store) -> None:
    gate_obligations.register_obligation(
        store["state"], dispatch_id=DISPATCH, gate="codex_gate",
        pr_number=PR, pr_id=PR_ID, branch=BRANCH,
    )


def _gate(store, monkeypatch, *argv: str) -> Dict[str, Any]:
    _stub_door_edges(monkeypatch, store)
    return pr_merge._run_review_gate(PR, *argv)


def _key(monkeypatch, value: Optional[str]) -> None:
    if value is None:
        monkeypatch.delenv(SIGNAL_SOURCE, raising=False)
    else:
        monkeypatch.setenv(SIGNAL_SOURCE, value)


def _signal(value: str = rse.SIGNAL_SEAT_RULE) -> str:
    return value


# ---------------------------------------------------------------------------
# Key on: the door decides by the signal rule
# ---------------------------------------------------------------------------


def test_key_on_a_clean_codex_pass_needs_codex_only(tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, monkeypatch)
    _write(store, "codex_gate")
    _write_ledger(store, codex=(0, 0))
    _obligation(store)
    # A high-class head: under the risk rule this asks kimi too. The signal
    # rule sees a clean first seat and no signal, so codex alone suffices —
    # the plan's done-criterion, and the one case the key-off door refuses.
    _pin_risk(monkeypatch, HEAD, risk_class="high")
    _key(monkeypatch, _signal())

    gate, _ = _gate(store, monkeypatch)

    assert gate["verdict"] == "GO", gate["message"]
    record = gate["review_seats"]
    assert record["rule"] == rse.SIGNAL_SEAT_RULE
    assert record["seats"] == ["codex_gate"]
    assert record["signals"] == [] and record["unknown"] == []
    assert record["rule_value"] == rse.SIGNAL_SEAT_RULE

    _stub_door_edges(monkeypatch, store)
    rc = pr_merge.main(["--pr", PR_ID, "--dispatch-id", DISPATCH, "--dry-run", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert rc == pr_merge.EXIT_OK
    # PR 4 folded the shadow into the decision: the dry run says OK on codex
    # alone and names the rule it decided by.
    assert payload["shadow_seat_rule"]["rule"] == rse.SIGNAL_SEAT_RULE
    assert payload["shadow_seat_rule"]["seats"] == ["codex_gate"]


@pytest.mark.parametrize("name,setup", [
    ("real_warning", {"codex": (1, 0)}),
    ("security_path", {"risk": {"risk_class": "high", "paths": [("scripts/lib/auth_store.py", "security")]}}),
    ("schema", {"risk": {"risk_class": "high", "paths": [("schemas/report.sql", "schema")]}}),
    ("irreversible", {"risk": {"irreversible": True}}),
    ("sample", {"head": SAMPLE_HEAD}),
])
def test_key_on_every_signal_asks_the_second_seat(tmp_path, monkeypatch, name, setup):
    head = setup.get("head", HEAD)
    store = _store(tmp_path, monkeypatch, head=head)
    _write(store, "codex_gate")
    _write_ledger(store, codex=setup.get("codex", (0, 0)))
    _obligation(store)
    risk = setup.get("risk", {})
    _pin_risk(monkeypatch, head, risk_class=risk.get("risk_class", "medium"),
              paths=risk.get("paths"), irreversible=risk.get("irreversible", False))
    _key(monkeypatch, _signal())

    gate, _ = _gate(store, monkeypatch)

    record = gate["review_seats"]
    assert gate["verdict"] == "NO-GO"
    assert record["rule"] == rse.SIGNAL_SEAT_RULE
    assert record["seats"] == list(STACK)
    assert name in record["signals"]
    assert name in gate["message"]
    assert "kimi_gate" in gate["message"]


# ---------------------------------------------------------------------------
# Key on: every UNKNOWN and every fail-closed case keeps every seat
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", [
    "no_seat1_outcome", "seat1_fail", "unknown_warning_count", "unknown_prescan_count",
    "unknown_risk_class", "unknown_irreversible", "unknown_head",
])
def test_key_on_every_unknown_keeps_every_seat(tmp_path, monkeypatch, case):
    head = HEAD
    store = _store(tmp_path, monkeypatch, head=head)
    risk: Dict[str, Any] = {}
    if case == "seat1_fail":
        _write(store, "codex_gate", status="fail", report_body=FAIL_REPORT)
        _write_ledger(store, codex=(0, 0), verdict="fail")
    elif case == "unknown_warning_count":
        _write(store, "codex_gate")
        _write_ledger(store, codex=(None, 0))
    elif case == "unknown_prescan_count":
        _write(store, "codex_gate")
        _write_ledger(store, codex=(0, None))
    elif case == "unknown_risk_class":
        _write(store, "codex_gate")
        _write_ledger(store, codex=(0, 0))
        risk = {"derived": "nonsense"}
    elif case == "unknown_irreversible":
        _write(store, "codex_gate")
        _write_ledger(store, codex=(0, 0))
        risk = {"irreversible": None}
    elif case == "no_seat1_outcome":
        _write_ledger(store, codex=None)
    else:  # unknown_head: no codex record on any head
        _write_ledger(store, codex=(0, 0))

    _obligation(store)
    _pin_risk(monkeypatch, head, risk_class=risk.get("risk_class", "medium"),
              paths=risk.get("paths"), irreversible=risk.get("irreversible", False),
              derived=risk.get("derived"))
    _key(monkeypatch, _signal())

    if case == "unknown_head":
        evaluation = rse.evaluate_review_seats(
            PR_ID, store["results"], branch=BRANCH, head_sha=None,
            rule=rse.SIGNAL_SEAT_RULE, rule_value=rse.SIGNAL_SEAT_RULE,
        )
    else:
        gate, _ = _gate(store, monkeypatch)
        evaluation = rse.evaluate_review_seats(
            PR_ID, store["results"], branch=BRANCH, head_sha=head,
            rule=rse.SIGNAL_SEAT_RULE, rule_value=rse.SIGNAL_SEAT_RULE,
        )
        assert gate["review_seats"]["rule"] == rse.SIGNAL_SEAT_RULE
        assert gate["review_seats"]["seats"] == list(STACK)
        assert gate["verdict"] == "NO-GO"

    assert evaluation.seats == STACK
    assert evaluation.signal is not None and evaluation.signal.unknown
    expected = {
        "no_seat1_outcome": "seat1_outcome",
        "seat1_fail": "seat1_reviewer",
        "unknown_warning_count": "real_warning_count",
        "unknown_prescan_count": "prescan_count",
        "unknown_risk_class": "risk_class",
        "unknown_irreversible": "irreversible",
        "unknown_head": "head_sha",
    }[case]
    assert expected in evaluation.signal.unknown


def test_a_takeover_on_seat_1_keeps_every_seat():
    """The pure rule: a seat 1 filled through a takeover is no clean first seat."""
    decision = rse.seats_for_signals(rse.SeatSignalInput(
        stack=STACK, seat1_outcome="pass", seat1_reviewer="kimi_gate",
        seat1_takeover_from=rse.ROUTE_TAKEOVER, real_warning_count=0, prescan_count=0,
        risk_class="medium", irreversible=False, head_sha=HEAD,
    ))
    assert decision.seats == STACK


def test_key_on_a_seat2_fail_on_the_head_blocks_without_a_signal(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _write(store, "codex_gate")
    _write(store, "kimi_gate", status="fail", report_body=FAIL_REPORT)
    _write_ledger(store, codex=(0, 0))
    _obligation(store)
    _pin_risk(monkeypatch, HEAD)
    _key(monkeypatch, _signal())

    gate, _ = _gate(store, monkeypatch)

    assert gate["verdict"] == "NO-GO"
    assert gate["review_seats"]["seats"] == ["codex_gate"]
    assert gate["review_seats"]["blocking"] == ["kimi_gate"]
    assert "kimi_gate" in gate["message"]


# ---------------------------------------------------------------------------
# Key off and an unknown value: the old rule, unchanged
# ---------------------------------------------------------------------------


def test_key_off_a_real_warning_does_not_add_a_seat(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _write(store, "codex_gate")
    _write_ledger(store, codex=(3, 0))
    _obligation(store)
    _pin_risk(monkeypatch, HEAD)
    _key(monkeypatch, None)

    gate, _ = _gate(store, monkeypatch)

    assert rse.active_seat_rule().rule == rse.RISK_SEAT_RULE
    assert gate["verdict"] == "GO", gate["message"]
    assert gate["review_seats"]["rule"] == rse.RISK_SEAT_RULE
    assert gate["review_seats"]["seats"] == ["codex_gate"]
    assert "rule_value" not in gate["review_seats"]


def test_an_unknown_key_value_keeps_the_old_rule_and_is_recorded_by_name(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _write(store, "codex_gate")
    _write_ledger(store, codex=(3, 0))
    _obligation(store)
    _pin_risk(monkeypatch, HEAD)
    _key(monkeypatch, "every_seat_always")

    setting = rse.active_seat_rule()
    gate, _ = _gate(store, monkeypatch)

    assert (setting.rule, setting.value, setting.recognised) == (
        rse.RISK_SEAT_RULE, "every_seat_always", False)
    assert gate["verdict"] == "GO", gate["message"]
    assert gate["review_seats"]["rule"] == rse.RISK_SEAT_RULE
    assert gate["review_seats"]["seats"] == ["codex_gate"]
    assert gate["review_seats"]["rule_value"] == "every_seat_always"


def test_the_helper_names_both_known_values(monkeypatch):
    _key(monkeypatch, None)
    assert rse.active_seat_rule() == rse.ActiveSeatRule(rse.RISK_SEAT_RULE, "", True)
    _key(monkeypatch, rse.RISK_SEAT_RULE)
    assert rse.active_seat_rule() == rse.ActiveSeatRule(rse.RISK_SEAT_RULE, rse.RISK_SEAT_RULE, True)
    _key(monkeypatch, rse.SIGNAL_SEAT_RULE)
    assert rse.active_seat_rule() == rse.ActiveSeatRule(rse.SIGNAL_SEAT_RULE, rse.SIGNAL_SEAT_RULE, True)


# ---------------------------------------------------------------------------
# Track closure judges a receipt under the rule its OWN record names
# ---------------------------------------------------------------------------


def _contract():
    from review_contract import Deliverable, ReviewContract

    return ReviewContract(
        pr_id=PR_ID, pr_title="seat rule key", feature_title="second seat on a signal",
        branch=BRANCH, track="A", risk_class="low", merge_policy="standard",
        closure_stage="ready", deliverables=[Deliverable(category="impl", description="seat rule")],
        review_stack=list(STACK),
    )


def _closure(store, record: Dict[str, Any]) -> Dict[str, str]:
    checks = closure_verifier._validate_review_evidence(
        _contract(), store["results"], branch=BRANCH,
        merge_record={"event_type": "pr_merged", "pr_number": PR, "review_seats": record},
    )
    return {
        check.name: check.status for check in checks
        if check.name.startswith(("seat_", "gate_")) or check.name == "review_seats_not_required"
    }


def _signal_record(store, seats: List[str]) -> Dict[str, Any]:
    return {
        "rule": rse.SIGNAL_SEAT_RULE, "pr_id": PR_ID, "head_sha": store["head"],
        "seats": seats, "required_seats": seats, "stack": list(STACK),
        "signals": [] if seats == ["codex_gate"] else ["real_warning"],
        "unknown": [], "source": "config", "base_branch": "main", "verdict": "GO",
    }


def test_closure_judges_a_signal_receipt_by_its_own_rule_while_the_key_is_off(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _write(store, "codex_gate")
    _key(monkeypatch, None)

    checks = _closure(store, _signal_record(store, ["codex_gate"]))

    assert checks == {"seat_codex_gate": "PASS", "review_seats_not_required": "PASS"}
    assert rse.active_seat_rule().rule == rse.RISK_SEAT_RULE


def test_closure_judges_a_risk_receipt_by_its_own_rule_while_the_key_is_on(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _write(store, "codex_gate")
    _key(monkeypatch, rse.SIGNAL_SEAT_RULE)

    record = {
        "rule": rse.RISK_SEAT_RULE, "pr_id": PR_ID, "head_sha": store["head"],
        "seats": ["codex_gate"], "required_seats": ["codex_gate"], "stack": list(STACK),
        "risk_class": "medium", "source": "config", "base_branch": "main", "verdict": "GO",
    }
    checks = _closure(store, record)

    assert checks == {"seat_codex_gate": "PASS", "review_seats_not_required": "PASS"}
    assert rse.active_seat_rule().rule == rse.SIGNAL_SEAT_RULE


def test_closure_demands_every_stack_seat_from_an_inconsistent_signal_record(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _write(store, "codex_gate")
    record = _signal_record(store, ["codex_gate"])
    record["signals"] = ["real_warning"]

    checks = _closure(store, record)

    assert checks["seat_codex_gate"] == "PASS"
    assert checks["gate_kimi_gate"] == "FAIL"
    assert "review_seats_not_required" not in checks


