#!/usr/bin/env python3
"""Tests for the tmux signal hook scripts (scripts/hooks/tmux_signal_*.sh).

Covers:
- The guarded hook sentinel scripts (no-op when the worker env is unset)
- The Stop hook receipt-guarantee via the #788 converter (hermetic temp dirs)

The tmux dispatch lane that consumed these sentinels was removed on 2026-09-18;
the hooks stay as session management, so their contract stays under test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))


HOOKS_DIR = SCRIPT_DIR / "hooks"
SESSION_READY_HOOK = HOOKS_DIR / "tmux_signal_session_ready.sh"
PROMPT_RECEIVED_HOOK = HOOKS_DIR / "tmux_signal_prompt_received.sh"
STOP_RECEIPT_HOOK = HOOKS_DIR / "tmux_signal_stop_receipt.sh"


def _completion_protocol_block(dispatch_id: str) -> str:
    """The appended completion-protocol block the prompt-received hook parses.

    The hook anchors on the block's fixed heading and reads the escaped
    ``dispatch_id`` inside its fenced command. This shape is frozen from the
    tmux lane's ``_build_completion_protocol`` (last present at commit 6ad8f587,
    removed 2026-09-18): the lane no longer exists to generate it, so the
    fixture is now the hook's own contract.
    """
    return (
        "\n## Completion Protocol (interactive lane)\n\n```bash\n"
        "python3 /abs/scripts/append_receipt.py --receipt "
        f'"{{\\"event_type\\": \\"subprocess_completion\\", \\"dispatch_id\\": \\"{dispatch_id}\\"}}"\n'
        "```\n"
    )


# ---------------------------------------------------------------------------
# Hook scripts: guard behavior + sentinel writes
# ---------------------------------------------------------------------------
class TestHookGuards(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _run_hook(
        self,
        script: Path,
        env: dict,
        cwd: Path | None = None,
        input_text: str = "{}",
    ) -> subprocess.CompletedProcess:
        full_env = {"PATH": os.environ.get("PATH", "")}
        full_env.update(env)
        return subprocess.run(
            ["bash", str(script)],
            input=input_text,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(cwd) if cwd else None,
            timeout=15,
        )

    def test_session_ready_noop_when_env_unset(self) -> None:
        proc = self._run_hook(SESSION_READY_HOOK, env={})
        self.assertEqual(proc.returncode, 0)
        # No sentinel dir was provided, so nothing to assert except clean exit.

    def test_session_ready_writes_sentinel_when_guarded(self) -> None:
        sig = self.root / "sig"
        proc = self._run_hook(
            SESSION_READY_HOOK,
            env={
                "VNX_TMUX_SIGNAL_DIR": str(sig),
                "VNX_DISPATCH_ID": "disp-ready",
            },
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue((sig / "session_ready").is_file())

    def test_session_ready_does_not_write_when_only_one_var_set(self) -> None:
        sig = self.root / "sig-partial"
        proc = self._run_hook(
            SESSION_READY_HOOK,
            env={"VNX_TMUX_SIGNAL_DIR": str(sig)},  # missing VNX_DISPATCH_ID
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((sig / "session_ready").exists())

    def test_prompt_received_writes_sentinel_when_guarded(self) -> None:
        sig = self.root / "sig2"
        proc = self._run_hook(
            PROMPT_RECEIVED_HOOK,
            env={
                "VNX_TMUX_SIGNAL_DIR": str(sig),
                "VNX_DISPATCH_ID": "disp-prompt",
            },
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue((sig / "prompt_received").is_file())

    def test_prompt_received_noop_when_env_unset(self) -> None:
        proc = self._run_hook(PROMPT_RECEIVED_HOOK, env={})
        self.assertEqual(proc.returncode, 0)

    @unittest.skipUnless(shutil.which("jq"), "jq not available")
    def test_prompt_received_writes_mismatch_sentinel_when_dispatch_id_disagrees(self) -> None:
        """OI-1126 worker-side detectability: the delivered prompt's own embedded
        dispatch_id (from the completion-protocol JSON) disagreeing with
        VNX_DISPATCH_ID (set race-free at spawn/launch time) is the worker's own
        last-line-of-defence signal that it received a sibling dispatch's content."""
        sig = self.root / "sig-mismatch"
        prompt = (
            "## Completion Protocol (interactive lane)\n\n```bash\n"
            'python3 append_receipt.py --receipt "{\\"dispatch_id\\": \\"disp-OTHER\\"}"\n'
            "```\n"
        )
        proc = self._run_hook(
            PROMPT_RECEIVED_HOOK,
            env={"VNX_TMUX_SIGNAL_DIR": str(sig), "VNX_DISPATCH_ID": "disp-prompt"},
            input_text=json.dumps({"prompt": prompt}),
        )
        self.assertEqual(proc.returncode, 0)
        mismatch_path = sig / "dispatch_id_mismatch"
        self.assertTrue(mismatch_path.is_file(), "mismatch sentinel must be written")
        content = mismatch_path.read_text(encoding="utf-8")
        self.assertIn("disp-prompt", content)
        self.assertIn("disp-OTHER", content)
        # The base submission signal still fires — the prompt WAS submitted, just
        # not this dispatch's own content.
        self.assertTrue((sig / "prompt_received").is_file())

    @unittest.skipUnless(shutil.which("jq"), "jq not available")
    def test_prompt_received_no_mismatch_sentinel_when_dispatch_id_agrees(self) -> None:
        sig = self.root / "sig-agree"
        prompt = (
            "## Completion Protocol (interactive lane)\n\n```bash\n"
            'python3 append_receipt.py --receipt "{\\"dispatch_id\\": \\"disp-prompt\\"}"\n'
            "```\n"
        )
        proc = self._run_hook(
            PROMPT_RECEIVED_HOOK,
            env={"VNX_TMUX_SIGNAL_DIR": str(sig), "VNX_DISPATCH_ID": "disp-prompt"},
            input_text=json.dumps({"prompt": prompt}),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((sig / "dispatch_id_mismatch").exists())
        self.assertTrue((sig / "prompt_received").is_file())

    @unittest.skipUnless(shutil.which("jq"), "jq not available")
    def test_prompt_received_no_mismatch_sentinel_when_prompt_has_no_dispatch_id(self) -> None:
        """A prompt with no extractable dispatch_id must never false-positive."""
        sig = self.root / "sig-none"
        proc = self._run_hook(
            PROMPT_RECEIVED_HOOK,
            env={"VNX_TMUX_SIGNAL_DIR": str(sig), "VNX_DISPATCH_ID": "disp-prompt"},
            input_text=json.dumps({"prompt": "Do the thing, no protocol block here."}),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((sig / "dispatch_id_mismatch").exists())
        self.assertTrue((sig / "prompt_received").is_file())

    # -- OI-1126 round 3: scope the extraction to the appended protocol block --
    #
    # Fixtures below are built the way the removed lane assembled a body:
    # context text FIRST, completion-protocol block APPENDED LAST (see
    # _completion_protocol_block).

    @unittest.skipUnless(shutil.which("jq"), "jq not available")
    def test_prompt_received_ignores_foreign_id_quoted_earlier_in_body(self) -> None:
        """False-kill case: a dispatch instruction that legitimately quotes a
        different dispatch's identifier (a ledger excerpt, a JSON receipt
        fragment — VNX dispatch instructions do this routinely, since the
        fabric's own open items are ABOUT receipts and identifiers) must not
        trip the mismatch guard just because that foreign value appears
        BEFORE the code-guaranteed, always-appended-last protocol block."""
        sig = self.root / "sig-false-kill"
        context_body = (
            "Fix the receipt converter so it stops choking on this stale ledger "
            'excerpt: {"dispatch_id": "disp-FOREIGN-OLD", "status": "done"}\n\n'
            "Now implement the fix and commit.\n"
        )
        protocol_block = _completion_protocol_block("disp-prompt")
        prompt = context_body + protocol_block
        proc = self._run_hook(
            PROMPT_RECEIVED_HOOK,
            env={"VNX_TMUX_SIGNAL_DIR": str(sig), "VNX_DISPATCH_ID": "disp-prompt"},
            input_text=json.dumps({"prompt": prompt}),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(
            (sig / "dispatch_id_mismatch").exists(),
            "a foreign id quoted earlier in the instruction body must not "
            "trigger a false mismatch when the appended protocol id agrees",
        )
        self.assertTrue((sig / "prompt_received").is_file())

    @unittest.skipUnless(shutil.which("jq"), "jq not available")
    def test_prompt_received_detects_real_mismatch_despite_foreign_id_in_body(self) -> None:
        """Masking case: a foreign id earlier in the body must not hide a REAL
        crossing carried by the appended protocol block — the recorded
        delivered value must be the protocol's, not the body's earlier one."""
        sig = self.root / "sig-masking"
        context_body = (
            "Ledger excerpt for reference: "
            '{"dispatch_id": "disp-FOREIGN-OLD", "status": "done"}\n\n'
            "Now implement the fix and commit.\n"
        )
        protocol_block = _completion_protocol_block("disp-CROSSED")
        prompt = context_body + protocol_block
        proc = self._run_hook(
            PROMPT_RECEIVED_HOOK,
            env={"VNX_TMUX_SIGNAL_DIR": str(sig), "VNX_DISPATCH_ID": "disp-prompt"},
            input_text=json.dumps({"prompt": prompt}),
        )
        self.assertEqual(proc.returncode, 0)
        mismatch_path = sig / "dispatch_id_mismatch"
        self.assertTrue(mismatch_path.is_file(), "a genuine crossing must still be caught")
        content = mismatch_path.read_text(encoding="utf-8")
        self.assertIn("expected=disp-prompt", content)
        self.assertIn("delivered=disp-CROSSED", content)
        self.assertNotIn("disp-FOREIGN-OLD", content)

    @unittest.skipUnless(shutil.which("jq"), "jq not available")
    def test_prompt_received_no_mismatch_when_protocol_id_agrees_with_env(self) -> None:
        """Baseline still holds: a realistic context body plus the appended
        protocol block carrying the correct id writes no sentinel."""
        sig = self.root / "sig-baseline-agree"
        context_body = "Implement the fix described above, then commit and push.\n"
        protocol_block = _completion_protocol_block("disp-prompt")
        prompt = context_body + protocol_block
        proc = self._run_hook(
            PROMPT_RECEIVED_HOOK,
            env={"VNX_TMUX_SIGNAL_DIR": str(sig), "VNX_DISPATCH_ID": "disp-prompt"},
            input_text=json.dumps({"prompt": prompt}),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((sig / "dispatch_id_mismatch").exists())
        self.assertTrue((sig / "prompt_received").is_file())

    @unittest.skipUnless(shutil.which("jq"), "jq not available")
    def test_session_ready_captures_session_id_from_stdin(self) -> None:
        sig = self.root / "sig-sid"
        session_id = "123e4567-e89b-12d3-a456-426614174000"
        proc = self._run_hook(
            SESSION_READY_HOOK,
            env={
                "VNX_TMUX_SIGNAL_DIR": str(sig),
                "VNX_DISPATCH_ID": "disp-sid",
            },
            input_text=json.dumps({"session_id": session_id, "transcript_path": "/tmp/t.json"}),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue((sig / "session_ready").is_file())
        self.assertEqual((sig / "session_ready").read_text(encoding="utf-8").strip(), "disp-sid")
        self.assertTrue((sig / "session_id").is_file())
        self.assertEqual((sig / "session_id").read_text(encoding="utf-8").strip(), session_id)

    @unittest.skipUnless(shutil.which("jq"), "jq not available")
    def test_session_ready_empty_stdin_still_writes_sentinel(self) -> None:
        sig = self.root / "sig-empty"
        proc = self._run_hook(
            SESSION_READY_HOOK,
            env={
                "VNX_TMUX_SIGNAL_DIR": str(sig),
                "VNX_DISPATCH_ID": "disp-empty",
            },
            input_text="",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue((sig / "session_ready").is_file())
        self.assertFalse((sig / "session_id").exists())

    @unittest.skipUnless(shutil.which("jq"), "jq not available")
    def test_session_ready_garbage_stdin_still_writes_sentinel(self) -> None:
        sig = self.root / "sig-garbage"
        proc = self._run_hook(
            SESSION_READY_HOOK,
            env={
                "VNX_TMUX_SIGNAL_DIR": str(sig),
                "VNX_DISPATCH_ID": "disp-garbage",
            },
            input_text="not json {",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue((sig / "session_ready").is_file())

    def test_session_ready_no_jq_still_writes_sentinel(self) -> None:
        sig = self.root / "sig-nojq"
        session_id = "123e4567-e89b-12d3-a456-426614174000"
        # Place a fake jq earlier in PATH so command -v jq succeeds but it fails
        # to parse, simulating a broken/missing jq installation.
        fake_bin = self.root / "fakebin"
        fake_bin.mkdir()
        (fake_bin / "jq").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (fake_bin / "jq").chmod(0o755)
        proc = self._run_hook(
            SESSION_READY_HOOK,
            env={
                "VNX_TMUX_SIGNAL_DIR": str(sig),
                "VNX_DISPATCH_ID": "disp-nojq",
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            },
            input_text=json.dumps({"session_id": session_id}),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue((sig / "session_ready").is_file())
        # With a failing jq we cannot extract the id, but the pipe must be
        # drained and the primary sentinel still written.
        self.assertFalse((sig / "session_id").exists())


# ---------------------------------------------------------------------------
# Stop hook: receipt-guarantee via the #788 converter (hermetic)
# ---------------------------------------------------------------------------
_VALID_REPORT = """\
---
model: sonnet
---
**Dispatch-ID**: {did}

## Summary

This worker completed the hook-driven lane signal wiring and validated the
version-agnostic readiness and submission path end to end.

## Changes

- scripts/hooks/tmux_signal_*.sh

## Verification

python3 -m pytest tests/test_tmux_lane_signals.py -q

## Open Items

None
"""


class TestStopReceiptGuarantee(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Hermetic data dir: nothing here touches the live .vnx-data.
        self.data_dir = self.root / "data"
        (self.data_dir / "state").mkdir(parents=True)
        (self.data_dir / "unified_reports").mkdir(parents=True)
        self.sig = self.root / "sig"

    def _run_stop_hook(self, env: dict) -> subprocess.CompletedProcess:
        full_env = {"PATH": os.environ.get("PATH", "")}
        full_env.update(env)
        # cwd = real repo root so the hook's `git rev-parse` resolves scripts/lib;
        # VNX_DATA_DIR / VNX_STATE_DIR override keeps state writes hermetic.
        return subprocess.run(
            ["bash", str(STOP_RECEIPT_HOOK)],
            input="{}",
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(REPO_ROOT),
            timeout=20,
        )

    def test_noop_when_env_unset(self) -> None:
        proc = self._run_stop_hook(env={})
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((self.sig / "stopped").exists())

    def test_writes_stopped_sentinel_and_emits_receipt(self) -> None:
        did = "20260602-stoptest"
        report = self.data_dir / "unified_reports" / f"{did}.md"
        report.write_text(_VALID_REPORT.format(did=did), encoding="utf-8")

        proc = self._run_stop_hook(
            env={
                "VNX_DISPATCH_ID": did,
                "VNX_TMUX_SIGNAL_DIR": str(self.sig),
                "VNX_DATA_DIR": str(self.data_dir),
                "VNX_STATE_DIR": str(self.data_dir / "state"),
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # (a) stopped sentinel written
        self.assertTrue((self.sig / "stopped").is_file())

        # (b) governed receipt emitted promptly for this dispatch
        receipts = self.data_dir / "state" / "t0_receipts.ndjson"
        self.assertTrue(receipts.is_file(), "receipt file should exist after stop hook")
        lines = [
            json.loads(line)
            for line in receipts.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(
            any(r.get("dispatch_id") == did for r in lines),
            f"expected a receipt for {did}; got {lines}",
        )

    def test_dedup_no_double_receipt_on_rerun(self) -> None:
        did = "20260602-dedup"
        report = self.data_dir / "unified_reports" / f"{did}.md"
        report.write_text(_VALID_REPORT.format(did=did), encoding="utf-8")
        env = {
            "VNX_DISPATCH_ID": did,
            "VNX_TMUX_SIGNAL_DIR": str(self.sig),
            "VNX_DATA_DIR": str(self.data_dir),
            "VNX_STATE_DIR": str(self.data_dir / "state"),
        }
        self._run_stop_hook(env=env)
        self._run_stop_hook(env=env)  # rerun must not double-write

        receipts = self.data_dir / "state" / "t0_receipts.ndjson"
        lines = [
            json.loads(line)
            for line in receipts.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matching = [r for r in lines if r.get("dispatch_id") == did]
        self.assertEqual(len(matching), 1, f"expected exactly one receipt; got {matching}")


if __name__ == "__main__":
    unittest.main()
