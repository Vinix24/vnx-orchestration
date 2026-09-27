#!/usr/bin/env python3
"""The fallback gate stack of auto_gate_trigger is the default review stack.

When governance_enforcement.yaml names no mandatory gate, auto_gate_trigger
falls back to a default stack. That fallback used to seat gemini_review next to
codex_gate; with gemini gone as a reviewer the seat belongs to kimi_gate, the
second standing seat of VNX_DEFAULT_REVIEW_STACK (operator decision 2026-09-26).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import auto_gate_trigger
from config_registry import CONFIG_REGISTRY


def _registry_default_stack() -> list[str]:
    raw = CONFIG_REGISTRY["VNX_DEFAULT_REVIEW_STACK"].default
    return [item.strip() for item in raw.split(",") if item.strip()]


def test_fallback_stack_is_the_default_review_stack() -> None:
    assert auto_gate_trigger._DEFAULT_GATE_STACK == _registry_default_stack()


def test_fallback_stack_keeps_the_kimi_seat() -> None:
    assert "kimi_gate" in auto_gate_trigger._DEFAULT_GATE_STACK
    assert "gemini_review" not in auto_gate_trigger._DEFAULT_GATE_STACK


def test_no_enforcement_file_falls_back_to_the_default_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auto_gate_trigger, "_REPO_ROOT", tmp_path)
    assert auto_gate_trigger._load_required_gates() == _registry_default_stack()


def test_enforcement_file_without_a_mandatory_gate_falls_back_to_the_default_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("yaml")
    (tmp_path / ".vnx").mkdir()
    (tmp_path / ".vnx" / "governance_enforcement.yaml").write_text(
        "checks:\n"
        "  codex_gate_required:\n"
        "    level: 1\n"
        "  max_pr_lines:\n"
        "    level: 3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(auto_gate_trigger, "_REPO_ROOT", tmp_path)
    assert auto_gate_trigger._load_required_gates() == _registry_default_stack()


def test_unparseable_enforcement_file_falls_back_to_the_default_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".vnx").mkdir()
    (tmp_path / ".vnx" / "governance_enforcement.yaml").write_text("checks: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(auto_gate_trigger, "_REPO_ROOT", tmp_path)
    assert auto_gate_trigger._load_required_gates() == _registry_default_stack()


def test_level_2_requests_both_codex_and_kimi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kimi_gate_required is a peer of codex_gate_required: at level >= 2 both
    seats are requested, not codex alone (operator decision 2026-09-27)."""
    pytest.importorskip("yaml")
    (tmp_path / ".vnx").mkdir()
    (tmp_path / ".vnx" / "governance_enforcement.yaml").write_text(
        "checks:\n"
        "  codex_gate_required:\n"
        "    level: 2\n"
        "  kimi_gate_required:\n"
        "    level: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(auto_gate_trigger, "_REPO_ROOT", tmp_path)
    gates = auto_gate_trigger._load_required_gates()
    assert set(gates) == {"codex_gate", "kimi_gate"}


def test_kimi_gate_required_at_level_0_requests_codex_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project that dials kimi_gate_required off (level 0) keeps codex_gate
    as the sole required seat, instead of falling back to the default stack
    (which would silently re-add kimi)."""
    pytest.importorskip("yaml")
    (tmp_path / ".vnx").mkdir()
    (tmp_path / ".vnx" / "governance_enforcement.yaml").write_text(
        "checks:\n"
        "  codex_gate_required:\n"
        "    level: 2\n"
        "  kimi_gate_required:\n"
        "    level: 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(auto_gate_trigger, "_REPO_ROOT", tmp_path)
    assert auto_gate_trigger._load_required_gates() == ["codex_gate"]
