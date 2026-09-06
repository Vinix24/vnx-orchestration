"""Tests for OI-1635: the worker and the envelope can resolve VNX_DATA_DIR to
DIFFERENT stores, so a worker's real (rich) report can sit in a store the
governance emitter never looks at while the central ledger gets only the
generic completion-text wrapper.

Two things are pinned here:

1. ``data_dir_guard.check_report_store_split_brain`` — the detector, tested
   directly against a controlled pair of candidate stores (positive hit AND
   negative "nothing to find" case, per "nul is eerst een meetfout": a query
   that never fires is not proven correct by a passing test alone).
2. ``governance_emit.emit_unified_report`` — the call site. A RED-first
   reproduction: the worker writes its real report to store A; the emitter
   (mirroring provider_dispatch._emit_governance / envelope_govern._govern) is
   asked to write to store B, where nothing exists yet. Before this dispatch's
   fix the emitter silently wrote a generic narrative wrapper at store B with
   no signal that a richer report existed elsewhere. The guard added here logs
   a loud, greppable ERROR (VNX_REPORT_STORE_SPLIT_BRAIN) instead — it does not
   (and structurally cannot, from this single-call-site vantage point) merge
   the two stores; that migration is an explicit open item, not part of this
   fix.

Also covers the OTHER direction OI-1635 calls out explicitly: a SHORTER
completion_text must never clobber a LONGER existing on-disk report — proving
the existing idempotent early-return in emit_unified_report already does this
correctly (no change needed there).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import data_dir_guard  # noqa: E402
import governance_emit  # noqa: E402
from data_dir_guard import check_report_store_split_brain  # noqa: E402


# ---------------------------------------------------------------------------
# 1. The detector, in isolation
# ---------------------------------------------------------------------------


class TestCandidateReportStores:
    def test_positive_hit_when_other_store_has_the_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A report sitting in the repo-local store must be found when the
        emitter is about to write to the (empty) central store — the exact
        OI-1635 shape. This is the "case that exists" proof: a query that can
        only ever return None is not proven correct by the negative test alone.
        """
        repo_local = tmp_path / "repo-local" / ".vnx-data"
        central = tmp_path / "central" / ".vnx-data"
        (repo_local / "unified_reports").mkdir(parents=True)
        central.mkdir(parents=True)

        orphan = repo_local / "unified_reports" / "dispatch-abc.md"
        orphan.write_text("# real rich report\n\nactual work happened here\n")

        monkeypatch.setattr(data_dir_guard, "_resolve_repo_local_data_dir", lambda: repo_local)
        monkeypatch.setattr(data_dir_guard, "resolve_project_id", lambda: "myproj")
        monkeypatch.setattr(data_dir_guard, "resolve_central_data_dir", lambda pid: central)

        found = check_report_store_split_brain("dispatch-abc", central)

        assert found == orphan.resolve()

    def test_negative_when_no_other_store_has_the_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_local = tmp_path / "repo-local" / ".vnx-data"
        central = tmp_path / "central" / ".vnx-data"
        (repo_local / "unified_reports").mkdir(parents=True)
        central.mkdir(parents=True)
        # No report anywhere for this id.

        monkeypatch.setattr(data_dir_guard, "_resolve_repo_local_data_dir", lambda: repo_local)
        monkeypatch.setattr(data_dir_guard, "resolve_project_id", lambda: "myproj")
        monkeypatch.setattr(data_dir_guard, "resolve_central_data_dir", lambda pid: central)

        assert check_report_store_split_brain("dispatch-xyz", central) is None

    def test_negative_when_target_dir_is_the_only_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the repo-local and central resolvers agree with *data_dir*
        itself (the normal, non-split case), there is no "other" store to
        check at all."""
        only = tmp_path / "only" / ".vnx-data"
        only.mkdir(parents=True)

        monkeypatch.setattr(data_dir_guard, "_resolve_repo_local_data_dir", lambda: only)
        monkeypatch.setattr(data_dir_guard, "resolve_project_id", lambda: "myproj")
        monkeypatch.setattr(data_dir_guard, "resolve_central_data_dir", lambda pid: only)

        assert check_report_store_split_brain("dispatch-abc", only) is None

    def test_resolution_errors_are_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unresolvable project_id must not raise past the guard — advisory
        only, never a report-emit blocker."""

        def _raise():
            raise RuntimeError("no project_id here")

        monkeypatch.setattr(data_dir_guard, "_resolve_repo_local_data_dir", _raise)
        monkeypatch.setattr(data_dir_guard, "resolve_project_id", _raise)

        assert check_report_store_split_brain("dispatch-abc", tmp_path / ".vnx-data") is None


# ---------------------------------------------------------------------------
# 2. The call site: governance_emit.emit_unified_report
# ---------------------------------------------------------------------------


