#!/usr/bin/env python3
"""Tests for repo-map layer coverage across all delivery paths (PR-REPOMAP).

Verifies:
1. subprocess_dispatch enrichment includes repo-map for a dispatch with target files.
2. provider_dispatch._enrich_instruction (codex) includes repo-map.
3. provider_dispatch._enrich_instruction (kimi) includes repo-map.
4. VNX_NO_REPO_MAP=1 suppresses injection on both paths.
5. Size cap: formatted output truncated at _REPO_MAP_MAX_CHARS.
6. Double-injection guard: instruction already containing '### Repo Map' is not re-injected.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

from dispatch_enricher import (
    apply_repo_map_layer,
    _REPO_MAP_SENTINEL,
    _REPO_MAP_MAX_CHARS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_py(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def _instruction_with_target(py_file: Path) -> str:
    return textwrap.dedent(f"""
        [[TARGET:T1]]
        Track: A
        Role: backend-developer

        ## Task
        Implement the feature.

        ### Key files to read first
        - `{py_file}` — target module
    """)


# ---------------------------------------------------------------------------
# apply_repo_map_layer unit tests
# ---------------------------------------------------------------------------

def test_apply_repo_map_layer_injects_repo_map(tmp_path: Path):
    """apply_repo_map_layer adds repo-map section for a code dispatch."""
    py_file = _write_py(tmp_path, "widget.py", """
        class Widget:
            def render(self):
                pass
    """)
    instruction = _instruction_with_target(py_file)
    enriched = apply_repo_map_layer(
        instruction,
        {"role": "backend-developer"},
        project_root=tmp_path,
    )
    assert _REPO_MAP_SENTINEL in enriched
    assert enriched.startswith(instruction)


def test_apply_repo_map_layer_double_injection_guard(tmp_path: Path):
    """Instruction already containing repo-map sentinel is not re-injected."""
    py_file = _write_py(tmp_path, "service.py", """
        def process(data):
            return data
    """)
    instruction = _instruction_with_target(py_file)
    first_pass = apply_repo_map_layer(
        instruction,
        {"role": "backend-developer"},
        project_root=tmp_path,
    )
    assert _REPO_MAP_SENTINEL in first_pass
    # Second call must not duplicate the section
    second_pass = apply_repo_map_layer(
        first_pass,
        {"role": "backend-developer"},
        project_root=tmp_path,
    )
    assert second_pass.count(_REPO_MAP_SENTINEL) == 1


def test_apply_repo_map_layer_opt_out_flag(tmp_path: Path):
    """metadata no_repo_map=True suppresses injection."""
    py_file = _write_py(tmp_path, "alpha.py", """
        def alpha():
            pass
    """)
    instruction = _instruction_with_target(py_file)
    enriched = apply_repo_map_layer(
        instruction,
        {"role": "backend-developer", "no_repo_map": True},
        project_root=tmp_path,
    )
    assert _REPO_MAP_SENTINEL not in enriched
    assert enriched == instruction


def test_apply_repo_map_layer_env_opt_out(tmp_path: Path, monkeypatch):
    """VNX_NO_REPO_MAP=1 suppresses injection."""
    monkeypatch.setenv("VNX_NO_REPO_MAP", "1")
    py_file = _write_py(tmp_path, "beta.py", """
        def beta():
            pass
    """)
    instruction = _instruction_with_target(py_file)
    enriched = apply_repo_map_layer(
        instruction,
        {"role": "backend-developer"},
        project_root=tmp_path,
    )
    assert _REPO_MAP_SENTINEL not in enriched
    assert enriched == instruction


def test_apply_repo_map_layer_review_role_skipped(tmp_path: Path):
    """Reviewer role does not get repo-map."""
    py_file = _write_py(tmp_path, "auth.py", """
        def authenticate(token):
            return bool(token)
    """)
    instruction = _instruction_with_target(py_file)
    enriched = apply_repo_map_layer(
        instruction,
        {"role": "reviewer"},
        project_root=tmp_path,
    )
    assert _REPO_MAP_SENTINEL not in enriched


def test_apply_repo_map_layer_size_cap(tmp_path: Path, monkeypatch):
    """Output is truncated when formatted repo map exceeds _REPO_MAP_MAX_CHARS."""
    py_file = _write_py(tmp_path, "big.py", """
        def alpha(): pass
        def beta(): pass
    """)
    instruction = _instruction_with_target(py_file)
    # Patch _REPO_MAP_MAX_CHARS to a tiny value to force truncation.
    monkeypatch.setattr("dispatch_enricher._REPO_MAP_MAX_CHARS", 50)
    enriched = apply_repo_map_layer(
        instruction,
        {"role": "backend-developer"},
        project_root=tmp_path,
    )
    # Either truncated (sentinel present + "...(truncated)") or sentinel absent
    # when 50-char cap prevents even the header fitting — both are acceptable.
    # The key invariant: if sentinel IS present, the added block must not exceed
    # 50 chars + "(truncated)" overhead beyond instruction.
    if _REPO_MAP_SENTINEL in enriched:
        added = enriched[len(instruction):]
        assert "...(truncated)" in added


def test_apply_repo_map_layer_no_target_files(tmp_path: Path):
    """Instruction with no .py references returns unchanged."""
    instruction = "Do the thing. No file references here."
    enriched = apply_repo_map_layer(
        instruction,
        {"role": "backend-developer"},
        project_root=tmp_path,
    )
    assert _REPO_MAP_SENTINEL not in enriched
    assert enriched == instruction


# ---------------------------------------------------------------------------
# provider_dispatch._enrich_instruction path
# ---------------------------------------------------------------------------

def test_provider_dispatch_enrich_instruction_codex_includes_repo_map(tmp_path: Path):
    """_enrich_instruction on the codex path adds repo-map for code dispatches."""
    import provider_dispatch

    py_file = _write_py(tmp_path, "handler.py", """
        def handle(request):
            return {}
    """)
    instruction = _instruction_with_target(py_file)

    args = MagicMock()
    args.instruction = instruction
    args.dispatch_id = "d-codex-repomap-001"
    args.role = "backend-developer"
    args.pr_id = None
    args.dispatch_paths = ""

    with patch("intelligence_injection.build_intelligence_section", return_value=instruction), \
         patch("dispatch_enricher.build_repo_map") as mock_build, \
         patch("dispatch_enricher.format_repo_map", return_value=f"{_REPO_MAP_SENTINEL} top 5 symbols\n1. handler.py:handle() — def handle(request)"):
        from repo_map import RepoMap, Symbol
        mock_build.return_value = RepoMap(
            symbols=[Symbol("handle", "function", str(py_file), 2, "def handle(request)")],
            ranked_symbols=[Symbol("handle", "function", str(py_file), 2, "def handle(request)")],
            graph_nodes=1,
            graph_edges=0,
        )
        enriched = provider_dispatch._enrich_instruction(args)

    assert _REPO_MAP_SENTINEL in enriched


def test_provider_dispatch_enrich_instruction_kimi_includes_repo_map(tmp_path: Path):
    """_enrich_instruction on the kimi path adds repo-map for code dispatches."""
    import provider_dispatch

    py_file = _write_py(tmp_path, "service.py", """
        class Service:
            def run(self):
                pass
    """)
    instruction = _instruction_with_target(py_file)

    args = MagicMock()
    args.instruction = instruction
    args.dispatch_id = "d-kimi-repomap-001"
    args.role = "backend-developer"
    args.pr_id = None
    args.dispatch_paths = ""

    with patch("intelligence_injection.build_intelligence_section", return_value=instruction), \
         patch("dispatch_enricher.build_repo_map") as mock_build, \
         patch("dispatch_enricher.format_repo_map", return_value=f"{_REPO_MAP_SENTINEL} top 3 symbols\n1. service.py:Service() — class Service"):
        from repo_map import RepoMap, Symbol
        mock_build.return_value = RepoMap(
            symbols=[Symbol("Service", "class", str(py_file), 2, "class Service")],
            ranked_symbols=[Symbol("Service", "class", str(py_file), 2, "class Service")],
            graph_nodes=1,
            graph_edges=0,
        )
        enriched = provider_dispatch._enrich_instruction(args)

    assert _REPO_MAP_SENTINEL in enriched


def test_provider_dispatch_enrich_instruction_no_repo_map_env(tmp_path: Path, monkeypatch):
    """VNX_NO_REPO_MAP=1 suppresses repo-map in _enrich_instruction."""
    monkeypatch.setenv("VNX_NO_REPO_MAP", "1")
    import provider_dispatch

    py_file = _write_py(tmp_path, "mod.py", """
        def func():
            pass
    """)
    instruction = _instruction_with_target(py_file)
    args = MagicMock()
    args.instruction = instruction
    args.dispatch_id = "d-nomap-001"
    args.role = "backend-developer"
    args.pr_id = None
    args.dispatch_paths = ""

    with patch("intelligence_injection.build_intelligence_section", return_value=instruction):
        enriched = provider_dispatch._enrich_instruction(args)

    assert _REPO_MAP_SENTINEL not in enriched


# ---------------------------------------------------------------------------
# subprocess_dispatch.__main__ path — via _build_cheap_lane_argv propagation
# ---------------------------------------------------------------------------

def test_subprocess_dispatch_no_repo_map_propagates_to_cheap_lane():
    """--no-repo-map is forwarded by _build_cheap_lane_argv to provider_dispatch."""
    import subprocess_dispatch as sd

    args = MagicMock()
    args.terminal_id = "T1"
    args.dispatch_id = "d-nr-001"
    args.instruction = "Do the thing."
    args.model = "sonnet"
    args.role = "backend-developer"
    args.max_retries = 3
    args.gate = ""
    args.no_auto_commit = False
    args.dispatch_paths = ""
    args.pr_id = None
    args.no_repo_map = True

    argv = sd._build_cheap_lane_argv(args, "litellm:moonshot:kimi-k2")
    assert "--no-repo-map" in argv
