"""OI-2701 (a): an approved takeover fills seat 1 with its own counts. The key is patched through the
environment with no config store behind it, the seam ``tests/test_seat_rule_key.py`` uses."""
from __future__ import annotations

import logging
import sys
from dataclasses import replace
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gate_artifacts
import review_report_counts as rrc
import review_rounds
import review_seat_evaluation as rse
from review_seat_support import no_config_store

KEY = "VNX_REVIEW_TAKEOVER_FULL_FILL"
PAIR = "deepseek_gate:deepseek-v4-pro"
PR = 2592
HEAD = "9f3c2a1b7e4d5c6a8b0f1e2d3c4b5a6978879605"  # sample residue 4: the sample does not fire
STACK = ("codex_gate", "kimi_gate")
MEDIUM = {"risk_class": "medium", "derived": "medium", "reason": "fixture", "deciding": [], "irreversible": False}


@pytest.fixture(autouse=True)
def _key_off(monkeypatch):
    no_config_store(monkeypatch)
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_REVIEW_TAKEOVER_FULL_FILL", raising=False)


def _report(tmp_path, gate="deepseek_gate", pr=PR, advisory=()) -> str:
    # The transcript quotes the takeover's own header: only the report header may count.
    path = tmp_path / f"{gate}-{pr}.md"
    path.write_text(gate_artifacts.format_report(
        gate, "**Gate**: deepseek_gate\n**PR**: 2592", {"pr_id": str(pr), "branch": "b"},
        blocking=[], advisory=[{"message": text, "severity": "warning"} for text in advisory],
    ), encoding="utf-8")
    return str(path)


def _payload(report_path, **extra):
    return {
        "gate": "deepseek_gate", "pr_id": str(PR), "pr_number": PR, "status": "pass", "commit_sha": HEAD,
        "contract_hash": "sha256:deepseek", "report_path": report_path, "blocking_findings": [],
        "seat": "codex_gate", "takeover": True, "takeover_from": "kimi_gate", "model": "deepseek-v4-pro", **extra,
    }


def _decide(payload, risk=MEDIUM):
    line = review_rounds.line_from_payload(payload, gate="deepseek_gate", project_id="vnx-dev")
    fill = rse.SeatFill("codex_gate", True, "deepseek_gate", rse.ROUTE_TAKEOVER, "ok", True, record=payload)
    required = rse.RequiredSeats(STACK, rse.SOURCE_RECORDED, stack=STACK, risk=risk)
    rounds = rse.DoorRounds("", PR, (line,))
    return rse.seats_for_signals(rse.signal_input_for(required, (fill,), rounds, HEAD)), required, fill, rounds


def test_a_an_approved_takeover_pass_needs_seat_one_only(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, PAIR)
    decision, required, fill, rounds = _decide(_payload(_report(tmp_path)))
    assert decision.seats == ("codex_gate",)
    assert (decision.signals, decision.unknown, decision.takeover_full_fill) == ((), (), PAIR)
    assert decision.to_record()["takeover_full_fill"] == PAIR
    evaluation = rse.SeatEvaluation(
        rse.SeatScope(str(PR), tmp_path, head_sha=HEAD),
        replace(required, seats=decision.seats, rule=rse.SIGNAL_SEAT_RULE), (fill,), (), {}, rounds, decision,
    )
    assert evaluation.to_record()["takeover_full_fill"] == PAIR
    assert f"overname telt als stoel 1: {PAIR} ({KEY})" in evaluation.message()


@pytest.mark.parametrize("key,extra", [
    (None, {}), ("", {}),  # (b) the key empty: today's behaviour
    ("deepseek_gate:deepseek-flash", {}),  # (c) a pair for another model
    ("kimi_gate:deepseek-v4-pro", {}), (PAIR, {"model": "deepseek-flash"}), (PAIR, {"model": None}),
])
def test_b_c_a_takeover_outside_the_list_keeps_every_seat(tmp_path, monkeypatch, key, extra):
    if key is not None:
        monkeypatch.setenv(KEY, key)
    decision = _decide(_payload(_report(tmp_path), **extra))[0]
    assert decision.seats == STACK and decision.takeover_full_fill == ""
    assert "takeover_full_fill" not in decision.to_record()


@pytest.mark.parametrize("report", [{"pr": 2591}, {"gate": "codex_gate"}, None])
def test_d_an_unknown_warning_count_keeps_every_seat(tmp_path, monkeypatch, report):
    monkeypatch.setenv(KEY, PAIR)
    decision = _decide(_payload(_report(tmp_path, **report) if report else str(tmp_path / "gone.md")))[0]
    assert decision.seats == STACK and "real_warning_count" in decision.unknown
    assert decision.takeover_full_fill == ""


