"""The signal seat rule (track ``tweede-stoel-op-signaal``, PR 2).

Plan: ``claudedocs/specs/tweede-stoel-op-signaal/plan.md`` (Approach 1-5,
Invoercontract, Deliverable 2). Measurement:
``claudedocs/2026-10-30-tweede-stoel-op-waarschuwing.md``.

``review_seat_evaluation.seats_for_signals`` is a pure function: one typed
input, the required seats, the rule name and the signals that fired. The merge
door (``pr_merge``) computes it in SHADOW on every dry run and every merge and
records the outcome next to the seat record; PR 2 changes no decision.

Pinned here: one test per signal that fires alone; the clean input gives seat 1
only; one test per ``UNKNOWN`` field of the input contract and per fail-closed
case (each gives every seat); a ``gate-merge`` path alone is no signal; the
sample is deterministic on the full 40-character head; and the door's dry-run
output and ``pr_merged`` receipt carry ``shadow_seat_rule`` while the seats the
door requires stay the ones of ``RISK_SEAT_RULE``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gate_obligations
import pr_merge
import review_seat_evaluation as rse
from review_seat_support import pin_review_stack

PR = 2478
PR_ID = str(PR)
BRANCH = "dispatch/dlv-83abb30569bd"
DISPATCH = "dlv-83abb30569bd"
#: Sample residue 4: the deterministic sample does NOT fire on this head.
HEAD = "9f3c2a1b7e4d5c6a8b0f1e2d3c4b5a6978879605"
#: Sample residue 0: the deterministic sample fires on this head.
SAMPLE_HEAD = "c" * 40
STACK = ("codex_gate", "kimi_gate")
EVERY_SEAT = list(STACK)
CLEAN_REPORT = "# review\n\nThe reviewer read the whole diff and found nothing that holds the merge.\n"


# ---------------------------------------------------------------------------
# The pure rule
# ---------------------------------------------------------------------------


def _input(**overrides: Any) -> Any:
    values: Dict[str, Any] = {
        "stack": STACK,
        "seat1_outcome": "pass",
        "seat1_reviewer": "codex_gate",
        "seat1_takeover_from": "",
        "real_warning_count": 0,
        "prescan_count": 0,
        "risk_class": "medium",
        "risk_paths": (),
        "irreversible": False,
        "head_sha": HEAD,
    }
    values.update(overrides)
    return rse.SeatSignalInput(**values)


def _decision(**overrides: Any) -> Any:
    return rse.seats_for_signals(_input(**overrides))


def test_the_rule_name_sits_next_to_the_risk_rule():
    assert rse.SIGNAL_SEAT_RULE == "first_seat_or_every_seat_on_signal"
    assert rse.SIGNAL_SEAT_RULE != rse.RISK_SEAT_RULE


class TestOneSignalAlone:
    def test_real_warning(self):
        decision = _decision(real_warning_count=1)
        assert decision.signals == ("real_warning",)
        assert decision.seats == STACK
        assert decision.unknown == ()

    def test_prescan_notices_alone_are_no_signal(self):
        decision = _decision(real_warning_count=0, prescan_count=3)
        assert decision.signals == ()
        assert decision.seats == ("codex_gate",)

    def test_security_path_by_category(self):
        decision = _decision(
            risk_class="high", risk_paths=(("scripts/lib/auth_store.py", "security"),),
        )
        assert decision.signals == ("security_path",)
        assert decision.seats == STACK

    def test_security_path_by_smart_router_kind(self):
        # The category says gate-merge; smart_router.security_path_kind names
        # the "hooks" directory as a security path. Either one is the signal.
        decision = _decision(
            risk_class="high", risk_paths=(("hooks/dispatch_hook.py", "gate-merge"),),
        )
        assert decision.signals == ("security_path",)
        assert decision.seats == STACK

    def test_schema(self):
        decision = _decision(
            risk_class="high", risk_paths=(("schemas/report.sql", "schema"),),
        )
        assert decision.signals == ("schema",)
        assert decision.seats == STACK

    def test_irreversible(self):
        decision = _decision(irreversible=True)
        assert decision.signals == ("irreversible",)
        assert decision.seats == STACK

    def test_sample(self):
        decision = _decision(head_sha=SAMPLE_HEAD)
        assert decision.signals == ("sample",)
        assert decision.seats == STACK

    def test_every_signal_at_once_keeps_the_fixed_order(self):
        decision = _decision(
            real_warning_count=2, risk_class="high",
            risk_paths=(("scripts/lib/auth_store.py", "security"), ("schemas/report.sql", "schema")),
            irreversible=True, head_sha=SAMPLE_HEAD,
        )
        assert decision.signals == ("real_warning", "security_path", "schema", "irreversible", "sample")
        assert decision.seats == STACK


class TestNoSignal:
    def test_a_clean_codex_pass_on_the_head_needs_seat_one_only(self):
        decision = _decision()
        assert decision.seats == ("codex_gate",)
        assert decision.signals == ()
        assert decision.unknown == ()
        assert decision.rule == rse.SIGNAL_SEAT_RULE

    def test_a_gate_merge_path_alone_is_no_signal(self):
        decision = _decision(
            risk_class="high", risk_paths=(("scripts/lib/plan_gate_panel.py", "gate-merge"),),
        )
        assert decision.signals == ()
        assert decision.seats == ("codex_gate",)


class TestUnknownAndFailClosed:
    @pytest.mark.parametrize("field,value", [
        ("seat1_outcome", "unknown"),
        ("seat1_reviewer", "unknown"),
        ("seat1_reviewer", ""),
        ("seat1_takeover_from", "unknown"),
        ("real_warning_count", None),
        ("prescan_count", None),
        ("risk_class", "unknown"),
        ("irreversible", None),
        ("head_sha", "unknown"),
        ("head_sha", "abc123"),
    ])
    def test_an_unknown_field_gives_every_seat(self, field, value):
        decision = _decision(**{field: value})
        assert decision.seats == STACK
        assert field in decision.unknown

    def test_a_fail_of_seat_one_gives_every_seat(self):
        decision = _decision(seat1_outcome="fail")
        assert decision.seats == STACK
        assert "seat1_outcome" not in decision.unknown

    def test_no_outcome_on_the_head_gives_every_seat(self):
        decision = _decision(seat1_outcome=rse.UNKNOWN)
        assert decision.seats == STACK

    def test_a_takeover_on_seat_one_gives_every_seat(self):
        decision = _decision(seat1_takeover_from="kimi_gate")
        assert decision.seats == STACK
        assert decision.signals == ()

    def test_an_unknown_risk_class_gives_every_seat(self):
        decision = _decision(risk_class=rse.UNKNOWN, risk_paths=())
        assert decision.seats == STACK
        assert "risk_class" in decision.unknown


class TestSampleIsDeterministic:
    def test_the_fixed_head_fires_the_sample(self):
        assert (SAMPLE_HEAD, _decision(head_sha=SAMPLE_HEAD).signals) == (SAMPLE_HEAD, ("sample",))

    def test_the_same_head_always_gives_the_same_answer(self):
        first = _decision(head_sha=SAMPLE_HEAD)
        second = _decision(head_sha=SAMPLE_HEAD)
        assert (first.seats, first.signals) == (second.seats, second.signals)

    def test_the_other_fixed_head_does_not_fire_the_sample(self):
        assert _decision(head_sha=HEAD).signals == ()


# ---------------------------------------------------------------------------
# The door: a real store, a real seat evaluation, a shadow record
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch) -> Dict[str, Path]:
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
    return {
        "state": state,
        "reviews": state / "review_gates",
        "results": state / "review_gates" / "results",
        "reports": data_dir / "unified_reports",
        "receipts": state / "t0_receipts.ndjson",
    }


_PR_DATA = {
    "number": PR, "title": "signal seat shadow", "state": "OPEN", "isDraft": False,
    "headRefName": BRANCH, "baseRefName": "main", "headRefOid": HEAD,
    "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
}


def _go() -> Dict[str, Any]:
    return {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}


def _report(store, name: str) -> str:
    path = store["reports"] / name
    path.write_text(CLEAN_REPORT, encoding="utf-8")
    return str(path)


def _record(store, gate: str) -> Dict[str, Any]:
    return {
        "gate": gate, "pr_id": PR_ID, "pr_number": PR, "status": "pass",
        "commit_sha": HEAD, "branch": BRANCH, "contract_hash": f"sha256:{gate}",
        "report_path": _report(store, f"{gate}.md"), "blocking_findings": [],
        "advisory_findings": [], "evidence_source": "live", "test_run": False,
    }


def _write(store, gate: str) -> None:
    path = store["results"] / f"pr-{PR}-{gate}.json"
    path.write_text(json.dumps(_record(store, gate)), encoding="utf-8")


def _ledger_row(store, seat: str, warnings: Optional[int], notices: Optional[int], recorded_at: str) -> Dict[str, Any]:
    import review_rounds

    row: Dict[str, Any] = {
        "project_id": review_rounds.store_project_id(store["reviews"]),
        "pr_number": PR, "head_sha": HEAD, "seat": seat, "reviewer": seat,
        "takeover_from": "", "verdict": "pass", "blocking_count": 0,
        "report_path": str(store["reports"] / f"{seat}.md"), "dispatch_id": f"d-{seat}",
        "recorded_at": recorded_at, "branch": BRANCH, "source": "gate",
    }
    if warnings is not None:
        row["warning_count"] = warnings
    if notices is not None:
        row["prescan_notice_count"] = notices
    return row


def _write_ledger(store, codex: tuple[int, int] = (0, 0)) -> None:
    import review_rounds

    opened = {
        "kind": "opened", "project_id": review_rounds.store_project_id(store["reviews"]),
        "pr_number": PR, "basis": "new", "evidence": 0,
        "recorded_at": "2026-10-30T09:00:00Z", "source": "gate",
    }
    rows = [
        opened,
        _ledger_row(store, "codex_gate", codex[0], codex[1], "2026-10-30T10:00:00Z"),
        _ledger_row(store, "kimi_gate", None, None, "2026-10-30T10:05:00Z"),
    ]
    (store["reviews"] / "rounds.ndjson").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8",
    )


def _pin_risk(monkeypatch, risk_class: str = "medium", paths: Optional[List[tuple]] = None, irreversible: bool = False) -> None:
    import review_risk_class

    def fake(*args: Any, **kwargs: Any) -> Any:
        return review_risk_class.RiskClass(
            risk_class=risk_class, derived=risk_class, reason="fixture",
            deciding=tuple(paths or ()), irreversible=irreversible, head_sha=HEAD,
            merge_base="a" * 40, base_ref="main", rule_version="fixture", files=1,
        )

    monkeypatch.setattr(review_risk_class, "merge_head_risk_class", fake)


def _stub_door_edges(monkeypatch) -> None:
    import file_scope_overlap

    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (_go(), dict(_PR_DATA)))
    monkeypatch.setattr(pr_merge, "_query_pr", lambda n: dict(_PR_DATA))
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


def _door(store, monkeypatch, *argv: str):
    _stub_door_edges(monkeypatch)
    return pr_merge.main(["--pr", PR_ID, "--dispatch-id", DISPATCH, *argv])


def _json_out(capsys) -> Dict[str, Any]:
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):])


def test_the_dry_run_carries_the_shadow_record(store, monkeypatch, capsys):
    _write(store, "codex_gate")
    _write(store, "kimi_gate")
    _write_ledger(store, codex=(0, 0))
    _obligation(store)
    _pin_risk(monkeypatch, "medium")

    rc = _door(store, monkeypatch, "--dry-run", "--json")

    assert rc == pr_merge.EXIT_OK
    shadow = _json_out(capsys)["shadow_seat_rule"]
    assert shadow == {
        "rule": "first_seat_or_every_seat_on_signal",
        "seats": ["codex_gate"],
        "signals": [],
        "unknown": [],
        "head_sha": HEAD,
    }


def test_a_signal_lands_in_the_shadow_but_not_in_the_doors_decision(store, monkeypatch, capsys):
    _write(store, "codex_gate")
    _write(store, "kimi_gate")
    _write_ledger(store, codex=(1, 0))
    _obligation(store)
    _pin_risk(monkeypatch, "medium")
    _stub_door_edges(monkeypatch)

    gate, _ = pr_merge._run_review_gate(PR)

    rc = _door(store, monkeypatch, "--dry-run", "--json")

    assert rc == pr_merge.EXIT_OK
    shadow = _json_out(capsys)["shadow_seat_rule"]
    assert shadow["signals"] == ["real_warning"]
    assert shadow["seats"] == EVERY_SEAT
    # The door's own decision is still the one of RISK_SEAT_RULE on this head.
    assert gate["review_seats"]["rule"] == "first_seat_or_every_seat_by_risk"
    assert gate["review_seats"]["seats"] == ["codex_gate"]


def test_the_seats_the_door_requires_are_identical_with_and_without_the_shadow(store, monkeypatch, capsys):
    _write(store, "codex_gate")
    _write(store, "kimi_gate")
    _write_ledger(store, codex=(0, 0))
    _obligation(store)
    _pin_risk(monkeypatch, "medium")

    seen: List[Dict[str, Any]] = []

    def fake_merge(**kwargs: Any) -> Dict[str, Any]:
        seen.append(dict(kwargs))
        return {
            "success": True, "pr_number": PR, "dispatch_id": DISPATCH, "merge_method": "squash",
            "pr_title": "", "branch": "", "receipt_status": None, "receipt_ok": False,
            "register_ok": False, "error": "", "dry_run": True, "overlaps": [],
        }

    monkeypatch.setattr(pr_merge, "merge_pr", fake_merge)
    assert _door(store, monkeypatch, "--dry-run", "--json") == pr_merge.EXIT_OK
    first = seen[-1]["review_seats"]

    monkeypatch.setattr(
        pr_merge, "_shadow_seat_rule",
        lambda *a, **k: {"rule": "first_seat_or_every_seat_on_signal", "seats": EVERY_SEAT,
                         "signals": ["sample"], "unknown": [], "head_sha": HEAD},
    )
    assert _door(store, monkeypatch, "--dry-run", "--json") == pr_merge.EXIT_OK
    second = seen[-1]["review_seats"]

    assert first == second
    assert first["rule"] == "first_seat_or_every_seat_by_risk"
    assert first["seats"] == ["codex_gate"]


def test_a_failure_while_gathering_is_unknown_and_never_raises(store, monkeypatch):
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the store is unreadable")

    monkeypatch.setattr(pr_merge, "_seat_signal_input", boom)

    shadow = pr_merge._shadow_seat_rule(
        PR, HEAD, branch=BRANCH, base_branch="main", state_dir=store["state"],
        review_seats={"verdict": "GO", "seats": ["codex_gate"], "stack": list(STACK)},
    )

    assert shadow["rule"] == "first_seat_or_every_seat_on_signal"
    assert shadow["seats"] == EVERY_SEAT
    assert shadow["signals"] == []
    assert shadow["unknown"] == [
        "seat1_outcome", "seat1_reviewer", "seat1_takeover_from", "real_warning_count",
        "prescan_count", "risk_class", "irreversible",
    ]
    assert shadow["head_sha"] == HEAD


def _receipts(store) -> List[Dict[str, Any]]:
    path = store["receipts"]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestTheDoorGatherer:
    """The negative paths of the door's own readers: malformed evidence stays UNKNOWN."""

    @pytest.mark.parametrize("risk", [
        None,
        {},
        {"risk_class": "high", "derived": "high", "reason": "fail-closed: the diff cannot be read", "deciding": []},
        {"risk_class": "high", "reason": "no derived class", "deciding": []},
        {"risk_class": "nonsense", "derived": "medium", "reason": "x", "deciding": []},
    ])
    def test_an_unreadable_risk_reading_is_unknown(self, risk):
        assert pr_merge._signal_risk(risk) == ("unknown", ())

    def test_a_readable_risk_reading_keeps_its_paths(self):
        risk = {
            "risk_class": "high", "derived": "high", "reason": "schema path schemas/a.sql",
            "deciding": [{"path": "schemas/a.sql", "category": "schema"}],
            "irreversible": False,
        }
        assert pr_merge._signal_risk(risk) == ("high", (("schemas/a.sql", "schema"),))

    def test_an_irreversible_raise_on_a_medium_diff_is_a_known_high(self):
        risk = {"risk_class": "high", "derived": "medium", "reason": "the dispatch spec says irreversible: true",
                "deciding": [], "irreversible": True}
        assert pr_merge._signal_risk(risk) == ("high", ())

    @pytest.mark.parametrize("value", ["", None, "abc123", "z" * 40])
    def test_a_head_that_is_no_full_sha_is_unknown(self, value):
        assert pr_merge._signal_sha(value) == "unknown"

    def test_a_full_head_is_kept(self):
        assert pr_merge._signal_sha(HEAD) == HEAD


def test_the_pr_merged_receipt_carries_the_shadow_record(store, monkeypatch):
    _write(store, "codex_gate")
    _write(store, "kimi_gate")
    _write_ledger(store, codex=(2, 0))
    _obligation(store)
    _pin_risk(monkeypatch, "medium")
    monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m, h="": (True, ""))

    rc = _door(store, monkeypatch)

    assert rc == pr_merge.EXIT_OK
    receipt = [row for row in _receipts(store) if row.get("event_type") == "pr_merged"][-1]
    assert receipt["shadow_seat_rule"] == {
        "rule": "first_seat_or_every_seat_on_signal",
        "seats": EVERY_SEAT,
        "signals": ["real_warning"],
        "unknown": [],
        "head_sha": HEAD,
    }
    # The merge record still owns the decision, unchanged.
    assert receipt["review_seats"]["rule"] == "first_seat_or_every_seat_by_risk"
    assert receipt["review_seats"]["seats"] == ["codex_gate"]
