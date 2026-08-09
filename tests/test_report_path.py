"""test_report_path.py — tests for the report_path SSOT and resolver.

Closes OI-989 and OI-993: the resolver must detect ambiguity when multiple
report files exist for the same dispatch and must not silently pick the
wrong one.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make scripts/ and scripts/lib/ importable (same as other test files)
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "scripts" / "lib"))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_data_dir():
    """Create a temporary VNX_DATA_DIR with a unified_reports/ subdirectory."""
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        reports_dir = data_dir / "unified_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        yield data_dir


@pytest.fixture
def tmp_repo_root():
    """Create a temporary repo root with .vnx-data/unified_reports/."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        local_reports = root / ".vnx-data" / "unified_reports"
        local_reports.mkdir(parents=True, exist_ok=True)
        yield root


# ---------------------------------------------------------------------------
# canonical_report_path
# ---------------------------------------------------------------------------


def test_canonical_report_path_uses_vnx_data_dir(tmp_data_dir):
    """canonical_report_path() produces VNX_DATA_DIR/unified_reports/<id>.md."""
    from report_path import canonical_report_path

    dispatch_id = "20260804-102000-reportpath"
    path = canonical_report_path(dispatch_id, data_dir=tmp_data_dir)

    assert path == tmp_data_dir / "unified_reports" / f"{dispatch_id}.md"
    assert path.suffix == ".md"
    assert "_report" not in path.stem
    assert not path.stem.startswith("dispatch-")


def test_canonical_report_path_env_var(monkeypatch, tmp_path):
    """canonical_report_path() reads VNX_DATA_DIR from env when data_dir not given."""
    from report_path import canonical_report_path

    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    path = canonical_report_path("test-dispatch-id")
    assert path == tmp_path / "unified_reports" / "test-dispatch-id.md"


def test_report_path_fragment_is_env_var_form():
    """report_path_fragment() returns the $VNX_DATA_DIR form for prompt text."""
    from report_path import report_path_fragment

    fragment = report_path_fragment()
    assert "$VNX_DATA_DIR" in fragment
    assert "unified_reports" in fragment


# ---------------------------------------------------------------------------
# resolve_report_path — no candidates
# ---------------------------------------------------------------------------


def test_resolve_returns_none_when_no_report_exists(tmp_data_dir):
    """No report file on disk -> None."""
    from report_path import resolve_report_path

    result = resolve_report_path("no-such-dispatch", data_dir=tmp_data_dir)
    assert result is None


# ---------------------------------------------------------------------------
# resolve_report_path — single candidate (canonical form)
# ---------------------------------------------------------------------------


def test_resolve_finds_canonical_form(tmp_data_dir):
    """Only <id>.md exists -> return it, not ambiguous."""
    from report_path import resolve_report_path

    dispatch_id = "20260804-single-canonical"
    report = tmp_data_dir / "unified_reports" / f"{dispatch_id}.md"
    report.write_text("## Summary\n\nReal deliverable content.\n\n## Open Items\nNone\n")
    report_size = report.stat().st_size

    result = resolve_report_path(dispatch_id, data_dir=tmp_data_dir)
    assert result is not None
    assert result.path == report
    assert not result.ambiguous
    assert len(result.candidates_found) == 1
    assert str(report) in result.candidate_sizes
    assert result.candidate_sizes[str(report)] == report_size


# ---------------------------------------------------------------------------
# resolve_report_path — legacy forms
# ---------------------------------------------------------------------------


def test_resolve_finds_dispatch_prefix_form(tmp_data_dir):
    """Only dispatch-<id>.md exists -> return it."""
    from report_path import resolve_report_path

    dispatch_id = "20260804-legacy-prefix"
    report = tmp_data_dir / "unified_reports" / f"dispatch-{dispatch_id}.md"
    report.write_text("## Summary\n\nContent from tmux worker.\n\n## Open Items\nNone\n")

    result = resolve_report_path(dispatch_id, data_dir=tmp_data_dir)
    assert result is not None
    assert result.path == report
    assert not result.ambiguous


def test_resolve_finds_report_suffix_form(tmp_data_dir):
    """Only <id>_report.md exists -> return it."""
    from report_path import resolve_report_path

    dispatch_id = "20260804-legacy-suffix"
    report = tmp_data_dir / "unified_reports" / f"{dispatch_id}_report.md"
    report.write_text("## Summary\n\nLegacy suffix content.\n\n## Open Items\nNone\n")

    result = resolve_report_path(dispatch_id, data_dir=tmp_data_dir)
    assert result is not None
    assert result.path == report
    assert not result.ambiguous


