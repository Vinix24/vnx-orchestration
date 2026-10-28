"""RG-5: the B3 override also through ``VNX_OVERRIDE_B3``, never without a reason.

Plan: ``claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/review-rondegrens-met-tiebreaker.md``
(revision 1b, decision 8). The documented carrier stays ``--override-b3
--reason "..."``; next to it the merge door (``pr_merge.py``) and the review
gate CLI (``review_gate_manager.py``) also read ``VNX_OVERRIDE_B3=1`` plus
``VNX_OVERRIDE_B3_REASON``. The flag wins when both are given, either carrier
without a non-empty reason is refused with the door's existing text, and the
record names the carrier (``flag`` or ``env``) next to the reason.

Nothing leaves ``tmp_path``: the door's gh edges are stubbed by the R1
harness, and no provider lane is reached.
"""
from __future__ import annotations

import json

import pytest

import review_stack_support
from test_r1_round_ledger import (
    _clean_config_registry, _codex_rounds, _door_pass, _line_for, _opened, _rc, _stub_door_edges,
    _write_ledger, door, env, BRANCH, PR,
)

ENV_REASON = "operator: env carrier, the last fix is one line"
FLAG_REASON = "operator: flag carrier, the last fix is one line"
REFUSAL_TEXT = '--override-b3 zonder reden geweigerd: geef --reason "..." mee'


