"""Gate-profiel P1 round 2: an invalid governance profile is a structured refusal at every review-seat boundary.

``resolve_review_seats()`` raises ``GateProfileConfigError`` (a ``ReviewGateConfigError``) for a
malformed ``VNX_GOVERNANCE_PROFILE``. Each boundary that used to turn a ``ReviewSeatError`` into a
named refusal must do the same for the shared base, with the raw value in the text.
"""
from __future__ import annotations

import json

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import isolate_config_registry

import gate_enforcement_verify as gev
import review_seat_evaluation as rse

HEAD = "a" * 40
PR_ID = "2490"


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


def _record_seats(requests_dir, seats):
    requests_dir.mkdir(parents=True, exist_ok=True)
    (requests_dir / f"pr-{PR_ID}-codex_gate.json").write_text(json.dumps({
        "gate": "codex_gate", "pr_id": PR_ID, "pr_number": int(PR_ID), "commit_sha": HEAD,
        "requested_at": "2026-11-08T08:00:00Z",
        "review_seats": {"source": rse.SOURCE_RECORDED, "seats": list(seats), "value": ",".join(seats)},
    }), encoding="utf-8")


@pytest.mark.parametrize("raw", ["Heavy", "zwaar"])
class TestInvalidProfileRefuses:
    def test_fresh_config_seats_returns_the_refusal(self, monkeypatch, raw):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", raw)
        seats, value, refusal = rse.fresh_config_seats()
        assert seats is None and value == ""
        assert raw in refusal

    def test_merge_evaluation_refuses_without_recorded_seats(self, monkeypatch, tmp_path, raw):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", raw)
        required = rse.required_seats(tmp_path / "requests", PR_ID, HEAD)
        assert required.seats == () and raw in required.error

    def test_merge_evaluation_keeps_recorded_seats_and_names_the_refusal(self, monkeypatch, tmp_path, raw):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", raw)
        _record_seats(tmp_path / "requests", ["codex_gate", "kimi_gate"])
        required = rse.required_seats(tmp_path / "requests", PR_ID, HEAD, by_risk=False)
        assert required.stack == ("codex_gate", "kimi_gate")
        assert raw in required.note

    def test_evaluate_review_seats_does_not_raise(self, monkeypatch, tmp_path, raw):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", raw)
        results = tmp_path / "results"
        results.mkdir()
        evaluation = rse.evaluate_review_seats(PR_ID, results, head_sha=HEAD, round_ledger=False)
        assert raw in str(evaluation)

    def test_enforcement_verify_default_stack_is_a_verification_error(self, monkeypatch, raw):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", raw)
        with pytest.raises(gev.VerificationError, match=raw):
            gev._default_stack()


@pytest.mark.parametrize("raw", ["light", "standard", "heavy"])
def test_valid_profiles_give_todays_seats(monkeypatch, raw):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", raw)
    seats, _value, refusal = rse.fresh_config_seats()
    assert refusal == "" and seats
    assert list(gev._default_stack()) == list(seats)


def test_unset_profile_gives_todays_seats(monkeypatch):
    monkeypatch.delenv("VNX_GOVERNANCE_PROFILE", raising=False)
    seats, _value, refusal = rse.fresh_config_seats()
    assert refusal == "" and seats
