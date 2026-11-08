"""Gate profile P5: the in-repo consumer proof.

A throwaway project store with ``VNX_GOVERNANCE_PROFILE=light`` has one review seat, and that
seat's verdict, written through the same writer the door uses (``record_result`` ->
``gate_outcome.commit_gate_outcome``), books exactly one ``review_gate_result`` receipt with
``pr_number``, ``head_sha`` and PASS. The record is not built by hand: the writer builds it.
This proves the mechanism only; the one real consumer run is the track's O8.
"""
from __future__ import annotations

import json

import pytest

import review_stack_support
from review_stack_support import HEAD_SHA, PR, isolate_config_registry

import review_gate_manager as rgm
from review_seats import resolve_review_seats


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return review_stack_support.build_project_store(tmp_path, monkeypatch)


def _receipts(env):
    lines = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):
        path = env["state_dir"] / name
        lines += path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [r for r in map(json.loads, filter(str.strip, lines)) if r.get("event_type") == "review_gate_result"]


def _record(env, gate):
    report = env["reports_dir"] / f"{gate}-report.md"
    report.write_text("# report\n", encoding="utf-8")
    rgm.ReviewGateManager().record_result(
        gate=gate, pr_number=PR, branch=review_stack_support.BRANCH, status="pass", summary="clean",
        contract_hash="contract-hash", report_path=str(report), pr_id=str(PR),
    )


def test_light_with_one_seat_books_exactly_one_review_gate_result(env, monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
    resolved = resolve_review_seats()
    assert resolved.seats == ("codex_gate",) and resolved.profile == "light"

    for gate in resolved.seats:
        _record(env, gate)

    (receipt,) = _receipts(env)
    assert receipt["gate"] == "codex_gate"
    assert str(receipt["pr_number"]) == str(PR)
    assert receipt["head_sha"] == HEAD_SHA and receipt["commit_sha"] == HEAD_SHA
    assert receipt["gate_status"] == "pass"


def test_the_result_record_comes_from_the_writer_and_carries_the_head(env, monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
    _record(env, "codex_gate")
    stored = json.loads((env["results_dir"] / f"pr-{PR}-codex_gate.json").read_text(encoding="utf-8"))
    assert stored["status"] == "pass" and stored["commit_sha"] == HEAD_SHA
    assert not (env["results_dir"] / f"pr-{PR}-kimi_gate.json").exists(), "light has no second seat"


def test_a_second_seat_under_light_is_not_booked_by_the_profile(env, monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
    assert "kimi_gate" not in resolve_review_seats().stack
    assert _receipts(env) == []
