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
import importlib.util as _ilu
import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_SCRIPTS_LIB = _SCRIPTS_DIR / "lib"

_RUNNER_PATH_0017 = _SCRIPTS_LIB / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_k8c", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)
sys.path.insert(0, str(_SCRIPTS_LIB))
sys.path.insert(0, str(_SCRIPTS_DIR))

import append_receipt  # noqa: E402
import coordination_db  # noqa: E402
import dispatch_lifecycle as lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import project_id_migration  # noqa: E402
import report_to_receipt_converter as rc  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from restore_stranded_reports import (  # noqa: E402
    _RESTORED_MARKER,
    apply_restoration,
    build_ledger_model_index,
    find_stranded_reports,
    main,
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


def _write_stranded_report(
    path: Path, dispatch_id: str, *, extra_frontmatter: str = "", extra_body: str = "",
    model: str | None = "unknown",
) -> Path:
    """A dispatch report whose frontmatter carries model: unknown — the
    real-world shape found in the live store (the emitter stamps the
    sentinel rather than omitting the key). ``model=None`` omits the key
    entirely (the other stranded shape). No status/exit_code (mirrors
    report_to_receipt_converter tests' own `_write_report_without_model`)
    so the fail-closed branch/PR-delivery checks — which shell out to git/gh
    — are never reached; only the model check matters here."""
    model_line = f"model: {model}\n" if model is not None else ""
    path.write_text(
        f"---\ndispatch_id: {dispatch_id}\nprovider: claude\n{model_line}"
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


# ---------------------------------------------------------------------------
# Part 6 (K-8c): the scan routes through the lifecycle decision
# ---------------------------------------------------------------------------

PROJECT = "proj-alpha"
ROLE = "backend-developer"
T0 = "2026-11-03T08:00:00Z"


@pytest.fixture()
def lifecycle_db(state_dir: Path) -> Path:
    """A real coordination store at the production shape (project_id + composite UNIQUE)."""
    coordination_db.init_schema(state_dir)
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        _mod_0017._rebuild_dispatches(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return state_dir


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "SPEC.md"
    path.write_text("# staged spec\n\nbody\n", encoding="utf-8")
    return path


@pytest.fixture()
def v3(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


@pytest.fixture()
def clock(monkeypatch):
    """Only ``_now`` is pinned; every lifecycle line is still written by ``begin``."""
    state = {"now": T0}
    monkeypatch.setattr(dispatch_start, "_now", lambda: state["now"])
    return state


@pytest.fixture()
def hermetic_append(monkeypatch):
    """Keep the append layer real, but never fire its process-spawning hooks."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


def _insert_dispatch_row(
    state_dir: Path, dispatch_id: str, *, attempt_count: int = 1, project_id: str = PROJECT,
) -> None:
    with coordination_db.get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json) "
            "VALUES (?, ?, 'running', ?, '{}')",
            (dispatch_id, project_id, attempt_count),
        )
        conn.commit()


def _corrupt_row_metadata(state_dir: Path, dispatch_id: str) -> None:
    """The row exists but is unreadable: ``metadata_json`` is not JSON."""
    with coordination_db.get_connection(state_dir) as conn:
        conn.execute(
            "UPDATE dispatches SET metadata_json = 'this is not json' "
            "WHERE dispatch_id = ? AND project_id = ?",
            (dispatch_id, PROJECT),
        )
        conn.commit()


def _begin(
    state_dir: Path, spec_file: Path, dispatch_id: str, *, project_id: str = PROJECT,
) -> dispatch_start.Handover:
    return dispatch_start.begin(
        project_id=project_id, dispatch_id=dispatch_id, role=ROLE, base_sha="a" * 40,
        spec_file=spec_file, target_slot="T1", state_dir=state_dir,
    )


def _warnings(caplog) -> list:
    return [record for record in caplog.records
            if record.levelname == "WARNING" and record.name == "restore_stranded_reports"]


class TestLifecycleRefusalIsVisible:
    """``find_stranded_reports`` must not swallow ``ReportPathRefused``.

    The report stays where it is — a refusal is a named decision, not a
    failure and not a silent skip — but it is logged per dispatch and counted.
    """

    def test_two_refusals_are_logged_counted_and_left_in_place(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock,
            hermetic_append, caplog, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")  # a legacy row is undecidable here
        dispatch_ids = ("20260906-refused-a", "20260906-refused-b")
        for dispatch_id in dispatch_ids:
            _insert_dispatch_row(state_dir, dispatch_id)
            _begin(state_dir, spec_file, dispatch_id)  # the real writer stamps the row
            _corrupt_row_metadata(state_dir, dispatch_id)  # the row is unreadable from here on
            _write_stranded_report(reports_dir / f"{dispatch_id}.md", dispatch_id)
        hashes_before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in reports_dir.glob("*.md")
        }

        with caplog.at_level(logging.WARNING):
            scan = find_stranded_reports(data_dir, state_dir)

        assert [refusal.dispatch_id for refusal in scan.refused] == list(dispatch_ids)
        assert {refusal.code for refusal in scan.refused} == {"lifecycle_mode_undecidable"}
        assert [refusal.path.name for refusal in scan.refused] == [
            f"{dispatch_id}.md" for dispatch_id in dispatch_ids
        ]
        # The unreadable dispatch ROW is the named reason, and it is carried
        # through to the log and to the refusal line (never dropped).
        assert all("row: unreadable" in refusal.detail for refusal in scan.refused)
        assert scan.candidates == ()
        warnings = _warnings(caplog)
        assert len(warnings) == 2
        for dispatch_id, record in zip(dispatch_ids, warnings):
            message = record.getMessage()
            assert dispatch_id in message
            assert "lifecycle_mode_undecidable" in message
            assert "nothing was booked" in message

        # Nothing was booked and neither report was touched: both stay retryable.
        assert _receipts(state_dir) == []
        assert {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in reports_dir.glob("*.md")
        } == hashes_before

        # The headline names the count, and every refusal gets one line, in
        # --list and in --apply; the exit code does not change because of them.
        exit_list = main(["--list", "--data-dir", str(data_dir), "--state-dir", str(state_dir)])
        listing = capsys.readouterr().out
        assert "2 refused on the ledger lifecycle decision" in listing
        assert listing.count("REFUSED dispatch_id=") == 2
        for dispatch_id in dispatch_ids:
            assert f"dispatch_id={dispatch_id} file={dispatch_id}.md code=lifecycle_mode_undecidable" in listing

        other = tmp_path / "no-refusals"
        (other / "unified_reports").mkdir(parents=True)
        (other / "state").mkdir()
        exit_clean = main(["--list", "--data-dir", str(other), "--state-dir", str(other / "state")])
        assert exit_list == exit_clean == 0

        exit_apply = main(["--apply", "--data-dir", str(data_dir), "--state-dir", str(state_dir)])
        applying = capsys.readouterr().out
        assert applying.count("REFUSED dispatch_id=") == 2
        assert "2 refused on the lifecycle decision" in applying
        assert exit_apply == 0

    def test_a_legacy_stranded_report_is_still_a_candidate(self, data_dir, state_dir, reports_dir):
        """The legacy branch is untouched: no lifecycle line, no store DB → a candidate."""
        _write_stranded_report(reports_dir / "20260906-legacy.md", "20260906-legacy")

        scan = find_stranded_reports(data_dir, state_dir)

        assert scan.refused == ()
        assert [candidate.dispatch_id for candidate in scan.candidates] == ["20260906-legacy"]
        assert scan.candidates[0].receipt["event_type"] == "task_complete"

    def test_a_v3_stranded_report_is_booked_as_report_ingested(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3,
            hermetic_append):
        dispatch_id = "20260906-v3"
        _insert_dispatch_row(state_dir, dispatch_id)
        _begin(state_dir, spec_file, dispatch_id)
        _write_stranded_report(reports_dir / f"{dispatch_id}.md", dispatch_id)

        scan = find_stranded_reports(data_dir, state_dir)

        assert len(scan.candidates) == 1
        receipt = scan.candidates[0].receipt
        assert receipt["event_type"] == "report_ingested"
        assert receipt["event_type"] != "task_complete"
        assert receipt["attempt"] == 1
        assert receipt["end_pending"] is True

    def test_a_non_refusal_exception_from_the_core_builder_is_still_skipped_silently(
            self, data_dir, state_dir, reports_dir, monkeypatch, caplog):
        _write_stranded_report(reports_dir / "20260906-boom.md", "20260906-boom")

        def _explode(*args, **kwargs):
            raise ValueError("the core builder cannot parse this report")

        monkeypatch.setattr(rc, "_build_receipt_from_report_core", _explode)
        with caplog.at_level(logging.WARNING):
            scan = find_stranded_reports(data_dir, state_dir)

        assert scan.candidates == ()
        assert scan.refused == ()
        assert _warnings(caplog) == []


# ---------------------------------------------------------------------------
# Part 7 (K-8c round 2): the restore books what the converter books
# ---------------------------------------------------------------------------

T1 = "2026-11-03T09:00:00Z"
T2 = "2026-11-03T10:00:00Z"
T3 = "2026-11-03T11:00:00Z"


@pytest.fixture(autouse=True)
def _tenant(monkeypatch):
    """The door exports VNX_PROJECT_ID; the decision resolves the tenant itself."""
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)


def _epoch(zulu: str) -> float:
    return datetime.fromisoformat(zulu.replace("Z", "+00:00")).timestamp()


def _end_line(dispatch_id: str, *, attempt: int) -> dict:
    return {
        "timestamp": T2, "event_type": "dispatch_finished", "project_id": PROJECT,
        "dispatch_id": dispatch_id, "attempt": attempt, "end_state": "completed",
        "head_sha": "c" * 40, "model": "deepseek-flash",
    }


def _finalize(state_dir: Path, dispatch_id: str, *, attempt: int = 1) -> None:
    lifecycle.finalize(
        state_dir=state_dir, project_id=PROJECT, dispatch_id=dispatch_id, attempt=attempt,
        end_state="completed", end_line=_end_line(dispatch_id, attempt=attempt),
    )


def _lines(state_dir: Path) -> list:
    ledger = state_dir / "t0_receipts.ndjson"
    if not ledger.exists():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _of_type(state_dir: Path, event_type: str, dispatch_id: str | None = None) -> list:
    return [line for line in _lines(state_dir)
            if line.get("event_type") == event_type
            and (dispatch_id is None or line.get("dispatch_id") == dispatch_id)]


class TestApplyBooksTheRoutedLine:
    """Codex's blocker, end to end through ``main(["--apply", ...])``.

    The scan converts the candidate, but ``apply_restoration`` used to rebuild
    the receipt through ``rc.build_receipt_from_report``, which did not route on
    the K-8a head: a v3 restore still appended ``task_complete``. K-8b wires the
    builder; this proves the restore now books the same line the converter does.
    """

    def test_v3_apply_books_one_report_ingested_and_no_task_complete(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3,
            hermetic_append, capsys):
        dispatch_id = "20260906-v3-apply"
        clock["now"] = T0
        _insert_dispatch_row(state_dir, dispatch_id)
        _begin(state_dir, spec_file, dispatch_id)  # the real writer stamps the v3 start
        report = _write_stranded_report(reports_dir / f"{dispatch_id}.md", dispatch_id)
        _write_dispatch_spec(data_dir, dispatch_id, model="sonnet", provider="claude")
        os.utime(report, (_epoch(T1), _epoch(T1)))

        exit_code = main(["--apply", "--data-dir", str(data_dir), "--state-dir", str(state_dir)])

        assert exit_code == 0
        assert "1 restored" in capsys.readouterr().out
        ingested = _of_type(state_dir, "report_ingested", dispatch_id)
        assert len(ingested) == 1
        assert ingested[0]["attempt"] == 1
        assert ingested[0]["end_pending"] is True
        assert "end_outcome_id" not in ingested[0]
        assert ingested[0]["identity_restored"] is True
        assert _of_type(state_dir, "task_complete", dispatch_id) == []
        assert _RESTORED_MARKER in report.read_text(encoding="utf-8")

    def test_legacy_report_without_project_id_and_a_db_row_books_task_complete(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock,
            hermetic_append, capsys, monkeypatch):
        """Codex's warning (OI-2520): a legacy report with no tenant and a live DB
        must stay a candidate, and ``--apply`` books the same ``task_complete``."""
        monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")  # legacy on purpose
        dispatch_id = "20260906-legacy-db"
        clock["now"] = T0
        _insert_dispatch_row(state_dir, dispatch_id)
        _begin(state_dir, spec_file, dispatch_id)  # legacy: no v3 env, no ledger line
        report = _write_stranded_report(reports_dir / f"{dispatch_id}.md", dispatch_id)
        _write_dispatch_spec(data_dir, dispatch_id, model="sonnet", provider="claude")
        assert "project_id" not in report.read_text(encoding="utf-8")

        scan = find_stranded_reports(data_dir, state_dir)

        assert scan.refused == ()
        assert [candidate.dispatch_id for candidate in scan.candidates] == [dispatch_id]
        assert scan.candidates[0].receipt["event_type"] == "task_complete"

        exit_code = main(["--apply", "--data-dir", str(data_dir), "--state-dir", str(state_dir)])

        assert exit_code == 0
        assert "1 restored" in capsys.readouterr().out
        completes = _of_type(state_dir, "task_complete", dispatch_id)
        assert len(completes) == 1
        assert completes[0]["identity_restored"] is True
        assert _of_type(state_dir, "report_ingested", dispatch_id) == []


class TestTheReportsOwnAttemptReachesTheScan:
    """Rule 2: the scan hands the decision the report's own attempt through the
    converter's single reader, exactly as the converter's writer does."""

    def test_quoted_attempt_one_after_attempt_two_started(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3,
            hermetic_append):
        dispatch_id = "20260906-attempt-one"
        clock["now"] = T0
        _insert_dispatch_row(state_dir, dispatch_id)
        _begin(state_dir, spec_file, dispatch_id)          # attempt 1
        _finalize(state_dir, dispatch_id, attempt=1)       # attempt 1 ends
        clock["now"] = T2
        _begin(state_dir, spec_file, dispatch_id)          # attempt 2 starts

        report = _write_stranded_report(
            reports_dir / f"{dispatch_id}.md", dispatch_id, extra_frontmatter="attempt: '1'\n",
        )
        os.utime(report, (_epoch(T3), _epoch(T3)))  # written after attempt 2 started

        scan = find_stranded_reports(data_dir, state_dir)

        assert len(scan.candidates) == 1
        receipt = scan.candidates[0].receipt
        assert receipt["event_type"] == "report_ingested"
        assert receipt["attempt"] == 1
        finished_one = [line for line in _of_type(state_dir, "dispatch_finished", dispatch_id)
                        if line.get("attempt") == 1]
        assert receipt["end_outcome_id"] == finished_one[0]["outcome_id"]


# ---------------------------------------------------------------------------
# Part 8 (K-8c round 3): the scan takes its hint from the converter's one builder
# ---------------------------------------------------------------------------

OTHER_PROJECT = "proj-beta"


def _two_tenant_starts(state_dir: Path, spec_file: Path, dispatch_id: str) -> None:
    """A real v3 start for the SAME dispatch_id under two tenants (composite key)."""
    _insert_dispatch_row(state_dir, dispatch_id)
    _insert_dispatch_row(state_dir, dispatch_id, project_id=OTHER_PROJECT)
    _begin(state_dir, spec_file, dispatch_id)
    _begin(state_dir, spec_file, dispatch_id, project_id=OTHER_PROJECT)


class TestTheScanTakesItsHintFromTheOneBuilder:
    """Codex's warning on 06425353: the restore's local hint copied only ``attempt``.

    The core receipt names no tenant, so with starts under two tenants the
    decision refused ``project_ambiguous`` while the converter — which forwards
    the report's own ``project_id`` through ``report_hint_fields`` — routed the
    same report. This round makes the restore call that one builder too.
    """

    @pytest.mark.parametrize("tenant_line", [
        "project_id: proj-alpha",
        "project_id: 'proj-alpha'",
        'project_id: "proj-alpha"',
    ])
    def test_two_tenant_starts_the_report_naming_its_tenant_is_routed(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3,
            hermetic_append, capsys, tenant_line):
        dispatch_id = "20260906-two-tenants"
        clock["now"] = T0
        _two_tenant_starts(state_dir, spec_file, dispatch_id)
        report = _write_stranded_report(
            reports_dir / f"{dispatch_id}.md", dispatch_id,
            extra_frontmatter=f"{tenant_line}\n",
        )
        _write_dispatch_spec(data_dir, dispatch_id, model="sonnet", provider="claude")
        os.utime(report, (_epoch(T1), _epoch(T1)))

        scan = find_stranded_reports(data_dir, state_dir)

        # Routed, not refused: the tenant the report names reaches the decision.
        assert scan.refused == ()
        assert [candidate.dispatch_id for candidate in scan.candidates] == [dispatch_id]
        receipt = scan.candidates[0].receipt
        assert receipt["event_type"] == "report_ingested"
        assert receipt["project_id"] == PROJECT
        assert receipt["attempt"] == 1

        exit_code = main(["--apply", "--data-dir", str(data_dir), "--state-dir", str(state_dir)])

        assert exit_code == 0
        assert "1 restored" in capsys.readouterr().out
        ingested = _of_type(state_dir, "report_ingested", dispatch_id)
        assert len(ingested) == 1
        assert ingested[0]["project_id"] == PROJECT
        assert ingested[0]["attempt"] == 1
        assert ingested[0]["identity_restored"] is True
        assert _of_type(state_dir, "task_complete", dispatch_id) == []
        assert [line for line in _lines(state_dir)
                if line.get("dispatch_id") == dispatch_id
                and line.get("project_id") == OTHER_PROJECT
                and line.get("event_type") == "report_ingested"] == []

    def test_the_scan_line_equals_the_converter_line(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3,
            hermetic_append):
        """Parity: for one report file, scan and converter book the same identity."""
        dispatch_id = "20260906-parity"
        clock["now"] = T0
        _two_tenant_starts(state_dir, spec_file, dispatch_id)
        report = _write_stranded_report(
            reports_dir / f"{dispatch_id}.md", dispatch_id,
            extra_frontmatter="project_id: 'proj-alpha'\nattempt: '1'\n",
        )
        os.utime(report, (_epoch(T1), _epoch(T1)))
        text = report.read_text(encoding="utf-8")

        scan = find_stranded_reports(data_dir, state_dir)
        assert scan.refused == ()
        scan_line = scan.candidates[0].receipt

        built = rc.build_receipt_from_report(report, text, state_dir=state_dir)
        assert built is not None
        for key in ("dispatch_id", "project_id", "attempt", "event_type"):
            assert scan_line[key] == built[key], key

    def test_the_scan_asks_the_converters_one_builder(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3,
            hermetic_append, monkeypatch):
        """A spy on ``report_hint_fields`` fires: the restore keeps no hint code of its own."""
        dispatch_id = "20260906-spy"
        clock["now"] = T0
        _two_tenant_starts(state_dir, spec_file, dispatch_id)
        report = _write_stranded_report(
            reports_dir / f"{dispatch_id}.md", dispatch_id,
            extra_frontmatter="project_id: proj-alpha\n",
        )
        os.utime(report, (_epoch(T1), _epoch(T1)))
        seen = []
        real = rc.report_hint_fields

        def _spy(text, receipt):
            named = real(text, receipt)
            seen.append(named)
            return named

        monkeypatch.setattr(rc, "report_hint_fields", _spy)

        find_stranded_reports(data_dir, state_dir)

        assert seen and seen[0]["project_id"] == PROJECT

    def test_the_restore_scan_keeps_no_attempt_reader_of_its_own(self):
        """A guard: `git grep -n "_named_attempt" scripts/restore_stranded_reports.py` is empty."""
        run = subprocess.run(
            ["git", "grep", "-n", "_named_attempt", "scripts/restore_stranded_reports.py"],
            capture_output=True, text=True, cwd=VNX_ROOT,
        )
        assert run.returncode == 1, run.stdout + run.stderr  # 1 = no match
        assert run.stdout == ""


# ---------------------------------------------------------------------------
# Part 9 (K-8c round 4): the restore must not move the report's write time
# ---------------------------------------------------------------------------

class TestTheRestoreKeepsTheWriteTime:
    """Codex's blocker on 270ead47: the audit annotation advanced the mtime.

    ``_lifecycle_report_line`` places a report that names no ``attempt`` by its
    write time (ADR-045 K-8a). Appending the annotation after booking moved
    that time past attempt 2's start, so the next converter scan of the same
    file picked attempt 2 — ``_same_booking`` is attempt-exact by design (K-8b
    rule 5) and did not suppress it: a second ``report_ingested``, wrong attempt.
    """

    def _restored_after_attempt_two(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3,
    ) -> tuple:
        """Attempt 1 runs, the report lands inside it, then attempt 2 starts."""
        dispatch_id = "20260906-mtime-attempt"
        clock["now"] = T0
        _insert_dispatch_row(state_dir, dispatch_id)
        _begin(state_dir, spec_file, dispatch_id)          # attempt 1 starts (T0)
        # No ``model:`` key at all: stranded on the missing model, and once the
        # restore appends its ``**Model**: sonnet`` tail the converter's own
        # parser resolves a real model — so the converter can actually re-book.
        report = _write_stranded_report(
            reports_dir / f"{dispatch_id}.md", dispatch_id, model=None)
        _write_dispatch_spec(data_dir, dispatch_id, model="sonnet", provider="claude")
        os.utime(report, (_epoch(T1), _epoch(T1)))         # written during attempt 1 (T1)
        _finalize(state_dir, dispatch_id, attempt=1)       # attempt 1 ends (T2)
        clock["now"] = T3
        _begin(state_dir, spec_file, dispatch_id)          # attempt 2 starts (T3)
        return dispatch_id, report

    def test_the_converter_over_the_restored_file_books_nothing_new(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3,
            hermetic_append, capsys):
        dispatch_id, report = self._restored_after_attempt_two(
            data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3)

        assert main(["--apply", "--data-dir", str(data_dir), "--state-dir", str(state_dir)]) == 0
        assert "1 restored" in capsys.readouterr().out
        ingested = _of_type(state_dir, "report_ingested", dispatch_id)
        assert len(ingested) == 1
        assert ingested[0]["attempt"] == 1

        # The converter's own build over the same (now annotated) file still
        # reads attempt 1, and its booking is a duplicate: no second line.
        built = rc.build_receipt_from_report(
            report, report.read_text(encoding="utf-8"), state_dir=state_dir)
        assert built is not None
        assert built["event_type"] == "report_ingested"
        assert built["attempt"] == 1
        result = append_receipt.append_receipt_payload(
            built, receipts_file=str(state_dir / "t0_receipts.ndjson"),
            cache_window_seconds=300, skip_enrichment=True,
        )
        assert result.status == "duplicate"
        ingested = _of_type(state_dir, "report_ingested", dispatch_id)
        assert len(ingested) == 1
        assert ingested[0]["attempt"] == 1

    def test_the_exact_nanosecond_write_time_survives_and_the_annotation_lands(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3,
            hermetic_append):
        dispatch_id, report = self._restored_after_attempt_two(
            data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3)
        before = report.stat().st_mtime_ns

        assert main(["--apply", "--data-dir", str(data_dir), "--state-dir", str(state_dir)]) == 0

        assert report.stat().st_mtime_ns == before
        assert _RESTORED_MARKER in report.read_text(encoding="utf-8")

    def test_a_failing_utime_keeps_the_booking_and_names_the_failure(
            self, data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3,
            hermetic_append, monkeypatch, caplog):
        dispatch_id, report = self._restored_after_attempt_two(
            data_dir, state_dir, reports_dir, lifecycle_db, spec_file, clock, v3)
        scan = find_stranded_reports(data_dir, state_dir)
        candidate = scan.candidates[0]
        resolution = resolve_identity(
            candidate, data_dir, build_ledger_model_index(state_dir),
            report.read_text(encoding="utf-8"))
        assert resolution is not None

        def _boom(*args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(os, "utime", _boom)
        with caplog.at_level(logging.WARNING):
            ok, detail = apply_restoration(
                candidate, resolution, data_dir=data_dir, state_dir=state_dir)

        # The booking already stood when utime failed: it is kept, and the
        # failure is named in the returned message and in exactly one warning.
        assert ok is True
        assert "write time not restored" in detail
        assert "read-only file system" in detail
        ingested = _of_type(state_dir, "report_ingested", dispatch_id)
        assert len(ingested) == 1
        assert ingested[0]["attempt"] == 1
        assert _RESTORED_MARKER in report.read_text(encoding="utf-8")
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert report.name in warnings[0].getMessage()
        assert "read-only file system" in warnings[0].getMessage()
