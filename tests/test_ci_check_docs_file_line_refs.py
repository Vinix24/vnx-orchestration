#!/usr/bin/env python3
"""Tests for scripts/ci/check_docs_file_line_refs.py (OI-1107).

The check fails CI when a ``docs/`` ``file.py:123`` citation drifts from the
tree.  These tests pin the three behaviours that make it safe to run on every
merge: (1) it resolves the three citation forms the docs actually use, (2) it
ignores bare filenames and code-fence placeholders, and (3) it scopes itself
to living docs so frozen point-in-time artifacts never false-alarm.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent

sys.path.insert(0, str(VNX_ROOT / "scripts" / "ci"))

import check_docs_file_line_refs as check  # noqa: E402


# ---------------------------------------------------------------------------
# resolve_ref — the three citation forms docs use
# ---------------------------------------------------------------------------


def test_resolve_ref_exact_path() -> None:
    paths = ["scripts/lib/foo.py", "scripts/lib/bar.py"]
    status, resolved = check.resolve_ref("scripts/lib/foo.py", paths)
    assert status == "ok"
    assert resolved == "scripts/lib/foo.py"


def test_resolve_ref_basename_unique() -> None:
    paths = ["scripts/lib/foo.py", "tests/test_foo.py"]
    status, resolved = check.resolve_ref("foo.py", paths)
    assert status == "ok"
    assert resolved == "scripts/lib/foo.py"


def test_resolve_ref_suffix_path() -> None:
    # Cited relative to scripts/lib/ (e.g. append_receipt_internals/payload.py).
    paths = ["scripts/lib/append_receipt_internals/payload.py"]
    status, resolved = check.resolve_ref("append_receipt_internals/payload.py", paths)
    assert status == "ok"
    assert resolved == "scripts/lib/append_receipt_internals/payload.py"


def test_resolve_ref_not_found() -> None:
    status, _ = check.resolve_ref("missing.py", ["scripts/lib/real.py"])
    assert status == "not_found"


def test_resolve_ref_ambiguous() -> None:
    paths = ["a/foo.py", "b/foo.py"]
    status, _ = check.resolve_ref("foo.py", paths)
    assert status == "ambiguous"


def test_resolve_ref_hidden_dir_path() -> None:
    # A leading-dot citation resolves exactly, and does not collide with the
    # same-named template under templates/init/.
    paths = [".github/workflows/attestation-gate.yml", "templates/init/attestation-gate.yml"]
    status, resolved = check.resolve_ref(".github/workflows/attestation-gate.yml", paths)
    assert status == "ok"
    assert resolved == ".github/workflows/attestation-gate.yml"


# ---------------------------------------------------------------------------
# scan_markdown — what is a citation, and what is not
# ---------------------------------------------------------------------------


def test_scan_markdown_finds_inline_refs() -> None:
    text = "see `dispatch_cli.py:1700` and `foo.py:120-130`"
    cites = [(f, l) for _, f, l in check.scan_markdown(text)]
    assert ("dispatch_cli.py", "1700") in cites
    assert ("foo.py", "120-130") in cites


def test_scan_markdown_skips_fenced_blocks() -> None:
    # A schema example inside a code fence is a placeholder, not a citation.
    text = (
        "```json\n"
        '{"ref": "scripts/lib/foo.py:10-20"}\n'
        "```\n"
        "real `scripts/lib/bar.py:5`\n"
    )
    cites = [(f, l) for _, f, l in check.scan_markdown(text)]
    assert cites == [("scripts/lib/bar.py", "5")]


def test_scan_markdown_ignores_bare_filename() -> None:
    text = "open `scripts/lib/foo.py` first"
    assert check.scan_markdown(text) == []


def test_scan_markdown_hidden_dir_citation() -> None:
    text = "see `.github/workflows/attestation-gate.yml:55-67`"
    files = [f for _, f, _ in check.scan_markdown(text)]
    assert ".github/workflows/attestation-gate.yml" in files
    # The leading dot must be preserved, not misread as a dot-less path.
    assert "github/workflows/attestation-gate.yml" not in files


# ---------------------------------------------------------------------------
# is_live_doc — the frozen-doc carve-out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("lane-conformity-matrix.md", True),
        ("governance/ATTESTATION_ENFORCEMENT.md", True),
        ("core/DISPATCH_RULES.md", True),
        ("_archive/old.md", False),
        ("examples/example_headless_research.md", False),
        ("investigations/20260801-oi-triage.md", False),
        ("governance/decisions/ADR-035.md", False),
        # Historical proposals/design notes citing a dead architecture — frozen.
        ("internal/plans/VNX_STATE_SIMPLIFICATION_PROPOSAL.md", False),
        ("internal/intelligence/INTELLIGENCE_INJECTION_V1.1.md", False),
    ],
)
def test_is_live_doc(rel: str, expected: bool) -> None:
    assert check.is_live_doc(rel) is expected


# ---------------------------------------------------------------------------
# check_ref / check_docs — drift detection
# ---------------------------------------------------------------------------


def test_check_ref_out_of_bounds(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("a\nb\nc\n", encoding="utf-8")
    violation = check.check_ref("x.py", "10", tmp_path, ["x.py"])
    assert violation is not None
    assert "out of bounds" in violation


def test_check_ref_in_bounds(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("a\nb\nc\n", encoding="utf-8")
    assert check.check_ref("x.py", "2-3", tmp_path, ["x.py"]) is None


def test_check_ref_reversed_range_flagged(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("a\nb\nc\n", encoding="utf-8")
    violation = check.check_ref("x.py", "3-1", tmp_path, ["x.py"])
    assert violation is not None


def test_check_docs_planted_drift_fails(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "living.md").write_text("see `x.py:999`\n", encoding="utf-8")
    (tmp_path / "x.py").write_text("a\n", encoding="utf-8")
    violations = check.check_docs(docs, tmp_path, ["x.py", "docs/living.md"])
    assert any("out of bounds" in v for v in violations)


def test_check_docs_skips_frozen_dirs(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    (docs / "investigations").mkdir(parents=True)
    (docs / "investigations" / "frozen.md").write_text("see `x.py:999`\n", encoding="utf-8")
    (tmp_path / "x.py").write_text("a\n", encoding="utf-8")
    # The frozen dir is tracked, but still skipped: is_live_doc carves it out.
    assert check.check_docs(docs, tmp_path, ["x.py", "docs/investigations/frozen.md"]) == []


def test_check_docs_skips_untracked_docs(tmp_path: Path) -> None:
    # A gitignored local-only doc (absent from the tracked list) must not be
    # scanned: it never ships, and its citations describe a local architecture.
    docs = tmp_path / "docs"
    (docs / "internal").mkdir(parents=True)
    (docs / "internal" / "local-only.md").write_text("see `x.py:999`\n", encoding="utf-8")
    (tmp_path / "x.py").write_text("a\n", encoding="utf-8")
    assert check.check_docs(docs, tmp_path, ["x.py"]) == []


# ---------------------------------------------------------------------------
# integration — the live tree is clean (and the check actually runs it)
# ---------------------------------------------------------------------------


def test_current_tree_passes() -> None:
    rc = check.main(["--root", str(VNX_ROOT)])
    assert rc == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
