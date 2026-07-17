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

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from report_contract_scope import (  # noqa: E402
    classify_non_report_dispatch,
    classify_report_dispatch,
    contract_invalid_effective_timestamp,
    contract_invalid_window_days,
    is_report_producing,
    is_stale_contract_invalid,
    resolve_dispatch_authority,
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


# ---------------------------------------------------------------------------
# resolve_dispatch_authority() — governed-source lookup (codex-gate fix-round,
# Finding 1 BLOCKING: the exemption must never be classified off report-body
# content a worker controls)
# ---------------------------------------------------------------------------

def _write_spec(data_dir: Path, dispatch_id: str, status: str, payload: dict) -> None:
    spec_dir = data_dir / "dispatches" / status / dispatch_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "dispatch-spec.json").write_text(json.dumps(payload), encoding="utf-8")


class TestResolveDispatchAuthority:
    def test_none_dispatch_id_returns_none(self, tmp_path):
        assert resolve_dispatch_authority(None, data_dir=tmp_path) is None

    def test_no_spec_no_register_returns_none(self, tmp_path):
        assert resolve_dispatch_authority("20260716-nothing-here", data_dir=tmp_path) is None

    def test_finds_role_from_spec_in_pending(self, tmp_path):
        did = "20260716-spec-pending"
        _write_spec(tmp_path, did, "pending", {"role": "backend-developer"})
        assert resolve_dispatch_authority(did, data_dir=tmp_path) == {
            "role": "backend-developer", "task_class": None,
        }

    def test_finds_role_from_spec_in_active(self, tmp_path):
        did = "20260716-spec-active"
        _write_spec(tmp_path, did, "active", {"role": "code-reviewer", "task_class": "02_code_review"})
        assert resolve_dispatch_authority(did, data_dir=tmp_path) == {
            "role": "code-reviewer", "task_class": "02_code_review",
        }

    def test_finds_role_from_spec_in_completed(self, tmp_path):
        did = "20260716-spec-completed"
        _write_spec(tmp_path, did, "completed", {"role": "backend-developer"})
        assert resolve_dispatch_authority(did, data_dir=tmp_path) == {
            "role": "backend-developer", "task_class": None,
        }

    def test_data_dir_defaults_to_state_dir_parent(self, tmp_path):
        did = "20260716-derived-data-dir"
        _write_spec(tmp_path, did, "pending", {"role": "backend-developer"})
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        assert resolve_dispatch_authority(did, state_dir=state_dir) == {
            "role": "backend-developer", "task_class": None,
        }

    def test_falls_back_to_register_when_no_spec(self, tmp_path):
        did = "20260716-register-fallback"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "dispatch_register.ndjson").write_text(
            json.dumps({"dispatch_id": did, "extra": {"role": "backend-developer"}}) + "\n",
            encoding="utf-8",
        )
        assert resolve_dispatch_authority(did, state_dir=state_dir) == {
            "role": "backend-developer", "task_class": None,
        }

    def test_falls_back_to_adr005_register_path(self, tmp_path):
        """dispatch_register.register_proposed_track_dispatch() writes to
        <state_dir>/../events/dispatch_register.ndjson, not <state_dir> itself."""
        did = "20260716-adr005-register"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        events_dir = tmp_path / "events"
        events_dir.mkdir(parents=True)
        (events_dir / "dispatch_register.ndjson").write_text(
            json.dumps({"dispatch_id": did, "extra": {"role": "backend-developer"}}) + "\n",
            encoding="utf-8",
        )
        assert resolve_dispatch_authority(did, state_dir=state_dir) == {
            "role": "backend-developer", "task_class": None,
        }

    def test_spec_wins_over_register(self, tmp_path):
        did = "20260716-spec-wins"
        _write_spec(tmp_path, did, "pending", {"role": "backend-developer"})
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "dispatch_register.ndjson").write_text(
            json.dumps({"dispatch_id": did, "extra": {"role": "code-reviewer"}}) + "\n",
            encoding="utf-8",
        )
        assert resolve_dispatch_authority(did, state_dir=state_dir)["role"] == "backend-developer"

    def test_rejects_path_traversal_dispatch_id(self, tmp_path):
        # First char of _ID_RE is alnum-only — "../.." can never match, so this
        # must fall through to "no authoritative record" rather than escape tmp_path.
        assert resolve_dispatch_authority("../../../etc/passwd", data_dir=tmp_path) is None

    def test_malformed_spec_json_does_not_crash(self, tmp_path):
        did = "20260716-malformed-spec"
        spec_dir = tmp_path / "dispatches" / "pending" / did
        spec_dir.mkdir(parents=True)
        (spec_dir / "dispatch-spec.json").write_text("{not valid json", encoding="utf-8")
        assert resolve_dispatch_authority(did, data_dir=tmp_path) is None

    def test_malformed_register_lines_skipped(self, tmp_path):
        did = "20260716-malformed-register-line"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "dispatch_register.ndjson").write_text(
            "not valid json\n"
            + json.dumps({"dispatch_id": did, "extra": {"role": "backend-developer"}}) + "\n",
            encoding="utf-8",
        )
        assert resolve_dispatch_authority(did, state_dir=state_dir) == {
            "role": "backend-developer", "task_class": None,
        }


