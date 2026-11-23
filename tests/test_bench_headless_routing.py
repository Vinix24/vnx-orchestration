"""Regression tests for lane_adapter.py HEADLESS_FORCED_MODELS routing.

HEADLESS_FORCED_MODELS was emptied on 2026-06-15 (1e31cf37e) and kept as a mechanism: a
model listed there takes the subprocess_dispatch route. The routing predicate (lane id OR
model_arg) is the contract; the tests pin it against the CURRENT set and against a model
added to the set, so they do not hard-code which models the fleet forces headless.

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


def _headless_forced(lane: dict, forced: set[str] | None = None) -> bool:
    """Mirrors the routing predicate in lane_adapter.dispatch()."""
    forced = HEADLESS_FORCED_MODELS if forced is None else forced
    return lane["id"] in forced or lane["model_arg"] in forced


# --- current register ---

def test_forced_set_is_empty_by_decision():
    """1e31cf37e emptied the set: no lane is forced onto the subprocess route today."""
    assert HEADLESS_FORCED_MODELS == set()


def test_sonnet_short_alias_model_arg_not_in_set():
    """The old predicate would have missed this — model_arg 'sonnet' is not a full id."""
    assert "sonnet" not in HEADLESS_FORCED_MODELS


def test_no_current_lane_is_forced_headless():
    for lane in (
        {"id": "claude-sonnet-4-6", "model_arg": "sonnet", "provider": "claude"},
        {"id": "claude-opus-4-8", "model_arg": "claude-opus-4-8", "provider": "claude"},
        {"id": "claude-haiku-4-5", "model_arg": "haiku", "provider": "claude"},
    ):
        assert _headless_forced(lane) is False


# --- the mechanism (lane id OR model_arg), with a model added to the set ---

def test_lane_id_in_set_routes_headless_despite_short_model_arg():
    lane = {"id": "claude-sonnet-4-6", "model_arg": "sonnet", "provider": "claude"}
    assert _headless_forced(lane, {"claude-sonnet-4-6"}) is True


def test_model_arg_in_set_routes_headless():
    lane = {"id": "other-id", "model_arg": "claude-opus-4-8", "provider": "claude"}
    assert _headless_forced(lane, {"claude-opus-4-8"}) is True


def test_lane_in_neither_field_is_not_forced():
    lane = {"id": "claude-haiku-4-5", "model_arg": "haiku", "provider": "claude"}
    assert _headless_forced(lane, {"claude-opus-4-8"}) is False
