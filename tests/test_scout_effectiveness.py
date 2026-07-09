#!/usr/bin/env python3
"""Tests for the scout effectiveness measurement harness.

Dispatch-ID: 20260709-180848-scout-effectiveness-b

Covers correlation math, cohort statistics, and the net verdict on a small
fixture corpus. All state is written to temporary directories; no production
receipts are touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import scout_effectiveness as se  # noqa: E402


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n", encoding="utf-8")


@pytest.fixture
def fixture_corpus(tmp_path: Path):
    """A tiny read-only corpus with 2 enriched and 2 non-enriched dispatches."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    receipts = [
        # Enriched dispatches: lower worker tokens/cost (scout is helping).
        {
            "dispatch_id": "20260709-100000-enriched-a",
            "terminal_id": "T1",
            "provider": "claude",
            "model": "claude-sonnet-4-6",
            "status": "success",
            "duration_seconds": 120.0,
            "token_usage": {"input": 8000, "output": 2000},
            "cost_usd": 0.120,
        },
        {
            "dispatch_id": "20260709-100001-enriched-b",
            "terminal_id": "T1",
            "provider": "claude",
            "model": "claude-sonnet-4-6",
            "status": "success",
            "duration_seconds": 110.0,
            "token_usage": {"input": 7000, "output": 1500},
            "cost_usd": 0.105,
        },
        # Non-enriched dispatches: higher worker tokens/cost.
        {
            "dispatch_id": "20260709-100002-plain-a",
            "terminal_id": "T1",
            "provider": "claude",
            "model": "claude-sonnet-4-6",
            "status": "success",
            "duration_seconds": 180.0,
            "token_usage": {"input": 12000, "output": 3000},
            "cost_usd": 0.180,
        },
        {
            "dispatch_id": "20260709-100003-plain-b",
            "terminal_id": "T2",
            "provider": "claude",
            "model": "claude-sonnet-4-6",
            "status": "failed",
            "duration_seconds": 200.0,
            "token_usage": {"input": 13000, "output": 3500},
            "cost_usd": 0.200,
        },
    ]

    scout_receipts = [
        {
            "event_type": "scout_prepass",
            "dispatch_id": "20260709-100000-enriched-a",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "cost_usd": 0.002,
            "duration_seconds": 1.5,
        },
        {
            "event_type": "scout_prepass",
            "dispatch_id": "20260709-100001-enriched-b",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "cost_usd": 0.0015,
            "duration_seconds": 1.2,
        },
    ]

    _write_ndjson(state_dir / "t0_receipts.ndjson", receipts)
    _write_ndjson(state_dir / "scout_receipts.ndjson", scout_receipts)

    # One sidecar to exercise the sidecar-discovery path; the other enriched
    # dispatch is detected purely from scout_receipts.ndjson.
    (state_dir / "scout").mkdir(parents=True)
    (state_dir / "scout" / "20260709-100000-enriched-a.json").write_text(
        json.dumps({"schema_version": 1, "dispatch_id": "20260709-100000-enriched-a"}),
        encoding="utf-8",
    )

    return state_dir


class TestCorrelation:
    def test_sidecar_discovery(self, fixture_corpus):
        ids = se.list_scout_sidecar_dispatch_ids(fixture_corpus)
        assert ids == {"20260709-100000-enriched-a"}

    def test_correlate_splits_enriched(self, fixture_corpus):
        receipts, _ = se.load_receipts(fixture_corpus)
        scout_receipts, _ = se.load_scout_receipts(fixture_corpus)
        sidecar_ids = se.list_scout_sidecar_dispatch_ids(fixture_corpus)
        records = se.correlate_records(receipts, scout_receipts, sidecar_ids)

        enriched = [r for r in records if r.scout_enriched]
        non_enriched = [r for r in records if not r.scout_enriched]

        assert len(records) == 4
        assert len(enriched) == 2
        assert len(non_enriched) == 2
        assert {r.dispatch_id for r in enriched} == {
            "20260709-100000-enriched-a",
            "20260709-100001-enriched-b",
        }

    def test_scout_cost_aggregation(self, fixture_corpus):
        receipts, _ = se.load_receipts(fixture_corpus)
        scout_receipts, _ = se.load_scout_receipts(fixture_corpus)
        records = se.correlate_records(receipts, scout_receipts, set())
        by_id = {r.dispatch_id: r for r in records}
        assert by_id["20260709-100000-enriched-a"].scout_cost_usd == pytest.approx(0.002)
        assert by_id["20260709-100001-enriched-b"].scout_cost_usd == pytest.approx(0.0015)
        assert by_id["20260709-100002-plain-a"].scout_cost_usd == 0.0


