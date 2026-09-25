"""tests/test_parked_register.py — absence-is-loud, punt 2: what the operator
parked is one register with a reason, not a name that only the beacon reader
knows.

The intelligence layer was PARKED on 2026-09-09 (golf C / #1832). Three readers
reported the consequence as a fault: session_state_freshness read
t0_recommendations.json as STALE (98 days) and put SessionStart on BLOCKED, and
build_t0_state._measure_launchd_liveness read three never-installed plists as
not_loaded and pinned launchd_liveness.overall on fail. ``beacon_register`` is
the one module that says what is parked and why; these tests pin its content and
that every entry names something a reader can actually reach.
"""
from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib"
LAUNCHD_DIR = REPO / "scripts" / "launchd"
sys.path.insert(0, str(LIB))

import beacon_register  # noqa: E402
from session_state_freshness import ARTIFACTS  # noqa: E402


def _template_labels() -> "set[str]":
    labels = set()
    for path in LAUNCHD_DIR.glob("*.plist"):
        with path.open("rb") as fh:
            labels.add(plistlib.load(fh)["Label"])
    return labels


class TestParkedComponents:
    def test_the_two_original_writers_are_still_parked(self):
        assert {"learning_loop", "intelligence_daemon"} <= beacon_register.PARKED_COMPONENTS

    def test_the_self_learning_loop_subsystem_beacon_is_parked_with_them(self):
        """Operator decision 2026-09-24: the same intelligence layer, so the same parking."""
        assert "intelligence-self-learning-loop" in beacon_register.PARKED_COMPONENTS

    def test_exactly_the_three_components_the_operator_parked(self):
        assert beacon_register.PARKED_COMPONENTS == frozenset({
            "learning_loop", "intelligence_daemon", "intelligence-self-learning-loop",
        })

    def test_parked_component_names_reads_all_three_sorted(self):
        assert beacon_register.parked_component_names() == (
            "intelligence-self-learning-loop", "intelligence_daemon", "learning_loop",
        )

    def test_a_component_nobody_parked_is_not_in_the_set(self):
        assert "t0_state_builder" not in beacon_register.PARKED_COMPONENTS
        assert "governance-enforcement-stack" not in beacon_register.PARKED_COMPONENTS


class TestParkedArtifacts:
    def test_t0_recommendations_is_parked_with_the_producer_named(self):
        reason = beacon_register.parked_artifact_reason("t0_recommendations")
        assert reason is not None
        assert "generate_t0_recommendations.py" in reason
        assert "nightly intelligence pipeline" in reason
        assert "2026-09-09" in reason

    @pytest.mark.parametrize("name", ["t0_state", "open_items", "terminal_state", "dashboard_status"])
    def test_every_other_artifact_is_not_parked(self, name):
        assert beacon_register.parked_artifact_reason(name) is None

    def test_a_parked_artifact_is_one_session_state_freshness_actually_reads(self):
        """An entry naming an artifact nobody reads would park nothing, silently."""
        assert set(beacon_register.PARKED_ARTIFACTS) <= set(ARTIFACTS)


class TestParkedLaunchdJobs:
    PARKED = {
        "com.vnx.nightly-intelligence-pipeline",
        "com.vnx.receipt-classifier-batch",
        "com.vnx.headless-trigger",
    }

    def test_exactly_the_three_jobs_the_operator_parked(self):
        assert set(beacon_register.PARKED_LAUNCHD_JOBS) == self.PARKED

    def test_the_two_intelligence_jobs_carry_the_same_decision_as_the_beacon_components(self):
        for label in ("com.vnx.nightly-intelligence-pipeline", "com.vnx.receipt-classifier-batch"):
            reason = beacon_register.parked_launchd_reason(label)
            assert reason is not None
            assert "2026-09-09" in reason and "#1832" in reason

    def test_headless_trigger_is_an_opt_in_not_the_intelligence_layer(self):
        reason = beacon_register.parked_launchd_reason("com.vnx.headless-trigger")
        assert reason is not None
        assert "opt-in" in reason
        assert "#1832" not in reason

    def test_a_job_that_is_expected_and_not_parked_has_no_reason(self):
        assert beacon_register.parked_launchd_reason("com.vnx.ledger-health") is None
        assert beacon_register.parked_launchd_reason("com.vnx.gate-obligation-runner.vnx-dev") is None

    def test_every_parked_job_is_a_label_a_real_template_declares(self):
        """A label no template declares is never expected, so parking it is dead text."""
        assert set(beacon_register.PARKED_LAUNCHD_JOBS) <= _template_labels()


class TestEveryEntryHasAReason:
    def test_reasons_are_non_empty_strings(self):
        for mapping in (beacon_register.PARKED_ARTIFACTS, beacon_register.PARKED_LAUNCHD_JOBS):
            for name, reason in mapping.items():
                assert isinstance(reason, str) and reason.strip(), name

    def test_the_registers_are_read_only(self):
        with pytest.raises(TypeError):
            beacon_register.PARKED_ARTIFACTS["dashboard_status"] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            beacon_register.PARKED_LAUNCHD_JOBS["com.vnx.ledger-health"] = "x"  # type: ignore[index]
