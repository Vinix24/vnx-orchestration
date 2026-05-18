"""Tests for the shared runtime_overrides module.

Verifies that apply_runtime_overrides is the single canonical implementation
and that both delivery.py and recovery.py import from it.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

# Ensure scripts/lib is on the path
_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from subprocess_dispatch_internals.runtime_overrides import apply_runtime_overrides


def test_no_overrides_returns_defaults(monkeypatch):
    """When no VNX_* env vars set, original values pass through unchanged."""
    monkeypatch.delenv("VNX_CHUNK_TIMEOUT", raising=False)
    monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)
    ct, td = apply_runtime_overrides(30.0, 600.0)
    assert ct == 30.0
    assert td == 600.0


def test_single_chunk_timeout_override(monkeypatch):
    """VNX_CHUNK_TIMEOUT overrides chunk_timeout, total_deadline unchanged."""
    monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "45.5")
    monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)
    ct, td = apply_runtime_overrides(30.0, 600.0)
    assert ct == 45.5
    assert td == 600.0


def test_single_total_deadline_override(monkeypatch):
    """VNX_TOTAL_DEADLINE overrides total_deadline, chunk_timeout unchanged."""
    monkeypatch.delenv("VNX_CHUNK_TIMEOUT", raising=False)
    monkeypatch.setenv("VNX_TOTAL_DEADLINE", "1200.0")
    ct, td = apply_runtime_overrides(30.0, 600.0)
    assert ct == 30.0
    assert td == 1200.0


def test_both_overrides_applied(monkeypatch):
    """Both env vars applied simultaneously."""
    monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "10.0")
    monkeypatch.setenv("VNX_TOTAL_DEADLINE", "300.0")
    ct, td = apply_runtime_overrides(30.0, 600.0)
    assert ct == 10.0
    assert td == 300.0


def test_invalid_value_silently_ignored(monkeypatch):
    """Non-float env var values are silently ignored; originals preserved."""
    monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "not-a-number")
    monkeypatch.setenv("VNX_TOTAL_DEADLINE", "also-bad")
    ct, td = apply_runtime_overrides(30.0, 600.0)
    assert ct == 30.0
    assert td == 600.0


def test_delivery_imports_shared_module():
    """delivery.py must import apply_runtime_overrides from runtime_overrides."""
    import subprocess_dispatch_internals.delivery as delivery_mod
    import subprocess_dispatch_internals.runtime_overrides as ro_mod
    # The name bound in delivery's module namespace must point to the shared function
    assert hasattr(delivery_mod, "apply_runtime_overrides")
    assert delivery_mod.apply_runtime_overrides is ro_mod.apply_runtime_overrides


def test_recovery_imports_shared_module():
    """recovery.py must import apply_runtime_overrides from runtime_overrides."""
    import subprocess_dispatch_internals.recovery as recovery_mod
    import subprocess_dispatch_internals.runtime_overrides as ro_mod
    assert hasattr(recovery_mod, "apply_runtime_overrides")
    assert recovery_mod.apply_runtime_overrides is ro_mod.apply_runtime_overrides


def test_no_local_definition_in_delivery():
    """delivery.py must NOT define its own _apply_runtime_overrides."""
    import subprocess_dispatch_internals.delivery as delivery_mod
    assert not hasattr(delivery_mod, "_apply_runtime_overrides"), (
        "delivery.py still defines _apply_runtime_overrides locally; remove it"
    )


def test_no_local_definition_in_recovery():
    """recovery.py must NOT define its own _apply_runtime_overrides."""
    import subprocess_dispatch_internals.recovery as recovery_mod
    assert not hasattr(recovery_mod, "_apply_runtime_overrides"), (
        "recovery.py still defines _apply_runtime_overrides locally; remove it"
    )