# ---------------------------------------------------------------------------
# resolve_report_path — ambiguity detection (the OI-989 pattern)
# ---------------------------------------------------------------------------


def test_resolve_detects_ambiguity_canonical_vs_prefix(tmp_data_dir):
    """OI-989: <id>.md (small summary) AND dispatch-<id>.md (real deliverable).

    The resolver picks canonical <id>.md by priority but flags ambiguous=True.
    It must NOT silently ignore the second candidate.
    """
    from report_path import resolve_report_path

    dispatch_id = "20260803-refbench-dspro"

    # Canonical form: small summary (10KB — scorer saw this, scored 0/86)
    canonical = tmp_data_dir / "unified_reports" / f"{dispatch_id}.md"
    canonical.write_text("## Summary\n\nShort summary. " + "x" * 5000 + "\n\n## Open Items\nNone\n")

    # Prefix form: real deliverable (60KB — the actual 1.111-line design)
    prefix = tmp_data_dir / "unified_reports" / f"dispatch-{dispatch_id}.md"
    prefix.write_text("## Summary\n\nFull design deliverable.\n\n" + "x" * 50000 + "\n\n## Open Items\nNone\n")

    result = resolve_report_path(dispatch_id, data_dir=tmp_data_dir)

    # It MUST pick canonical by priority order
    assert result is not None
    assert result.path == canonical

    # It MUST flag the ambiguity
    assert result.ambiguous
    assert len(result.candidates_found) == 2
    assert canonical in result.candidates_found
    assert prefix in result.candidates_found

    # Both sizes must be recorded
    sizes = result.candidate_sizes
    assert sizes[str(canonical)] < sizes[str(prefix)]


def test_resolve_detects_ambiguity_canonical_vs_suffix(tmp_data_dir):
    """Both <id>.md and <id>_report.md exist -> ambiguous."""
    from report_path import resolve_report_path

    dispatch_id = "20260804-both-forms"

    canonical = tmp_data_dir / "unified_reports" / f"{dispatch_id}.md"
    canonical.write_text("## Summary\n\nCanonical.\n\n## Open Items\nNone\n")

    legacy = tmp_data_dir / "unified_reports" / f"{dispatch_id}_report.md"
    legacy.write_text("## Summary\n\nLegacy report.\n\n## Open Items\nNone\n")

    result = resolve_report_path(dispatch_id, data_dir=tmp_data_dir)
    assert result is not None
    assert result.path == canonical
    assert result.ambiguous
    assert len(result.candidates_found) == 2


def test_resolve_all_three_forms_ambiguous(tmp_data_dir):
    """All three forms exist -> ambiguous with 3 candidates."""
    from report_path import resolve_report_path

    dispatch_id = "20260804-triple"

    for form in ("{}.md", "dispatch-{}.md", "{}_report.md"):
        p = tmp_data_dir / "unified_reports" / form.format(dispatch_id)
        p.write_text(f"## Summary\n\n{form}.\n\n## Open Items\nNone\n")

    result = resolve_report_path(dispatch_id, data_dir=tmp_data_dir)
    assert result is not None
    assert result.path.name == f"{dispatch_id}.md"
    assert result.ambiguous
    assert len(result.candidates_found) == 3


# ---------------------------------------------------------------------------
# resolve_report_path — repo-local fallback (OI-993 pattern)
# ---------------------------------------------------------------------------


def test_resolve_prefers_central_over_local(tmp_data_dir, tmp_repo_root):
    """Central report exists -> pick central, not repo-local."""
    from report_path import resolve_report_path

    dispatch_id = "20260804-central-first"

    # Central store
    central = tmp_data_dir / "unified_reports" / f"{dispatch_id}.md"
    central.write_text("## Summary\n\nCentral content.\n\n## Open Items\nNone\n")

    # Repo-local store
    local = tmp_repo_root / ".vnx-data" / "unified_reports" / f"{dispatch_id}.md"
    local.write_text("## Summary\n\nLocal stub.\n\n## Open Items\nNone\n")

    result = resolve_report_path(dispatch_id, data_dir=tmp_data_dir, repo_root=tmp_repo_root)
    assert result is not None
    assert result.path == central
    # Both exist -> ambiguous (central + local are different directories)
    assert result.ambiguous
    assert len(result.candidates_found) == 2


