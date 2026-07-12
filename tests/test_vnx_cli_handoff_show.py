#!/usr/bin/env python3
"""P3 regression: `vnx handoff show --logdir <path>` must not require a
resolvable project_id.

Background: `_show()` used to call `_resolve_project_id(args)` BEFORE
checking `args.logdir`, so inspecting an explicit/archived handoff.md
(possibly outside any VNX project) failed with an auto-resolution error even
though no project_id is needed to read a handoff by explicit path. The fix
defers `_resolve_project_id()` until it's actually needed: the DEFAULT
logdir fallback, or `--mark-ready` (which needs project_id to write the
.ready signal).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from vnx_cli.commands import handoff as handoff_mod  # noqa: E402

_HANDOFF_MD = """---
context: t0-rotation
project: some-project
date: 2026-07-12T00:00:00Z
branch: main
---

# T0 Context Rotation Handoff

## Waar we middenin zitten

Working tree clean on branch `main`.

## State

- Branch: `main`

## Next steps

No pending horizon items detected.
"""


def _args(**overrides: object) -> types.SimpleNamespace:
    base = dict(logdir=None, terminal="T0", mark_ready=False, rotation_id=None, project_id=None, project_dir=".")
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _unresolvable_project_id(_args: object) -> str:
    """Stand-in for the real `_resolve_project_id`, which calls
    `sys.exit(2)` when auto-resolution fails (no --project-id, no
    resolvable project). Raising SystemExit here reproduces that failure
    deterministically regardless of the ambient test environment's own
    project markers."""
    raise SystemExit(2)


class TestShowWithExplicitLogdirDefersProjectId:
    def test_succeeds_without_a_resolvable_project_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handoff_dir = tmp_path / "archived_handoff"
        handoff_dir.mkdir()
        (handoff_dir / "handoff.md").write_text(_HANDOFF_MD, encoding="utf-8")

        monkeypatch.setattr(handoff_mod, "_resolve_project_id", _unresolvable_project_id)

        result = handoff_mod._show(_args(logdir=str(handoff_dir)))
        assert result == 0

    def test_never_calls_resolve_project_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handoff_dir = tmp_path / "archived_handoff"
        handoff_dir.mkdir()
        (handoff_dir / "handoff.md").write_text(_HANDOFF_MD, encoding="utf-8")

        calls: list[object] = []
        monkeypatch.setattr(
            handoff_mod, "_resolve_project_id", lambda a: calls.append(a) or "should-not-happen",
        )

        result = handoff_mod._show(_args(logdir=str(handoff_dir)))
        assert result == 0
        assert calls == []


class TestShowStillResolvesProjectIdWhenNeeded:
    """The deferral must not turn into an outright removal — the default
    (no --logdir) path and --mark-ready still need project_id."""

    def test_no_logdir_still_resolves_project_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[object] = []

        def _fake_resolve(a: object) -> str:
            calls.append(a)
            return "fallback-project"

        monkeypatch.setattr(handoff_mod, "_resolve_project_id", _fake_resolve)
        monkeypatch.setattr(handoff_mod, "_resolve_logdir", lambda a, pid: tmp_path / "nope")

        result = handoff_mod._show(_args(logdir=None))
        assert result == 1  # no handoff.md at the fake logdir — but project_id WAS resolved
        assert len(calls) == 1

    def test_mark_ready_with_explicit_logdir_still_resolves_project_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handoff_dir = tmp_path / "archived_handoff"
        handoff_dir.mkdir()
        (handoff_dir / "handoff.md").write_text(_HANDOFF_MD, encoding="utf-8")

        calls: list[object] = []

        def _fake_resolve(a: object) -> str:
            calls.append(a)
            return "fallback-project"

        monkeypatch.setattr(handoff_mod, "_resolve_project_id", _fake_resolve)
        monkeypatch.setattr(handoff_mod, "_mark_ready", lambda a, pid: (calls.append(pid), 0)[1])

        result = handoff_mod._show(_args(logdir=str(handoff_dir), mark_ready=True))
        assert result == 0
        assert len(calls) == 2  # once for _resolve_project_id, once recording the pid passed to _mark_ready
        assert calls[1] == "fallback-project"
