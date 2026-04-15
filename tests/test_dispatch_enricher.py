#!/usr/bin/env python3
"""Tests for DispatchEnricher — dispatch instruction enrichment pipeline."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

SCRIPTS_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

from dispatch_enricher import DispatchEnricher, extract_target_files  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — minimal Python files for real repo_map extraction
# ---------------------------------------------------------------------------

def _write_py(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# extract_target_files
# ---------------------------------------------------------------------------

def test_extract_target_files_from_key_files_section(tmp_path: Path):
    instruction = textwrap.dedent("""
        ## Instruction
        Do the thing.

        ### Key files to read first
        - `scripts/lib/repo_map.py` — the repo map builder
        - `scripts/lib/headless_dispatch_daemon.py` — dispatch flow

        ### Success criteria
        - It works.
    """)
    files = extract_target_files(instruction, {})
    assert "scripts/lib/repo_map.py" in files
    assert "scripts/lib/headless_dispatch_daemon.py" in files


def test_extract_target_files_from_metadata_context_files(tmp_path: Path):
    files = extract_target_files("No key-files section here.", {
        "context_files": ["scripts/lib/foo.py", "scripts/lib/bar.py"],
    })
    assert "scripts/lib/foo.py" in files
    assert "scripts/lib/bar.py" in files


def test_extract_target_files_fallback_to_backtick_paths():
    instruction = "Read `scripts/lib/some_module.py` and `scripts/lib/other.py`."
    files = extract_target_files(instruction, {})
    assert "scripts/lib/some_module.py" in files
    assert "scripts/lib/other.py" in files


def test_extract_target_files_ignores_non_python():
    instruction = textwrap.dedent("""
        ### Key files to read first
        - `scripts/lib/foo.py` — python
        - `scripts/run.sh` — shell (should be ignored)
        - `Makefile` — ignored
    """)
    files = extract_target_files(instruction, {})
    assert files == ["scripts/lib/foo.py"]


# ---------------------------------------------------------------------------
# test_repo_map_injected_for_code_dispatch
# ---------------------------------------------------------------------------

def test_repo_map_injected_for_code_dispatch(tmp_path: Path):
    """Code dispatch (role=backend-developer) gets repo map appended."""
    py_file = _write_py(tmp_path, "widget.py", """
        class Widget:
            def render(self):
                pass

            def update(self, data):
                pass
    """)

    instruction = textwrap.dedent(f"""
        [[TARGET:T1]]
        Track: A
        Role: backend-developer

        ## Instruction
        Do the thing.

        ### Key files to read first
        - `{py_file.name}` — widget module
    """)

    enricher = DispatchEnricher()
    enriched = enricher.enrich(instruction, {
        "role": "backend-developer",
        "track": "A",
        "gate": "f55-pr2",
        "no_repo_map": False,
        "project_root": str(tmp_path),
    })

    assert "### Repo Map" in enriched
    assert enriched.startswith(instruction), "Original instruction must be preserved verbatim"


# ---------------------------------------------------------------------------
# test_repo_map_skipped_for_research_dispatch
# ---------------------------------------------------------------------------

def test_repo_map_skipped_for_research_dispatch(tmp_path: Path):
    """Review dispatch (role=reviewer, track=C) must NOT receive a repo map."""
    py_file = _write_py(tmp_path, "service.py", """
        def process(data):
            return data
    """)

    instruction = textwrap.dedent(f"""
        [[TARGET:T3]]
        Track: C
        Role: reviewer

        ## Instruction
        Review this PR.

        ### Key files to read first
        - `{py_file.name}` — the service
    """)

    enricher = DispatchEnricher()
    enriched = enricher.enrich(instruction, {
        "role": "reviewer",
        "track": "C",
        "gate": "f55-pr2-review",
        "no_repo_map": False,
        "project_root": str(tmp_path),
    })

    assert "### Repo Map" not in enriched
    assert enriched == instruction


def test_repo_map_skipped_when_no_repo_map_flag_set(tmp_path: Path):
    """Explicit no_repo_map=True suppresses injection regardless of role."""
    py_file = _write_py(tmp_path, "alpha.py", """
        def alpha():
            pass
    """)

    instruction = textwrap.dedent(f"""
        [[TARGET:T1]]
        Track: A
        Role: backend-developer

        ### Key files to read first
        - `{py_file.name}`
    """)

    enricher = DispatchEnricher()
    enriched = enricher.enrich(instruction, {
        "role": "backend-developer",
        "track": "A",
        "no_repo_map": True,
        "project_root": str(tmp_path),
    })

    assert "### Repo Map" not in enriched
    assert enriched == instruction


# ---------------------------------------------------------------------------
# test_enricher_preserves_original_instruction
# ---------------------------------------------------------------------------

def test_enricher_preserves_original_instruction(tmp_path: Path):
    """Enriched output starts with the exact original instruction text."""
    py_file = _write_py(tmp_path, "calc.py", """
        def add(a, b):
            return a + b

        def subtract(a, b):
            return a - b
    """)

    original = textwrap.dedent(f"""
        ## Task
        Implement the calculator.

        ### Key files to read first
        - `{py_file.name}` — math module
    """)

    enricher = DispatchEnricher()
    enriched = enricher.enrich(original, {
        "role": "backend-developer",
        "track": "A",
        "project_root": str(tmp_path),
    })

    assert enriched.startswith(original), (
        "Original instruction text must appear verbatim at the start of enriched output"
    )
    # Enrichment section follows the original
    suffix = enriched[len(original):]
    assert suffix.startswith("\n\n### Repo Map")


# ---------------------------------------------------------------------------
# test_enricher_extension_point
# ---------------------------------------------------------------------------

def test_enricher_extension_point():
    """DispatchEnricher.enrich() accepts instruction + metadata and returns str."""
    enricher = DispatchEnricher()

    # No target files → no repo map, but must return a string cleanly
    result = enricher.enrich("Plain research instruction.", {
        "role": "backend-developer",
        "track": "A",
        "no_repo_map": False,
    })
    assert isinstance(result, str)
    # No files found → original unchanged
    assert result == "Plain research instruction."


def test_enricher_is_callable_with_all_optional_metadata_absent():
    """enrich() must work with a minimal metadata dict (no crash)."""
    enricher = DispatchEnricher()
    result = enricher.enrich("Minimal dispatch.", {})
    assert isinstance(result, str)


def test_enricher_skips_repo_map_gracefully_when_files_missing(tmp_path: Path):
    """When referenced .py files do not exist, enricher skips without crashing."""
    instruction = textwrap.dedent("""
        ### Key files to read first
        - `scripts/lib/nonexistent_file_abc123.py` — does not exist
    """)

    enricher = DispatchEnricher()
    enriched = enricher.enrich(instruction, {
        "role": "backend-developer",
        "track": "A",
        "project_root": str(tmp_path),
    })

    # repo_map.build_repo_map returns empty symbols for missing files — no crash
    assert isinstance(enriched, str)


def test_enricher_security_engineer_skips_repo_map(tmp_path: Path):
    """security-engineer role is a review role — should skip repo map."""
    py_file = _write_py(tmp_path, "auth.py", """
        def authenticate(token):
            return bool(token)
    """)

    instruction = textwrap.dedent(f"""
        ### Key files to read first
        - `{py_file.name}`
    """)

    enricher = DispatchEnricher()
    enriched = enricher.enrich(instruction, {
        "role": "security-engineer",
        "track": "C",
        "project_root": str(tmp_path),
    })

    assert "### Repo Map" not in enriched
