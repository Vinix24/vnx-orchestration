#!/usr/bin/env python3
"""Tests for the OI-1203 push-verified delivery gate.

A dispatch with ``output_kind=pr`` may only book ``success`` when work is
demonstrably delivered: either a ``pr_ref`` that resolves to a real PR on
GitHub (queried, never taken from the report at face value), or a branch that
is actually on origin.  Both failing means the worker left work uncommitted or
unpushed in a (possibly reaped) worktree, and the success claim is refused.

The five scenarios required by the dispatch are exercised through
``build_receipt_from_report`` — the same write path the receipt processor runs
— so the check is proven to live in the receipt write path, not a side script.
The remote primitives (``_check_branch_on_origin`` and ``_verify_pr_exists``)
are monkeypatched to deterministic values; these are therefore TESTS of the
gate, not a live run of a real non-pushing dispatch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import report_to_receipt_converter as rtc
from report_to_receipt_converter import (
    _check_pr_delivery,
    _parse_pr_number,
    _resolve_output_kind,
    build_receipt_from_report,
)


def _v1_frontmatter(dispatch_id: str, **extra) -> str:
    """A v1-valid YAML frontmatter (15 required fields), plus ``**extra``."""
    fm = {
        "schema_version": 1,
        "dispatch_id": dispatch_id,
        "provider": "claude",
        "sub_provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "terminal_id": "T1",
        "pool_id": "headless",
        "role": "identity_unresolved",
        "task_class": "implementation",
        "pr_id": "none",
        "duration_seconds": 12.5,
        "exit_code": 0,
        "token_usage": {"input": 1234, "output": 567, "cache_read": 89},
        "cost_usd": 0.0421,
        "route_decision": {
            "strategy": "default",
            "selected_provider": "claude",
            "selected_model": "claude-sonnet-4-6",
            "reason": "primary route",
        },
        "status": "unknown",
        "terminal": "T1",
        "timestamp": "2026-06-01T21:34:16Z",
    }
    fm.update(extra)
    return yaml.safe_dump(fm, sort_keys=False).strip()


_BODY = (
    "## Summary\n\nImplemented the feature per dispatch specification. "
    "All tests pass and coverage is at target.\n\n"
    "## Changes\n\n- scripts/lib/example.py: added X\n\n"
    "## Verification\n\npytest tests/ -x: 42 passed\n\n"
    "## Open Items\n\nNone\n"
)


def _build(tmp_path: Path, dispatch_id: str, **extra) -> dict:
    """Write a report and run it through the converter's write path."""
    text = f"---\n{_v1_frontmatter(dispatch_id, **extra)}\n---\n\n{_BODY}"
    p = tmp_path / f"{dispatch_id}.md"
    p.write_text(text, encoding="utf-8")
    return build_receipt_from_report(p, text)


def _success_receipt(tmp_path, dispatch_id, **extra):
    return _build(tmp_path, dispatch_id, status="success", **extra)


# ---------------------------------------------------------------------------
# The five required acceptance scenarios (OI-1203)
# ---------------------------------------------------------------------------


class TestPushVerifiedGate:
    def test_pr_kind_no_push_no_pr_ref_is_not_success(
        self, tmp_path, monkeypatch
    ):
        """output_kind=pr + uncommitted work + no push → no success, reason present."""
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: False)

        receipt = _success_receipt(
            tmp_path, "20260815-oi1203-no-push", output_kind="pr",
        )

        assert receipt is not None
        assert receipt["event_type"] == "task_failed"
        assert receipt["status"] == "failure"
        violations = receipt["fail_closed_violations"]
        assert any("branch_not_on_origin" in v for v in violations), (
            f"expected a delivery reason, got: {violations}"
        )

    def test_pr_kind_pushed_branch_is_success(self, tmp_path, monkeypatch):
        """output_kind=pr + a branch on origin → success."""
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: True)

        receipt = _success_receipt(
            tmp_path, "20260815-oi1203-pushed", output_kind="pr",
        )

        assert receipt is not None
        assert receipt["event_type"] == "task_complete"
        assert receipt["status"] == "success"

    def test_pr_kind_valid_pr_ref_without_push_is_success(self, tmp_path, monkeypatch):
        """output_kind=pr + a verified pr_ref (no local push) → success."""
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: False)
        monkeypatch.setattr(rtc, "_verify_pr_exists", lambda _n: True)

        receipt = _success_receipt(
            tmp_path, "20260815-oi1203-pr-ref", output_kind="pr", pr_ref="#1514",
        )

        assert receipt is not None
        assert receipt["event_type"] == "task_complete"
        assert receipt["status"] == "success"

    def test_pr_kind_nonexistent_pr_ref_is_not_success(self, tmp_path, monkeypatch):
        """output_kind=pr + a pr_ref that does not exist → no success (the claim
        is tested, not believed)."""
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: False)
        monkeypatch.setattr(rtc, "_verify_pr_exists", lambda _n: False)

        receipt = _success_receipt(
            tmp_path, "20260815-oi1203-bogus-pr", output_kind="pr", pr_ref="#999999",
        )

        assert receipt is not None
        assert receipt["event_type"] == "task_failed"
        assert receipt["status"] == "failure"
        violations = receipt["fail_closed_violations"]
        assert any("pr_ref_not_verified" in v for v in violations), (
            f"expected a pr_ref_not_verified reason, got: {violations}"
        )

    def test_non_pr_kind_is_untouched(self, tmp_path, monkeypatch):
        """output_kind != pr → the delivery check is skipped; success as before.

        A document/analysis dispatch has no PR to deliver and must not be
        blocked by the absence of a pushed branch. The branch check is never
        even consulted here — assert that by leaving a failing stub in place.
        """
        monkeypatch.setattr(
            rtc,
            "_check_branch_on_origin",
            lambda _did: (_ for _ in ()).throw(AssertionError("must not be called")),
        )

        receipt = _success_receipt(
            tmp_path, "20260815-oi1203-doc", output_kind="doc",
        )

        assert receipt is not None
        assert receipt["event_type"] == "task_complete"
        assert receipt["status"] == "success"


