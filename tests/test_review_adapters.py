"""tests/test_review_adapters.py — Unit tests for GeminiAdapter and CodexAdapter.

Tests capability declarations, availability checks, inline file content
handling, timeout handling, and the resolve_adapter factory.

No subprocess calls hit real CLIs — all subprocess.Popen calls are patched.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure scripts/lib and scripts/lib/adapters are importable
SCRIPTS_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_LIB / "adapters"))

from provider_adapter import Capability
from adapters.gemini_adapter import GeminiAdapter
from adapters.codex_adapter import CodexAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_popen_mock(stdout_bytes: bytes, stderr_bytes: bytes = b"", returncode: int = 0):
    """Return a mock subprocess.Popen that immediately exits."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 12345

    # stdin
    stdin_mock = MagicMock()
    proc.stdin = stdin_mock

    # stdout / stderr as file-like objects with real fileno
    import io
    import os
    import tempfile

    # Write bytes to a temp file so fileno() and os.read() work
    stdout_r, stdout_w = os.pipe()
    os.write(stdout_w, stdout_bytes)
    os.close(stdout_w)

    stderr_r, stderr_w = os.pipe()
    os.write(stderr_w, stderr_bytes)
    os.close(stderr_w)

    stdout_file = os.fdopen(stdout_r, "rb")
    stderr_file = os.fdopen(stderr_r, "rb")

    proc.stdout = stdout_file
    proc.stderr = stderr_file
    proc.poll.return_value = returncode

    return proc


# ---------------------------------------------------------------------------
# GeminiAdapter capability tests
# ---------------------------------------------------------------------------

class TestGeminiAdapterCapabilities(unittest.TestCase):
    def setUp(self):
        self.adapter = GeminiAdapter("T3")

    def test_gemini_adapter_capabilities(self):
        caps = self.adapter.capabilities()
        self.assertIn(Capability.REVIEW, caps)
        self.assertIn(Capability.DIGEST, caps)

    def test_gemini_no_code_capability(self):
        caps = self.adapter.capabilities()
        self.assertNotIn(Capability.CODE, caps)

    def test_gemini_no_decision_capability(self):
        caps = self.adapter.capabilities()
        self.assertNotIn(Capability.DECISION, caps)

    def test_gemini_name(self):
        self.assertEqual(self.adapter.name(), "gemini")

    def test_gemini_supports_review(self):
        self.assertTrue(self.adapter.supports(Capability.REVIEW))

    def test_gemini_does_not_support_code(self):
        self.assertFalse(self.adapter.supports(Capability.CODE))


# ---------------------------------------------------------------------------
# CodexAdapter capability tests
# ---------------------------------------------------------------------------

class TestCodexAdapterCapabilities(unittest.TestCase):
    def setUp(self):
        self.adapter = CodexAdapter("T3")

    def test_codex_adapter_capabilities(self):
        caps = self.adapter.capabilities()
        self.assertIn(Capability.REVIEW, caps)
        self.assertIn(Capability.DECISION, caps)

    def test_codex_no_code_capability(self):
        caps = self.adapter.capabilities()
        self.assertNotIn(Capability.CODE, caps)

    def test_codex_no_digest_capability(self):
        caps = self.adapter.capabilities()
        self.assertNotIn(Capability.DIGEST, caps)

    def test_codex_name(self):
        self.assertEqual(self.adapter.name(), "codex")

    def test_codex_supports_decision(self):
        self.assertTrue(self.adapter.supports(Capability.DECISION))

    def test_codex_does_not_support_code(self):
        self.assertFalse(self.adapter.supports(Capability.CODE))


# ---------------------------------------------------------------------------
# Availability tests
# ---------------------------------------------------------------------------

class TestGeminiUnavailableWhenNoCLI(unittest.TestCase):
    def test_gemini_unavailable_when_no_cli(self):
        adapter = GeminiAdapter("T3")
        with patch("adapters.gemini_adapter.shutil.which", return_value=None):
            self.assertFalse(adapter.is_available())

    def test_gemini_available_when_cli_present(self):
        adapter = GeminiAdapter("T3")
        with patch("adapters.gemini_adapter.shutil.which", return_value="/usr/bin/gemini"):
            self.assertTrue(adapter.is_available())

    def test_codex_unavailable_when_no_cli(self):
        adapter = CodexAdapter("T3")
        with patch("adapters.codex_adapter.shutil.which", return_value=None):
            self.assertFalse(adapter.is_available())

    def test_codex_available_when_cli_present(self):
        adapter = CodexAdapter("T3")
        with patch("adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"):
            self.assertTrue(adapter.is_available())


# ---------------------------------------------------------------------------
# Inline file content tests (no PR references)
# ---------------------------------------------------------------------------

