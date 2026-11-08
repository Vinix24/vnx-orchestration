"""The four harness seats are requested through one path (review-lane P3b).

ADR-046 Amendment 1, T0 decision 6: kimi, glm, deepseek and claude build the
same request payload through ``_request_harness_seat``. One key set, one
``report_path`` rule (set when the seat is requested, empty when it is not
executable), and the four ``provider`` labels stay byte-equal.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_lane_rows
import gate_request_handler
import review_stack_support

PR, BRANCH, MODE, RISK = 5102, "feat/p3b", "standard", "high"

SEATS = [
    ("_request_kimi", "kimi_gate", "kimi"),
    ("_request_glm", "glm_gate", "glm"),
    ("_request_deepseek", "deepseek_gate", "deepseek"),
    ("_request_claude", "claude_gate", "claude"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    monkeypatch.setattr("gate_request_handler.get_pr_base_branch", lambda _pr: "")
    monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr: "a" * 40)
    return store


def _manager():
    import review_gate_manager
    return review_gate_manager.ReviewGateManager()


def _request(env, method, monkeypatch, available):
    monkeypatch.setattr(gate_request_handler, "gate_is_available", lambda *a, **k: available)
    return getattr(_manager(), method)(
        PR, BRANCH, RISK, ["scripts/a.py"], MODE, dispatch_id="dlv-test"
    )


def _four(env, monkeypatch, available):
    return {
        gate: _request(env, method, monkeypatch, available) for method, gate, _ in SEATS
    }


@pytest.mark.parametrize("available", [True, False])
def test_the_four_payloads_have_one_key_set(env, monkeypatch, available):
    key_sets = {gate: sorted(p) for gate, p in _four(env, monkeypatch, available).items()}

    assert len({tuple(keys) for keys in key_sets.values()}) == 1, key_sets


@pytest.mark.parametrize("available", [True, False])
def test_report_path_follows_one_rule_for_all_four(env, monkeypatch, available):
    paths = {gate: p["report_path"] for gate, p in _four(env, monkeypatch, available).items()}

    if available:
        assert all(path and gate in path for gate, path in paths.items()), paths
    else:
        assert paths == {gate: "" for _, gate, _ in SEATS}


@pytest.mark.parametrize("method,gate,label", SEATS)
def test_the_four_labels_stay_byte_equal(env, monkeypatch, method, gate, label):
    payload = _request(env, method, monkeypatch, True)

    assert payload["provider"] == label
    assert payload["gate"] == gate
    on_disk = json.loads(_manager()._request_path(gate, PR).read_text(encoding="utf-8"))
    assert {k: on_disk[k] for k in payload} == payload


@pytest.mark.parametrize("method,gate,label", SEATS)
def test_an_unavailable_seat_is_booked_with_a_reason(env, monkeypatch, method, gate, label):
    payload = _request(env, method, monkeypatch, False)

    assert payload["status"] == "not_executable"
    assert payload["reason"]
    assert payload["reason_detail"]
    assert payload["failure_reason"] == payload["reason_detail"]
    assert payload["resolved_at"] == payload["requested_at"]
    assert payload["dispatch_id"] == "dlv-test"


def test_a_synthetic_row_goes_through_the_one_path(env, monkeypatch):
    row = dataclasses.replace(
        gate_lane_rows.GATE_LANE_ROWS[0], gate="synthetic_gate", lane="synthetic",
        request_provider="synthetic-label", dispatcher="provider_dispatch",
    )
    monkeypatch.setattr(
        gate_request_handler, "GATE_LANE_ROWS", gate_lane_rows.GATE_LANE_ROWS + (row,)
    )
    requests = _manager()
    kwargs = dict(pr_number=PR, branch=BRANCH, risk_class=RISK,
                  changed_files=["scripts/a.py"], mode=MODE, dispatch_id="dlv-test")

    synthetic = requests._request_harness_seat("synthetic_gate", True, **kwargs)
    seat = _request(env, "_request_kimi", monkeypatch, True)

    assert synthetic["provider"] == "synthetic-label"
    assert "synthetic_gate" in synthetic["report_path"]
    assert sorted(synthetic) == sorted(seat)
