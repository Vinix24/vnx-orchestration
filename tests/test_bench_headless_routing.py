"""Regression tests for lane_adapter.py HEADLESS_FORCED_MODELS routing.

Root cause (2026-06-05): models.yaml uses short aliases as model_arg
(e.g. "sonnet" for claude-sonnet-4-6). The original check was
`lane["model_arg"] in HEADLESS_FORCED_MODELS` — HEADLESS_FORCED_MODELS
stores full IDs like "claude-sonnet-4-6", so "sonnet" was never found and
the lane fell through to the interactive path (which hangs per #63390).

Fix: check `lane["id"] OR lane["model_arg"]` against the set.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmark" / "field-tests" / "runners"))

from lane_adapter import HEADLESS_FORCED_MODELS


def _headless_forced(lane: dict) -> bool:
    """Mirrors the routing predicate in lane_adapter.dispatch()."""
    return lane["id"] in HEADLESS_FORCED_MODELS or lane["model_arg"] in HEADLESS_FORCED_MODELS


# --- sonnet (the regression case) ---

def test_sonnet_short_alias_model_arg_not_in_set():
    """The old predicate would have missed this — model_arg 'sonnet' is not in the set."""
    assert "sonnet" not in HEADLESS_FORCED_MODELS


def test_sonnet_full_id_is_in_set():
    """lane['id'] carries the full name; it must be in HEADLESS_FORCED_MODELS."""
    assert "claude-sonnet-4-6" in HEADLESS_FORCED_MODELS


def test_sonnet_lane_routes_headless():
    """Combined predicate must route sonnet to headless despite short model_arg."""
    lane = {"id": "claude-sonnet-4-6", "model_arg": "sonnet", "provider": "claude"}
    assert _headless_forced(lane) is True


# --- opus-4-8 (existing headless lane, full alias) ---

def test_opus_4_8_routes_headless():
    lane = {"id": "claude-opus-4-8", "model_arg": "claude-opus-4-8", "provider": "claude"}
    assert _headless_forced(lane) is True


# --- haiku (interactive lane — must NOT be headless-forced) ---

def test_haiku_not_headless_forced():
    lane = {"id": "claude-haiku-4-5", "model_arg": "haiku", "provider": "claude"}
    assert _headless_forced(lane) is False
