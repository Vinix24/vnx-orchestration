"""test_fase0_rol_in_receipt.py — the dispatch role reaches the receipt.

Fase 0, doorgiftegat van de rol. Four drops, one test class each:

1. envelope_govern must not let an unrelated earlier ledger line (review gate,
   report_parser, converter, pr_enforcement) suppress the lane's own
   role-carrying ReceiptV2; only a real lane completion receipt does, for
   compact and spaced serialisations alike.
2. dispatch_identity.resolve_effective_role reads the dispatch's own spec.
3. report_parser stamps the role through that resolver.
4. The subprocess lane receipt carries the role.

Every test runs against a tmp store (VNX_*_DIR pinned to tmp_path); nothing
touches ~/.vnx-data. ADR-007: a second project store with a colliding
dispatch_id holds a different role that must never leak.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts"))

from append_receipt import append_receipt_payload  # noqa: E402
from dispatch_identity import resolve_effective_role  # noqa: E402
from envelope_govern import _govern  # noqa: E402
from envelope_govern_support import _receipt_exists_for_dispatch  # noqa: E402
from envelope_types import EnvelopeSpec, _AdapterResult  # noqa: E402

DISPATCH_ID = "20261002-fase0-role-test"
SPEC_ROLE = "quality-engineer"
OTHER_ROLE = "security-engineer"
PROJECT = "proj-a"


def _make_store(root: Path) -> Path:
    """Create a tmp store; return its data dir."""
    for sub in ("state", "dispatches/completed", "unified_reports"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _write_spec(data_dir: Path, dispatch_id: str, role, bucket: str = "completed") -> Path:
    spec_dir = data_dir / "dispatches" / bucket / dispatch_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    path = spec_dir / "dispatch-spec.json"
    payload = {"dispatch_id": dispatch_id}
    if role is not None:
        payload["role"] = role
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture()
def store(tmp_path, monkeypatch):
    data_dir = _make_store(tmp_path / "proj-a")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    return data_dir


@pytest.fixture()
def other_store(tmp_path):
    """A second project store, same dispatch_id, different role (ADR-007)."""
    data_dir = _make_store(tmp_path / "proj-b")
    _write_spec(data_dir, DISPATCH_ID, OTHER_ROLE)
    return data_dir


def _ledger(state_dir: Path) -> list:
    path = state_dir / "t0_receipts.ndjson"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _valid_body() -> str:
    return (
        "## Summary\n\nImplemented the feature with full test coverage. All tests pass "
        "and the implementation is complete and correct in every regard.\n\n"
        "## Changes\n\n- Implemented feature X\n\n"
        "## Verification\n\n- pytest passed: 5/5\n\n"
        "## Open Items\n\nNone\n"
    )


# ---------------------------------------------------------------------------
# Drop 1: envelope dedup
# ---------------------------------------------------------------------------


def _envelope_spec(store: Path) -> EnvelopeSpec:
    return EnvelopeSpec(
        dispatch_id=DISPATCH_ID,
        terminal_id="T1",
        provider="claude",
        model="sonnet",
        instruction="do the thing",
        role=SPEC_ROLE,
        pr_id=None,
        state_dir=store / "state",
        data_dir=store,
    )


def _run_govern(spec: EnvelopeSpec):
    report = spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"
    report.write_text(_valid_body(), encoding="utf-8")
    result = _AdapterResult(returncode=0, completion_text=_valid_body(), status="success")
    with patch("envelope_govern._archive_dispatch_events", return_value=("", True)), \
         patch("envelope_govern._clear_dispatch_events"):
        return _govern(
            spec, result,
            start_time=datetime(2026, 10, 2, 12, 0, 0),
            end_time=datetime(2026, 10, 2, 12, 1, 0),
        )


def _seed(store: Path, entry: dict) -> None:
    entry = {"timestamp": "2026-10-02T11:00:00+00:00", "model": "sonnet", "provider": "claude", **entry}
    append_receipt_payload(entry, receipts_file=str(store / "state" / "t0_receipts.ndjson"))


# The report_parser and converter seeds carry the worker's own "done" status:
# ADR-038 outcome dedup (compute_outcome_id over dispatch_id/event_type/status)
# folds a second task_complete/success for one dispatch at append time, which is
# a separate mechanism from the envelope pre-check under test here.
UNRELATED_LINES = {
    "review_gate_request": {
        "event_type": "review_gate_request", "receipt_kind": "review_gate",
        "dispatch_id": DISPATCH_ID, "status": "requested", "gate": "codex_gate",
    },
    "report_parser": {
        "event_type": "task_complete", "receipt_kind": "dispatch",
        "dispatch_id": DISPATCH_ID, "status": "done", "contract_valid": True,
        "report_file": f"{DISPATCH_ID}.md",
    },
    "converter": {
        "event_type": "task_complete", "receipt_kind": "dispatch",
        "dispatch_id": DISPATCH_ID, "status": "done",
        "role": "identity_unresolved", "pr_link": "https://example.invalid/pr/1",
    },
    "pr_enforcement": {
        "event_type": "pr_enforcement", "receipt_kind": "dispatch",
        "source": "pr_enforcement", "dispatch_id": DISPATCH_ID, "status": "failed",
    },
}


class TestEnvelopeDedupNarrowed:
    @pytest.mark.parametrize("kind", sorted(UNRELATED_LINES))
    def test_unrelated_earlier_line_does_not_suppress_lane_receipt(self, store, kind):
        spec = _envelope_spec(store)
        _seed(store, UNRELATED_LINES[kind])

        _run_govern(spec)

        v2 = [r for r in _ledger(store / "state")
              if r.get("dispatch_id") == DISPATCH_ID and "completion_pct" in r]
        assert len(v2) == 1, f"lane ReceiptV2 missing after earlier {kind} line"
        assert v2[0].get("role") == SPEC_ROLE

    def test_subprocess_safety_net_receipt_still_dedups(self, store):
        spec = _envelope_spec(store)
        _seed(store, {
            "event_type": "subprocess_completion", "receipt_kind": "dispatch",
            "source": "subprocess", "dispatch_id": DISPATCH_ID, "status": "done",
            "terminal": "T1", "terminal_id": "T1",
        })

        _run_govern(spec)

        assert [r for r in _ledger(store / "state") if "completion_pct" in r] == []

    def test_spaced_receipt_v2_line_dedups(self, store):
        """A ReceiptV2 line rewritten with default separators (nightly rewrite)."""
        spec = _envelope_spec(store)
        ledger = store / "state" / "t0_receipts.ndjson"
        ledger.write_text(json.dumps({
            "schema_version": 2, "dispatch_id": DISPATCH_ID, "role": SPEC_ROLE,
            "receipt_kind": "dispatch", "status": "success", "completion_pct": 100,
            "risk": 0.0, "findings": [], "deadline_seconds": 900,
        }) + "\n", encoding="utf-8")
        assert '"dispatch_id": "' in ledger.read_text(encoding="utf-8")

        _run_govern(spec)

        v2 = [r for r in _ledger(store / "state") if "completion_pct" in r]
        assert len(v2) == 1

    def test_unreadable_ledger_is_fail_closed(self, store):
        ledger = store / "state" / "t0_receipts.ndjson"
        ledger.write_text('{"dispatch_id":"x"}\n', encoding="utf-8")
        real_open = open

        def _boom(path, *a, **k):
            if str(path) == str(ledger):
                raise OSError("Permission denied")
            return real_open(path, *a, **k)

        with patch("builtins.open", _boom):
            assert _receipt_exists_for_dispatch(ledger, DISPATCH_ID) is True

    def test_other_dispatch_line_does_not_count(self, store):
        ledger = store / "state" / "t0_receipts.ndjson"
        ledger.write_text(json.dumps({
            "dispatch_id": DISPATCH_ID + "-2", "receipt_kind": "dispatch", "source": "subprocess",
        }) + "\nnot json " + DISPATCH_ID + "\n", encoding="utf-8")
        assert _receipt_exists_for_dispatch(ledger, DISPATCH_ID) is False

    def test_contract_downgrade_still_appends_corrective_record(self, store):
        spec = _envelope_spec(store)
        _seed(store, {
            "event_type": "subprocess_completion", "receipt_kind": "dispatch",
            "source": "subprocess", "dispatch_id": DISPATCH_ID, "status": "done",
            "terminal": "T1", "terminal_id": "T1",
        })
        result = _AdapterResult(returncode=0, completion_text="no headings", status="success")
        report = store / "unified_reports" / f"{DISPATCH_ID}.md"
        report.write_text("no headings at all\n", encoding="utf-8")
        with patch("envelope_govern._archive_dispatch_events", return_value=("", True)), \
             patch("envelope_govern._clear_dispatch_events"):
            _govern(spec, result,
                    start_time=datetime(2026, 10, 2, 12, 0, 0),
                    end_time=datetime(2026, 10, 2, 12, 1, 0))
        v2 = [r for r in _ledger(store / "state") if "completion_pct" in r]
        assert len(v2) == 1
        assert v2[0]["status"] == "contract_invalid"


# ---------------------------------------------------------------------------
# Drop 2: resolver reads the spec
# ---------------------------------------------------------------------------


class TestResolverSpecStep:
    def test_spec_role_between_caller_and_metadata(self, store):
        _write_spec(store, DISPATCH_ID, SPEC_ROLE)
        assert resolve_effective_role(None, DISPATCH_ID, PROJECT, state_dir=store / "state") == SPEC_ROLE

    def test_caller_role_wins_over_spec(self, store):
        _write_spec(store, DISPATCH_ID, SPEC_ROLE)
        assert resolve_effective_role(
            "debugger", DISPATCH_ID, PROJECT, state_dir=store / "state") == "debugger"

    def test_no_spec_is_unresolved(self, store):
        assert resolve_effective_role(
            None, DISPATCH_ID, PROJECT, state_dir=store / "state") == "identity_unresolved"

    @pytest.mark.parametrize("role", ["", "  ", "identity_unresolved", None])
    def test_unusable_spec_role_falls_through(self, store, role):
        _write_spec(store, DISPATCH_ID, role)
        assert resolve_effective_role(
            None, DISPATCH_ID, PROJECT, state_dir=store / "state") == "identity_unresolved"

    def test_unreadable_spec_falls_through(self, store):
        path = _write_spec(store, DISPATCH_ID, SPEC_ROLE)
        path.write_text("{not json", encoding="utf-8")
        assert resolve_effective_role(
            None, DISPATCH_ID, PROJECT, state_dir=store / "state") == "identity_unresolved"

    def test_spec_in_other_store_is_not_used(self, store, other_store):
        assert resolve_effective_role(
            None, DISPATCH_ID, PROJECT, state_dir=store / "state") == "identity_unresolved"
        assert resolve_effective_role(
            None, DISPATCH_ID, "proj-b", state_dir=other_store / "state") == OTHER_ROLE

    def test_no_state_dir_uses_env_store(self, store):
        _write_spec(store, DISPATCH_ID, SPEC_ROLE)
        assert resolve_effective_role(None, DISPATCH_ID, PROJECT) == SPEC_ROLE

    def test_path_traversal_dispatch_id_is_ignored(self, store):
        _write_spec(store, DISPATCH_ID, SPEC_ROLE)
        assert resolve_effective_role(
            None, "../completed/" + DISPATCH_ID, PROJECT,
            state_dir=store / "state") == "identity_unresolved"


class TestConverterTakesSpecRole:
    def _report(self, store, **extra):
        from test_report_to_receipt_converter import _write_frontmatter_report
        path = store / "unified_reports" / f"{DISPATCH_ID}.md"
        _write_frontmatter_report(path, DISPATCH_ID, project_id=PROJECT, **extra)
        return path

    def _convert(self, store, report):
        from report_to_receipt_converter import convert_report_to_receipt
        convert_report_to_receipt(report, receipts_file=str(store / "state" / "t0_receipts.ndjson"))
        return _ledger(store / "state")[0]

    def test_spec_role_used(self, store):
        _write_spec(store, DISPATCH_ID, SPEC_ROLE)
        assert self._convert(store, self._report(store)).get("role") == SPEC_ROLE

    def test_without_spec_unresolved(self, store):
        assert self._convert(store, self._report(store)).get("role") == "identity_unresolved"

    def test_report_role_keeps_precedence(self, store):
        _write_spec(store, DISPATCH_ID, SPEC_ROLE)
        assert self._convert(store, self._report(store, role="debugger")).get("role") == "debugger"

    def test_other_store_spec_not_used(self, store, other_store):
        assert self._convert(store, self._report(store)).get("role") == "identity_unresolved"


# ---------------------------------------------------------------------------
# Drop 3: report_parser stamps the role
# ---------------------------------------------------------------------------


class TestReportParserRole:
    def _parse(self, store, body_extra=""):
        from report_parser import ReportParser
        path = store / "unified_reports" / f"{DISPATCH_ID}.md"
        path.write_text(
            f"# Report\n\n**Dispatch-ID**: {DISPATCH_ID}\n**Status**: success\n"
            f"**Date**: 2026-10-02T10:00:00+00:00\n{body_extra}\n" + _valid_body(),
            encoding="utf-8",
        )
        return ReportParser().parse_report(str(path))

    def test_spec_role_stamped(self, store):
        _write_spec(store, DISPATCH_ID, SPEC_ROLE)
        assert self._parse(store).get("role") == SPEC_ROLE

    def test_report_role_keeps_precedence(self, store):
        _write_spec(store, DISPATCH_ID, SPEC_ROLE)
        assert self._parse(store, "**Role**: debugger\n").get("role") == "debugger"

    def test_no_source_is_marker(self, store):
        assert self._parse(store).get("role") == "identity_unresolved"

    def test_other_store_spec_not_used(self, store, other_store):
        assert self._parse(store).get("role") == "identity_unresolved"


# ---------------------------------------------------------------------------
# Drop 4: subprocess lane receipt
# ---------------------------------------------------------------------------


class TestSubprocessLaneReceiptRole:
    @pytest.fixture(autouse=True)
    def _state(self, store):
        import subprocess_dispatch
        with patch.object(subprocess_dispatch, "_default_state_dir", return_value=store / "state"):
            yield

    def _sub_result(self):
        from subprocess_dispatch_internals.delivery_runtime import _SubprocessResult
        return _SubprocessResult(
            success=True, session_id="s", event_count=3,
            manifest_path="/m.json", touched_files=frozenset(),
        )

    def _monitor(self):
        m = MagicMock()
        m.stuck_count = 0
        return m

    def _common(self):
        return dict(
            dispatch_id=DISPATCH_ID, terminal_id="T1", attempt=0,
            sub_result=self._sub_result(), monitor=self._monitor(),
            commit_hash_before="abc", dispatch_start_ts="2026-10-02T00:00:00+00:00",
            pre_sha="abc", model="sonnet", pr_id=None, mandate_id=None,
            instruction="x", role=SPEC_ROLE,
        )

    def _patches(self):
        import subprocess_dispatch as sd
        return (
            patch.object(sd, "_get_commit_hash", return_value="abc"),
            patch.object(sd, "_check_commit_since", return_value=False),
            patch.object(sd, "_ensure_unified_report"),
            patch.object(sd, "_update_pattern_confidence", return_value=0),
            patch.object(sd, "_capture_dispatch_outcome"),
            patch.object(sd, "cleanup_worker_exit"),
        )

    def test_handle_success_stamps_role(self, store):
        from subprocess_dispatch_internals.recovery import _handle_success
        p = self._patches()
        with p[0], p[1], p[2], p[3], p[4], p[5]:
            _handle_success(
                auto_commit=False, gate="", pre_dispatch_dirty=frozenset(),
                manifest_paths=None, lease_generation=None, **self._common(),
            )
        rows = [r for r in _ledger(store / "state") if r.get("source") == "subprocess"]
        assert [r.get("role") for r in rows] == [SPEC_ROLE]

    def test_handle_final_failure_stamps_role(self, store):
        from subprocess_dispatch_internals.recovery import _handle_final_failure
        import inspect
        params = inspect.signature(_handle_final_failure).parameters
        kwargs = {k: v for k, v in self._common().items() if k in params}
        for name in ("max_retries", "auto_commit", "gate", "pre_dispatch_dirty",
                     "manifest_paths", "lease_generation"):
            if name in params and name not in kwargs:
                kwargs[name] = {"max_retries": 1, "auto_commit": False, "gate": "",
                                "pre_dispatch_dirty": frozenset(), "manifest_paths": None,
                                "lease_generation": None}[name]
        p = self._patches()
        with p[0], p[1], p[2], p[3], p[4], p[5]:
            _handle_final_failure(**kwargs)
        rows = [r for r in _ledger(store / "state") if r.get("source") == "subprocess"]
        assert [r.get("role") for r in rows] == [SPEC_ROLE]

    def test_no_role_argument_gets_spec_role(self, store):
        from subprocess_dispatch_internals.receipt_writer import _write_receipt
        _write_spec(store, DISPATCH_ID, SPEC_ROLE)
        _write_receipt(DISPATCH_ID, "T1", "failed", failure_reason="orchestrator died")
        assert _ledger(store / "state")[0].get("role") == SPEC_ROLE

    def test_no_role_no_spec_is_marker(self, store):
        from subprocess_dispatch_internals.receipt_writer import _write_receipt
        _write_receipt(DISPATCH_ID, "T1", "failed", failure_reason="orchestrator died")
        assert _ledger(store / "state")[0].get("role") == "identity_unresolved"

    def test_caller_role_wins_over_spec(self, store):
        from subprocess_dispatch_internals.recovery import _handle_success
        _write_spec(store, DISPATCH_ID, OTHER_ROLE)
        p = self._patches()
        with p[0], p[1], p[2], p[3], p[4], p[5]:
            _handle_success(
                auto_commit=False, gate="", pre_dispatch_dirty=frozenset(),
                manifest_paths=None, lease_generation=None, **self._common(),
            )
        rows = [r for r in _ledger(store / "state") if r.get("source") == "subprocess"]
        assert [r.get("role") for r in rows] == [SPEC_ROLE]

    def test_other_store_spec_not_used(self, store, other_store):
        from subprocess_dispatch_internals.receipt_writer import _write_receipt
        _write_receipt(DISPATCH_ID, "T1", "done")
        assert _ledger(store / "state")[0].get("role") == "identity_unresolved"
