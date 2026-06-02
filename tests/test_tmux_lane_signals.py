#!/usr/bin/env python3
"""Tests for the hook-driven tmux interactive lane signals.

Covers the version-agnostic hook-contract path that replaces TUI-string scraping:
- SessionStart sentinel → readiness (with TUI-marker fallback)
- UserPromptSubmit sentinel → submission (with _still_staged fallback)
- _looks_working() structural token-counter detector (2.1.160-robust)
- The guarded hook sentinel scripts (no-op when worker env unset)
- The Stop hook receipt-guarantee via the #788 converter (hermetic temp dirs)

The receipt remains authoritative for completion; these tests assert the lane no
longer hard-depends on a specific Claude Code version's TUI wording.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from tmux_interactive_dispatch import TmuxInteractiveDispatch, TmuxResult  # noqa: E402

HOOKS_DIR = SCRIPT_DIR / "hooks"
SESSION_READY_HOOK = HOOKS_DIR / "tmux_signal_session_ready.sh"
PROMPT_RECEIVED_HOOK = HOOKS_DIR / "tmux_signal_prompt_received.sh"
STOP_RECEIPT_HOOK = HOOKS_DIR / "tmux_signal_stop_receipt.sh"


class _CaptureRunner:
    """Minimal tmux runner stub: capture-pane returns a fixed content string."""

    def __init__(self, capture_content: str = "") -> None:
        self._capture_content = capture_content
        self.commands: list[list[str]] = []

    def available(self) -> bool:
        return True

    def run(self, args, *, timeout: int = 10, input_text=None) -> TmuxResult:
        self.commands.append(list(args))
        if args and args[0] == "capture-pane":
            return TmuxResult(0, self._capture_content)
        return TmuxResult(0, "")


def _make_lane(runner: _CaptureRunner, root: Path) -> TmuxInteractiveDispatch:
    return TmuxInteractiveDispatch(
        root,
        runner=runner,
        receipts_file=root / "t0_receipts.ndjson",
        project_root=root,
    )


# ---------------------------------------------------------------------------
# Readiness: sentinel-first, TUI fallback
# ---------------------------------------------------------------------------
class TestReadinessSentinel(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_session_ready_sentinel_marks_ready_without_tui_marker(self) -> None:
        # Pane content has NO TUI readiness marker — only the sentinel proves ready.
        runner = _CaptureRunner(capture_content="(nothing recognizable here)")
        lane = _make_lane(runner, self.root)
        sig = self.root / "sig"
        sig.mkdir()
        (sig / "session_ready").write_text("dispatch-x\n", encoding="utf-8")

        ready = lane._wait_ready(
            "%1",
            ready_markers=("for shortcuts",),
            warmup_timeout=0.5,
            poll_interval=0.01,
            signal_dir=sig,
        )
        self.assertTrue(ready)

    def test_empty_signal_dir_falls_back_to_tui_markers(self) -> None:
        # No sentinel; pane shows a TUI marker → fallback path returns ready.
        runner = _CaptureRunner(capture_content="Welcome\n? for shortcuts")
        lane = _make_lane(runner, self.root)
        sig = self.root / "sig"
        sig.mkdir()  # empty — no session_ready file

        ready = lane._wait_ready(
            "%1",
            ready_markers=("for shortcuts",),
            warmup_timeout=0.5,
            poll_interval=0.01,
            signal_dir=sig,
        )
        self.assertTrue(ready)

    def test_no_sentinel_no_marker_times_out(self) -> None:
        runner = _CaptureRunner(capture_content="(no marker)")
        lane = _make_lane(runner, self.root)
        sig = self.root / "sig"
        sig.mkdir()

        ready = lane._wait_ready(
            "%1",
            ready_markers=("for shortcuts",),
            warmup_timeout=0.1,
            poll_interval=0.01,
            signal_dir=sig,
        )
        self.assertFalse(ready)


# ---------------------------------------------------------------------------
# Submission: sentinel-first, _still_staged fallback
# ---------------------------------------------------------------------------
class TestSubmissionSentinel(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_prompt_received_sentinel_treated_as_submitted(self) -> None:
        # Pane still shows the staged paste, but the hook sentinel overrides it.
        staged = "[Pasted text +120 lines]\n<!-- VNX-END-OF-INSTRUCTION -->"
        runner = _CaptureRunner(capture_content=staged)
        lane = _make_lane(runner, self.root)
        sig = self.root / "sig"
        sig.mkdir()
        (sig / "prompt_received").write_text("dispatch-x\n", encoding="utf-8")

        with _fast_submit_env():
            submitted = lane._verify_submit("%1", "Do the thing.", signal_dir=sig)
        self.assertTrue(submitted)

    def test_no_sentinel_uses_working_marker_fallback(self) -> None:
        # No sentinel; pane shows the working token-counter → submitted via fallback.
        runner = _CaptureRunner(capture_content="✢ Smooshing… (18s · ↓ 739 tokens)")
        lane = _make_lane(runner, self.root)
        sig = self.root / "sig"
        sig.mkdir()  # empty

        with _fast_submit_env():
            submitted = lane._verify_submit("%1", "Do the thing.", signal_dir=sig)
        self.assertTrue(submitted)


# ---------------------------------------------------------------------------
# _looks_working: version-robust structural detector
# ---------------------------------------------------------------------------
class TestLooksWorking(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lane = _make_lane(_CaptureRunner(), Path(self._tmp.name))

    def test_true_for_2_1_160_spinner_token_counter(self) -> None:
        self.assertTrue(self.lane._looks_working("✢ Smooshing… (18s · ↓ 739 tokens)"))

    def test_true_for_up_tokens_variant(self) -> None:
        self.assertTrue(self.lane._looks_working("● Pondering (3s · ↑ 12 tokens)"))

    def test_true_for_legacy_esc_to_interrupt(self) -> None:
        self.assertTrue(self.lane._looks_working("... (esc to interrupt)"))

    def test_false_for_idle_prompt_glyph(self) -> None:
        self.assertFalse(self.lane._looks_working("❯ "))

    def test_false_for_empty(self) -> None:
        self.assertFalse(self.lane._looks_working(""))


# ---------------------------------------------------------------------------
# Hook scripts: guard behavior + sentinel writes
# ---------------------------------------------------------------------------
class TestHookGuards(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _run_hook(self, script: Path, env: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
        full_env = {"PATH": os.environ.get("PATH", "")}
        full_env.update(env)
        return subprocess.run(
            ["bash", str(script)],
            input="{}",
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


# ---------------------------------------------------------------------------
# Stop hook: receipt-guarantee via the #788 converter (hermetic)
# ---------------------------------------------------------------------------
_VALID_REPORT = """\
**Dispatch-ID**: {did}

## Summary

This worker completed the hook-driven lane signal wiring and validated the
version-agnostic readiness and submission path end to end.

## Changes

- scripts/lib/tmux_interactive_dispatch.py
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


class _fast_submit_env:
    """Context manager: zero out submit-verify timing env so tests run fast."""

    _ENV = {
        "VNX_TMUX_SUBMIT_RETRY_DELAY": "0",
        "VNX_TMUX_SUBMIT_VERIFY_TIMEOUT": "0.1",
        "VNX_TMUX_SUBMIT_MAX_RETRIES": "1",
    }

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._ENV}
        os.environ.update(self._ENV)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


if __name__ == "__main__":
    unittest.main()