# ---------------------------------------------------------------------------
# The check lives in the write path, not a side script
# ---------------------------------------------------------------------------


class TestCheckIsInWritePath:
    def test_success_receipt_uses_fail_closed_checks_with_merged(self, tmp_path, monkeypatch):
        """build_receipt_from_report passes merged into the fail-closed gate, so
        the delivery check is part of the receipt build — not an after-the-fact
        script. A success claim with output_kind=pr and neither delivery signal
        is refused at build time."""
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: False)

        receipt = _success_receipt(
            tmp_path, "20260815-oi1203-write-path", output_kind="pr",
        )

        assert receipt["event_type"] == "task_failed"


# ---------------------------------------------------------------------------
# Helper units: output-kind resolution, PR-number parsing, delivery decision
# ---------------------------------------------------------------------------


class TestResolveOutputKind:
    def test_defaults_to_pr_when_absent(self):
        assert _resolve_output_kind(None) == "pr"
        assert _resolve_output_kind({}) == "pr"
        assert _resolve_output_kind({"output_kind": ""}) == "pr"
        assert _resolve_output_kind({"output_kind": "  "}) == "pr"

    def test_lowercases_and_strips(self):
        assert _resolve_output_kind({"output_kind": "PR"}) == "pr"
        assert _resolve_output_kind({"output_kind": " Doc "}) == "doc"


class TestParsePrNumber:
    def test_parses_hash_and_plain(self):
        assert _parse_pr_number("#1514") == 1514
        assert _parse_pr_number("1514") == 1514
        assert _parse_pr_number("  #42  ") == 42

    def test_returns_none_for_garbage(self):
        assert _parse_pr_number(None) is None
        assert _parse_pr_number("") is None
        assert _parse_pr_number("not-a-number") is None
        assert _parse_pr_number("PR-1") is None


class TestCheckPrDelivery:
    def test_no_signal_no_push_returns_violation(self, monkeypatch):
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: False)
        violations = _check_pr_delivery("d-1", {"output_kind": "pr"})
        assert any("branch_not_on_origin" in v for v in violations)

    def test_push_wins_without_pr_ref(self, monkeypatch):
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: True)
        assert _check_pr_delivery("d-1", {}) == []

    def test_verified_pr_wins_without_push(self, monkeypatch):
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: False)
        monkeypatch.setattr(rtc, "_verify_pr_exists", lambda _n: True)
        assert _check_pr_delivery("d-1", {"pr_ref": "#1"}) == []

    def test_bogus_pr_ref_and_no_push_returns_violation(self, monkeypatch):
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: False)
        monkeypatch.setattr(rtc, "_verify_pr_exists", lambda _n: False)
        violations = _check_pr_delivery("d-1", {"pr_ref": "#999999"})
        assert any("pr_ref_not_verified" in v for v in violations)

    def test_push_wins_even_with_bogus_pr_ref(self, monkeypatch):
        """A bogus pr_ref claim does not veto a real push — delivery is OR."""
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: True)
        monkeypatch.setattr(rtc, "_verify_pr_exists", lambda _n: False)
        assert _check_pr_delivery("d-1", {"pr_ref": "#999999"}) == []

    def test_int_pr_ref_from_yaml_is_verified(self, monkeypatch):
        """A worker writing ``pr_ref: 1514`` unquoted yields an int through
        YAML; it must still parse and be verified rather than crash the gate."""
        monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: False)
        monkeypatch.setattr(rtc, "_verify_pr_exists", lambda _n: True)
        assert _check_pr_delivery("d-1", {"pr_ref": 1514}) == []