# ---------------------------------------------------------------------------
# classify_report_dispatch() — the governance-safe wrapper call sites must use
# ---------------------------------------------------------------------------

class TestClassifyReportDispatch:
    def test_authoritative_role_overrides_forged_body_fields(self, tmp_path):
        """T-adv1 core: a spec-backed backend-developer dispatch cannot
        self-exempt via forged report-body role/task_class/read_only."""
        did = "20260716-authoritative-override"
        _write_spec(tmp_path, did, "pending", {"role": "backend-developer"})
        result = classify_report_dispatch(
            did,
            role="code-reviewer",
            task_class="research_structured",
            read_only=True,
            data_dir=tmp_path,
        )
        assert result is None

    def test_dispatch_id_prefix_disabled_when_authority_exists(self, tmp_path):
        """A dispatch_id that happens to start with 'panel-' but HAS a real
        spec (role=backend-developer) must not be exempted by the prefix."""
        did = "panel-but-actually-a-governed-build-worker"
        _write_spec(tmp_path, did, "pending", {"role": "backend-developer"})
        assert classify_report_dispatch(did, data_dir=tmp_path) is None

    def test_no_authority_falls_back_to_dispatch_id_prefix(self, tmp_path):
        """T-adv2 core: a genuinely ungoverned panel seat (no spec, no
        register record) keeps its existing prefix-based exemption."""
        assert classify_report_dispatch("panel-real-seat-1", data_dir=tmp_path) == "panel_seat"

    def test_no_authority_falls_back_to_body_role(self, tmp_path):
        assert classify_report_dispatch(
            "20260716-ungoverned-review", role="code-reviewer", data_dir=tmp_path
        ) == "review_role"

    def test_authoritative_task_class_from_spec_exempts(self, tmp_path):
        did = "20260716-spec-task-class"
        _write_spec(tmp_path, did, "pending", {"role": "reviewer", "task_class": "research_structured"})
        assert classify_report_dispatch(did, data_dir=tmp_path) == "review_role"

    def test_no_state_dir_no_data_dir_behaves_like_pure_function(self):
        """No lookup roots supplied at all -> same as classify_non_report_dispatch."""
        assert classify_report_dispatch("panel-no-roots-given") == "panel_seat"
        assert classify_report_dispatch("20260716-plain", role="backend-developer") is None


# ---------------------------------------------------------------------------
# contract_invalid_effective_timestamp() — Finding 2 (HIGH): windowing must
# key off the processor-stamped ingest time, not the worker-suppliable
# timestamp/recorded_at.
# ---------------------------------------------------------------------------

class TestContractInvalidEffectiveTimestamp:
    def test_prefers_ingested_at(self):
        assert contract_invalid_effective_timestamp(
            {"ingested_at": "A", "timestamp": "B", "recorded_at": "C"}
        ) == "A"

    def test_falls_back_to_timestamp_when_no_ingested_at(self):
        assert contract_invalid_effective_timestamp({"timestamp": "B"}) == "B"

    def test_falls_back_to_recorded_at_when_neither_ingested_at_nor_timestamp(self):
        assert contract_invalid_effective_timestamp({"recorded_at": "C"}) == "C"

    def test_returns_none_when_nothing_present(self):
        assert contract_invalid_effective_timestamp({}) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