class TestEmitUnifiedReportSplitBrainGuard:
    def test_logs_loud_when_writing_wrapper_over_an_orphaned_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """RED reproduction of OI-1635's worker-vs-envelope split brain.

        The worker "wrote" its real, rich report to store A (simulating a
        provider-lane worker whose own $VNX_DATA_DIR resolution landed on the
        repo-local default instead of the central store). The emitter is then
        asked — exactly like _emit_governance / envelope_govern._govern are —
        to write to store B, where the dispatch has no report yet. Before this
        fix this ran completely silently; now it must log the loud,
        greppable VNX_REPORT_STORE_SPLIT_BRAIN marker.
        """
        store_a = tmp_path / "store-a" / ".vnx-data"
        store_b = tmp_path / "store-b" / ".vnx-data"
        (store_a / "unified_reports").mkdir(parents=True)
        store_b.mkdir(parents=True)

        dispatch_id = "20260905-splitbrain-repro"
        real_report = store_a / "unified_reports" / f"{dispatch_id}.md"
        real_report.write_text(
            "# Dispatch " + dispatch_id + "\n\n"
            "**Dispatch-ID**: " + dispatch_id + "\n\n"
            "## Summary\n\nThe worker's actual, detailed work — findings, diffs, "
            "everything a real completion report should contain, well past 50 "
            "characters.\n\n## Changes\n\nreal changes\n\n## Verification\n\nran "
            "tests\n\n## Open Items\n\nNone\n"
        )

        monkeypatch.setattr(data_dir_guard, "_resolve_repo_local_data_dir", lambda: store_a)
        monkeypatch.setattr(data_dir_guard, "resolve_project_id", lambda: "myproj")
        monkeypatch.setattr(data_dir_guard, "resolve_central_data_dir", lambda pid: store_a)

        with caplog.at_level(logging.ERROR, logger="governance_emit"):
            written = governance_emit.emit_unified_report(
                dispatch_id=dispatch_id,
                terminal_id="T1",
                provider="kimi",
                instruction="do the work",
                response_text="short narrative completion text",
                findings=None,
                duration_seconds=12.3,
                data_dir=store_b,
            )

        # The emitter still writes its generic wrapper at the target store —
        # this dispatch's fix is detection, not a store merge (that migration
        # is an explicit open item).
        assert written == store_b / "unified_reports" / f"{dispatch_id}.md"
        assert written.read_text().count("short narrative completion text") == 1

        split_brain_lines = [
            r for r in caplog.records if "VNX_REPORT_STORE_SPLIT_BRAIN" in r.getMessage()
        ]
        assert len(split_brain_lines) == 1, (
            f"expected exactly one split-brain ERROR log line, got {len(split_brain_lines)}: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        assert dispatch_id in split_brain_lines[0].getMessage()
        assert str(real_report) in split_brain_lines[0].getMessage()

        # The orphaned real report is untouched — still on disk, still rich.
        assert real_report.exists()
        assert "findings, diffs" in real_report.read_text()

    def test_no_split_brain_log_when_stores_agree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The common, non-split case: nothing logs when there is no other
        store to disagree with (the negative control for the test above)."""
        only = tmp_path / "only" / ".vnx-data"
        only.mkdir(parents=True)

        monkeypatch.setattr(data_dir_guard, "_resolve_repo_local_data_dir", lambda: only)
        monkeypatch.setattr(data_dir_guard, "resolve_project_id", lambda: "myproj")
        monkeypatch.setattr(data_dir_guard, "resolve_central_data_dir", lambda pid: only)

        with caplog.at_level(logging.ERROR, logger="governance_emit"):
            governance_emit.emit_unified_report(
                dispatch_id="20260905-no-split",
                terminal_id="T1",
                provider="kimi",
                instruction="do the work",
                response_text="normal completion",
                findings=None,
                duration_seconds=1.0,
                data_dir=only,
            )

        assert not any(
            "VNX_REPORT_STORE_SPLIT_BRAIN" in r.getMessage() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# 3. The reverse direction OI-1635 explicitly asks for: shorter completion
#    text must never clobber a longer, already-contract-valid on-disk report.
# ---------------------------------------------------------------------------


class TestIdempotentGuardAlreadyProtectsLongerExisting:
    def test_shorter_completion_text_does_not_overwrite_richer_existing_report(
        self, tmp_path: Path
    ) -> None:
        """Proves the EXISTING idempotent early-return already does the right
        thing here — no change was needed in this direction, only measured."""
        data_dir = tmp_path / ".vnx-data"
        (data_dir / "unified_reports").mkdir(parents=True)
        dispatch_id = "20260905-idempotent-check"
        report_path = data_dir / "unified_reports" / f"{dispatch_id}.md"
        rich_body = (
            "# Dispatch " + dispatch_id + "\n\n"
            "**Dispatch-ID**: " + dispatch_id + "\n\n"
            "## Summary\n\nA long, detailed, contract-valid report the worker "
            "itself wrote, well past 50 characters of summary content.\n\n"
            "## Changes\n\nreal changes\n\n## Verification\n\nran tests\n\n"
            "## Open Items\n\nNone\n"
        )
        report_path.write_text(rich_body)

        written = governance_emit.emit_unified_report(
            dispatch_id=dispatch_id,
            terminal_id="T1",
            provider="kimi",
            instruction="do the work",
            response_text="x",  # much shorter than rich_body
            findings=None,
            duration_seconds=1.0,
            data_dir=data_dir,
        )

        assert written == report_path
        assert report_path.read_text() == rich_body