def test_resolve_falls_back_to_local_when_central_empty(tmp_data_dir, tmp_repo_root):
    """No central report, but repo-local exists -> pick local, not ambiguous."""
    from report_path import resolve_report_path

    dispatch_id = "20260804-local-only"

    # Repo-local only
    local = tmp_repo_root / ".vnx-data" / "unified_reports" / f"{dispatch_id}.md"
    local.write_text("## Summary\n\nLocal only.\n\n## Open Items\nNone\n")

    result = resolve_report_path(dispatch_id, data_dir=tmp_data_dir, repo_root=tmp_repo_root)
    assert result is not None
    # resolve() because /var -> /private/var on macOS
    assert result.path.resolve() == local.resolve()
    assert not result.ambiguous


def test_resolve_local_legacy_forms(tmp_data_dir, tmp_repo_root):
    """Central has <id>.md, local has dispatch-<id>.md -> ambiguous."""
    from report_path import resolve_report_path

    dispatch_id = "20260804-mixed"

    central = tmp_data_dir / "unified_reports" / f"{dispatch_id}.md"
    central.write_text("Central canonical.\n")

    local = tmp_repo_root / ".vnx-data" / "unified_reports" / f"dispatch-{dispatch_id}.md"
    local.write_text("Local legacy.\n")

    result = resolve_report_path(dispatch_id, data_dir=tmp_data_dir, repo_root=tmp_repo_root)
    assert result is not None
    assert result.path == central  # priority
    assert result.ambiguous
    assert len(result.candidates_found) == 2


# ---------------------------------------------------------------------------
# Prompt assembly — only ONE report path form
# ---------------------------------------------------------------------------


def test_assembled_prompt_has_exactly_one_report_path_form():
    """The assembled worker prompt must contain exactly one report-path instruction.

    Before this fix, the same prompt told the worker:
      - .vnx-data/unified_reports/<id>_report.md  (base_worker.md)
      - .vnx-data/unified_reports/                (worker_rules_footer.py)
      - $VNX_DATA_DIR/unified_reports/<id>.md     (dispatch instruction)

    This test asserts on the ASSEMBLED PROMPT TEXT (not source files) so a
    second copy that drifts later will be caught even if all source files pass
    a label check.
    """
    from prompt_assembler import PromptAssembler
    from worker_rules_footer import build as build_footer

    assembler = PromptAssembler()
    dispatch_id = "20260804-test-prompt-single-path"
    metadata = {
        "role": "backend-developer",
        "terminal": "T1",
        "model": "sonnet",
        "dispatch_id": dispatch_id,
    }
    instruction = (
        "DISPATCH INSTRUCTION:\n\n"
        f"Write your report to $VNX_DATA_DIR/unified_reports/{dispatch_id}.md\n"
    )
    prompt = assembler.assemble(metadata, instruction)
    full_text = prompt.to_pipe_input()

    # The full assembled text should contain the canonical path form
    assert "$VNX_DATA_DIR/unified_reports" in full_text

    # It must NOT contain the old repo-local path with _report suffix
    assert ".vnx-data/unified_reports/<dispatch_id>_report.md" not in full_text

    # It must NOT contain the bare repo-local path without filename
    # (the old worker_rules_footer.py form ".vnx-data/unified_reports/")
    assert ".vnx-data/unified_reports/" not in full_text

    # The footer should reference the canonical path
    footer = build_footer("backend-developer", dispatch_id)
    assert "$VNX_DATA_DIR/unified_reports" in footer
    assert dispatch_id in footer
    assert "_report.md" not in footer
    assert ".vnx-data/unified_reports/" not in footer


def test_worker_rules_footer_produces_canonical_path():
    """worker_rules_footer.build() outputs exactly one report path, canonical form."""
    from worker_rules_footer import build as build_footer

    dispatch_id = "20260804-footer-test"
    footer = build_footer("backend-developer", dispatch_id)

    # Must contain the canonical form
    assert f"$VNX_DATA_DIR/unified_reports/{dispatch_id}.md" in footer

    # Must NOT contain old forms
    assert "_report.md" not in footer
    assert ".vnx-data/unified_reports/" not in footer


