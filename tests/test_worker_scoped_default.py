"""mcp-scoped-default: scoped worker-mode is the fabric default (14-08).

Per operator directive 2026-08-14, blanket ``--dangerously-skip-permissions``
is no longer the default: headless workers spawn scoped by default (empty
ambient MCP + ``acceptEdits`` + role allow-list), because worktree isolation
bounds the filesystem, not the network, and an MCP server talks to a service
outside the checkout.

This module tests the shared predicate, against the real
``worker_scoped_enabled`` (no mocked predicate):

  1. worker_scoped_enabled() is True with no env vars set
  2. the legacy falsey VNX_WORKER_SCOPED opt-out still disables it

The tmux lane's spawn-line tests that used to sit here (blanket skip needing both
opt-outs, MCP flag order, the import-fault fallback) went with the lane on
2026-09-18. The subprocess lane's spawn line is covered in
tests/test_worker_capability_scoping.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from worker_permissions import worker_scoped_enabled  # noqa: E402


class TestWorkerScopedEnabledDefault:
    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        monkeypatch.delenv("VNX_WORKER_BLANKET_SKIP", raising=False)
        assert worker_scoped_enabled() is True

    def test_legacy_falsey_opt_out_disables(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_BLANKET_SKIP", raising=False)
        monkeypatch.setenv("VNX_WORKER_SCOPED", "0")
        assert worker_scoped_enabled() is False
