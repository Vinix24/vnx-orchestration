"""M5-a registry documentation coverage and advisory missing-page diagnostics."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from vnx_cli.commands.subsystems import build_rows
import config_registry as cr

ROOT = Path(__file__).resolve().parents[1]
REASONS = {
    "docs-bloat": "geschrapt", "test-suite": "geen-module",
    "cross-project-federation": "niet-gebouwd",
    "effectiveness-probe-framework": "niet-gebouwd",
}
CANDIDATES = (
    "pool merge-door provider-switch cost-ledger daily-metrics worker-permissions "
    "receipt-pipeline event-streams control-centre unified-supervisor state-aggregator "
    "producer-freshness-monitor wiring-gate"
).split()


def test_every_registry_and_cockpit_row_has_existing_doc():
    docs = {name: meta.get("doc") for name, meta in cr.CONFIG_REGISTRY_SUBSYSTEMS.items()}
    for entry in cr.CONFIG_REGISTRY.values():
        assert getattr(entry, "doc", None), entry.key
        docs[entry.subsystem] = entry.doc
    assert {r["subsystem"]: r.get("doc") for r in build_rows()} == docs
    for name, doc in docs.items():
        if name in REASONS:
            assert doc == REASONS[name]
        else:
            assert isinstance(doc, str) and doc.startswith("docs/modules/"), name
            assert (ROOT / doc).resolve().is_relative_to(ROOT / "docs/modules"), name
            assert (ROOT / doc).is_file(), (name, doc)
    assert {doc for doc in docs.values() if doc not in REASONS.values()} == {
        p.relative_to(ROOT).as_posix() for p in (ROOT / "docs/modules").glob("*.md")
    }


def test_literal_reasons_belong_to_exactly_the_four_named_rows():
    assert {r["subsystem"]: r.get("doc") for r in build_rows()
            if r.get("doc") in REASONS.values()} == REASONS


@pytest.mark.parametrize("name", CANDIDATES)
def test_m3_m4_candidates_are_distinct_flagless_rows(name):
    rows = [r for r in build_rows() if r["subsystem"] == name]
    assert len(rows) == 1
    assert rows[0]["flag"] is None
    assert rows[0]["doc"] == f"docs/modules/{name}.md"
    assert "plan-gate-task-class-scope" not in {r["subsystem"] for r in build_rows()}


def test_unknown_subsystem_document_lookup_refuses():
    entry = replace(next(iter(cr.CONFIG_REGISTRY.values())), subsystem="unknown-subsystem")
    with pytest.raises(KeyError):
        _ = entry.doc


def test_drift_names_missing_page_but_exits_zero():
    proc = subprocess.run([sys.executable, "-c", """
import sys
sys.path.insert(0, 'scripts')
import check_subsystems_drift as drift
import config_registry as cr
cr.CONFIG_REGISTRY_SUBSYSTEMS['provider-routing']['doc'] = 'docs/modules/missing-page.md'
raise SystemExit(drift.main())
"""], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "provider-routing" in proc.stderr
    assert "docs/modules/missing-page.md" in proc.stderr
    assert "MISSING DOC" in proc.stderr
