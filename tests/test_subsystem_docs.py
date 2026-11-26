"""Registry documentation coverage, six-column rendering and strict drift checks."""
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from vnx_cli.commands.subsystems import build_rows, _parse_seed_health, _render_md
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


@pytest.fixture
def ledger_root(tmp_path):
    shutil.copytree(ROOT / "docs/modules", tmp_path / "docs/modules")
    (tmp_path / "docs/core").mkdir()
    shutil.copy2(ROOT / "docs/core/SUBSYSTEMS.md", tmp_path / "docs/core/SUBSYSTEMS.md")
    return tmp_path


def run_drift(root, setup=""):
    return subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
sys.path.insert(0, 'scripts')
import check_subsystems_drift as drift
import config_registry as cr
drift._REPO_ROOT = Path(sys.argv[1])
drift.LEDGER_PATH = drift._REPO_ROOT / 'docs/core/SUBSYSTEMS.md'
""" + setup + "\nraise SystemExit(drift.main())\n", str(root)],
        cwd=ROOT, capture_output=True, text=True)


def test_rendered_table_has_six_columns_and_registry_links():
    rows = build_rows()
    for row in rows:
        row["health"] = "unknown"
    lines = _render_md(rows).splitlines()
    assert all(len(line.strip("|").split("|")) == 6 for line in lines)
    assert lines[0] == "| subsystem | what | flag | status | health | doc |"
    for row, line in zip(rows, lines[2:], strict=True):
        doc = row["doc"]
        expected = doc if doc in REASONS.values() else f"[{doc}](../modules/{Path(doc).name})"
        assert line.strip("|").split("|")[-1].strip() == expected


def test_seed_health_reads_every_registry_row():
    seed = _parse_seed_health(ROOT)
    assert len(seed) == len(build_rows())
    assert set(seed) == {row["subsystem"] for row in build_rows()}


def test_seed_health_reads_six_cells(tmp_path):
    path = tmp_path / "docs/core/SUBSYSTEMS.md"
    path.parent.mkdir(parents=True)
    path.write_text("| example | what | — | LIVE | degraded — measured | geen-module |\n")
    assert _parse_seed_health(tmp_path) == {"example": "degraded — measured"}


def test_link_column_is_inside_generated_markers():
    text = (ROOT / "docs/core/SUBSYSTEMS.md").read_text()
    start = text.index("<!-- BEGIN generated:")
    end = text.index("<!-- END generated:")
    assert start < text.index("| subsystem |") < text.index("../modules/provider-routing.md") < end


def test_drift_accepts_regenerated_ledger(ledger_root):
    proc = run_drift(ledger_root)
    assert proc.returncode == 0, proc.stderr
    assert f"({len(build_rows())} subsystems)" in proc.stdout


def test_drift_names_missing_page_and_exits_one(ledger_root):
    proc = run_drift(ledger_root,
        "cr.CONFIG_REGISTRY_SUBSYSTEMS['provider-routing']['doc'] = 'docs/modules/missing-page.md'")
    assert proc.returncode == 1, proc.stderr
    assert "provider-routing" in proc.stderr
    assert "docs/modules/missing-page.md" in proc.stderr
    assert "MISSING DOC" in proc.stderr


@pytest.mark.parametrize("value", [None, "", 42, "unknown-reason", "geen-module", "../README.md", "/tmp/page.md"])
def test_drift_rejects_invalid_doc(ledger_root, value):
    proc = run_drift(ledger_root,
        f"cr.CONFIG_REGISTRY_SUBSYSTEMS['provider-routing']['doc'] = {value!r}")
    assert proc.returncode == 1, proc.stderr
    assert "provider-routing" in proc.stderr
    assert "INVALID DOC" in proc.stderr


@pytest.mark.parametrize("name", ["provider-routing", *REASONS])
def test_drift_rejects_missing_doc_field(ledger_root, name):
    setup = (f"if {name!r} in cr.CONFIG_REGISTRY_SUBSYSTEMS:\n"
             f"    del cr.CONFIG_REGISTRY_SUBSYSTEMS[{name!r}]['doc']\n"
             f"else:\n    del cr.SUBSYSTEM_DOCS[{name!r}]")
    proc = run_drift(ledger_root, setup)
    assert proc.returncode != 0
    assert "KeyError" in proc.stderr


@pytest.mark.parametrize("name", REASONS)
@pytest.mark.parametrize("value", [None, "", 42, "unknown-reason"])
def test_drift_never_exempts_invalid_literal_reason(ledger_root, name, value):
    setup = (f"cr.SUBSYSTEM_DOCS[{name!r}] = {value!r}\n"
             f"cr.CONFIG_REGISTRY_SUBSYSTEMS.get({name!r}, {{}}).update(doc={value!r})")
    proc = run_drift(ledger_root, setup)
    assert proc.returncode == 1, proc.stderr
    assert f"INVALID DOC — {name}:" in proc.stderr


def test_drift_module_directory_read_failure_is_visible(ledger_root):
    directory = ledger_root / "docs/modules"
    directory.rename(ledger_root / "docs/saved-modules")
    directory.write_text("Not a directory\n")
    proc = run_drift(ledger_root)
    assert proc.returncode == 2
    assert "cannot check module documentation" in proc.stderr
    assert "Not a directory" in proc.stderr


def test_drift_names_orphan_page(ledger_root):
    page = ledger_root / "docs/modules/nested/orphan.md"
    page.parent.mkdir()
    page.write_text("# Unregistered module\n")
    proc = run_drift(ledger_root)
    assert proc.returncode == 1, proc.stderr
    assert "ORPHAN DOC" in proc.stderr
    assert "docs/modules/nested/orphan.md" in proc.stderr


def test_drift_rejects_symlink_doc(ledger_root):
    (ledger_root / "docs/modules/alias.md").symlink_to("provider-routing.md")
    proc = run_drift(ledger_root,
        "cr.CONFIG_REGISTRY_SUBSYSTEMS['provider-routing']['doc'] = 'docs/modules/alias.md'")
    assert proc.returncode == 1, proc.stderr
    assert "INVALID DOC" in proc.stderr
    assert "provider-routing" in proc.stderr


@pytest.mark.parametrize("replacement", ["../modules/pool.md", "", "unknown-reason"])
def test_drift_names_changed_link(ledger_root, replacement):
    path = ledger_root / "docs/core/SUBSYSTEMS.md"
    text = path.read_text()
    path.write_text(text.replace("../modules/provider-routing.md", replacement, 1))
    proc = run_drift(ledger_root)
    assert proc.returncode == 1, proc.stderr
    assert "provider-routing" in proc.stderr
    assert "generated" in proc.stderr


def test_drift_ignores_health_changes(ledger_root):
    path = ledger_root / "docs/core/SUBSYSTEMS.md"
    text = path.read_text()
    path.write_text(text.replace("works — dispatch outcomes routed correctly", "unknown — no measurement"))
    proc = run_drift(ledger_root)
    assert proc.returncode == 0, proc.stderr
