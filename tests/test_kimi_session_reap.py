"""Tests for kimi one-shot session reaping (OI-812).

kimi-cli persists a resumable session dir per invocation with no auto-GC, TTL,
or no-persist flag. One-shot ``--print`` dispatches are never resumed, so
without an explicit reap the session dirs (wire.jsonl + context.jsonl +
state.json) accrue unbounded (543 dirs / 697MB on 2026-07-28, manually cleaned
to 316M). These tests cover:

  * ``_find_kimi_session_dir`` — locating the session dir by scanning buckets;
  * ``_reap_kimi_session``     — guarded removal confined to the sessions tree;
  * ``spawn_kimi`` wiring      — a clean one-shot run reaps the session dir
    after the token harvest; a run without a resume line or a failed dispatch
    leaves on-disk sessions untouched.

Every test runs against a throwaway ``KIMI_SHARE_DIR`` so the real ``~/.kimi``
tree is never touched.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from provider_spawns.kimi_spawn import (  # noqa: E402
    _find_kimi_session_dir,
    _reap_kimi_session,
    spawn_kimi,
)

_SESSION_ID = "c2f1ae72-7d30-46e0-8b41-ca8938a13b89"


def _make_session_dir(share_dir: Path, session_id: str, bucket: str = "bucket1") -> Path:
    """Create a kimi-style session dir the way the CLI lays it out on disk."""
    session_dir = share_dir / "sessions" / bucket / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "wire.jsonl").write_text('{"type": "metadata", "protocol_version": "1.10"}\n')
    (session_dir / "context.jsonl").write_text("")
    (session_dir / "state.json").write_text("{}")
    return session_dir


def _make_proc_with_stderr(events: list, returncode: int = 0, stderr_text: str = "") -> MagicMock:
    """Fake Popen with a real-pipe stdout (drain_stream needs fileno()) and a
    BytesIO stderr carrying the kimi resume line."""
    data = b"".join((json.dumps(e) + "\n").encode() for e in events)
    read_fd, write_fd = os.pipe()

    def _writer():
        try:
            os.write(write_fd, data)
        finally:
            os.close(write_fd)

    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()

    fake_proc = MagicMock()
    fake_proc.returncode = returncode
    fake_proc.poll.return_value = returncode
    fake_proc.stdout = os.fdopen(read_fd, "rb", buffering=0)
    fake_proc.stderr = io.BytesIO(stderr_text.encode())
    fake_proc.wait = MagicMock(return_value=returncode)
    fake_proc.kill = MagicMock()
    fake_proc._writer_thread = writer_thread
    return fake_proc


class TestFindKimiSessionDir(unittest.TestCase):
    def test_finds_session_across_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            _make_session_dir(share, _SESSION_ID, bucket="bucket1")
            session_dir = _find_kimi_session_dir(_SESSION_ID, {"KIMI_SHARE_DIR": str(share)})
            self.assertIsNotNone(session_dir)
            self.assertEqual(session_dir.name, _SESSION_ID)
            self.assertTrue(session_dir.is_dir())

    def test_returns_none_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            _make_session_dir(share, "11111111-2222-3333-4444-555555555555", bucket="bucket1")
            self.assertIsNone(_find_kimi_session_dir(_SESSION_ID, {"KIMI_SHARE_DIR": str(share)}))

    def test_returns_none_when_sessions_root_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            self.assertIsNone(_find_kimi_session_dir(_SESSION_ID, {"KIMI_SHARE_DIR": str(share)}))

    def test_returns_none_when_share_dir_missing(self) -> None:
        share = Path(tempfile.gettempdir()) / "kimi-reap-test-absent-share"
        self.assertIsNone(_find_kimi_session_dir(_SESSION_ID, {"KIMI_SHARE_DIR": str(share)}))

    def test_depth_one_dir_is_not_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            # A dir directly under sessions/ (the legacy file bucket, not a
            # session) must never be treated as a session dir.
            (share / "sessions" / _SESSION_ID).mkdir(parents=True)
            self.assertIsNone(_find_kimi_session_dir(_SESSION_ID, {"KIMI_SHARE_DIR": str(share)}))

    def test_rejects_path_traversal_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            self.assertIsNone(_find_kimi_session_dir("../../etc", {"KIMI_SHARE_DIR": str(share)}))


class TestReapKimiSession(unittest.TestCase):
    def test_removes_session_dir_keeps_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            session_dir = _make_session_dir(share, _SESSION_ID, bucket="bucket1")
            self.assertTrue(session_dir.is_dir())
            self.assertTrue(_reap_kimi_session(_SESSION_ID, {"KIMI_SHARE_DIR": str(share)}))
            self.assertFalse(session_dir.exists())
            # The bucket (work-dir md5 dir) survives — only the session is dead.
            self.assertTrue((share / "sessions" / "bucket1").is_dir())

    def test_returns_false_when_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            self.assertFalse(_reap_kimi_session(_SESSION_ID, {"KIMI_SHARE_DIR": str(share)}))

    def test_returns_false_on_invalid_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            _make_session_dir(share, _SESSION_ID, bucket="bucket1")
            # A path-traversal id must be refused before any path is touched.
            self.assertFalse(_reap_kimi_session("../../etc", {"KIMI_SHARE_DIR": str(share)}))
            self.assertTrue((share / "sessions" / "bucket1" / _SESSION_ID).is_dir())

    def test_refuses_to_reap_bucket_dirs(self) -> None:
        # If a regressed finder ever returned a bucket dir (depth 1 under the
        # sessions tree), the reap must refuse — only two-level session dirs are
        # ever removed.
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            bucket = share / "sessions" / "bucket1"
            bucket.mkdir(parents=True)
            with patch("provider_spawns.kimi_spawn._find_kimi_session_dir", return_value=bucket):
                self.assertFalse(_reap_kimi_session(_SESSION_ID, {"KIMI_SHARE_DIR": str(share)}))
            self.assertTrue(bucket.is_dir())

    def test_refuses_to_reap_outside_sessions_tree(self) -> None:
        # A finder regression pointing outside the kimi sessions subtree must be
        # refused before anything is deleted.
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            outside = Path(tmp) / "outside" / _SESSION_ID
            outside.mkdir(parents=True)
            with patch("provider_spawns.kimi_spawn._find_kimi_session_dir", return_value=outside):
                self.assertFalse(_reap_kimi_session(_SESSION_ID, {"KIMI_SHARE_DIR": str(share)}))
            self.assertTrue(outside.is_dir())


class TestSpawnKimiReapsSession(unittest.TestCase):
    """End-to-end through spawn_kimi: a clean one-shot run reaps the session dir."""

    _ANSWER_EVENTS = [
        {"role": "assistant", "content": [{"type": "text", "text": "okay"}]},
    ]
    _STDERR = "\nTo resume this session: kimi -r %s\n" % _SESSION_ID

    def _run(self, events, returncode=0, stderr_text="", share_dir=None, harvest_return=None):
        fake_proc = _make_proc_with_stderr(events, returncode=returncode, stderr_text=stderr_text)
        extra_env = {}
        if share_dir is not None:
            extra_env["KIMI_SHARE_DIR"] = str(share_dir)
        try:
            with patch("provider_spawns.kimi_spawn._start_kimi_subprocess") as mock_start, \
                    patch("provider_spawns.kimi_spawn._harvest_session_token_usage",
                          return_value=harvest_return) as mock_harvest:
                mock_start.return_value = (fake_proc, None)
                result = spawn_kimi(
                    "prompt", dispatch_id="d-reap", terminal_id="T2", extra_env=extra_env,
                )
        finally:
            fake_proc._writer_thread.join(timeout=5)
        return result, mock_harvest

    def test_clean_run_reaps_session_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            session_dir = _make_session_dir(share, _SESSION_ID, bucket="bucket1")
            result, mock_harvest = self._run(
                self._ANSWER_EVENTS, returncode=0, stderr_text=self._STDERR,
                share_dir=share, harvest_return=None,
            )
            self.assertIsNone(result.error, f"unexpected error: {result.error!r}")
            self.assertEqual(result.returncode, 0)
            # The harvest runs (it needs the export before the dir is gone).
            mock_harvest.assert_called_once()
            self.assertFalse(
                session_dir.exists(), "one-shot session dir must be reaped post-dispatch"
            )
            self.assertTrue((share / "sessions" / "bucket1").is_dir())

    def test_clean_run_reaps_even_when_harvest_yields_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            session_dir = _make_session_dir(share, _SESSION_ID, bucket="bucket1")
            result, _ = self._run(
                self._ANSWER_EVENTS, returncode=0, stderr_text=self._STDERR,
                share_dir=share,
                harvest_return={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
            )
            self.assertIsNone(result.error, f"unexpected error: {result.error!r}")
            self.assertFalse(
                session_dir.exists(), "one-shot session dir must be reaped even with tokens"
            )

    def test_no_resume_line_leaves_session_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            session_dir = _make_session_dir(share, _SESSION_ID, bucket="bucket1")
            result, mock_harvest = self._run(
                self._ANSWER_EVENTS, returncode=0, stderr_text="kimi exited normally",
                share_dir=share,
            )
            self.assertIsNone(result.error, f"unexpected error: {result.error!r}")
            mock_harvest.assert_not_called()
            self.assertTrue(session_dir.exists())

    def test_failed_dispatch_leaves_session_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            session_dir = _make_session_dir(share, _SESSION_ID, bucket="bucket1")
            events = [{"event_type": "error", "message": "rate limit exceeded"}]
            result, mock_harvest = self._run(
                events, returncode=0, stderr_text=self._STDERR, share_dir=share,
            )
            self.assertIsNotNone(result.error)
            self.assertNotEqual(result.returncode, 0)
            mock_harvest.assert_not_called()
            self.assertTrue(session_dir.exists())


if __name__ == "__main__":
    unittest.main()
