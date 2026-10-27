#!/usr/bin/env python3
"""Tests for pool_worker_runner.py — N-2 single-claim worker (ADR-007 + ADR-018).

Required coverage:
  - claims one dispatch, loads bundle, delegates to delivery, returns EXIT_OK
  - empty queue → EXIT_NO_WORK; no delivery call
  - FM-4: project_id mismatch → EXIT_PROJECT_MISMATCH; no delivery call
  - path-traversal guard: dispatch_id with '../' or absolute path → EXIT_INVALID_DISPATCH_ID
  - ledger events: claim_next_queued_dispatch (N-1) emits dispatch_claimed + dispatch_claim_provenance
  - state-dir resolution: VNX_STATE_DIR='' + VNX_DATA_DIR set → canonical dir, not empty path
"""
from __future__ import annotations
import contextlib, importlib.util as _ilu, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR / "lib" / "migrations"))

from coordination_db import db_path_from_state_dir
from runtime_coordination import get_connection, init_schema

# Load migration 0026 (project-scoped claim index + claimed_by/claimed_at)
_m26 = _ilu.module_from_spec(_s := _ilu.spec_from_file_location(
    "apply_0026", SCRIPT_DIR / "lib" / "migrations" / "apply_0026.py"))
_s.loader.exec_module(_m26)
_m17 = _ilu.module_from_spec(_s2 := _ilu.spec_from_file_location(
    "apply_0017", SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"))
_s2.loader.exec_module(_m17)
_MIGS_SQL = Path(__file__).resolve().parent.parent / "schemas" / "migrations" / "0026_dispatch_claim.sql"

def _apply_0026(state_dir):
    return _m26.apply_migration(db_path_from_state_dir(state_dir), _MIGS_SQL)

import dispatch_bridge  # noqa: E402
import pool_worker_runner  # noqa: E402
import skill_prefix  # noqa: E402
import subprocess_dispatch  # noqa: E402
import subprocess_dispatch_internals.delivery as _delivery  # noqa: E402
import subprocess_dispatch_internals.recovery as _recovery  # noqa: E402
from provider_spawns.claude_spawn import ClaudeSpawnResult  # noqa: E402
from pool_worker_runner import (  # noqa: E402
    EXIT_NO_WORK, EXIT_OK, EXIT_PROJECT_MISMATCH, EXIT_BUNDLE_MISSING,
    EXIT_INVALID_DISPATCH_ID, run, _validate_dispatch_id, _resolve_state_dir,
)


class _Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.sd = self._t.name
        init_schema(self.sd)
        with get_connection(self.sd) as c:
            c.execute("INSERT OR IGNORE INTO runtime_schema_version (version, description) VALUES (13,'stub')")
            c.execute("INSERT OR IGNORE INTO runtime_schema_version (version, description) VALUES (14,'stub')")
            c.commit()
        _apply_0026(self.sd)
        with get_connection(self.sd) as c:
            _m17._rebuild_dispatches(c)
            c.commit()
        self.dd = Path(self._t.name) / "dispatches"
        self.dd.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._t.cleanup()

    def _queue(self, did, pid="proj"):
        with get_connection(self.sd) as c:
            c.execute("INSERT INTO dispatches (dispatch_id, project_id, state, priority) VALUES (?,?,'queued','P2')", (did, pid))
            c.commit()

    def _bundle(self, did, provider="claude", instr="# test"):
        d = self.dd / did
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.txt").write_text(instr, encoding="utf-8")
        (d / "bundle.json").write_text(
            json.dumps({"dispatch_id": did, "target_profile": {"provider": provider}, "gate": ""}),
            encoding="utf-8")

    def _run(self, tid="T1", pid="proj", **kw):
        return run(terminal_id=tid, project_id=pid,
                   state_dir=Path(self.sd), dispatch_dir=self.dd, **kw)


class TestClaimsAndDelivers(_Base):

    def test_claims_one_dispatch_delegates_to_delivery_returns_ok(self):
        """Mocked broker+delivery: runner claims one dispatch, delegates, returns EXIT_OK."""
        self._queue("d-001")
        self._bundle("d-001", instr="# Instruction body")

        with patch("pool_worker_runner._deliver_claude", return_value=EXIT_OK) as m:
            result = self._run()

        self.assertEqual(result, EXIT_OK)
        m.assert_called_once()
        args = m.call_args.args
        self.assertEqual(args[0], "T1")          # terminal_id
        self.assertEqual(args[1], "d-001")        # dispatch_id
        self.assertEqual(args[2], "# Instruction body")  # instruction


class TestEmptyQueue(_Base):

    def test_empty_queue_returns_no_work_no_delivery(self):
        """Empty queue → EXIT_NO_WORK; no side effects; delivery never called."""
        with patch("pool_worker_runner._deliver_claude") as m:
            result = self._run()
        self.assertEqual(result, EXIT_NO_WORK)
        m.assert_not_called()


class TestProjectIdMismatch(_Base):
    """ADR-018 FM-4: post-claim project_id guard refuses cross-project dispatch."""

    def test_mismatch_refuses_dispatch_no_delivery(self):
        """Simulated edge case: claim returns project-b dispatch, worker is project-a."""
        with get_connection(self.sd) as c:
            c.execute("INSERT INTO dispatches (dispatch_id, project_id, state, priority)"
                      " VALUES ('d-cross','project-b','claimed','P2')")
            c.commit()
        self._bundle("d-cross")

        with patch("pool_worker_runner.claim_next_queued_dispatch", return_value="d-cross"):
            with patch("pool_worker_runner._deliver_claude") as m:
                result = self._run(pid="project-a")

        self.assertEqual(result, EXIT_PROJECT_MISMATCH)
        m.assert_not_called()


class TestBundleMissing(_Base):

    def test_missing_bundle_json_returns_bundle_missing(self):
        self._queue("d-nobundle")
        self.assertEqual(self._run(), EXIT_BUNDLE_MISSING)

    def test_missing_prompt_txt_returns_bundle_missing(self):
        self._queue("d-noprompt")
        d = self.dd / "d-noprompt"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bundle.json").write_text(
            json.dumps({"dispatch_id": "d-noprompt", "target_profile": {"provider": "claude"}, "gate": ""}),
            encoding="utf-8")
        self.assertEqual(self._run(), EXIT_BUNDLE_MISSING)


class TestPathTraversalGuard(_Base):
    """Security: dispatch_id containing path separators or traversal must be refused."""

    def test_dotdot_slash_refused(self):
        """dispatch_id '../secret' triggers EXIT_INVALID_DISPATCH_ID, no file read."""
        with patch("pool_worker_runner.claim_next_queued_dispatch", return_value="../secret"):
            with patch("pool_worker_runner._deliver_claude") as m:
                result = self._run()
        self.assertEqual(result, EXIT_INVALID_DISPATCH_ID)
        m.assert_not_called()

    def test_absolute_path_refused(self):
        """/etc/passwd as dispatch_id triggers EXIT_INVALID_DISPATCH_ID."""
        with patch("pool_worker_runner.claim_next_queued_dispatch", return_value="/etc/passwd"):
            with patch("pool_worker_runner._deliver_claude") as m:
                result = self._run()
        self.assertEqual(result, EXIT_INVALID_DISPATCH_ID)
        m.assert_not_called()

    def test_embedded_slash_refused(self):
        """dispatch_id 'a/b' triggers EXIT_INVALID_DISPATCH_ID."""
        with patch("pool_worker_runner.claim_next_queued_dispatch", return_value="a/b"):
            with patch("pool_worker_runner._deliver_claude") as m:
                result = self._run()
        self.assertEqual(result, EXIT_INVALID_DISPATCH_ID)
        m.assert_not_called()

    def test_normal_slug_resolves(self):
        """A well-formed dispatch_id resolves correctly and does not raise."""
        with tempfile.TemporaryDirectory() as td:
            dd = Path(td) / "dispatches"
            dd.mkdir()
            resolved = _validate_dispatch_id("20260529-abc123", dd)
            self.assertTrue(str(resolved).startswith(os.path.realpath(dd)))
            self.assertTrue(str(resolved).endswith("20260529-abc123"))


class TestLedgerEventsOnClaim(_Base):
    """ADR-005: claim_next_queued_dispatch (N-1) emits dispatch_claimed + dispatch_claim_provenance.

    The runner must NOT emit a third duplicate event. We verify the N-1 events exist and that
    pool_worker_runner itself does not emit an additional event of its own.
    """

    def test_n1_emits_two_events_runner_does_not_duplicate(self):
        """After a real claim, exactly dispatch_claimed + dispatch_claim_provenance exist; no third."""
        from coordination_db import get_events
        self._queue("d-ledger")
        self._bundle("d-ledger", instr="# ledger test")

        with patch("pool_worker_runner._deliver_claude", return_value=EXIT_OK):
            result = self._run(tid="T1", pid="proj")

        self.assertEqual(result, EXIT_OK)

        with get_connection(self.sd) as c:
            dispatch_events = get_events(c, entity_id="d-ledger", limit=50)

        event_types = [e["event_type"] for e in dispatch_events]
        self.assertIn("dispatch_claimed", event_types)
        self.assertIn("dispatch_claim_provenance", event_types)
        # Runner must not add a third distinct event for the same dispatch
        self.assertEqual(len(dispatch_events), 2,
                         f"Expected exactly 2 events for dispatch, got {len(dispatch_events)}: {event_types}")


class TestStateDirResolution(unittest.TestCase):
    """VNX_STATE_DIR='' must fall through to VNX_DATA_DIR, not produce Path('') / 'state'."""

    def test_empty_vnx_state_dir_falls_through_to_vnx_data_dir(self):
        env = {"VNX_STATE_DIR": "", "VNX_DATA_DIR": "/tmp/mydata"}
        with patch.dict(os.environ, env, clear=False):
            result = _resolve_state_dir()
        self.assertEqual(result, Path("/tmp/mydata") / "state")

    def test_non_empty_vnx_state_dir_wins(self):
        env = {"VNX_STATE_DIR": "/tmp/mystate", "VNX_DATA_DIR": "/tmp/mydata"}
        with patch.dict(os.environ, env, clear=False):
            result = _resolve_state_dir()
        self.assertEqual(result, Path("/tmp/mystate"))

    def test_both_absent_uses_canonical_resolver(self):
        """No env vars set -> resolves via vnx_paths.resolve_paths(), not a repo-relative guess.

        A raw _LIB_DIR.parents[1] / ".vnx-data" / "state" default would resolve the
        KEYSTONE (not the project's ~/.vnx-data/<project>) in a central install. See #1023.
        """
        stripped = {k: v for k, v in os.environ.items()
                    if k not in ("VNX_STATE_DIR", "VNX_DATA_DIR")}
        with patch.dict(os.environ, stripped, clear=True):
            with patch("pool_worker_runner._canonical_data_dir",
                        return_value=Path("/tmp/canonical-project/.vnx-data")):
                result = _resolve_state_dir()
        self.assertEqual(result, Path("/tmp/canonical-project/.vnx-data") / "state")

    def test_both_absent_falls_back_when_canonical_resolver_unavailable(self):
        """Canonical resolver import/call failure -> last-resort repo-relative fallback."""
        stripped = {k: v for k, v in os.environ.items()
                    if k not in ("VNX_STATE_DIR", "VNX_DATA_DIR")}
        with patch.dict(os.environ, stripped, clear=True):
            with patch("pool_worker_runner._canonical_data_dir", return_value=None):
                result = _resolve_state_dir()
        self.assertTrue(str(result).endswith(os.path.join(".vnx-data", "state")))


class TestDeadlineFallback(_Base):
    """When dispatch-spec.json is corrupt or unreadable, log a warning and fall back to 900s."""

    def test_corrupt_spec_logs_warning_falls_back_to_900(self):
        """Invalid JSON in dispatch-spec.json -> warning logged, fallback to 900s, delivery proceeds."""
        self._queue("d-corrupt")
        self._bundle("d-corrupt", instr="# deadline test")
        corrupt_spec = self.dd / "d-corrupt" / "dispatch-spec.json"
        corrupt_spec.write_text("{bad json", encoding="utf-8")

        with self.assertLogs("pool_worker_runner", level="WARNING") as log_ctx:
            with patch("pool_worker_runner._deliver_claude", return_value=EXIT_OK) as m:
                result = self._run()

        self.assertEqual(result, EXIT_OK)
        m.assert_called_once()
        self.assertEqual(
            m.call_args.kwargs.get("deadline_seconds"), 900,
            "Corrupt spec must fall back to default 900s deadline",
        )
        self.assertTrue(
            any("Failed to read deadline_seconds" in msg for msg in log_ctx.output),
            f"Expected warning about spec read failure in: {log_ctx.output}",
        )


# ---------------------------------------------------------------------------
# RS-4 / OI-2372: the skill named by the claimed dispatch's spec reaches the
# worker's prompt. Before this change run() read only deadline_seconds from
# dispatch-spec.json, so the skill never left the file.
# ---------------------------------------------------------------------------

SKILL = "rspoolskill"
MARKER = "RS4-POOL-SKILL-MARKER-7c31"
SKILL_DID = "20261027-pool-skill"


class TestSpecSkillDelivery(_Base):
    """Real prompt assembly; only the spawn (an external process) is patched."""

    ROLE = "backend-developer"

    def setUp(self):
        super().setUp()
        self.tmp = Path(self._t.name)
        self.root = _fake_project_root(self.tmp)

    def _spec(self, skill, role=None, provider="claude", did=SKILL_DID):
        role = role or self.ROLE
        self._bundle(did, provider=provider, instr="do the task")
        d = self.dd / did
        (d / "bundle.json").write_text(json.dumps({
            "dispatch_id": did, "gate": "",
            "target_profile": {"provider": provider, "role": role},
        }), encoding="utf-8")
        spec = {"schema_version": 1, "deadline_seconds": 900}
        if skill is not None:
            spec["skill"] = skill
        (d / "dispatch-spec.json").write_text(json.dumps(spec), encoding="utf-8")
        self._queue(did)

    def _deliver(self, skill, role=None):
        self._spec(skill, role, did=f"{SKILL_DID}-{role or self.ROLE}")
        return _run_capturing(self.sd, self.dd, self.root)["prompt"]

    def test_a_spec_skill_reaches_the_prompt(self):
        prompt = self._deliver(SKILL)
        self.assertIn(MARKER, prompt,
                      "the pool worker's prompt lacks the skill its spec bound")
        self.assertIn("do the task", prompt)

    def test_a_spec_without_a_skill_leaves_the_prompt_unchanged(self):
        prompt = self._deliver(None)
        self.assertNotIn(MARKER, prompt)
        self.assertNotIn("# SKILL:", prompt)
        # Same bytes the pre-RS-4 lane assembled: the new kwarg's default is inert.
        self.assertEqual(prompt, _baseline_prompt(self.sd, self.root, self.ROLE,
                                                  f"{SKILL_DID}-{self.ROLE}"))

    def test_a_reviewer_role_never_gets_the_skill(self):
        for role in ("review-gate", "plan-reviewer"):
            with self.subTest(role=role):
                prompt = self._deliver(SKILL, role=role)
                self.assertNotIn(MARKER, prompt)
                self.assertIn("do the task", prompt)

    def test_the_door_route_is_told_the_skill(self):
        """The default (door) route restages the spec, so it must carry the skill."""
        self._spec(SKILL)
        seen = {}

        def _bridge(**kwargs):
            seen.update(kwargs)
            return 0

        with patch.dict(os.environ, {"VNX_SINGLE_ENTRY_DISPATCH": "1"}, clear=False), \
             patch.object(dispatch_bridge, "bridge_dispatch", side_effect=_bridge):
            result = self._run()
        self.assertEqual(result, EXIT_OK)
        self.assertEqual(seen.get("skill"), SKILL,
                         f"the door was staged without the spec's skill: {sorted(seen)}")

    def test_the_provider_lane_is_told_the_skill(self):
        self._spec(SKILL, provider="kimi")
        seen = {}
        with patch.object(pool_worker_runner, "_deliver_provider",
                          side_effect=lambda *a, **kw: (seen.update(kw), EXIT_OK)[1]):
            self.assertEqual(self._run(), EXIT_OK)
        self.assertEqual(seen.get("skill"), SKILL)


def _fake_project_root(tmp_path):
    root = tmp_path / "project"
    (root / "skills" / SKILL).mkdir(parents=True)
    (root / "skills" / SKILL / "SKILL.md").write_text(
        f"---\nname: {SKILL}\n---\n\n{MARKER}: apply the method.\n", encoding="utf-8")
    fake_file = root / "scripts" / "lib" / "subprocess_dispatch.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.touch()
    return root


@contextlib.contextmanager
def _pool_env(root, captured):
    """Side effects AROUND the assembly patched; assembly and skill resolution real."""
    result = ClaudeSpawnResult(
        returncode=0, completion={}, events_written=0, session_id="sess-rs4",
        timed_out=False, completion_text="ok", token_usage=None)

    def _spawn(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return result

    patches = [
        patch.dict(os.environ, {"VNX_DISPATCH_LEGACY": "1"}, clear=False),
        patch.object(subprocess_dispatch, "__file__",
                     str(root / "scripts" / "lib" / "subprocess_dispatch.py")),
        patch.object(subprocess_dispatch, "_build_intelligence_section", lambda *a, **k: ""),
        patch.object(subprocess_dispatch, "_resolve_agent_cwd", lambda role: None),
        patch.object(subprocess_dispatch, "_write_manifest", lambda **kw: root / "manifest.json"),
        patch.object(subprocess_dispatch, "_promote_manifest", lambda *a, **kw: None),
        patch.object(_delivery, "_start_heartbeat", lambda *a, **kw: (None, None, None)),
        patch.object(_recovery, "_init_recovery_state", lambda *a, **kw: ("ts", "", [], None)),
        patch.object(_recovery, "_book_attempt", lambda *a, **kw: None),
        patch.object(_recovery, "_handle_success", lambda *a, **kw: None),
        patch("provider_spawns.claude_spawn.spawn_claude", side_effect=_spawn),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield captured


def _run_capturing(sd, dd, root):
    captured = {}
    with _pool_env(root, captured):
        rc = run(terminal_id="T1", project_id="proj", state_dir=Path(sd), dispatch_dir=dd)
    assert rc == EXIT_OK, f"pool delivery returned {rc}"
    return captured


def _baseline_prompt(sd, root, role, did):
    """The prompt the pre-RS-4 lane assembled: deliver_with_recovery with no skill."""
    captured = {}
    with _pool_env(root, captured), patch.object(
            skill_prefix, "build_skill_prefix",
            side_effect=AssertionError("build_skill_prefix ran with no skill")):
        ok = subprocess_dispatch.deliver_with_recovery(
            # Same model the pool run resolves (VNX_DISPATCH_MODEL default).
            terminal_id="T1", instruction="do the task", model="kimi-k3",
            dispatch_id=did, role=role, max_retries=0, auto_commit=False)
    assert ok is True, "the no-skill baseline delivery failed"
    return captured["prompt"]


if __name__ == "__main__":
    unittest.main()