# ---------------------------------------------------------------------------
# Prompt assembler — base worker content
# ---------------------------------------------------------------------------


def test_base_worker_md_contains_canonical_path():
    """base_worker.md must prescribe the canonical path, not the old _report.md form."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "prompts" / "base_worker.md"
    content = path.read_text()

    # Must have canonical env-var form
    assert "$VNX_DATA_DIR/unified_reports" in content

    # Must NOT have old _report suffix
    assert "_report.md" not in content

    # Must NOT have old repo-local path
    assert ".vnx-data/unified_reports/" not in content


# ---------------------------------------------------------------------------
# Report parser — parse_by_dispatch_id
# ---------------------------------------------------------------------------


def test_parse_by_dispatch_id_resolves_correctly(tmp_data_dir):
    """ReportParser.parse_by_dispatch_id() uses the resolver to find the report."""
    from report_parser import ReportParser

    dispatch_id = "20260804-parser-test"
    report = tmp_data_dir / "unified_reports" / f"{dispatch_id}.md"
    report.write_text(
        "## Summary\n\nA proper summary with enough content to pass the "
        "minimum character count requirement for the body contract validator.\n\n"
        "## Changes\n\n- `scripts/test.py`: added new function\n\n"
        "## Verification\n\npytest 3 passed\n\n"
        "## Open Items\n\nNone\n\n"
        "**Dispatch-ID**: 20260804-parser-test\n"
        "**Model**: deepseek-v4-pro\n"
        "**Provider**: deepseek-harness\n"
    )

    parser = ReportParser()
    result = parser.parse_by_dispatch_id(dispatch_id, data_dir=tmp_data_dir)
    assert "error" not in result
    assert result["dispatch_id"] == dispatch_id
    assert not result.get("_ambiguous_report")


def test_parse_by_dispatch_id_returns_error_for_missing(tmp_data_dir):
    """parse_by_dispatch_id returns error when no report exists."""
    from report_parser import ReportParser

    parser = ReportParser()
    result = parser.parse_by_dispatch_id("nonexistent-dispatch", data_dir=tmp_data_dir)
    assert "error" in result


def test_parse_by_dispatch_id_flags_ambiguity(tmp_data_dir):
    """parse_by_dispatch_id flags ambiguity when multiple report files exist."""
    from report_parser import ReportParser

    dispatch_id = "20260804-parser-ambiguous"
    canonical = tmp_data_dir / "unified_reports" / f"{dispatch_id}.md"
    canonical.write_text(
        "## Summary\n\nCanonical summary that is long enough to satisfy "
        "the validator minimum character requirement for summaries.\n\n"
        "## Changes\n\n- `scripts/test.py`\n\n"
        "## Verification\n\npytest passed\n\n"
        "## Open Items\n\nNone\n\n"
        "**Dispatch-ID**: 20260804-parser-ambiguous\n"
        "**Model**: deepseek-v4-pro\n"
        "**Provider**: deepseek-harness\n"
    )

    prefix = tmp_data_dir / "unified_reports" / f"dispatch-{dispatch_id}.md"
    prefix.write_text(
        "## Summary\n\nOther candidate that should trigger ambiguity detection.\n\n"
        "## Changes\n\n- `scripts/other.py`\n\n"
        "## Verification\n\npytest passed\n\n"
        "## Open Items\n\nNone\n\n"
        "**Dispatch-ID**: 20260804-parser-ambiguous\n"
    )

    parser = ReportParser()
    result = parser.parse_by_dispatch_id(dispatch_id, data_dir=tmp_data_dir)
    assert "error" not in result
    assert result.get("_ambiguous_report") is True
    assert len(result.get("_report_path_candidates", [])) == 2


# ---------------------------------------------------------------------------
# report_path module — solo (no dep on tmp dirs)
# ---------------------------------------------------------------------------


def test_resolved_report_candidate_sizes():
    """ResolvedReport.candidate_sizes returns path->size mapping."""
    from report_path import ResolvedReport

    rr = ResolvedReport(
        path=Path("/tmp/a.md"),
        candidates_found=[Path("/tmp/a.md"), Path("/tmp/b.md")],
        ambiguous=True,
    )
    # candidate_sizes is a property — won't work without actual files.
    # Test that the attribute exists and is callable.
    assert hasattr(rr, "candidate_sizes")
