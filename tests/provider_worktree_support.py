"""tests/provider_worktree_support.py — the provider-lane isolation seam, stubbed.

Isolation is default-on (OI-1090), so every provider_dispatch handler call —
claude, codex, gemini, litellm, kimi, glm-harness, and `main()` routing to them —
built and removed a REAL git worktree before doing any of the work these tests
assert on (receipt fields, prompts, report bodies). `grep -n worktree` in those
test files finds nothing: they never look at the checkout, they only pay for it.

Two seams, both patched while the stub is on:

``provider_dispatch._prepare_provider_workdir``
    Handed back a plain directory under ``tmp_path``, in the shape the real one
    returns on this base: a 2-tuple ``(isolation_worktree, worker_cwd)`` of
    ``Path``s. Both entries are the same directory while
    ``VNX_BENCH_SEED_MATERIALIZE`` is unset, which is the case here; benchmark
    seed materialization stays on its own path in tests/test_bench_worker_isolation.py.
``provider_dispatch._finish_provider_worktree``
    A no-op that records the call (``stub.finish``), so a test can assert the
    teardown is still wired to the dispatch.

The real path is NOT unpinned suite-wide. The fixture also installs a spy on
``dispatch_worktree_isolation.create_dispatch_worktree`` (and
``...remove_dispatch_worktree``), so a test that reaches the real seam fails its
``assert_not_called()`` no-worktree assertion instead of silently building a
checkout. Tests that must keep the real path opt out with
``@pytest.mark.real_provider_worktree``; the isolation behaviour itself is pinned
by tests/test_provider_dispatch_worktree_isolation.py::test_isolated_worktree_creates_and_removes.

Red run hook: ``VNX_PROVIDER_WORKTREE_STUB=0`` keeps the two handler seams real
(only the spy stays), so the no-worktree assertion fails exactly the way it does
on the pre-TSK-6 tree — the handler really calls ``create_dispatch_worktree`` —
without touching the operator's checkout.
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

REAL_WORKTREE_MARKER = "real_provider_worktree"
_STUB_ENV = "VNX_PROVIDER_WORKTREE_STUB"


@dataclass
class ProviderWorktreeStub:
    """What a test can assert on: the stub path, the teardown, the real seam."""

    worktree: Path
    finish: MagicMock
    create_worktree: Optional[MagicMock]
    prepared: Optional[MagicMock]


def _stub_enabled() -> bool:
    return os.environ.get(_STUB_ENV, "1") != "0"


@pytest.fixture(autouse=True)
def provider_worktree_stub(request, tmp_path):
    """Stub provider worktree creation for the whole module (opt out per test)."""
    worktree = tmp_path / "provider-worktree"
    worktree.mkdir()
    stub = ProviderWorktreeStub(
        worktree=worktree, finish=MagicMock(), create_worktree=None, prepared=None,
    )
    if request.node.get_closest_marker(REAL_WORKTREE_MARKER) is not None:
        yield stub
        return
    with ExitStack() as stack:
        stub.create_worktree = stack.enter_context(
            patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=worktree)
        )
        stack.enter_context(patch("dispatch_worktree_isolation.remove_dispatch_worktree"))
        if _stub_enabled():
            stub.prepared = stack.enter_context(
                patch(
                    "provider_dispatch._prepare_provider_workdir",
                    return_value=(worktree, worktree),
                )
            )
            stack.enter_context(
                patch("provider_dispatch._finish_provider_worktree", stub.finish)
            )
        yield stub