@pytest.fixture(autouse=True)
def _no_override_env(monkeypatch):
    """The carrier env never leaks in from the shell or from another test."""
    monkeypatch.delenv("VNX_OVERRIDE_B3", raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_B3_REASON", raising=False)


def _capped_door(door):
    """A capped PR (three B3.1 blockers) whose two PASSes are in the ledger."""
    codex, kimi = _door_pass(door, "codex_gate", tag="01"), _door_pass(door, "kimi_gate", tag="02")
    _write_ledger(door["ledger"], [_opened()] + _codex_rounds(3) + [_line_for(codex, at=20), _line_for(kimi, at=21)])


def _merged_receipts(door):
    raw = door["receipts"].read_text().splitlines() if door["receipts"].exists() else []
    receipts = [json.loads(line) for line in raw if line.strip()]
    return [r for r in receipts if r.get("event_type") == "pr_merged"]


class TestTheEnvCarrierAtTheMergeDoor:
    def test_the_env_pair_lets_the_capped_seat_count_in_a_dry_run(self, door, monkeypatch):
        """Acceptance 1, dry-run half: the door itself goes GO, through the env entry point."""
        import pr_merge

        _capped_door(door)
        _stub_door_edges(monkeypatch, door)
        monkeypatch.setenv("VNX_OVERRIDE_B3", "1")
        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", ENV_REASON)

        namespace = type("Args", (), {"override_b3": False, "reason": None})()
        assert pr_merge._b3_override_arg(namespace) == ENV_REASON
        assert pr_merge._b3_override_carrier(namespace) == "env"

        rc = _rc(pr_merge.main, ["--pr", str(PR), "--dispatch-id", "20261027-rg5-door", "--dry-run"])

        assert rc == pr_merge.EXIT_OK
        assert _merged_receipts(door) == [], "a dry run merges nothing and writes no receipt"

    def test_the_env_pair_lands_carrier_env_in_the_pr_merged_receipt(self, door, monkeypatch):
        """Acceptance 1, record half: the reason and ``carrier: env`` are in the receipt."""
        import pr_merge

        _capped_door(door)
        _stub_door_edges(monkeypatch, door)
        monkeypatch.setenv("VNX_OVERRIDE_B3", "1")
        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", ENV_REASON)

        rc = _rc(pr_merge.main, ["--pr", str(PR), "--dispatch-id", "20261027-rg5-door"])

        assert rc == pr_merge.EXIT_OK
        override = _merged_receipts(door)[-1]["review_seats"]["round_ledger"]["override"]
        assert override == {"flag": "--override-b3", "carrier": "env", "reason": ENV_REASON, "applied": True}

    def test_the_env_pair_without_a_reason_is_refused_with_the_existing_text(self, door, monkeypatch, capsys):
        """Acceptance 2: ``VNX_OVERRIDE_B3=1`` alone is a NO-GO, in the door's own words."""
        import pr_merge

        _capped_door(door)
        _stub_door_edges(monkeypatch, door)
        monkeypatch.setenv("VNX_OVERRIDE_B3", "1")
        monkeypatch.delenv("VNX_OVERRIDE_B3_REASON", raising=False)

        rc = _rc(pr_merge.main, ["--pr", str(PR), "--dispatch-id", "20261027-rg5-door"])

        assert rc == pr_merge.EXIT_ERROR
        assert REFUSAL_TEXT in capsys.readouterr().err
        assert _merged_receipts(door) == [], "a refused merge writes no pr_merged receipt"

    def test_the_flag_wins_when_both_carriers_are_given(self, door, monkeypatch):
        """Acceptance 4: with both, the flag's reason and ``carrier: flag`` are recorded."""
        import pr_merge

        _capped_door(door)
        _stub_door_edges(monkeypatch, door)
        monkeypatch.setenv("VNX_OVERRIDE_B3", "1")
        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", ENV_REASON)

        rc = _rc(pr_merge.main, [
            "--pr", str(PR), "--dispatch-id", "20261027-rg5-door", "--override-b3", "--reason", FLAG_REASON,
        ])

        assert rc == pr_merge.EXIT_OK
        override = _merged_receipts(door)[-1]["review_seats"]["round_ledger"]["override"]
        assert override == {"flag": "--override-b3", "carrier": "flag", "reason": FLAG_REASON, "applied": True}


class TestTheEnvCarrierAtTheReviewGateCli:
    def test_the_flag_still_reports_carrier_flag_on_the_gate_record(self, env):
        """Acceptance 3: the documented flag path is unchanged apart from the carrier it names."""
        import review_gate_manager

        _write_ledger(env["ledger"], _codex_rounds(2, 1, 1, 1, 1))

        rc = _rc(review_gate_manager.main, [
            "request", "--pr", str(PR), "--branch", BRANCH, "--review-stack", "codex_gate",
            "--changed-files", "x.py", "--override-b3", "--reason", FLAG_REASON,
        ])

        assert rc == 0
        request = json.loads((env["requests_dir"] / f"pr-{PR}-codex_gate.json").read_text())
        assert request["override_b3"]["carrier"] == "flag"
        assert request["override_b3"]["reason"] == FLAG_REASON

    def test_the_env_pair_reports_carrier_env_on_the_gate_record(self, env, monkeypatch):
        """Acceptance 1 (gate side): the request's ``override_b3`` field names the env carrier."""
        import review_gate_manager

        _write_ledger(env["ledger"], _codex_rounds(2, 1, 1, 1, 1))
        monkeypatch.setenv("VNX_OVERRIDE_B3", "1")
        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", ENV_REASON)

        rc = _rc(review_gate_manager.main, [
            "request", "--pr", str(PR), "--branch", BRANCH, "--review-stack", "codex_gate",
            "--changed-files", "x.py",
        ])

        assert rc == 0
        request = json.loads((env["requests_dir"] / f"pr-{PR}-codex_gate.json").read_text())
        assert request["override_b3"]["carrier"] == "env"
        assert request["override_b3"]["reason"] == ENV_REASON

    def test_the_env_pair_without_a_reason_is_refused(self, env, monkeypatch, capsys):
        """Acceptance 2 (gate side): the same refusal, and no request is written."""
        import review_gate_manager

        monkeypatch.setenv("VNX_OVERRIDE_B3", "1")
        monkeypatch.delenv("VNX_OVERRIDE_B3_REASON", raising=False)

        rc = _rc(review_gate_manager.main, [
            "request", "--pr", str(PR), "--branch", BRANCH, "--review-stack", "codex_gate",
            "--changed-files", "x.py",
        ])

        assert rc == 2
        assert "VNX_OVERRIDE_B3=1 needs a non-empty VNX_OVERRIDE_B3_REASON" in capsys.readouterr().err
        assert not (env["requests_dir"] / f"pr-{PR}-codex_gate.json").exists()