class TestCodexInlineFileContents(unittest.TestCase):
    """Verify that CodexAdapter builds prompts with inline file contents,
    not GitHub PR references."""

    def test_codex_inline_file_contents(self):
        """Prompt should contain FILE: markers, not PR number references."""
        adapter = CodexAdapter("T3")
        captured_prompt: list[str] = []

        def fake_collect(payload, *, subprocess_run):
            return "\n--- FILE: scripts/lib/foo.py ---\nprint('hello')\n"

        with patch("adapters.codex_adapter.collect_file_contents", side_effect=fake_collect):
            prompt = adapter._build_prompt(
                "Review this change for security issues.",
                changed_files=["scripts/lib/foo.py"],
            )

        self.assertIn("FILE: scripts/lib/foo.py", prompt)
        self.assertIn("Review this change for security issues.", prompt)
        # Must NOT contain PR number references
        self.assertNotIn("PR #", prompt)
        self.assertNotIn("pull request", prompt.lower())

    def test_gemini_inline_file_contents(self):
        """GeminiAdapter prompt should also use inline file contents."""
        adapter = GeminiAdapter("T3")

        def fake_collect(payload, *, subprocess_run):
            return "\n--- FILE: scripts/lib/bar.py ---\nx = 1\n"

        with patch("adapters.gemini_adapter.collect_file_contents", side_effect=fake_collect):
            prompt = adapter._build_prompt(
                "Digest these changes.",
                changed_files=["scripts/lib/bar.py"],
            )

        self.assertIn("FILE: scripts/lib/bar.py", prompt)
        self.assertNotIn("PR #", prompt)

    def test_codex_empty_files_uses_instruction_only(self):
        """When no files available, prompt is just the instruction."""
        adapter = CodexAdapter("T3")

        with patch("adapters.codex_adapter.collect_file_contents", return_value=""):
            prompt = adapter._build_prompt("Analyze security.", changed_files=[])

        self.assertEqual(prompt, "Analyze security.")


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------

class TestGeminiTimeoutHandling(unittest.TestCase):
    def test_gemini_timeout_handling(self):
        """execute() returns status='timeout' when process exceeds timeout."""
        adapter = GeminiAdapter("T3")

        def slow_drain(proc, timeout, *args, **kwargs):
            # Simulate timeout by returning "timeout" status
            return "", "", "timeout"

        with patch("adapters.gemini_adapter.shutil.which", return_value="/usr/bin/gemini"), \
             patch("adapters.gemini_adapter.collect_file_contents", return_value=""), \
             patch("adapters.gemini_adapter.subprocess.Popen") as mock_popen, \
             patch.object(GeminiAdapter, "_drain_with_timeout", side_effect=slow_drain), \
             patch.object(GeminiAdapter, "_kill"):
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.returncode = None
            mock_popen.return_value = mock_proc

            result = adapter.execute(
                "Review this.",
                context={"changed_files": []},
            )

        self.assertEqual(result.status, "timeout")
        self.assertIn("timeout", result.output.lower())
        self.assertFalse(result.committed)
        self.assertEqual(result.provider, "gemini")

    def test_codex_timeout_handling(self):
        """execute() returns status='timeout' when codex process exceeds timeout."""
        adapter = CodexAdapter("T3")

        def slow_drain(proc, timeout, stall):
            return "", "", "timeout"

        with patch("adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"), \
             patch("adapters.codex_adapter.collect_file_contents", return_value=""), \
             patch("adapters.codex_adapter.subprocess.Popen") as mock_popen, \
             patch.object(CodexAdapter, "_drain_with_stall_detection", side_effect=slow_drain), \
             patch.object(CodexAdapter, "_kill"):
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.returncode = None
            mock_popen.return_value = mock_proc

            result = adapter.execute("Analyze this.", context={"changed_files": []})

        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.provider, "codex")

    def test_codex_stall_handling(self):
        """execute() returns status='failed' when codex stalls."""
        adapter = CodexAdapter("T3")

        def stall_drain(proc, timeout, stall):
            return "", "", "stall"

        with patch("adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"), \
             patch("adapters.codex_adapter.collect_file_contents", return_value=""), \
             patch("adapters.codex_adapter.subprocess.Popen") as mock_popen, \
             patch.object(CodexAdapter, "_drain_with_stall_detection", side_effect=stall_drain), \
             patch.object(CodexAdapter, "_kill"):
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.returncode = None
            mock_popen.return_value = mock_proc

            result = adapter.execute("Analyze.", context={"changed_files": []})

        self.assertEqual(result.status, "failed")
        self.assertIn("stall", result.output.lower())


# ---------------------------------------------------------------------------
# resolve_adapter factory
# ---------------------------------------------------------------------------

