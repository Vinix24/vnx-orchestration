"""gate_dispatch_identity_purge.py — the OI-1725 cleanup script.

The guard added in this dispatch refuses a NEW poisoned record, but the
records already on disk from before the fix (``pr-1840-kimi_gate.json``,
``pr-1841-kimi_gate.json``) stay poisoned. This script removes them, and only
them, by what they ARE — never by PR number, so a fresh instance of the same
collision is removed too.

It VERIFIES the misstand before writing and refuses (exit 2, no write) when
the misstand is not provable, exactly like
``gate_obligation_reopen_stale_evidence.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_dispatch_identity_purge as purge

_BUILDER_DISPATCH_ID = "20260911-oi1711-publicatiepad-werkt-twee-keer"


def _poisoned_record(gate: str = "kimi_gate", dispatch_id: str = _BUILDER_DISPATCH_ID) -> dict:
    """The measured shape: a harness-lane gate result carrying the builder's
    dispatch-id instead of a gate-eigen one, with ``provider``/``model`` never
    stamped (the exact defect of #1837)."""
    return {
        "gate": gate,
        "pr_id": "1840",
        "pr_number": 1840,
        "status": "completed",
        "dispatch_id": dispatch_id,
        "provider": None,
        "model": None,
        "report_path": "",
    }


def _clean_record(gate: str = "kimi_gate") -> dict:
    return {
        "gate": gate,
        "pr_id": "1840",
        "pr_number": 1840,
        "status": "completed",
        "dispatch_id": f"{gate[:-5]}-gate-pr1840-1789147884",
        "provider": "kimi",
        "model": "kimi-k3",
        "report_path": "",
    }


# ---------------------------------------------------------------------------
# 1. The recognition model: what a poisoned record IS
# ---------------------------------------------------------------------------


class TestRecognitionModel:

    def test_a_builder_dispatch_id_is_the_builder_form(self):
        assert purge.is_builder_dispatch_id(_BUILDER_DISPATCH_ID) is True
        assert purge.is_builder_dispatch_id("20260912-oi1725-poort-krijgt-eigen-identiteit") is True

    def test_a_gate_eigen_id_is_not_the_builder_form(self):
        assert purge.is_builder_dispatch_id("kimi-gate-pr1840-1789147884") is False
        assert purge.is_builder_dispatch_id("glm-gate-pr1839-1789147884") is False

    def test_recognises_by_what_it_is_not_by_pr_number(self):
        """A builder dispatch-id for an entirely different dispatch/PR is still
        the same misstand — the predicate keys on the shape, not on a known name."""
        fresh = _poisoned_record(dispatch_id="20260912-oi1725-nieuwe-vorm-van-dezelfde-botsing")
        assert purge.is_poisoned_harness_lane_record(fresh) is True

    def test_a_clean_gate_eigen_record_is_not_poisoned(self):
        assert purge.is_poisoned_harness_lane_record(_clean_record()) is False

    def test_a_path_binary_gate_with_a_builder_id_is_legitimate(self):
        """codex_gate carries the builder's dispatch-id BY DESIGN — never a
        purge target."""
        codex = _poisoned_record(gate="codex_gate", dispatch_id=_BUILDER_DISPATCH_ID)
        assert purge.is_poisoned_harness_lane_record(codex) is False

    def test_the_report_condition_requires_no_verdict_and_a_builder_identity(self):
        record = _clean_record()
        record["dispatch_id"] = ""  # first condition cannot fire
        record["report_path"] = "/tmp/does-not-matter.md"
        # A report that carries the builder's dispatch-id but no gate verdict
        # block is the builder's own report, read back as the gate's verdict.
        assert purge.has_verdict_block("```json\n{\"verdict\": \"pass\"}\n```\n") is True
        assert purge.has_verdict_block("the builder's summary, no verdict here") is False
        assert purge.carries_builder_identity(
            f"Dispatch-ID: {_BUILDER_DISPATCH_ID}\n\n## Summary\n...\n"
        ) is True
        assert purge.carries_builder_identity("just a plain summary") is False


# ---------------------------------------------------------------------------
# 2. (d) The sweep removes a provably poisoned record and leaves an audit line
# ---------------------------------------------------------------------------


class TestSweepRemovesProvablyPoisoned:

    def _state_dir(self, tmp_path):
        state_dir = tmp_path / "state"
        results_dir = state_dir / "review_gates" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        return state_dir, results_dir

    def test_removes_the_poisoned_record_and_leaves_an_audit_line(self, tmp_path):
        state_dir, results_dir = self._state_dir(tmp_path)
        entry = results_dir / "pr-1840-kimi_gate.json"
        entry.write_text(json.dumps(_poisoned_record()), encoding="utf-8")

        outcome = purge.sweep_poisoned(state_dir, write=True)

        assert outcome["action"] == "removed"
        assert outcome["removed"] == [entry.name]
        assert not entry.exists(), "the poisoned record must be gone"

        audit_path = state_dir / "governance_audit.ndjson"
        assert audit_path.exists(), "a removal must leave a governance audit line behind"
        line = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert line["check_name"] == "gate_dispatch_identity_purge"
        assert _BUILDER_DISPATCH_ID in line["message"]

    def test_dry_run_is_the_default_and_deletes_nothing(self, tmp_path):
        state_dir, results_dir = self._state_dir(tmp_path)
        entry = results_dir / "pr-1840-kimi_gate.json"
        entry.write_text(json.dumps(_poisoned_record()), encoding="utf-8")

        outcome = purge.sweep_poisoned(state_dir, write=False)

        assert outcome["action"] == "would_remove"
        assert entry.exists(), "a dry run must never delete"
        assert not (state_dir / "governance_audit.ndjson").exists()

    def test_an_unreadable_record_is_never_deleted(self, tmp_path):
        state_dir, results_dir = self._state_dir(tmp_path)
        torn = results_dir / "pr-1840-kimi_gate.json"
        torn.write_text('{"gate": "kimi_gate", "dispatch_id": "20260', encoding="utf-8")
        poisoned = results_dir / "pr-1841-glm_gate.json"
        poisoned.write_text(json.dumps(_poisoned_record(gate="glm_gate")), encoding="utf-8")

        outcome = purge.sweep_poisoned(state_dir, write=True)

        assert outcome["removed"] == [poisoned.name]
        assert torn.exists(), "an unparseable record must never be deleted on a guess"
        assert outcome["unreadable"] == [torn.name]


# ---------------------------------------------------------------------------
# 3. (c) The sweep refuses when the misstand is not provable
# ---------------------------------------------------------------------------


class TestSweepRefusesWhenNotProvable:

    def test_exit_2_and_no_write_when_nothing_is_provably_poisoned(self, tmp_path):
        state_dir = tmp_path / "state"
        results_dir = state_dir / "review_gates" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        entry = results_dir / "pr-1840-kimi_gate.json"
        entry.write_text(json.dumps(_clean_record()), encoding="utf-8")

        rc = purge.main(["--state-dir", str(state_dir), "--write"])

        assert rc == 2
        assert entry.exists(), "refusal must leave the record untouched"
        assert not (state_dir / "governance_audit.ndjson").exists()

    def test_a_gate_eigen_record_is_refused_directly(self, tmp_path):
        state_dir, results_dir = TestSweepRemovesProvablyPoisoned()._state_dir(tmp_path)
        entry = results_dir / "pr-1840-kimi_gate.json"
        entry.write_text(json.dumps(_clean_record()), encoding="utf-8")

        with pytest.raises(purge.PurgeRefused):
            purge.sweep_poisoned(state_dir, write=True)
        assert entry.exists()
