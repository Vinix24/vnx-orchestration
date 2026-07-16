"""Tests for scripts/lib/report_contract_scope.py.

Covers:
  - classify_non_report_dispatch(): panel/bench/smoke dispatch_id prefixes,
    phantom_guard-style role/task_class/read_only exemptions, and the
    negative case (a real build-worker dispatch is NOT exempt).
  - is_stale_contract_invalid(): default/overridden window, missing/
    unparseable timestamps (fail-open), and boundary behaviour.
  - truthy(): frontmatter/body-field string coercion.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from report_contract_scope import (  # noqa: E402
    classify_non_report_dispatch,
    contract_invalid_window_days,
    is_report_producing,
    is_stale_contract_invalid,
    truthy,
)


# ---------------------------------------------------------------------------
# classify_non_report_dispatch — exempt classes
# ---------------------------------------------------------------------------

class TestClassifyExemptClasses:
    def test_panel_seat_prefix_exempt(self):
        assert classify_non_report_dispatch(
            dispatch_id="panel-architecture-diverge-1-abc123"
        ) == "panel_seat"

    def test_panel_seat_case_insensitive(self):
        assert classify_non_report_dispatch(dispatch_id="PANEL-Architecture-verify") == "panel_seat"

    def test_bench_prefix_exempt(self):
        assert classify_non_report_dispatch(dispatch_id="bench-model-x-task-y-20260716") == "benchmark"

    def test_smoke_prefix_exempt(self):
        assert classify_non_report_dispatch(dispatch_id="smoke-skill-injection-check") == "benchmark"

    def test_review_role_exempt(self):
        assert classify_non_report_dispatch(
            dispatch_id="20260716-some-review", role="code-reviewer"
        ) == "review_role"

    def test_review_role_case_insensitive_and_whitespace(self):
        assert classify_non_report_dispatch(role="  Plan-Reviewer  ") == "review_role"

    def test_research_structured_task_class_exempt(self):
        assert classify_non_report_dispatch(task_class="research_structured") == "research_structured"

    def test_other_review_task_class_exempt(self):
        assert classify_non_report_dispatch(task_class="02_code_review") == "research_structured"

    def test_read_only_flag_exempt(self):
        assert classify_non_report_dispatch(read_only=True) == "read_only"

    def test_read_only_wins_over_absent_role(self):
        assert classify_non_report_dispatch(dispatch_id="20260716-x", read_only=True) == "read_only"


# ---------------------------------------------------------------------------
# classify_non_report_dispatch — the negative case (no over-exemption)
# ---------------------------------------------------------------------------

class TestClassifyRealBuildWorkerNotExempt:
    def test_plain_dispatch_id_not_exempt(self):
        assert classify_non_report_dispatch(dispatch_id="20260716-report-contract-scope") is None

    def test_backend_developer_role_not_exempt(self):
        assert classify_non_report_dispatch(
            dispatch_id="20260716-fix-something", role="backend-developer"
        ) is None

    def test_no_fields_at_all_not_exempt(self):
        assert classify_non_report_dispatch() is None

    def test_task_class_that_looks_like_coding_not_exempt(self):
        assert classify_non_report_dispatch(task_class="01_code_generation") is None

    def test_dispatch_id_containing_but_not_prefixed_with_panel_not_exempt(self):
        # "panel" appears mid-string, not as a prefix — must NOT be exempted.
        assert classify_non_report_dispatch(dispatch_id="20260716-review-panel-followup") is None

    def test_is_report_producing_true_for_real_worker(self):
        assert is_report_producing(dispatch_id="20260716-real-work", role="backend-developer") is True

    def test_is_report_producing_false_for_panel_seat(self):
        assert is_report_producing(dispatch_id="panel-arch-synth") is False


# ---------------------------------------------------------------------------
# truthy()
# ---------------------------------------------------------------------------

class TestTruthy:
    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "Yes"])
    def test_truthy_strings(self, value):
        assert truthy(value) is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "", None, "bananas"])
    def test_falsy_values(self, value):
        assert truthy(value) is False

    def test_bool_passthrough(self):
        assert truthy(True) is True
        assert truthy(False) is False


# ---------------------------------------------------------------------------
# is_stale_contract_invalid()
# ---------------------------------------------------------------------------

class TestIsStaleContractInvalid:
    def _iso(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_default_window_is_14_days(self):
        assert contract_invalid_window_days() == 14

    def test_fresh_timestamp_not_stale(self):
        now = datetime.now(tz=timezone.utc)
        ts = self._iso(now - timedelta(days=1))
        assert is_stale_contract_invalid(ts, now=now) is False

    def test_old_timestamp_is_stale(self):
        now = datetime.now(tz=timezone.utc)
        ts = self._iso(now - timedelta(days=26))
        assert is_stale_contract_invalid(ts, now=now) is True

    def test_boundary_just_inside_window_not_stale(self):
        now = datetime.now(tz=timezone.utc)
        ts = self._iso(now - timedelta(days=13))
        assert is_stale_contract_invalid(ts, now=now) is False

    def test_boundary_just_outside_window_is_stale(self):
        now = datetime.now(tz=timezone.utc)
        ts = self._iso(now - timedelta(days=15))
        assert is_stale_contract_invalid(ts, now=now) is True

    def test_missing_timestamp_fails_open_not_stale(self):
        assert is_stale_contract_invalid(None) is False
        assert is_stale_contract_invalid("") is False

    def test_unparseable_timestamp_fails_open_not_stale(self):
        assert is_stale_contract_invalid("not-a-timestamp") is False

    def test_explicit_window_days_override(self):
        now = datetime.now(tz=timezone.utc)
        ts = self._iso(now - timedelta(days=5))
        assert is_stale_contract_invalid(ts, window_days=3, now=now) is True
        assert is_stale_contract_invalid(ts, window_days=7, now=now) is False

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("VNX_CONTRACT_INVALID_WINDOW_DAYS", "3")
        assert contract_invalid_window_days() == 3
        now = datetime.now(tz=timezone.utc)
        ts = self._iso(now - timedelta(days=5))
        assert is_stale_contract_invalid(ts, now=now) is True

    def test_env_override_invalid_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("VNX_CONTRACT_INVALID_WINDOW_DAYS", "not-a-number")
        assert contract_invalid_window_days() == 14


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