@pytest.mark.parametrize("advisory,risk,signal", [
    (("the lock is never released",), MEDIUM, "real_warning"),  # (e)
    ((), {**MEDIUM, "risk_class": "high", "derived": "high",
          "deciding": [{"path": "scripts/lib/auth_store.py", "category": "security"}]}, "security_path"),  # (f)
])
def test_e_f_a_signal_after_an_approved_takeover_keeps_every_seat(tmp_path, monkeypatch, advisory, risk, signal):
    monkeypatch.setenv(KEY, PAIR)
    decision = _decide(_payload(_report(tmp_path, advisory=advisory)), risk=risk)[0]
    assert (decision.seats, decision.signals, decision.takeover_full_fill) == (STACK, (signal,), PAIR)


@pytest.mark.parametrize("value", ["deepseek_gate", ":x", "a:", "a:b:c"])
def test_g_a_malformed_key_value_keeps_every_seat_and_names_the_key(tmp_path, monkeypatch, caplog, value):
    monkeypatch.setenv(KEY, value)
    caplog.set_level(logging.WARNING, logger=rse.logger.name)
    decision = _decide(_payload(_report(tmp_path)))[0]
    assert decision.seats == STACK and decision.takeover_full_fill == ""
    assert KEY in caplog.text and repr(value) in caplog.text


def test_g_a_malformed_entry_approves_nobody_beside_a_valid_one():
    assert rse.parse_full_fill_pairs(f" {PAIR} , junk,, ") == (("deepseek_gate", "deepseek-v4-pro"),)
    assert rse.parse_full_fill_pairs(None) == () and rse.parse_full_fill_pairs(7) == ()


def test_h_a_takeover_reads_only_its_own_report(tmp_path):
    own = _report(tmp_path, advisory=("check the lock",))
    assert rrc.counts_for_payload(_payload(own)) == rrc.ReportCounts(warnings=1, notices=0)
    twice = tmp_path / "twice.md"
    twice.write_text(Path(own).read_text().replace("**Gate**: deepseek_gate", "**Gate**: deepseek_gate\n**Gate**: x", 1))
    for path in (_report(tmp_path, gate="codex_gate"), _report(tmp_path, pr=2591), str(twice), str(tmp_path / "no.md")):
        assert rrc.counts_for_payload(_payload(path)) is None
    for bad in ({"pr_number": None}, {"pr_number": "2592"}, {"pr_number": True}, {"gate": ""}, {"gate": None}):
        assert rrc.counts_for_payload(_payload(own, **bad)) is None
    # A verdict that is no takeover reads the report it names, header or not (unchanged).
    foreign = _report(tmp_path, gate="codex_gate", pr=2591)
    assert rrc.counts_for_payload(_payload(foreign, takeover=False)) == rrc.ReportCounts(0, 0)


def test_i_the_round_line_carries_the_reviewer_model(tmp_path):
    payload = _payload(_report(tmp_path))
    line = review_rounds.line_from_payload(payload, gate="deepseek_gate", project_id="vnx-dev")
    assert review_rounds.RoundLine.from_dict(line.to_dict()).reviewer_model == "deepseek-v4-pro"
    for model in (None, 7, ""):
        old = review_rounds.line_from_payload({**payload, "model": model}, gate="deepseek_gate", project_id="vnx-dev")
        assert "reviewer_model" not in old.to_dict() and review_rounds.RoundLine.from_dict(old.to_dict()).reviewer_model == ""
    with pytest.raises(ValueError):
        review_rounds.RoundLine.from_dict({**line.to_dict(), "reviewer_model": 7})


_FULL = dict(
    stack=STACK, seat1_outcome="pass", seat1_reviewer="deepseek_gate", seat1_takeover_from="kimi_gate",
    real_warning_count=0, prescan_count=0, risk_class="medium", irreversible=False, head_sha=HEAD,
    seat1_reviewer_model="deepseek-v4-pro", seat1_blocking_count=0, full_fill_pairs=(("deepseek_gate", "deepseek-v4-pro"),),
)


@pytest.mark.parametrize("field,value", [
    ("seat1_blocking_count", None), ("seat1_blocking_count", 1), ("seat1_blocking_count", True),
    ("seat1_outcome", "fail"), ("seat1_reviewer", rse.UNKNOWN), ("seat1_reviewer_model", ""),
    ("seat1_takeover_from", rse.UNKNOWN), ("real_warning_count", None), ("prescan_count", None),
    ("full_fill_pairs", ()), ("full_fill_pairs", (PAIR,)), ("full_fill_pairs", (("deepseek_gate",),)),
])
def test_the_pure_rule_keeps_every_seat_when_one_condition_fails(field, value):
    assert rse.seats_for_signals(rse.SeatSignalInput(**_FULL)).seats == ("codex_gate",)
    decision = rse.seats_for_signals(rse.SeatSignalInput(**{**_FULL, field: value}))
    assert decision.seats == STACK and decision.takeover_full_fill == ""
