"""The request handler names the provider in the gate's row (review-lane P3a).

ADR-046 Amendment 1, A3, T0 decision 6: the ``provider`` label of a request
payload comes from ``GateLaneRow.request_provider`` through one lookup, and the
four labels stay byte-equal because ``stop_conditions`` and ``provider_costs``
match on them.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import types
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_lane_rows
import gate_request_handler
import review_stack_support
from gate_request_handler import GateRequestHandlerMixin, request_provider

PR, BRANCH, MODE, RISK = 5101, "feat/p3a", "standard", "high"

FOUR_LABELS = {
    "kimi_gate": "kimi",
    "glm_gate": "glm",
    "deepseek_gate": "deepseek",
    "claude_gate": "claude",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "review_gate_manager",
                        types.SimpleNamespace(_utc_now=lambda: "2026-11-08T10:00:00Z"))
    return store


class _Requests(GateRequestHandlerMixin):
    def __init__(self, env):
        self.requests_dir, self.results_dir = env["requests_dir"], env["results_dir"]
        self.state_dir = env["state_dir"]

    def _request_path(self, gate, pr_number):
        return self.requests_dir / f"pr-{pr_number}-{gate}.json"

    def _build_report_path(self, *, gate, requested_at, pr_number=None, pr_id=""):
        return f"report-{gate}-{pr_number or pr_id}.md"


def _request(env, method, monkeypatch):
    monkeypatch.setattr(gate_request_handler, "gate_is_available", lambda *a, **k: True)
    return getattr(_Requests(env), method)(PR, BRANCH, RISK, ["scripts/a.py"], MODE)


@pytest.mark.parametrize("method,gate", [
    ("_request_kimi", "kimi_gate"),
    ("_request_glm", "glm_gate"),
    ("_request_deepseek", "deepseek_gate"),
    ("_request_claude", "claude_gate"),
])
def test_the_four_labels_on_a_real_request_payload(env, monkeypatch, method, gate):
    payload = _request(env, method, monkeypatch)

    assert payload["gate"] == gate
    assert payload["provider"] == FOUR_LABELS[gate]
    on_disk = json.loads(_Requests(env)._request_path(gate, PR).read_text(encoding="utf-8"))
    assert on_disk["provider"] == FOUR_LABELS[gate]


@pytest.mark.parametrize("method,gate", [
    ("_request_kimi", "kimi_gate"),
    ("_request_glm", "glm_gate"),
    ("_request_deepseek", "deepseek_gate"),
    ("_request_claude", "claude_gate"),
])
def test_a_relabelled_row_reaches_the_payload(env, monkeypatch, method, gate):
    rows = tuple(
        dataclasses.replace(r, request_provider="relabelled") if r.gate == gate else r
        for r in gate_lane_rows.GATE_LANE_ROWS
    )
    monkeypatch.setattr(gate_request_handler, "GATE_LANE_ROWS", rows)

    assert _request(env, method, monkeypatch)["provider"] == "relabelled"


def test_the_lookup_gives_the_four_labels_from_the_rows():
    assert {r.gate: request_provider(r.gate) for r in gate_lane_rows.GATE_LANE_ROWS} == FOUR_LABELS


def test_a_synthetic_row_gets_its_own_label_without_a_handler_change(env, monkeypatch):
    row = dataclasses.replace(
        gate_lane_rows.GATE_LANE_ROWS[0], gate="synthetic_gate", lane="synthetic",
        request_provider="synthetic-label", dispatcher="provider_dispatch",
    )
    monkeypatch.setattr(
        gate_request_handler, "GATE_LANE_ROWS", gate_lane_rows.GATE_LANE_ROWS + (row,)
    )

    payload = _Requests(env)._request_harness_seat(
        "synthetic_gate", True, pr_number=PR, branch=BRANCH, risk_class=RISK,
        changed_files=["scripts/a.py"], mode=MODE, dispatch_id="",
    )

    assert payload["gate"] == "synthetic_gate"
    assert payload["provider"] == "synthetic-label"


@pytest.mark.parametrize("gate", ["", "no_such_gate", "codex_gate", None])
def test_a_gate_without_a_row_refuses_by_name(gate):
    with pytest.raises(ValueError, match="no gate lane row"):
        request_provider(gate)


def test_a_gate_without_a_row_writes_no_request(env):
    requests = _Requests(env)
    with pytest.raises(ValueError, match="no gate lane row"):
        requests._request_harness_seat(
            "no_such_gate", True, pr_number=PR, branch=BRANCH, risk_class=RISK,
            changed_files=[], mode=MODE, dispatch_id="",
        )
    assert not requests._request_path("no_such_gate", PR).exists()
