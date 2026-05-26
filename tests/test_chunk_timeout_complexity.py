"""Tests for complexity-scaled chunk_timeout / total_deadline defaults.

Background: a subprocess dispatch was killed by the per-chunk timeout (300s)
while a worker was doing legitimate compute-heavy work without emitting a
tool-event for >300s. The fix scales the base timeouts by --complexity so
"high" dispatches get more headroom, while env overrides
(VNX_CHUNK_TIMEOUT / VNX_TOTAL_DEADLINE) retain top precedence.

Precedence (highest to lowest):
    env override  >  complexity-scaled default  >  function-signature base
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure scripts/lib is on the path
_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from subprocess_dispatch_internals.runtime_overrides import (  # noqa: E402
    apply_runtime_overrides,
    complexity_timeout_defaults,
)


# --- complexity-scaled defaults -------------------------------------------


def test_high_complexity_gets_more_headroom():
    """complexity=high yields chunk_timeout=600 / total_deadline=1800."""
    ct, td = complexity_timeout_defaults("high")
    assert ct == 600.0
    assert td == 1800.0


def test_medium_complexity_unchanged():
    """complexity=medium keeps the historical base defaults (300 / 900)."""
    ct, td = complexity_timeout_defaults("medium")
    assert ct == 300.0
    assert td == 900.0


def test_low_complexity_unchanged():
    """complexity=low keeps the historical base defaults (300 / 900)."""
    ct, td = complexity_timeout_defaults("low")
    assert ct == 300.0
    assert td == 900.0


def test_unknown_complexity_falls_back_to_base():
    """An unexpected --complexity value falls back to base, never below baseline."""
    ct, td = complexity_timeout_defaults("ludicrous")
    assert ct == 300.0
    assert td == 900.0


def test_none_complexity_falls_back_to_base():
    """None (no value) falls back to base defaults instead of crashing."""
    ct, td = complexity_timeout_defaults(None)  # type: ignore[arg-type]
    assert ct == 300.0
    assert td == 900.0


def test_complexity_is_case_insensitive():
    """Mixed-case complexity values resolve like their lowercase form."""
    ct, td = complexity_timeout_defaults("HIGH")
    assert ct == 600.0
    assert td == 1800.0


# --- precedence: env override wins over complexity-scaled default ----------


def test_env_chunk_timeout_wins_over_high_complexity(monkeypatch):
    """VNX_CHUNK_TIMEOUT overrides the complexity-scaled chunk_timeout.

    Mirrors the production call path: complexity_timeout_defaults seeds the
    base values, then apply_runtime_overrides runs downstream and env wins.
    """
    monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "1200.0")
    monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)
    base_ct, base_td = complexity_timeout_defaults("high")
    ct, td = apply_runtime_overrides(base_ct, base_td)
    assert ct == 1200.0  # env wins
    assert td == 1800.0  # complexity-scaled default retained


def test_env_total_deadline_wins_over_high_complexity(monkeypatch):
    """VNX_TOTAL_DEADLINE overrides the complexity-scaled total_deadline."""
    monkeypatch.delenv("VNX_CHUNK_TIMEOUT", raising=False)
    monkeypatch.setenv("VNX_TOTAL_DEADLINE", "3600.0")
    base_ct, base_td = complexity_timeout_defaults("high")
    ct, td = apply_runtime_overrides(base_ct, base_td)
    assert ct == 600.0  # complexity-scaled default retained
    assert td == 3600.0  # env wins


def test_env_wins_regardless_of_complexity(monkeypatch):
    """With both env vars set, the env values are used for every complexity."""
    monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "42.0")
    monkeypatch.setenv("VNX_TOTAL_DEADLINE", "84.0")
    for complexity in ("low", "medium", "high"):
        ct, td = apply_runtime_overrides(*complexity_timeout_defaults(complexity))
        assert ct == 42.0
        assert td == 84.0


def test_no_env_lets_complexity_scaled_value_pass_through(monkeypatch):
    """Without env overrides, the complexity-scaled defaults flow through unchanged."""
    monkeypatch.delenv("VNX_CHUNK_TIMEOUT", raising=False)
    monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)
    ct, td = apply_runtime_overrides(*complexity_timeout_defaults("high"))
    assert ct == 600.0
    assert td == 1800.0
