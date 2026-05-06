"""Tests for build_doc_indexes.py and scripts.lib.strategy.doc_indexes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import build_doc_indexes as builder  # noqa: E402
from scripts.lib.strategy.doc_indexes import DocEntry, load_adr_index, load_prd_index  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prd_frontmatter(
    doc_id: str = "PRD-001",
    title: str = "Foo Feature",
    version: str = "1.0",
    status: str = "active",
    supersedes: str | None = None,
) -> str:
    lines = [
        "---",
        f"id: {doc_id}",
        f"title: {title}",
        f"version: \"{version}\"",
        f"status: {status}",
    ]
    if supersedes is not None:
        lines.append(f"supersedes: {supersedes}")
    else:
        lines.append("supersedes: null")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    return "\n".join(lines)


def _adr_frontmatter(
    doc_id: str = "ADR-001",
    title: str = "Bar Decision",
    version: str = "1.0",
    status: str = "active",
    supersedes: str | None = None,
) -> str:
    return _prd_frontmatter(doc_id, title, version, status, supersedes)


def _setup_docs(tmp_path: Path, prds: list[tuple[str, str]], adrs: list[tuple[str, str]]) -> Path:
    """Create synthetic docs dirs and return the project_root (tmp_path)."""
    if prds:
        prd_dir = tmp_path / "docs" / "prds"
        prd_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in prds:
            (prd_dir / filename).write_text(content, encoding="utf-8")
    if adrs:
        adr_dir = tmp_path / "docs" / "adrs"
        adr_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in adrs:
            (adr_dir / filename).write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Tests: builder
# ---------------------------------------------------------------------------

class TestBuildDocIndexes:
    def test_happy_path_prd_and_adr(self, tmp_path):
        data_dir = tmp_path / ".vnx-data"
        project_root = _setup_docs(
            tmp_path,
            prds=[("PRD-001-foo.md", _prd_frontmatter())],
            adrs=[("ADR-001-bar.md", _adr_frontmatter())],
        )

        prd_index, adr_index = builder.build(data_dir=data_dir, project_root=project_root)

        assert len(prd_index) == 1
        assert len(adr_index) == 1

        prd = prd_index[0]
        assert prd["id"] == "PRD-001"
        assert prd["title"] == "Foo Feature"
        assert prd["version"] == "1.0"
        assert prd["status"] == "active"
        assert prd["supersedes"] is None
        assert "docs/prds/PRD-001-foo.md" in prd["path"]

        adr = adr_index[0]
        assert adr["id"] == "ADR-001"
        assert adr["title"] == "Bar Decision"
        assert adr["status"] == "active"

    def test_index_files_written(self, tmp_path):
        data_dir = tmp_path / ".vnx-data"
        project_root = _setup_docs(
            tmp_path,
            prds=[("PRD-001-foo.md", _prd_frontmatter())],
            adrs=[("ADR-001-bar.md", _adr_frontmatter())],
        )

        prd_index, adr_index = builder.build(data_dir=data_dir, project_root=project_root)

        strategy_dir = data_dir / "strategy"
        strategy_dir.mkdir(parents=True, exist_ok=True)
        (strategy_dir / "prd_index.json").write_text(json.dumps(prd_index, indent=2))
        (strategy_dir / "adr_index.json").write_text(json.dumps(adr_index, indent=2))

        assert (strategy_dir / "prd_index.json").exists()
        assert (strategy_dir / "adr_index.json").exists()

        written_prd = json.loads((strategy_dir / "prd_index.json").read_text())
        assert isinstance(written_prd, list)
        assert written_prd[0]["id"] == "PRD-001"

    def test_missing_prds_dir_returns_empty(self, tmp_path):
        data_dir = tmp_path / ".vnx-data"
        project_root = tmp_path
        # docs/prds does NOT exist; docs/adrs does
        _setup_docs(tmp_path, prds=[], adrs=[("ADR-001-bar.md", _adr_frontmatter())])

        prd_index, adr_index = builder.build(data_dir=data_dir, project_root=project_root)

        assert prd_index == []
        assert len(adr_index) == 1

    def test_missing_adrs_dir_returns_empty(self, tmp_path):
        data_dir = tmp_path / ".vnx-data"
        project_root = tmp_path
        _setup_docs(tmp_path, prds=[("PRD-001-foo.md", _prd_frontmatter())], adrs=[])

        prd_index, adr_index = builder.build(data_dir=data_dir, project_root=project_root)

        assert adr_index == []
        assert len(prd_index) == 1

    def test_both_dirs_missing_returns_empty(self, tmp_path):
        data_dir = tmp_path / ".vnx-data"
        project_root = tmp_path

        prd_index, adr_index = builder.build(data_dir=data_dir, project_root=project_root)

        assert prd_index == []
        assert adr_index == []

    def test_bad_frontmatter_no_crash(self, tmp_path):
        bad_content = "---\n: this is: not: valid: yaml:\n---\n\n# Title\n"
        data_dir = tmp_path / ".vnx-data"
        project_root = _setup_docs(
            tmp_path,
            prds=[("PRD-002-bad.md", bad_content)],
            adrs=[],
        )

        prd_index, adr_index = builder.build(data_dir=data_dir, project_root=project_root)

        assert len(prd_index) == 1
        assert prd_index[0]["status"] == "draft"

    def test_bad_frontmatter_unknown_status_defaults_to_draft(self, tmp_path):
        content = _prd_frontmatter(doc_id="PRD-003", status="unknown_status")
        data_dir = tmp_path / ".vnx-data"
        project_root = _setup_docs(tmp_path, prds=[("PRD-003-bad.md", content)], adrs=[])

        prd_index, _ = builder.build(data_dir=data_dir, project_root=project_root)

        assert prd_index[0]["status"] == "draft"

    def test_sorted_by_id(self, tmp_path):
        data_dir = tmp_path / ".vnx-data"
        project_root = _setup_docs(
            tmp_path,
            prds=[
                ("PRD-003-c.md", _prd_frontmatter(doc_id="PRD-003", title="C")),
                ("PRD-001-a.md", _prd_frontmatter(doc_id="PRD-001", title="A")),
                ("PRD-002-b.md", _prd_frontmatter(doc_id="PRD-002", title="B")),
            ],
            adrs=[],
        )

        prd_index, _ = builder.build(data_dir=data_dir, project_root=project_root)

        ids = [e["id"] for e in prd_index]
        assert ids == sorted(ids)

    def test_supersedes_chain(self, tmp_path):
        data_dir = tmp_path / ".vnx-data"
        project_root = _setup_docs(
            tmp_path,
            prds=[],
            adrs=[
                ("ADR-001-original.md", _adr_frontmatter(
                    doc_id="ADR-001", title="Original Decision", status="superseded"
                )),
                ("ADR-002-replacement.md", _adr_frontmatter(
                    doc_id="ADR-002", title="Replacement Decision",
                    status="active", supersedes="ADR-001"
                )),
            ],
        )

        _, adr_index = builder.build(data_dir=data_dir, project_root=project_root)

        assert len(adr_index) == 2
        by_id = {e["id"]: e for e in adr_index}
        assert "ADR-001" in by_id
        assert "ADR-002" in by_id
        assert by_id["ADR-001"]["status"] == "superseded"
        assert by_id["ADR-001"]["supersedes"] is None
        assert by_id["ADR-002"]["supersedes"] == "ADR-001"
        assert by_id["ADR-002"]["status"] == "active"

    def test_no_frontmatter_uses_stem_as_id_and_title(self, tmp_path):
        content = "# Just a plain markdown file\n\nNo frontmatter here.\n"
        data_dir = tmp_path / ".vnx-data"
        project_root = _setup_docs(
            tmp_path,
            prds=[("PRD-042-plain.md", content)],
            adrs=[],
        )

        prd_index, _ = builder.build(data_dir=data_dir, project_root=project_root)

        assert len(prd_index) == 1
        assert prd_index[0]["id"] == "PRD-042-plain"
        assert prd_index[0]["status"] == "draft"


# ---------------------------------------------------------------------------
# Tests: typed loader (doc_indexes.py)
# ---------------------------------------------------------------------------

class TestDocIndexesLoader:
    def _write_strategy(self, tmp_path: Path, prd_data: list, adr_data: list) -> Path:
        strategy_dir = tmp_path / "strategy"
        strategy_dir.mkdir(parents=True, exist_ok=True)
        (strategy_dir / "prd_index.json").write_text(json.dumps(prd_data, indent=2))
        (strategy_dir / "adr_index.json").write_text(json.dumps(adr_data, indent=2))
        return strategy_dir

    def test_load_prd_index_returns_docentry_list(self, tmp_path):
        data = [{"id": "PRD-001", "path": "docs/prds/PRD-001.md",
                  "version": "1.0", "status": "active", "supersedes": None, "title": "Foo"}]
        strategy_dir = self._write_strategy(tmp_path, data, [])

        result = load_prd_index(strategy_dir=strategy_dir)

        assert len(result) == 1
        assert isinstance(result[0], DocEntry)
        assert result[0].id == "PRD-001"
        assert result[0].status == "active"
        assert result[0].supersedes is None

    def test_load_adr_index_returns_docentry_list(self, tmp_path):
        data = [{"id": "ADR-001", "path": "docs/adrs/ADR-001.md",
                  "version": "1.0", "status": "active", "supersedes": None, "title": "Bar"}]
        strategy_dir = self._write_strategy(tmp_path, [], data)

        result = load_adr_index(strategy_dir=strategy_dir)

        assert len(result) == 1
        assert isinstance(result[0], DocEntry)
        assert result[0].id == "ADR-001"

    def test_missing_file_returns_empty(self, tmp_path):
        strategy_dir = tmp_path / "strategy"
        strategy_dir.mkdir(parents=True, exist_ok=True)

        assert load_prd_index(strategy_dir=strategy_dir) == []
        assert load_adr_index(strategy_dir=strategy_dir) == []

    def test_supersedes_field_preserved(self, tmp_path):
        data = [
            {"id": "ADR-001", "path": "docs/adrs/ADR-001.md",
             "version": "1.0", "status": "superseded", "supersedes": None, "title": "Old"},
            {"id": "ADR-002", "path": "docs/adrs/ADR-002.md",
             "version": "1.0", "status": "active", "supersedes": "ADR-001", "title": "New"},
        ]
        strategy_dir = self._write_strategy(tmp_path, [], data)

        result = load_adr_index(strategy_dir=strategy_dir)

        by_id = {e.id: e for e in result}
        assert by_id["ADR-002"].supersedes == "ADR-001"
        assert by_id["ADR-001"].supersedes is None

    def test_missing_json_file_returns_empty_not_error(self, tmp_path):
        strategy_dir = tmp_path / "nonexistent_strategy"

        result = load_prd_index(strategy_dir=strategy_dir)

        assert result == []

    def test_all_valid_statuses_accepted(self, tmp_path):
        statuses = ["draft", "active", "superseded", "retired"]
        data = [
            {"id": f"PRD-00{i}", "path": f"docs/prds/PRD-00{i}.md",
             "version": "1.0", "status": s, "supersedes": None, "title": f"Doc {i}"}
            for i, s in enumerate(statuses, 1)
        ]
        strategy_dir = self._write_strategy(tmp_path, data, [])

        result = load_prd_index(strategy_dir=strategy_dir)

        loaded_statuses = {e.status for e in result}
        assert loaded_statuses == set(statuses)
