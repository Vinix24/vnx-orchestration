#!/usr/bin/env python3
"""terminal_lane.py — one rule for a terminal's dispatch transport (T-G1).

The worker terminals (T1-T3) always run on the headless subprocess lane: their
``VNX_ADAPTER_T{n}`` selector is gone, so no value (``tmux`` included) moves a
worker back. Every other terminal keeps the explicit opt-in (T0:
``VNX_ADAPTER_T0=subprocess``, until T-B2 removes it). Keeping the rule in one
dependency-free module stops the dispatcher-side readers from drifting apart.
"""
from __future__ import annotations

import os

WORKER_TERMINALS = frozenset({"T1", "T2", "T3"})


def is_worker_terminal(terminal_id: str) -> bool:
    """Return True for a worker terminal (T1-T3), which has no transport selector."""
    return terminal_id in WORKER_TERMINALS


def is_headless_terminal(terminal_id: str) -> bool:
    """Return True when the terminal delivers on the subprocess (headless) lane.

    Workers always are; any other terminal only on the exact value ``subprocess``.
    """
    if is_worker_terminal(terminal_id):
        return True
    return os.environ.get(f"VNX_ADAPTER_{terminal_id}", "").lower() == "subprocess"
