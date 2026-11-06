#!/usr/bin/env python3
"""Characterisation tests for the two extraction steps of
``_build_receipt_from_report_core`` (golf 2, G2-13).

The core builder is the only writer of dispatch receipts, so the refactor that
lifts two of its steps into helpers must not move a byte of behaviour. These
tests are written against the OLD code first and assert the FULL receipt dict
(keys, values and key order), the log lines and the dead-letter side effect:

- Step A — the OI-1102 dispatch-register cross-check on a filename-only
  dispatch_id: dry-run moves nothing, a real run dead-letters the report with
  reason ``unknown_dispatch``, and a filename id the register knows falls
  through to a receipt.
- Step B — lane identity: a recognised route-decision provider lands on the
  receipt, and exhausting both the lane and the body books
  ``unrecognized_provider`` (``provider == unknown``, model empty).

Nothing in the converter is patched: every case writes real report files into
``tmp_path`` and uses a real state dir.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from report_to_receipt_converter import _build_receipt_from_report_core

_CONTRACT_BODY = (
    "## Summary\n\nThis summary has more than fifty non-whitespace characters "
    "so the body contract validates cleanly and the receipt is built.\n\n"
    "## Changes\n\n- scripts/lib/example.py: added X\n\n"
    "## Verification\n\npytest tests/ -x: 42 passed\n\n"
    "## Open Items\n\nNone\n"
)


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    return sd


def _write_filename_only_report(path: Path) -> Path:
    """A contract-valid report that carries no dispatch_id in its content."""
    path.write_text(
        "---\nprovider: claude\ntimestamp: 2026-06-01T00:00:00Z\n---\n\n"
        + _CONTRACT_BODY,
        encoding="utf-8",
    )
    return path


def _write_frontmatter_report(path: Path, dispatch_id: str, **extra) -> Path:
    fields = {
        "dispatch_id": dispatch_id,
        "provider": "claude",
        "timestamp": "2026-06-01T00:00:00Z",
        **extra,
    }
    fm = "\n".join(f"{key}: {value}" for key, value in fields.items())
    path.write_text(f"---\n{fm}\n---\n\n" + _CONTRACT_BODY, encoding="utf-8")
    return path


def _write_register(state_dir: Path, *dispatch_ids: str) -> None:
    lines = [json.dumps({"dispatch_id": did}) for did in dispatch_ids]
    (state_dir / "dispatch_register.ndjson").write_text(
        "\n".join(lines) + "\n", encoding="utf-8",
    )


def _write_route_decision(state_dir: Path, dispatch_id: str, provider: str, model: str) -> dict:
    rd_dir = state_dir / "route_decisions"
    rd_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "dispatch_id": dispatch_id,
        "decision": {"provider": provider, "model": model},
        "timestamp": "2026-06-01T00:00:00Z",
    }
    (rd_dir / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


# ---------------------------------------------------------------------------
# Step A — OI-1102 filename-only dispatch_id cross-check
# ---------------------------------------------------------------------------

class TestStepAUnknownFilenameDispatchId:
    """The register cross-check has exactly three outcomes; pin all three."""

    def test_dry_run_returns_none_and_moves_nothing(self, tmp_path, state_dir, caplog):
        report = _write_filename_only_report(tmp_path / "20260601-chars-dryrun.md")
        # The register exists and knows a DIFFERENT dispatch — so the
        # filename id is provably unknown (a missing register fails open).
        _write_register(state_dir, "20260601-some-other-dispatch")

        with caplog.at_level(logging.INFO, logger="report_to_receipt_converter"):
            receipt = _build_receipt_from_report_core(
                report, report.read_text(encoding="utf-8"),
                state_dir=state_dir, dry_run=True,
            )

        assert receipt is None
        assert report.is_file(), "dry-run must never move the report"
        assert not (state_dir / "receipt_deadletter").exists()
        assert any(
            rec.levelno == logging.INFO
            and "[dry-run] would dead-letter" in rec.getMessage()
            and "20260601-chars-dryrun" in rec.getMessage()
            for rec in caplog.records
        ), "the dry-run skip must be logged at INFO"

    def test_real_run_dead_letters_with_unknown_dispatch(self, tmp_path, state_dir, caplog):
        report = _write_filename_only_report(tmp_path / "20260601-chars-deadletter.md")
        _write_register(state_dir, "20260601-some-other-dispatch")

        with caplog.at_level(logging.WARNING, logger="report_to_receipt_converter"):
            receipt = _build_receipt_from_report_core(
                report, report.read_text(encoding="utf-8"),
                state_dir=state_dir, dry_run=False,
            )

        assert receipt is None
        assert not report.exists(), "the unknown report is quarantined"
        moved = state_dir / "receipt_deadletter" / "20260601-chars-deadletter.md"
        assert moved.is_file()
        index = (state_dir / "receipt_deadletter" / "INDEX.txt").read_text(encoding="utf-8")
        assert "unknown_dispatch 20260601-chars-deadletter.md" in index
        assert any(
            rec.levelno == logging.WARNING
            and "not found in dispatch register — dead-lettering" in rec.getMessage()
            for rec in caplog.records
        ), "the dead-letter decision must be logged at WARNING"

    def test_register_known_filename_id_falls_through_to_a_receipt(self, tmp_path, state_dir):
        dispatch_id = "20260601-chars-known"
        report = _write_filename_only_report(tmp_path / f"{dispatch_id}.md")
        _write_register(state_dir, dispatch_id)

        receipt = _build_receipt_from_report_core(
            report, report.read_text(encoding="utf-8"), state_dir=state_dir,
        )

        assert report.is_file(), "a known dispatch is never dead-lettered"
        assert receipt is not None
        assert list(receipt) == [
            "dispatch_id", "receipt_kind", "role", "terminal", "provider",
            "model", "timestamp", "report_path", "event_type", "status",
            "contract_violations",
        ]
        assert receipt == {
            "dispatch_id": dispatch_id,
            "receipt_kind": "dispatch",
            "role": "identity_unresolved",
            "terminal": "unknown",
            "provider": "claude",
            "model": "",
            "timestamp": "2026-06-01T00:00:00Z",
            "report_path": str(report),
            "event_type": "report_contract_invalid",
            "status": "contract_invalid",
            "contract_violations": ["missing_content_dispatch_id"],
        }


# ---------------------------------------------------------------------------
# Step B — lane identity and the unrecognized-provider branch
# ---------------------------------------------------------------------------

class TestStepBLaneIdentity:
    def test_recognised_lane_provider_lands_on_the_receipt(self, tmp_path, state_dir, caplog):
        dispatch_id = "20260601-chars-lane"
        report = _write_frontmatter_report(tmp_path / f"{dispatch_id}.md", dispatch_id)
        route_record = _write_route_decision(
            state_dir, dispatch_id, "deepseek-harness", "deepseek-flash",
        )

        with caplog.at_level(logging.INFO, logger="report_to_receipt_converter"):
            receipt = _build_receipt_from_report_core(
                report, report.read_text(encoding="utf-8"), state_dir=state_dir,
            )

        assert receipt is not None
        assert list(receipt) == [
            "dispatch_id", "receipt_kind", "role", "terminal", "provider",
            "model", "timestamp", "report_path", "event_type", "status",
            "status_source", "route_decision",
        ]
        assert receipt == {
            "dispatch_id": dispatch_id,
            "receipt_kind": "dispatch",
            "role": "identity_unresolved",
            "terminal": "unknown",
            "provider": "deepseek-harness",
            "model": "deepseek-flash",
            "timestamp": "2026-06-01T00:00:00Z",
            "report_path": str(report),
            "event_type": "task_complete",
            "status": "done",
            "status_source": "report_contract",
            "route_decision": route_record,
        }
        assert not any(
            rec.levelno == logging.WARNING for rec in caplog.records
        ), "a recognised lane books no warning"

    def test_exhausted_identity_books_unrecognized_provider(self, tmp_path, state_dir, caplog):
        dispatch_id = "20260601-chars-prose-provider"
        report = tmp_path / f"{dispatch_id}.md"
        report.write_text(
            f"---\ndispatch_id: {dispatch_id}\nmodel: sonnet\n"
            "timestamp: 2026-06-01T00:00:00Z\n---\n\n"
            + _CONTRACT_BODY
            + "\n**Provider**: ` regel. Zonder die identiteitsregels landt je "
            "receipt niet.\n",
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger="report_to_receipt_converter"):
            receipt = _build_receipt_from_report_core(
                report, report.read_text(encoding="utf-8"), state_dir=state_dir,
            )

        assert receipt is not None
        assert list(receipt) == [
            "dispatch_id", "receipt_kind", "role", "terminal", "provider",
            "model", "timestamp", "report_path", "event_type", "status",
            "contract_violations",
        ]
        assert receipt == {
            "dispatch_id": dispatch_id,
            "receipt_kind": "dispatch",
            "role": "identity_unresolved",
            "terminal": "unknown",
            "provider": "unknown",
            "model": "",
            "timestamp": "2026-06-01T00:00:00Z",
            "report_path": str(report),
            "event_type": "report_contract_invalid",
            "status": "contract_invalid",
            "contract_violations": ["unrecognized_provider"],
        }
        assert any(
            rec.levelno == logging.WARNING
            and "unrecognized provider" in rec.getMessage()
            and dispatch_id in rec.getMessage()
            for rec in caplog.records
        ), "the refusal must be logged at WARNING"
