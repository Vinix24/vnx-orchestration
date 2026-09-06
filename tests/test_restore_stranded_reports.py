#!/usr/bin/env python3
"""Tests for restore_stranded_reports — recovering model-stranded reports.

F1-3 (20260906-f13-gestrande-rapporten): report_to_receipt_converter.py
fail-closed refuses (AppendReceiptError code missing_model/invalid_model_
shape) a dispatch-lane report whose resolved model is absent or a sentinel
("unknown"). Such a report sits stranded in unified_reports/ forever with
no automatic retry once the underlying cause is fixed. This module recovers
those reports ONLY when a real model can be verified from one of three
sources (dispatch-spec.json, an earlier ledger receipt, or the report's own
frontmatter/an unparsed bullet line) — never a default, never a guess.

Covers:
  1. find_stranded_reports() correctly classifies a stranded report and
     skips a healthy one, a non-dispatch report, and an already-watermarked
     one.
  2. Restoration via source (a) dispatch-spec.json: annotation block
     appended, original bytes preceding it byte-for-byte unchanged, receipt
     booked with identity_restored=True.
  3. No source anywhere: file untouched (sha256 identical), no receipt,
     listed as unrestorable.
  4. Restoration via source (c) bullet form (`- Model: x`, a form the
     converter's own bold-field parser does not recognise): the quoted
     line is cited in the annotation.
  5. Idempotency: two --apply passes on the same report produce exactly one
     annotation block and exactly one receipt.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_SCRIPTS_LIB = _SCRIPTS_DIR / "lib"
sys.path.insert(0, str(_SCRIPTS_LIB))
sys.path.insert(0, str(_SCRIPTS_DIR))

from restore_stranded_reports import (  # noqa: E402
    _RESTORED_MARKER,
    apply_restoration,
    build_ledger_model_index,
    find_stranded_reports,
    resolve_identity,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    (d / "unified_reports").mkdir(parents=True)
    (d / "dispatches").mkdir(parents=True)
    return d


@pytest.fixture()
def state_dir(data_dir: Path) -> Path:
    sd = data_dir / "state"
    sd.mkdir(parents=True)
    return sd


@pytest.fixture()
def reports_dir(data_dir: Path) -> Path:
    return data_dir / "unified_reports"


def _write_stranded_report(path: Path, dispatch_id: str, *, extra_frontmatter: str = "", extra_body: str = "") -> Path:
    """A dispatch report whose frontmatter carries model: unknown — the
    real-world shape found in the live store (the emitter stamps the
    sentinel rather than omitting the key). No status/exit_code (mirrors
    report_to_receipt_converter tests' own `_write_report_without_model`)
    so the fail-closed branch/PR-delivery checks — which shell out to git/gh
    — are never reached; only the model check matters here."""
    path.write_text(
        f"---\ndispatch_id: {dispatch_id}\nprovider: claude\nmodel: unknown\n"
        f"{extra_frontmatter}---\n\n"
        "## Summary\n\nImplemented the feature per dispatch specification. "
        "All tests pass and coverage is at target.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 42 passed\n\n"
        "## Open Items\n\nNone\n"
        f"{extra_body}",
        encoding="utf-8",
    )
    return path


def _write_healthy_report(path: Path, dispatch_id: str) -> Path:
    path.write_text(
        f"---\ndispatch_id: {dispatch_id}\nprovider: claude\nmodel: sonnet\n---\n\n"
        "## Summary\n\nImplemented the feature per dispatch specification. "
        "All tests pass and coverage is at target.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 42 passed\n\n"
        "## Open Items\n\nNone\n",
        encoding="utf-8",
    )
    return path


def _write_dispatch_spec(
    data_dir: Path, dispatch_id: str, *, model: str, provider: str = "claude", status_dir: str = "completed",
) -> Path:
    spec_dir = data_dir / "dispatches" / status_dir / dispatch_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / "dispatch-spec.json"
    spec_path.write_text(
        json.dumps({"dispatch_id": dispatch_id, "model": model, "provider": provider}),
        encoding="utf-8",
    )
    return spec_path


def _sha256_prefix(path: Path, n: int) -> str:
    return hashlib.sha256(path.read_bytes()[:n]).hexdigest()


def _receipts(state_dir: Path) -> list:
    receipts_file = state_dir / "t0_receipts.ndjson"
    if not receipts_file.exists():
        return []
    return [json.loads(line) for line in receipts_file.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Part 1: find_stranded_reports() classification
# ---------------------------------------------------------------------------

class TestFindStrandedReports:
    def test_finds_report_with_sentinel_model(self, data_dir, state_dir, reports_dir):
        _write_stranded_report(reports_dir / "20260906-a.md", "20260906-a")

        scan = find_stranded_reports(data_dir, state_dir)

        assert scan.total_scanned == 1
        assert len(scan.candidates) == 1
        assert scan.candidates[0].dispatch_id == "20260906-a"
        assert scan.candidates[0].reason_code == "missing_model"

    def test_skips_healthy_report(self, data_dir, state_dir, reports_dir):
        _write_healthy_report(reports_dir / "20260906-healthy.md", "20260906-healthy")

        scan = find_stranded_reports(data_dir, state_dir)

        assert scan.total_scanned == 1
        assert len(scan.candidates) == 0

    def test_skips_non_dispatch_report(self, data_dir, state_dir, reports_dir):
        (reports_dir / "worktree-release-20260906.md").write_text(
            "no dispatch_id, no model — a tool-output report", encoding="utf-8",
        )

        scan = find_stranded_reports(data_dir, state_dir)

        assert scan.skipped_non_dispatch == 1
        assert len(scan.candidates) == 0

    def test_skips_already_watermarked_report(self, data_dir, state_dir, reports_dir):
        import report_to_receipt_converter as rc

        report = _write_stranded_report(reports_dir / "20260906-b.md", "20260906-b")
        file_hash = rc._compute_sha256(report)
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / rc._WATERMARK_FILENAME).write_text(file_hash + "\n", encoding="utf-8")

        scan = find_stranded_reports(data_dir, state_dir)

        assert scan.already_handled == 1
        assert len(scan.candidates) == 0


# ---------------------------------------------------------------------------
# Part 2: restoration via source (a) — dispatch-spec.json
# ---------------------------------------------------------------------------

class TestRestoreFromDispatchSpec:
    def test_restore_appends_block_preserves_bytes_books_receipt(self, data_dir, state_dir, reports_dir):
        dispatch_id = "20260906-spec-restore"
        report = _write_stranded_report(reports_dir / f"{dispatch_id}.md", dispatch_id)
        _write_dispatch_spec(data_dir, dispatch_id, model="sonnet", provider="claude")

        original_bytes_len = len(report.read_bytes())
        original_prefix_sha = _sha256_prefix(report, original_bytes_len)

        scan = find_stranded_reports(data_dir, state_dir)
        assert len(scan.candidates) == 1
        candidate = scan.candidates[0]
        ledger_index = build_ledger_model_index(state_dir)
        resolution = resolve_identity(candidate, data_dir, ledger_index, report.read_text(encoding="utf-8"))
        assert resolution is not None
        assert resolution.source_kind == "dispatch_spec"
        assert resolution.model == "sonnet"

        ok, detail = apply_restoration(candidate, resolution, data_dir=data_dir, state_dir=state_dir)

        assert ok is True
        new_text = report.read_text(encoding="utf-8")
        assert _RESTORED_MARKER in new_text
        assert "**Model**: sonnet" in new_text
        # Bytes preceding the appended block are byte-for-byte unchanged.
        assert _sha256_prefix(report, original_bytes_len) == original_prefix_sha
        assert new_text[:original_bytes_len] == report.read_bytes()[:original_bytes_len].decode("utf-8")

        receipts = _receipts(state_dir)
        assert len(receipts) == 1
        assert receipts[0]["dispatch_id"] == dispatch_id
        assert receipts[0]["model"] == "sonnet"
        assert receipts[0]["identity_restored"] is True

    def test_dispatch_spec_takes_precedence_over_ledger_and_report(self, data_dir, state_dir, reports_dir):
        """Source order is fixed: (a) dispatch-spec.json wins even when (b)
        the ledger and (c) the report text both also carry a candidate
        value — the dispatch's own spec is the most authoritative source."""
        dispatch_id = "20260906-precedence"
        report = _write_stranded_report(
            reports_dir / f"{dispatch_id}.md", dispatch_id,
            extra_body="\n- Model: bullet-value\n",
        )
        _write_dispatch_spec(data_dir, dispatch_id, model="spec-value", provider="claude")
        ledger_path = state_dir / "t0_receipts.ndjson"
        ledger_path.write_text(
            json.dumps({"dispatch_id": dispatch_id, "event_type": "task_complete", "model": "ledger-value", "provider": "claude"}) + "\n",
            encoding="utf-8",
        )

        scan = find_stranded_reports(data_dir, state_dir)
        candidate = scan.candidates[0]
        ledger_index = build_ledger_model_index(state_dir)
        resolution = resolve_identity(candidate, data_dir, ledger_index, report.read_text(encoding="utf-8"))

        assert resolution.source_kind == "dispatch_spec"
        assert resolution.model == "spec-value"


# ---------------------------------------------------------------------------
# Part 3: no source anywhere — must NOT restore
# ---------------------------------------------------------------------------

class TestNoSourceNeverRestores:
    def test_no_source_leaves_file_untouched_and_lists_unrestorable(self, data_dir, state_dir, reports_dir):
        dispatch_id = "20260906-no-source"
        report = _write_stranded_report(reports_dir / f"{dispatch_id}.md", dispatch_id)
        original_sha = hashlib.sha256(report.read_bytes()).hexdigest()

        scan = find_stranded_reports(data_dir, state_dir)
        assert len(scan.candidates) == 1
        candidate = scan.candidates[0]
        ledger_index = build_ledger_model_index(state_dir)
        resolution = resolve_identity(candidate, data_dir, ledger_index, report.read_text(encoding="utf-8"))

        assert resolution is None
        # Simulate what main() does for the unrestorable bucket: nothing
        # is ever written to the file and no receipt is ever attempted.
        assert hashlib.sha256(report.read_bytes()).hexdigest() == original_sha
        assert _receipts(state_dir) == []


# ---------------------------------------------------------------------------
# Part 4: restoration via source (c) — unparsed bullet line
# ---------------------------------------------------------------------------

class TestRestoreFromBulletLine:
    def test_bullet_form_restored_with_quoted_line(self, data_dir, state_dir, reports_dir):
        dispatch_id = "20260906-bullet-restore"
        report = _write_stranded_report(
            reports_dir / f"{dispatch_id}.md", dispatch_id,
            extra_body="\n- Model: kimi-k3\n",
        )
        # No dispatch-spec.json and no ledger entry — only source (c) applies.

        scan = find_stranded_reports(data_dir, state_dir)
        candidate = scan.candidates[0]
        ledger_index = build_ledger_model_index(state_dir)
        resolution = resolve_identity(candidate, data_dir, ledger_index, report.read_text(encoding="utf-8"))

        assert resolution is not None
        assert resolution.source_kind == "bullet"
        assert resolution.model == "kimi-k3"
        assert "Model: kimi-k3" in resolution.source_detail

        ok, detail = apply_restoration(candidate, resolution, data_dir=data_dir, state_dir=state_dir)
        assert ok is True
        new_text = report.read_text(encoding="utf-8")
        assert "**Model**: kimi-k3" in new_text
        assert "bullet" in new_text.lower()
        assert "Model: kimi-k3" in new_text  # the cited source line


# ---------------------------------------------------------------------------
# Part 5: idempotency — two --apply passes, one block, one receipt
# ---------------------------------------------------------------------------

class TestApplyIsIdempotent:
    def test_second_apply_is_a_no_op(self, data_dir, state_dir, reports_dir):
        dispatch_id = "20260906-idempotent"
        report = _write_stranded_report(reports_dir / f"{dispatch_id}.md", dispatch_id)
        _write_dispatch_spec(data_dir, dispatch_id, model="sonnet", provider="claude")

        # First pass.
        scan1 = find_stranded_reports(data_dir, state_dir)
        candidate1 = scan1.candidates[0]
        ledger_index1 = build_ledger_model_index(state_dir)
        resolution1 = resolve_identity(candidate1, data_dir, ledger_index1, report.read_text(encoding="utf-8"))
        ok1, _ = apply_restoration(candidate1, resolution1, data_dir=data_dir, state_dir=state_dir)
        assert ok1 is True

        text_after_first = report.read_text(encoding="utf-8")
        assert text_after_first.count(_RESTORED_MARKER) == 1
        assert len(_receipts(state_dir)) == 1

        # Second pass: frontmatter `model: unknown` OUTRANKS the body in the
        # merge (`{**body, **fm}` — frontmatter is spread last), so the
        # report STILL scans as stranded even after the annotation block is
        # appended (measured — a body-only fix can never out-rank an
        # explicit frontmatter sentinel). The real safety net is
        # apply_restoration()'s own marker check, exercised below.
        scan2 = find_stranded_reports(data_dir, state_dir)
        assert len(scan2.candidates) == 1
        candidate2 = scan2.candidates[0]
        ledger_index2 = build_ledger_model_index(state_dir)
        resolution2 = resolve_identity(candidate2, data_dir, ledger_index2, report.read_text(encoding="utf-8"))
        assert resolution2 is not None

        ok2, detail2 = apply_restoration(
            candidate2, resolution2, data_dir=data_dir, state_dir=state_dir,
        )
        assert ok2 is False
        assert "already restored" in detail2

        text_after_second = report.read_text(encoding="utf-8")
        assert text_after_second == text_after_first  # byte-for-byte: no new block
        assert text_after_second.count(_RESTORED_MARKER) == 1
        assert len(_receipts(state_dir)) == 1  # no second receipt
