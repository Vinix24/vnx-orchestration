#!/usr/bin/env python3
"""Tests for scripts/lib/stop_conditions.py (T0 autonomous-chain halts).

Covers the tri-state contract (TRIGGERED / CLEAR / UNMEASURABLE — UNMEASURABLE
is a third branch, never a stand-in value of either other state) for all four
measurable checks:

  - check_main_ci_red               (E1)
  - check_gh_auth_dead               (E4)
  - check_provider_exhausted         (kimi-style structural exhaustion)
  - check_repeated_gate_failure_cause (E6)

plus run_all_checks()/write_halt_file() orchestration. gh is always mocked —
no network, no dependency on this machine's actual GitHub auth state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import stop_conditions as sc  # noqa: E402
from stop_conditions import (  # noqa: E402
    CheckStatus,
    StateDirUnresolvable,
    StopConditionResult,
    check_gh_auth_dead,
    check_main_ci_red,
    check_provider_exhausted,
    check_repeated_gate_failure_cause,
    run_all_checks,
    write_halt_file,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _proc(returncode=0, stdout="", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _run_list_output(conclusion, status="completed", head_sha="a" * 40, created_at="2026-09-04T08:00:00Z"):
    runs = [{"conclusion": conclusion, "status": status, "headSha": head_sha, "createdAt": created_at, "databaseId": 1}]
    return _proc(0, json.dumps(runs), "")


def _write_receipt_lines(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _attempt(provider, status, failure_class=None, dispatch_id="d1", ts="2026-09-04T00:00:00Z", event_type="task_complete"):
    return {
        "event_type": event_type,
        "provider": provider,
        "status": status,
        "failure_class": failure_class,
        "dispatch_id": dispatch_id,
        "timestamp": ts,
    }


def _kimi_403_failure_reason() -> str:
    # The exact live shape measured on the ledger 2026-09-03/04 (grounds the
    # exhaustion check against a case known to exist, not a fabricated one).
    return (
        "kimi-cli: [quota_or_auth] provider=kimi reason=quota_or_auth "
        "msg='Expecting value: line 1 column 1 (char 0)' "
        "raw='Error code: 403 - {\\'error\\': {\\'message\\': \"You've reached "
        "your weekly (7-day) usage limit"
    )


def _gate_result(pr_number, gate, *, status, reason=None, blocking_findings=None, recorded_at="2026-09-04T00:00:00Z"):
    d = {"pr_number": pr_number, "gate": gate, "status": status, "recorded_at": recorded_at}
    if reason is not None:
        d["reason"] = reason
    if blocking_findings is not None:
        d["blocking_findings"] = blocking_findings
    return d


# ── check_main_ci_red (E1) ──────────────────────────────────────────────


class TestMainCiRed:
    def test_gh_missing_is_unmeasurable(self, tmp_path):
        with patch("stop_conditions.shutil.which", return_value=None):
            result = check_main_ci_red(tmp_path)
        assert result.status == CheckStatus.UNMEASURABLE

    def test_success_conclusion_is_clear(self, tmp_path):
        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", return_value=_run_list_output("success")):
            result = check_main_ci_red(tmp_path)
        assert result.status == CheckStatus.CLEAR

    def test_failure_conclusion_is_triggered(self, tmp_path):
        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", return_value=_run_list_output("failure")):
            result = check_main_ci_red(tmp_path)
        assert result.status == CheckStatus.TRIGGERED
        assert "failure" in result.message

    def test_empty_run_list_is_unmeasurable(self, tmp_path):
        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", return_value=_proc(0, "[]", "")):
            result = check_main_ci_red(tmp_path)
        assert result.status == CheckStatus.UNMEASURABLE

    def test_in_progress_run_is_unmeasurable_not_clear(self, tmp_path):
        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", return_value=_run_list_output(None, status="in_progress")):
            result = check_main_ci_red(tmp_path)
        assert result.status == CheckStatus.UNMEASURABLE

    def test_gh_run_list_nonzero_exit_is_unmeasurable(self, tmp_path):
        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", return_value=_proc(1, "", "boom")):
            result = check_main_ci_red(tmp_path)
        assert result.status == CheckStatus.UNMEASURABLE

    def test_malformed_json_is_unmeasurable(self, tmp_path):
        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", return_value=_proc(0, "not json", "")):
            result = check_main_ci_red(tmp_path)
        assert result.status == CheckStatus.UNMEASURABLE

    def test_timeout_is_unmeasurable(self, tmp_path):
        import subprocess as _sp

        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", side_effect=_sp.TimeoutExpired(cmd="gh", timeout=15)):
            result = check_main_ci_red(tmp_path)
        assert result.status == CheckStatus.UNMEASURABLE


# ── check_gh_auth_dead (E4) ──────────────────────────────────────────────


class TestGhAuthDead:
    def test_gh_missing_is_unmeasurable(self):
        with patch("stop_conditions.shutil.which", return_value=None):
            result = check_gh_auth_dead()
        assert result.status == CheckStatus.UNMEASURABLE

    def test_auth_status_nonzero_is_triggered(self):
        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", return_value=_proc(1, "", "not logged in")):
            result = check_gh_auth_dead()
        assert result.status == CheckStatus.TRIGGERED

    def test_auth_timeout_is_unmeasurable(self):
        import subprocess as _sp

        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", side_effect=_sp.TimeoutExpired(cmd="gh", timeout=10)):
            result = check_gh_auth_dead()
        assert result.status == CheckStatus.UNMEASURABLE

    def test_auth_ok_quota_positive_is_clear(self):
        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", side_effect=[_proc(0, "", ""), _proc(0, "4999", "")]):
            result = check_gh_auth_dead()
        assert result.status == CheckStatus.CLEAR
        assert result.evidence["remaining"] == 4999

    def test_auth_ok_quota_zero_is_triggered(self):
        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", side_effect=[_proc(0, "", ""), _proc(0, "0", "")]):
            result = check_gh_auth_dead()
        assert result.status == CheckStatus.TRIGGERED

    def test_auth_ok_quota_check_fails_is_unmeasurable(self):
        with patch("stop_conditions.shutil.which", return_value="/usr/bin/gh"), \
             patch("stop_conditions.subprocess.run", side_effect=[_proc(0, "", ""), _proc(1, "", "boom")]):
            result = check_gh_auth_dead()
        assert result.status == CheckStatus.UNMEASURABLE
        assert result.evidence.get("auth_ok") is True


# ── check_provider_exhausted ─────────────────────────────────────────────


class TestProviderExhausted:
    def test_missing_receipts_file_is_unmeasurable(self, tmp_path):
        result = check_provider_exhausted(tmp_path / "nope.ndjson")
        assert result.status == CheckStatus.UNMEASURABLE

    def test_kimi_403_streak_triggers(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        records = [_attempt("kimi", "success")] + [
            _attempt("kimi", "failure", failure_class="auth_rejected", dispatch_id=f"d{i}")
            for i in range(3)
        ]
        _write_receipt_lines(receipts, records)
        result = check_provider_exhausted(receipts, threshold=3)
        assert result.status == CheckStatus.TRIGGERED
        assert "kimi" in result.message or "kimi" in json.dumps(result.evidence)

    def test_success_breaks_the_streak(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        records = [
            _attempt("kimi", "failure", failure_class="auth_rejected"),
            _attempt("kimi", "success"),
            _attempt("kimi", "failure", failure_class="auth_rejected"),
        ]
        _write_receipt_lines(receipts, records)
        result = check_provider_exhausted(receipts, threshold=3)
        assert result.status == CheckStatus.CLEAR

    def test_non_exhaustion_failures_do_not_trigger(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        records = [_attempt("glm-harness", "failure", failure_class="timeout") for _ in range(3)]
        _write_receipt_lines(receipts, records)
        result = check_provider_exhausted(receipts, threshold=3)
        assert result.status == CheckStatus.CLEAR

    def test_insufficient_attempts_is_unmeasurable(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        records = [_attempt("kimi", "failure", failure_class="auth_rejected")]
        _write_receipt_lines(receipts, records)
        result = check_provider_exhausted(receipts, threshold=3)
        assert result.status == CheckStatus.UNMEASURABLE

    def test_ambiguous_status_excluded_from_timeline(self, tmp_path):
        # "timeout"/"unknown"/"no_signal" statuses must not silently count as
        # either a success (masking exhaustion) or a matching failure.
        receipts = tmp_path / "t0_receipts.ndjson"
        records = (
            [_attempt("kimi", "unknown", failure_class=None)] * 5
            + [_attempt("kimi", "failure", failure_class="auth_rejected") for _ in range(3)]
        )
        _write_receipt_lines(receipts, records)
        result = check_provider_exhausted(receipts, threshold=3)
        assert result.status == CheckStatus.TRIGGERED

    def test_report_contract_invalid_event_type_excluded(self, tmp_path):
        # report_contract_invalid carries a top-level provider field too, but
        # is a report-format concern, not a provider-call outcome.
        receipts = tmp_path / "t0_receipts.ndjson"
        records = [
            {"event_type": "report_contract_invalid", "provider": "kimi", "status": "failure", "failure_class": "auth_rejected", "timestamp": "t"}
            for _ in range(5)
        ]
        _write_receipt_lines(receipts, records)
        result = check_provider_exhausted(receipts, threshold=3)
        assert result.status == CheckStatus.UNMEASURABLE

    def test_kimi_403_realistic_failure_reason_fixture(self, tmp_path):
        # Grounds the check against the exact live shape (nul-is-eerst-een-
        # meetfout discipline): the receipt ledger's real kimi 403 records
        # carry failure_class="auth_rejected" with this failure_reason text.
        receipts = tmp_path / "t0_receipts.ndjson"
        records = [
            {
                "event_type": "task_complete",
                "provider": "kimi",
                "status": "failure",
                "failure_class": "auth_rejected",
                "failure_reason": _kimi_403_failure_reason(),
                "dispatch_id": f"d{i}",
                "timestamp": "2026-09-04T00:00:00Z",
            }
            for i in range(3)
        ]
        _write_receipt_lines(receipts, records)
        result = check_provider_exhausted(receipts, threshold=3)
        assert result.status == CheckStatus.TRIGGERED


# ── check_repeated_gate_failure_cause (E6) ──────────────────────────────


class TestRepeatedGateFailureCause:
    def test_missing_results_dir_is_unmeasurable(self, tmp_path):
        result = check_repeated_gate_failure_cause(tmp_path / "nope")
        assert result.status == CheckStatus.UNMEASURABLE

    def test_same_reason_three_times_triggers(self, tmp_path):
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        for i, pr in enumerate([101, 102, 103]):
            d = _gate_result(pr, "codex_gate", status="unavailable", reason="provider_not_installed", recorded_at=f"2026-09-0{i+1}T00:00:00Z")
            (results_dir / f"pr-{pr}-codex_gate.json").write_text(json.dumps(d))
        result = check_repeated_gate_failure_cause(results_dir, n=3)
        assert result.status == CheckStatus.TRIGGERED

    def test_different_reasons_does_not_trigger(self, tmp_path):
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        reasons = ["provider_not_installed", "exit_nonzero", "dispatch_error"]
        for i, (pr, reason) in enumerate(zip([201, 202, 203], reasons)):
            d = _gate_result(pr, "codex_gate", status="failed", reason=reason, recorded_at=f"2026-09-0{i+1}T00:00:00Z")
            (results_dir / f"pr-{pr}-codex_gate.json").write_text(json.dumps(d))
        result = check_repeated_gate_failure_cause(results_dir, n=3)
        assert result.status == CheckStatus.CLEAR

    def test_clean_pass_breaks_the_streak(self, tmp_path):
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        d1 = _gate_result(301, "codex_gate", status="unavailable", reason="provider_not_installed", recorded_at="2026-09-01T00:00:00Z")
        d2 = _gate_result(302, "codex_gate", status="pass", recorded_at="2026-09-02T00:00:00Z")
        d3 = _gate_result(303, "codex_gate", status="unavailable", reason="provider_not_installed", recorded_at="2026-09-03T00:00:00Z")
        for pr, d in [(301, d1), (302, d2), (303, d3)]:
            (results_dir / f"pr-{pr}-codex_gate.json").write_text(json.dumps(d))
        result = check_repeated_gate_failure_cause(results_dir, n=3)
        assert result.status == CheckStatus.CLEAR

    def test_blocking_findings_repeated_triggers(self, tmp_path):
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        for i, pr in enumerate([401, 402, 403]):
            d = _gate_result(
                pr, "kimi_gate", status="completed",
                blocking_findings=[{"severity": "blocking", "message": "x"}],
                recorded_at=f"2026-09-0{i+1}T00:00:00Z",
            )
            (results_dir / f"pr-{pr}-kimi_gate.json").write_text(json.dumps(d))
        result = check_repeated_gate_failure_cause(results_dir, n=3)
        assert result.status == CheckStatus.TRIGGERED

    def test_insufficient_dated_records_is_unmeasurable(self, tmp_path):
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        d = _gate_result(501, "codex_gate", status="unavailable", reason="provider_not_installed")
        (results_dir / "pr-501-codex_gate.json").write_text(json.dumps(d))
        result = check_repeated_gate_failure_cause(results_dir, n=3)
        assert result.status == CheckStatus.UNMEASURABLE

    def test_missing_recorded_at_excluded_from_ordering(self, tmp_path):
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        # Two dated + one undated (same cause) — undated must not count
        # toward the n=3 window, so this reads as insufficient data, not
        # a triggered 3-streak.
        d1 = _gate_result(601, "codex_gate", status="unavailable", reason="provider_not_installed", recorded_at="2026-09-01T00:00:00Z")
        d2 = _gate_result(602, "codex_gate", status="unavailable", reason="provider_not_installed", recorded_at="2026-09-02T00:00:00Z")
        d3 = {"pr_number": 603, "gate": "codex_gate", "status": "unavailable", "reason": "provider_not_installed"}  # no recorded_at
        for pr, d in [(601, d1), (602, d2), (603, d3)]:
            (results_dir / f"pr-{pr}-codex_gate.json").write_text(json.dumps(d))
        result = check_repeated_gate_failure_cause(results_dir, n=3)
        assert result.status == CheckStatus.UNMEASURABLE

    def test_malformed_json_file_skipped(self, tmp_path):
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "pr-999-codex_gate.json").write_text("not json{")
        for i, pr in enumerate([701, 702, 703]):
            d = _gate_result(pr, "codex_gate", status="unavailable", reason="provider_not_installed", recorded_at=f"2026-09-0{i+1}T00:00:00Z")
            (results_dir / f"pr-{pr}-codex_gate.json").write_text(json.dumps(d))
        result = check_repeated_gate_failure_cause(results_dir, n=3)
        assert result.status == CheckStatus.TRIGGERED

    def test_different_gates_scored_independently(self, tmp_path):
        # codex_gate stays clean; kimi_gate has the 3x repeat — overall must
        # still trigger (any-sub-triggered wins), and the untriggered gate
        # must not mask it.
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        for i, pr in enumerate([801, 802, 803]):
            d = _gate_result(pr, "codex_gate", status="pass", recorded_at=f"2026-09-0{i+1}T00:00:00Z")
            (results_dir / f"pr-{pr}-codex_gate.json").write_text(json.dumps(d))
        for i, pr in enumerate([811, 812, 813]):
            d = _gate_result(pr, "kimi_gate", status="unavailable", reason="provider_not_installed", recorded_at=f"2026-09-0{i+1}T00:00:00Z")
            (results_dir / f"pr-{pr}-kimi_gate.json").write_text(json.dumps(d))
        result = check_repeated_gate_failure_cause(results_dir, n=3)
        assert result.status == CheckStatus.TRIGGERED


# ── tri-state discipline (cross-cutting) ────────────────────────────────


class TestTriState:
    def test_check_status_has_exactly_three_members(self):
        assert {m.value for m in CheckStatus} == {"triggered", "clear", "unmeasurable"}

    def test_combine_all_unmeasurable_stays_unmeasurable(self):
        subs = [
            StopConditionResult("x:a", CheckStatus.UNMEASURABLE, "no data"),
            StopConditionResult("x:b", CheckStatus.UNMEASURABLE, "no data"),
        ]
        combined = sc._combine("x", subs, sub_key="per_thing")
        assert combined.status == CheckStatus.UNMEASURABLE

    def test_combine_empty_is_unmeasurable_not_clear(self):
        combined = sc._combine("x", [], sub_key="per_thing")
        assert combined.status == CheckStatus.UNMEASURABLE

    def test_combine_one_triggered_wins_over_clear(self):
        subs = [
            StopConditionResult("x:a", CheckStatus.CLEAR, "fine"),
            StopConditionResult("x:b", CheckStatus.TRIGGERED, "bad"),
        ]
        combined = sc._combine("x", subs, sub_key="per_thing")
        assert combined.status == CheckStatus.TRIGGERED

    def test_to_dict_status_is_json_safe_string(self):
        r = StopConditionResult("id", CheckStatus.CLEAR, "msg")
        d = r.to_dict()
        json.dumps(d)  # must not raise
        assert d["status"] == "clear"


# ── orchestration ────────────────────────────────────────────────────────


class TestRunAllChecksAndHalt:
    def test_halt_written_when_any_triggered(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        triggered = StopConditionResult("gh_auth_dead", CheckStatus.TRIGGERED, "dead")
        clear1 = StopConditionResult("main_ci_red", CheckStatus.CLEAR, "ok")
        clear2 = StopConditionResult("provider_exhausted", CheckStatus.UNMEASURABLE, "no data")
        clear3 = StopConditionResult("repeated_gate_failure_cause", CheckStatus.CLEAR, "ok")
        with patch("stop_conditions.check_main_ci_red", return_value=clear1), \
             patch("stop_conditions.check_gh_auth_dead", return_value=triggered), \
             patch("stop_conditions.check_provider_exhausted", return_value=clear2), \
             patch("stop_conditions.check_repeated_gate_failure_cause", return_value=clear3):
            results = run_all_checks(state_dir=state_dir, project_root=tmp_path)
        assert any(r.status == CheckStatus.TRIGGERED for r in results)
        halt_path = state_dir / "halt.json"
        assert halt_path.is_file()
        payload = json.loads(halt_path.read_text())
        assert len(payload["triggered"]) == 1
        assert payload["triggered"][0]["check_id"] == "gh_auth_dead"
        assert len(payload["all_checks"]) == 4

    def test_no_halt_written_when_nothing_triggered(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        clear = StopConditionResult("x", CheckStatus.CLEAR, "ok")
        with patch("stop_conditions.check_main_ci_red", return_value=clear), \
             patch("stop_conditions.check_gh_auth_dead", return_value=clear), \
             patch("stop_conditions.check_provider_exhausted", return_value=clear), \
             patch("stop_conditions.check_repeated_gate_failure_cause", return_value=clear):
            run_all_checks(state_dir=state_dir, project_root=tmp_path)
        assert not (state_dir / "halt.json").is_file()

    def test_write_halt_file_is_atomic_no_partial_on_disk(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        results = [StopConditionResult("x", CheckStatus.TRIGGERED, "bad")]
        halt_path = write_halt_file(state_dir, results)
        assert halt_path == state_dir / "halt.json"
        # no leftover tmp files
        assert not list(state_dir.glob("*.tmp"))
        payload = json.loads(halt_path.read_text())
        assert payload["triggered"][0]["check_id"] == "x"


# ── central-mode path gate (OI: un-grandfathered violation, PR #1754 review) ─
#
# stop_conditions.py must never fall back to a __file__-anchored repo-local
# .vnx-data guess: scripts/check_no_file_derived_data_paths.py forbids this
# exact bug class (a central install would resolve the shared fabric
# checkout's own .vnx-data instead of the project's ~/.vnx-data/<project_id>,
# forking state). These tests pin that both directly (the gate's own scanner
# against this module's source) and behaviorally (the tri-state contract
# a caller sees when state-dir truly cannot be resolved).


class TestCentralModePathGate:
    def test_module_source_has_no_file_derived_data_path_violations(self):
        import importlib

        scripts_dir = str(Path(sc.__file__).resolve().parent.parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        gate = importlib.import_module("check_no_file_derived_data_paths")
        source = Path(sc.__file__).read_text(encoding="utf-8")
        violations = gate.check_source(source)
        assert violations == [], f"central-mode path gate violation(s) in stop_conditions.py: {violations}"

    def test_resolve_state_dir_raises_never_a_repo_local_fallback(self, monkeypatch):
        monkeypatch.delenv("VNX_STATE_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        with patch("vnx_paths.resolve_paths", side_effect=RuntimeError("boom")):
            with pytest.raises(StateDirUnresolvable):
                sc._resolve_state_dir()

    def test_resolve_state_dir_honors_direct_env_override_not_canonical(self, monkeypatch, tmp_path):
        # Direct VNX_STATE_DIR override still works (not __file__-anchored) —
        # only the removed repo-local __file__ fallback is forbidden.
        monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
        with patch("vnx_paths.resolve_paths", side_effect=RuntimeError("boom")):
            result = sc._resolve_state_dir()
        assert result == tmp_path

    def test_provider_exhausted_unmeasurable_when_state_dir_unresolvable(self):
        with patch("stop_conditions._resolve_state_dir", side_effect=StateDirUnresolvable("boom")):
            result = check_provider_exhausted()
        assert result.status == CheckStatus.UNMEASURABLE

    def test_repeated_gate_failure_cause_unmeasurable_when_state_dir_unresolvable(self):
        with patch("stop_conditions._resolve_state_dir", side_effect=StateDirUnresolvable("boom")):
            result = check_repeated_gate_failure_cause()
        assert result.status == CheckStatus.UNMEASURABLE

    def test_run_all_checks_survives_unresolvable_state_dir_no_halt_written(self, tmp_path):
        clear = StopConditionResult("x", CheckStatus.CLEAR, "ok")
        with patch("stop_conditions._resolve_state_dir", side_effect=StateDirUnresolvable("boom")), \
             patch("stop_conditions.check_main_ci_red", return_value=clear), \
             patch("stop_conditions.check_gh_auth_dead", return_value=clear):
            results = run_all_checks(state_dir=None, project_root=tmp_path)
        by_id = {r.check_id: r for r in results}
        assert by_id["provider_exhausted"].status == CheckStatus.UNMEASURABLE
        assert by_id["repeated_gate_failure_cause"].status == CheckStatus.UNMEASURABLE
        # nothing crashed and no halt.json materialized anywhere under tmp_path
        assert not list(tmp_path.rglob("halt.json"))

    def test_default_project_root_calls_resolver_with_no_file_anchor(self):
        with patch("stop_conditions.resolve_project_root", return_value=Path("/x")) as mock_resolver:
            sc._default_project_root()
        mock_resolver.assert_called_once_with()


# ── completion-outcome vocabulary (OI: hand-typed duplicate of receipt_verdict) ─


class TestCompletionOutcomeVocabulary:
    def test_vocab_is_imported_not_redefined(self):
        import receipt_verdict

        assert sc.SUCCESS_STATUSES is receipt_verdict.SUCCESS_STATUSES
        assert sc.HARD_FAILURE_STATUSES is receipt_verdict.HARD_FAILURE_STATUSES

    def test_contract_invalid_status_counts_as_a_real_failure_attempt(self, tmp_path):
        # "contract_invalid" is in the canonical HARD_FAILURE_STATUSES but was
        # NOT in the old hand-typed {"failed", "failure"} pair — with only the
        # hand-typed pair, only 1 of these 3 records would register as an
        # "attempt" (insufficient data -> UNMEASURABLE). With the canonical
        # vocabulary all 3 register; none are exhaustion-class, so the verdict
        # is a measured CLEAR, not a data-starved UNMEASURABLE.
        receipts = tmp_path / "t0_receipts.ndjson"
        records = [
            _attempt("glm-harness", "contract_invalid", failure_class=None),
            _attempt("glm-harness", "contract_invalid", failure_class=None),
            _attempt("glm-harness", "failure", failure_class="auth_rejected"),
        ]
        _write_receipt_lines(receipts, records)
        result = check_provider_exhausted(receipts, threshold=3)
        assert result.status == CheckStatus.CLEAR

    def test_completed_status_recognized_as_success_breaks_streak(self, tmp_path):
        # "completed" (distinct from "complete") is in the canonical
        # SUCCESS_STATUSES but was NOT in the old hand-typed
        # {"success", "done", "complete"} triple.
        receipts = tmp_path / "t0_receipts.ndjson"
        records = [
            _attempt("kimi", "failure", failure_class="auth_rejected"),
            _attempt("kimi", "completed"),
            _attempt("kimi", "failure", failure_class="auth_rejected"),
        ]
        _write_receipt_lines(receipts, records)
        result = check_provider_exhausted(receipts, threshold=3)
        assert result.status == CheckStatus.CLEAR
