#!/usr/bin/env python3
"""Tests for OI-1209: provider-lane ``VNX_WORKER_ROLE`` propagation.

The worker-scope PreToolUse enforcement hook
(``scripts/hooks/pretooluse_worker_scope_enforce.py``) reads its role from the
``VNX_WORKER_ROLE`` env var. The tmux lane already exports it; the provider lane
(kimi / deepseek-harness / glm-harness / codex / gemini / litellm) did not, so
every provider-lane worker resolved to the restrictive code-worker fallback
(``resolve_worker_profile: role is None ... is_fallback=True``) even when its
dispatch spec carried ``role=backend-developer``.

The fix exports the spec's role via ``provider_dispatch._worker_role_env`` and
threads it into each provider spawn's ``extra_env``, mirroring the tmux lane's
``if role: export`` contract. A missing role stays missing — nothing is exported
and the fallback stands.

OI-1635 (2026-09-05): ``_worker_role_env`` also stamps ``VNX_DATA_DIR`` /
``VNX_STATE_DIR`` / ``VNX_DATA_DIR_EXPLICIT=1`` onto the SAME env overlay — the
worker's dispatch instructions tell it to write its report to literal
``$VNX_DATA_DIR/unified_reports/<id>.md``, but nothing used to actually set that
var in the worker's own process, so the worker independently re-resolved it
(landing on the repo-local default instead of the central store the governance
emitter uses — see ``tests/test_report_store_split_brain.py`` for the
reproduction). The assertions below were updated from exact-dict-equality to
membership checks for this reason: the env dict now legitimately carries more
than just the role.

These tests exercise the argv/env assembly path DIRECTLY (no real spawn). The
dispatcher builds the worker argv with ``main``-branch code, so a worker running
on a feature branch is itself launched by the OLD spawn (OI-1201, wontfix): a
spawn-based test cannot observe this change. End-to-end verification is a
POST-MERGE measurement, not something this file claims: after merge, a
provider-lane dispatch with a role in its spec must no longer log
``resolve_worker_profile: role is None`` in the door log.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

REPO_ROOT = Path(__file__).resolve().parents[1]
PERMISSIONS_YAML = REPO_ROOT / ".vnx" / "worker_permissions.yaml"

import provider_dispatch as pd  # noqa: E402
from worker_permissions import resolve_worker_profile  # noqa: E402


class TestWorkerRoleEnvAssembly(unittest.TestCase):
    """The env-overlay builder follows the tmux-lane contract.

    A genuinely-set role in the spec is exported; a missing role is left missing
    (not empty, not a default). ``normalize_role`` also strips the empty sentinel
    and the canonical ``identity_unresolved`` marker so neither leaks into the
    worker env as a fabricated role.

    OI-1635: the overlay ALSO always carries VNX_DATA_DIR/VNX_STATE_DIR/
    VNX_DATA_DIR_EXPLICIT=1 (mirroring the tmux lane's completion-command
    env_prefix), independent of whether a role was set — so these assertions
    check role-key membership rather than exact dict equality.
    """

    def test_backend_developer_role_exported(self):
        env = pd._worker_role_env("backend-developer")
        self.assertEqual(env.get("VNX_WORKER_ROLE"), "backend-developer")
        self.assertEqual(env.get("VNX_DATA_DIR"), str(pd._resolve_data_dir()))
        self.assertEqual(env.get("VNX_DATA_DIR_EXPLICIT"), "1")

    def test_security_engineer_role_exported(self):
        # The value follows the spec — it is not a constant.
        env = pd._worker_role_env("security-engineer")
        self.assertEqual(env.get("VNX_WORKER_ROLE"), "security-engineer")
        self.assertEqual(env.get("VNX_DATA_DIR"), str(pd._resolve_data_dir()))

    def test_role_absent_when_none(self):
        env = pd._worker_role_env(None)
        self.assertNotIn("VNX_WORKER_ROLE", env)
        # Absent role does not suppress the data-dir stamp — that half of the
        # overlay is unconditional (OI-1635).
        self.assertEqual(env.get("VNX_DATA_DIR"), str(pd._resolve_data_dir()))

    def test_role_absent_when_empty_or_sentinel(self):
        # Not empty, not a default: an empty/sentinel role exports no
        # VNX_WORKER_ROLE key, so the hook keeps its existing fallback behavior.
        for role in ("", "   ", "identity_unresolved"):
            env = pd._worker_role_env(role)
            self.assertNotIn("VNX_WORKER_ROLE", env)


class TestHookResolvesRoleProfile(unittest.TestCase):
    """With ``VNX_WORKER_ROLE`` exported, ``resolve_worker_profile`` no longer
    lands on the restrictive fallback. This is the hook's own resolution path
    (``os.environ.get("VNX_WORKER_ROLE") or None`` → ``resolve_worker_profile``).
    """

    def test_backend_developer_resolves_real_profile(self):
        with patch.dict(os.environ, {"VNX_WORKER_ROLE": "backend-developer"}):
            role = os.environ.get("VNX_WORKER_ROLE") or None
            profile = resolve_worker_profile(role, yaml_path=PERMISSIONS_YAML)
        self.assertFalse(profile.is_fallback)
        self.assertEqual(profile.role, "backend-developer")

    def test_security_engineer_resolves_real_profile(self):
        with patch.dict(os.environ, {"VNX_WORKER_ROLE": "security-engineer"}):
            role = os.environ.get("VNX_WORKER_ROLE") or None
            profile = resolve_worker_profile(role, yaml_path=PERMISSIONS_YAML)
        self.assertFalse(profile.is_fallback)
        self.assertEqual(profile.role, "security-engineer")

    def test_missing_role_still_falls_back(self):
        # The contrast leg: without the env var the fallback is what a provider-lane
        # worker used to get unconditionally (is_fallback=True).
        profile = resolve_worker_profile(None, yaml_path=PERMISSIONS_YAML)
        self.assertTrue(profile.is_fallback)


def _kimi_args(role: "str | None") -> argparse.Namespace:
    return argparse.Namespace(
        provider="kimi",
        terminal_id="T1",
        dispatch_id="test-dispatch-kimi-role-prop",
        instruction="Say hi",
        model="default",
        max_retries=3,
        no_auto_commit=False,
        gate="",
        dispatch_paths="",
        pr_id=None,
        role=role,
        deadline_seconds=900,
    )


def _kimi_success_result():
    from provider_spawns.kimi_spawn import KimiSpawnResult

    return KimiSpawnResult(
        returncode=0,
        completion_text="ok",
        events_written=1,
        session_id=None,
        timed_out=False,
        stopped_early=False,
        token_usage=None,
        error=None,
        event_writer_failures=0,
    )


class TestProviderDispatchPassesRoleEnv(unittest.TestCase):
    """Wiring test: ``_dispatch_kimi`` threads the spec role into the spawn's
    ``extra_env``. The worktree prep and the spawn are mocked so no real git
    worktree or subprocess is created (a nested-worktree test would be the
    exact environment that breaks the dispatch tests)."""

    def test_kimi_receives_role_in_extra_env(self):
        args = _kimi_args("backend-developer")
        result = _kimi_success_result()

        with patch.object(pd, "_prepare_provider_workdir", return_value=(None, None)), \
             patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result) as mock_spawn, \
             patch("event_store.EventStore", return_value=MagicMock()), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/r.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/u.md")):
            pd._dispatch_kimi(args)

        mock_spawn.assert_called_once()
        extra_env = mock_spawn.call_args.kwargs.get("extra_env")
        self.assertEqual(extra_env.get("VNX_WORKER_ROLE"), "backend-developer")
        self.assertEqual(extra_env.get("VNX_DATA_DIR"), str(pd._resolve_data_dir()))

    def test_kimi_receives_no_role_env_when_absent(self):
        args = _kimi_args(None)
        result = _kimi_success_result()

        with patch.object(pd, "_prepare_provider_workdir", return_value=(None, None)), \
             patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result) as mock_spawn, \
             patch("event_store.EventStore", return_value=MagicMock()), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/r.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/u.md")):
            pd._dispatch_kimi(args)

        mock_spawn.assert_called_once()
        # Absent, not empty and not a default: no VNX_WORKER_ROLE key at all.
        # extra_env itself is no longer None (OI-1635: it always carries the
        # VNX_DATA_DIR stamp) — this is the split from the role-only contract.
        extra_env = mock_spawn.call_args.kwargs.get("extra_env")
        self.assertNotIn("VNX_WORKER_ROLE", extra_env)
        self.assertEqual(extra_env.get("VNX_DATA_DIR"), str(pd._resolve_data_dir()))


class TestTmuxLaneRegression(unittest.TestCase):
    """The tmux lane keeps exporting the role exactly as before (OI-1209 must not
    regress the existing path)."""

    def test_spawn_session_still_exports_role(self):
        from tmux_interactive_dispatch import TmuxInteractiveDispatch

        class _FakeRunner:
            def __init__(self) -> None:
                self.commands: list[list[str]] = []

            def run(self, args, *, timeout: int = 10, input_text=None):
                self.commands.append(list(args))

                class _R:
                    returncode = 0
                    stdout = "pane-1\n"
                    stderr = ""

                return _R()

        with tempfile.TemporaryDirectory() as td:
            fake = _FakeRunner()
            lane = TmuxInteractiveDispatch(
                td,
                runner=fake,
                project_root=td,
                receipts_file=str(Path(td) / "t0_receipts.ndjson"),
            )
            lane._spawn_session(
                "sess", Path(td), dispatch_id="d1", role="backend-developer"
            )

        new_session = [c for c in fake.commands if c and c[0] == "new-session"][0]
        self.assertIn("VNX_WORKER_ROLE=backend-developer", new_session)


if __name__ == "__main__":
    unittest.main()