class TestCohortStats:
    def test_means(self, fixture_corpus):
        receipts, _ = se.load_receipts(fixture_corpus)
        scout_receipts, _ = se.load_scout_receipts(fixture_corpus)
        records = se.correlate_records(receipts, scout_receipts, set())
        enriched = se.build_cohort(r for r in records if r.scout_enriched)
        non_enriched = se.build_cohort(r for r in records if not r.scout_enriched)

        assert enriched.mean_tokens == pytest.approx(9250.0)  # (10000 + 8500) / 2
        assert non_enriched.mean_tokens == pytest.approx(15750.0)  # (15000 + 16500) / 2
        assert enriched.mean_cost_usd == pytest.approx(0.1125)
        assert non_enriched.mean_cost_usd == pytest.approx(0.19)
        assert enriched.success_rate == pytest.approx(1.0)
        assert non_enriched.success_rate == pytest.approx(0.5)


class TestEffectivenessReport:
    def test_net_verdict_positive(self, fixture_corpus):
        receipts, _ = se.load_receipts(fixture_corpus)
        scout_receipts, _ = se.load_scout_receipts(fixture_corpus)
        records = se.correlate_records(receipts, scout_receipts, set())
        report = se.compute_effectiveness(records)

        assert report.total_dispatches == 4
        assert report.enriched_count == 2
        assert report.non_enriched_count == 2
        assert report.total_scout_cost_usd == pytest.approx(0.0035)

        # Non-enriched mean cost 0.19, enriched mean cost 0.1125 -> savings per
        # enriched dispatch = 0.0775. Across 2 enriched dispatches = 0.155.
        assert report.estimated_worker_cost_savings_usd == pytest.approx(0.155)
        # Scout cost 0.0035 -> net positive.
        assert report.net_usd == pytest.approx(0.1515)
        assert report.verdict == "scout pays for itself (observational)"

        # Token delta: enriched mean 9250, non-enriched mean 15750 -> delta -6500.
        assert report.token_delta_per_dispatch == pytest.approx(-6500.0)
        # Estimated token savings = 6500 * 2 = 13000.
        assert report.estimated_worker_token_savings == pytest.approx(13000.0)

    def test_report_serialization(self, fixture_corpus):
        receipts, _ = se.load_receipts(fixture_corpus)
        scout_receipts, _ = se.load_scout_receipts(fixture_corpus)
        records = se.correlate_records(receipts, scout_receipts, set())
        report = se.compute_effectiveness(records)
        payload = se.report_to_dict(report)

        assert payload["total_dispatches"] == 4
        assert payload["enriched"]["count"] == 2
        assert payload["non_enriched"]["count"] == 2
        assert payload["net_usd"] == pytest.approx(0.1515)
        assert "observational_note" in payload


class TestEmptyCorpus:
    def test_no_cohorts_warns(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _write_ndjson(state_dir / "t0_receipts.ndjson", [])
        _write_ndjson(state_dir / "scout_receipts.ndjson", [])

        receipts, _ = se.load_receipts(state_dir)
        scout_receipts, _ = se.load_scout_receipts(state_dir)
        records = se.correlate_records(receipts, scout_receipts, set())
        report = se.compute_effectiveness(records)

        assert report.enriched_count == 0
        assert report.non_enriched_count == 0
        assert report.verdict == "cannot judge ROI: missing cohort"


class TestCli:
    def _load_cli_main(self):
        """Import the CLI script under a distinct module name."""
        import importlib.util

        cli_path = SCRIPTS_DIR / "scout_effectiveness.py"
        spec = importlib.util.spec_from_file_location(
            "scout_effectiveness_cli", cli_path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["scout_effectiveness_cli"] = module
        spec.loader.exec_module(module)
        return module.main

    def test_cli_runs_and_writes_artifact(self, fixture_corpus, monkeypatch, capsys):
        main = self._load_cli_main()
        monkeypatch.setenv("VNX_STATE_DIR", str(fixture_corpus))
        rc = main(["--state-dir", str(fixture_corpus)])
        captured = capsys.readouterr()

        assert rc == 0
        assert "Scout Effectiveness Measurement" in captured.out
        artifact = fixture_corpus / "scout_effectiveness.json"
        assert artifact.is_file()
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        assert payload["enriched_count"] == 2
        assert payload["non_enriched_count"] == 2
        assert payload["net_usd"] == pytest.approx(0.1515)

    def test_cli_quiet_writes_json(self, fixture_corpus, monkeypatch, capsys):
        main = self._load_cli_main()
        monkeypatch.setenv("VNX_STATE_DIR", str(fixture_corpus))
        rc = main(["--state-dir", str(fixture_corpus), "--quiet"])
        captured = capsys.readouterr()

        assert rc == 0
        assert captured.out == ""
        assert (fixture_corpus / "scout_effectiveness.json").is_file()

    def test_cli_no_json(self, fixture_corpus, monkeypatch, capsys):
        main = self._load_cli_main()
        monkeypatch.setenv("VNX_STATE_DIR", str(fixture_corpus))
        rc = main(["--state-dir", str(fixture_corpus), "--no-json"])
        captured = capsys.readouterr()

        assert rc == 0
        assert "Scout Effectiveness Measurement" in captured.out
        assert not (fixture_corpus / "scout_effectiveness.json").exists()
