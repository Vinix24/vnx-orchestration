"""Gate profile P6 round 2 (B15): the profile reaches the ``review_gate_result`` receipt on the production path.

The helper tests in ``test_gate_profile_trail.py`` hand ``_outcome_receipt_fields`` a payload that already
carries the profile keys. The production result writers never put them there: the request carries them
(inside its ``review_seats`` record) and ``stamp_request_identity`` has to copy them onto the result. These
tests build the request with the real request writer (``ReviewGateManager.request_reviews``), read it back
from disk, let the real failure writer (``record_failure``, committed through ``commit_gate_outcome``) book a
decided ``failed`` result and read the receipt from the receipts file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import review_stack_support
from review_stack_support import isolate_config_registry

import config_registry
import gate_recorder
import gate_request_handler
import review_seats
import review_gate_manager

PR, BRANCH, HEAD = review_stack_support.PR, review_stack_support.BRANCH, review_stack_support.HEAD_SHA
KEY = "VNX_GOVERNANCE_PROFILE"
GATE = "codex_gate"
# A run that ended on a verdict failure (not an execution failure), so the record books ``failed``.
FAILED_RUN = {
    "reason": "gate_found_blockers", "reason_detail": "blocking finding",
    "duration_seconds": 1.0, "partial_output_lines": 0, "runner_pid": 1,
}


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("VNX_PARENT_DISPATCH", "VNX_TASK_CLASS", "VNX_TIER_FROM", "VNX_TIER_TO"):
        monkeypatch.delenv(name, raising=False)
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    headless = store["data_dir"] / "unified_reports" / "headless"
    headless.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_HEADLESS_REPORTS_DIR", str(headless))
    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")
    monkeypatch.setattr("gate_request_handler.get_pr_base_branch", lambda _pr: "")
    monkeypatch.setattr(gate_request_handler.GateRequestHandlerMixin, "_codex_headless_available", lambda self: True)
    return store


def _request_and_fail(env, review_stack=None):
    """Real request writer, then the real failure writer for the request it left on disk."""
    review_gate_manager.ReviewGateManager().request_reviews(
        pr_number=PR, branch=BRANCH, review_stack=review_stack, risk_class="medium",
        changed_files=["scripts/lib/example.py"], mode="per_pr", dispatch_id="20261108-gateprof-p6-r2",
    )
    request = json.loads((env["requests_dir"] / f"pr-{PR}-{GATE}.json").read_text(encoding="utf-8"))
    gate_recorder.record_failure(
        gate=GATE, pr_number=PR, pr_id=str(PR),
        result=FAILED_RUN,
        request_payload=request, requests_dir=env["requests_dir"], results_dir=env["results_dir"],
    )
    return request


def _result_receipts(env):
    lines = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):  # a receipt without dispatch_id is rerouted
        path = env["state_dir"] / name
        lines += path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    records = [json.loads(line) for line in lines if line.strip()]
    return [r for r in records if r.get("event_type") == "review_gate_result" and r.get("gate") == GATE]


class TestProfileReachesTheResultReceipt:
    def test_a_resolved_profile_lands_on_the_receipt(self, env, monkeypatch):
        rows = {KEY: "heavy"}
        config_registry.set_db_resolver(lambda _pid, key: rows.get(key))
        monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
        request = _request_and_fail(env)
        seats = request["review_seats"]
        assert (seats["profile"], seats["profile_source"]) == ("heavy", "project_config")
        (receipt,) = _result_receipts(env)
        assert (receipt["profile"], receipt["profile_source"]) == ("heavy", "project_config")
        result_path = gate_recorder.result_file_path(env["results_dir"], GATE, pr_number=PR, pr_id=str(PR))
        result = json.loads(Path(result_path).read_text(encoding="utf-8"))
        assert (result["profile"], result["profile_source"]) == ("heavy", "project_config")

    def test_a_profile_error_lands_on_the_receipt_without_a_profile(self, env, monkeypatch):
        # The request writer refuses a refused profile at several reads (takeover chain, round cap), so no
        # request is written with the error through the env alone. The seat record's own error branch is
        # given the shape its ``except`` returns (review_seats._explicit_profile_fields); the request writer,
        # the request file, the result writer and the receipt are the real ones.
        error = "VNX_GOVERNANCE_PROFILE='Heavy' (source: env) is refused: not one of the legal profile names"
        monkeypatch.setattr(
            review_seats, "_explicit_profile_fields",
            lambda project_id=None: {"profile": None, "profile_source": "env", "profile_error": error},
        )
        request = _request_and_fail(env, review_stack=[GATE])
        seats = request["review_seats"]
        assert seats["profile"] is None and "Heavy" in seats["profile_error"]
        (receipt,) = _result_receipts(env)
        assert "profile" not in receipt
        assert receipt["profile_error"] == seats["profile_error"]
        assert receipt["profile_source"] == seats["profile_source"]

    def test_a_request_from_before_p6_gives_a_receipt_without_profile_keys(self, env):
        review_gate_manager.ReviewGateManager().request_reviews(
            pr_number=PR, branch=BRANCH, review_stack=None, risk_class="medium",
            changed_files=["scripts/lib/example.py"], mode="per_pr", dispatch_id="20261108-gateprof-p6-r2",
        )
        path = env["requests_dir"] / f"pr-{PR}-{GATE}.json"
        request = json.loads(path.read_text(encoding="utf-8"))
        for key in ("profile", "profile_source", "profile_error"):
            request["review_seats"].pop(key, None)
            request.pop(key, None)
        gate_recorder.record_failure(
            gate=GATE, pr_number=PR, pr_id=str(PR),
            result=FAILED_RUN,
            request_payload=request, requests_dir=env["requests_dir"], results_dir=env["results_dir"],
        )
        (receipt,) = _result_receipts(env)
        assert not {"profile", "profile_source", "profile_error"} & set(receipt)
        assert receipt["gate"] == GATE and receipt["commit_sha"] == request["commit_sha"]


class TestStampRequestIdentityProfile:
    """The copy itself, with no store."""

    def test_top_level_request_keys_win_over_the_seat_record(self):
        request = {"profile": "light", "profile_source": "env", "review_seats": {"profile": "heavy", "profile_source": "project_config"}}
        result = gate_recorder.stamp_request_identity({}, request)
        assert (result["profile"], result["profile_source"]) == ("light", "env")

    def test_a_value_the_result_carries_is_not_overwritten(self):
        result = gate_recorder.stamp_request_identity({"profile": "standard"}, {"review_seats": {"profile": "heavy"}})
        assert result["profile"] == "standard"

    @pytest.mark.parametrize("bad", [None, "", "  ", 7, ["heavy"], {}])
    def test_a_missing_or_malformed_value_leaves_the_keys_absent(self, bad):
        request = {"profile": bad, "profile_source": bad, "profile_error": bad, "review_seats": bad}
        result = gate_recorder.stamp_request_identity({}, request)
        assert not {"profile", "profile_source", "profile_error"} & set(result)