class TestResolveAdapterGemini(unittest.TestCase):
    def test_resolve_adapter_gemini(self):
        """resolve_adapter returns a GeminiAdapter when VNX_PROVIDER_T3=gemini."""
        import os
        import importlib
        import adapters as _adapters_pkg

        with patch.dict(os.environ, {"VNX_PROVIDER_T3": "gemini"}):
            adapter = _adapters_pkg.resolve_adapter("T3")

        self.assertIsInstance(adapter, GeminiAdapter)

    def test_resolve_adapter_gemini_capabilities(self):
        """GeminiAdapter resolved from factory has correct capabilities."""
        import os
        import adapters as _adapters_pkg

        with patch.dict(os.environ, {"VNX_PROVIDER_T3": "gemini"}):
            adapter = _adapters_pkg.resolve_adapter("T3")

        self.assertIn(Capability.REVIEW, adapter.capabilities())
        self.assertNotIn(Capability.CODE, adapter.capabilities())


class TestResolveAdapterCodex(unittest.TestCase):
    def test_resolve_adapter_codex(self):
        """resolve_adapter returns a CodexAdapter when VNX_PROVIDER_T3=codex."""
        import os
        import adapters as _adapters_pkg

        with patch.dict(os.environ, {"VNX_PROVIDER_T3": "codex"}):
            adapter = _adapters_pkg.resolve_adapter("T3")

        self.assertIsInstance(adapter, CodexAdapter)

    def test_resolve_adapter_codex_capabilities(self):
        """CodexAdapter resolved from factory has correct capabilities."""
        import os
        import adapters as _adapters_pkg

        with patch.dict(os.environ, {"VNX_PROVIDER_T3": "codex"}):
            adapter = _adapters_pkg.resolve_adapter("T3")

        self.assertIn(Capability.DECISION, adapter.capabilities())
        self.assertNotIn(Capability.CODE, adapter.capabilities())

    def test_resolve_adapter_unknown_raises(self):
        """resolve_adapter raises ValueError for unknown provider names."""
        import os
        import adapters as _adapters_pkg

        with patch.dict(os.environ, {"VNX_PROVIDER_T3": "ollama"}):
            with self.assertRaises(ValueError) as ctx:
                _adapters_pkg.resolve_adapter("T3")
        self.assertIn("ollama", str(ctx.exception))

    def test_resolve_adapter_default_is_claude(self):
        """resolve_adapter defaults to ClaudeAdapter when env var is unset."""
        import os
        import adapters as _adapters_pkg
        from adapters.claude_adapter import ClaudeAdapter

        env = {k: v for k, v in os.environ.items() if k != "VNX_PROVIDER_T3"}
        with patch.dict(os.environ, env, clear=True):
            adapter = _adapters_pkg.resolve_adapter("T3")

        self.assertIsInstance(adapter, ClaudeAdapter)


# ---------------------------------------------------------------------------
# NDJSON parsing (codex)
# ---------------------------------------------------------------------------

class TestCodexNDJSONParsing(unittest.TestCase):
    def test_parses_agent_message_events(self):
        ndjson = "\n".join([
            json.dumps({"type": "agent_message", "content": "Finding: SQL injection risk"}),
            json.dumps({"type": "agent_message", "content": "Finding: no input validation"}),
            json.dumps({"type": "status", "value": "done"}),
        ])
        events, findings = CodexAdapter._parse_ndjson(ndjson)
        self.assertEqual(len(events), 3)
        self.assertIn("SQL injection risk", findings)
        self.assertIn("no input validation", findings)

    def test_skips_malformed_lines(self):
        ndjson = "not json\n" + json.dumps({"type": "agent_message", "content": "ok"})
        events, findings = CodexAdapter._parse_ndjson(ndjson)
        self.assertEqual(len(events), 1)
        self.assertIn("ok", findings)

    def test_falls_back_to_raw_when_no_message_events(self):
        ndjson = json.dumps({"type": "status", "value": "done"})
        events, findings = CodexAdapter._parse_ndjson(ndjson)
        # No agent_message events — findings is the raw stripped text
        self.assertEqual(findings, ndjson.strip())


# ---------------------------------------------------------------------------
# Gemini response parsing
# ---------------------------------------------------------------------------

class TestGeminiResponseParsing(unittest.TestCase):
    def test_extracts_response_key(self):
        raw = json.dumps({"response": "looks good", "model": "gemini-2.5-flash"})
        result = GeminiAdapter._parse_response(raw)
        self.assertEqual(result, "looks good")

    def test_extracts_text_key(self):
        raw = json.dumps({"text": "review findings here"})
        result = GeminiAdapter._parse_response(raw)
        self.assertEqual(result, "review findings here")

    def test_falls_back_to_raw_on_non_json(self):
        raw = "plain text response"
        result = GeminiAdapter._parse_response(raw)
        self.assertEqual(result, "plain text response")

    def test_falls_back_to_stripped_on_unknown_keys(self):
        raw = json.dumps({"verdict": "pass", "findings": []})
        result = GeminiAdapter._parse_response(raw)
        # No recognized key → returns the raw JSON stripped
        self.assertEqual(result, raw.strip())


if __name__ == "__main__":
    unittest.main()
