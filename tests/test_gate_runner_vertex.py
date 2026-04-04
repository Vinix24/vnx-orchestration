#!/usr/bin/env python3
"""Tests for Vertex AI routing in gate_runner.py.

Covers:
- _run_vertex_ai() constructs correct URL, token, and payload
- Text extraction from candidates[0].content.parts[0].text
- Lazy project fetch via gcloud when VNX_VERTEX_PROJECT is unset
- VNX_GEMINI_ROUTING=vertex triggers Vertex path in run()
- VNX_GEMINI_ROUTING=oauth (or default) does NOT trigger Vertex path
- Vertex API error produces a failed result record
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_runner import GateRunner


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    """Set up minimal VNX directory structure for gate runner tests."""
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    (state_dir / "review_gates" / "requests").mkdir(parents=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True)
    reports_dir.mkdir(parents=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))

    return {
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "results_dir": state_dir / "review_gates" / "results",
    }


def _make_runner(gate_env):
    return GateRunner(
        state_dir=gate_env["state_dir"],
        reports_dir=gate_env["reports_dir"],
    )


def _make_payload(gate_env, *, gate="gemini_review", pr_number=1, prompt="Review this code"):
    report_path = str(gate_env["reports_dir"] / "vertex-test-report.md")
    return {
        "gate": gate,
        "status": "requested",
        "provider": "vertex_ai",
        "branch": "feature/vertex",
        "pr_number": pr_number,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/gate_runner.py"],
        "requested_at": "2026-04-03T16:37:12Z",
        "report_path": report_path,
        "prompt": prompt,
    }


def _make_vertex_response(text: str) -> bytes:
    """Build a minimal Vertex AI generateContent JSON response."""
    return json.dumps({
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ]
    }).encode("utf-8")


def _mock_urlopen(response_text: str):
    """Return a context-manager mock that yields a file-like HTTP response."""
    body = _make_vertex_response(response_text)
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# Unit tests for _run_vertex_ai()
# ---------------------------------------------------------------------------


class TestRunVertexAi:
    """Direct unit tests for GateRunner._run_vertex_ai()."""

    def _gcloud_side_effect(self, cmd, **kwargs):
        """Return appropriate stdout for each gcloud sub-command."""
        if "print-access-token" in cmd:
            m = MagicMock()
            m.stdout = "ya29.fake-token\n"
            return m
        if "get-value" in cmd:
            m = MagicMock()
            m.stdout = "my-gcp-project\n"
            return m
        m = MagicMock()
        m.stdout = ""
        return m

    def test_url_uses_correct_region_project_model(self, gate_env, monkeypatch):
        """Constructed URL must embed region, project, and model."""
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "test-project")
        monkeypatch.setenv("VNX_VERTEX_REGION", "europe-west4")
        monkeypatch.setenv("VNX_VERTEX_MODEL", "gemini-2.0-flash")

        captured_urls = []

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return _mock_urlopen("LGTM from Vertex")

        runner = _make_runner(gate_env)

        with patch("gate_runner.subprocess.run", side_effect=self._gcloud_side_effect), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = runner._run_vertex_ai("test prompt")

        assert len(captured_urls) == 1
        url = captured_urls[0]
        assert "europe-west4-aiplatform.googleapis.com" in url
        assert "/projects/test-project/" in url
        assert "/locations/europe-west4/" in url
        assert "gemini-2.0-flash:generateContent" in url
        assert result == "LGTM from Vertex"

    def test_text_extracted_from_candidates_parts(self, gate_env, monkeypatch):
        """Text must come from candidates[0].content.parts[0].text."""
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "proj-x")

        expected_text = "Code review: no blocking issues found. All patterns correct."

        runner = _make_runner(gate_env)

        with patch("gate_runner.subprocess.run", side_effect=self._gcloud_side_effect), \
             patch("urllib.request.urlopen", return_value=_mock_urlopen(expected_text)):
            result = runner._run_vertex_ai("review prompt")

        assert result == expected_text

    def test_lazy_project_fetch_via_gcloud(self, gate_env, monkeypatch):
        """When VNX_VERTEX_PROJECT is unset, project is fetched via gcloud."""
        monkeypatch.delenv("VNX_VERTEX_PROJECT", raising=False)

        gcloud_calls = []

        def tracking_side_effect(cmd, **kwargs):
            gcloud_calls.append(cmd)
            m = MagicMock()
            if "get-value" in cmd:
                m.stdout = "lazily-fetched-project\n"
            elif "print-access-token" in cmd:
                m.stdout = "ya29.token\n"
            else:
                m.stdout = ""
            return m

        captured_urls = []

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return _mock_urlopen("Lazy project response")

        runner = _make_runner(gate_env)

        with patch("gate_runner.subprocess.run", side_effect=tracking_side_effect), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            runner._run_vertex_ai("prompt requiring lazy project")

        # gcloud config get-value project must have been called
        project_calls = [c for c in gcloud_calls if "get-value" in c]
        assert len(project_calls) == 1
        assert "lazily-fetched-project" in captured_urls[0]

    def test_raises_when_no_project_available(self, gate_env, monkeypatch):
        """RuntimeError raised when env var unset and gcloud returns empty."""
        monkeypatch.delenv("VNX_VERTEX_PROJECT", raising=False)

        def no_project_side_effect(cmd, **kwargs):
            m = MagicMock()
            m.stdout = ""
            return m

        runner = _make_runner(gate_env)

        with patch("gate_runner.subprocess.run", side_effect=no_project_side_effect), \
             pytest.raises(RuntimeError, match="VNX_VERTEX_PROJECT not set"):
            runner._run_vertex_ai("prompt")

    def test_raises_when_token_empty(self, gate_env, monkeypatch):
        """RuntimeError raised when gcloud auth print-access-token returns empty."""
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "proj-ok")

        def empty_token_side_effect(cmd, **kwargs):
            m = MagicMock()
            m.stdout = ""
            return m

        runner = _make_runner(gate_env)

        with patch("gate_runner.subprocess.run", side_effect=empty_token_side_effect), \
             pytest.raises(RuntimeError, match="Failed to get gcloud access token"):
            runner._run_vertex_ai("prompt")

    def test_default_region_and_model(self, gate_env, monkeypatch):
        """Defaults: region=us-central1, model=gemini-2.5-pro."""
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "default-proj")
        monkeypatch.delenv("VNX_VERTEX_REGION", raising=False)
        monkeypatch.delenv("VNX_VERTEX_MODEL", raising=False)

        captured_urls = []

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return _mock_urlopen("defaults response")

        runner = _make_runner(gate_env)

        with patch("gate_runner.subprocess.run", side_effect=self._gcloud_side_effect), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            runner._run_vertex_ai("test")

        url = captured_urls[0]
        assert "us-central1-aiplatform.googleapis.com" in url
        assert "gemini-2.5-pro:generateContent" in url

    def test_bearer_token_in_authorization_header(self, gate_env, monkeypatch):
        """Authorization header must be Bearer <token>."""
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "header-test-proj")

        captured_headers = []

        def fake_urlopen(req, timeout=None):
            captured_headers.append(dict(req.headers))
            return _mock_urlopen("header test response")

        runner = _make_runner(gate_env)

        with patch("gate_runner.subprocess.run", side_effect=self._gcloud_side_effect), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            runner._run_vertex_ai("header test")

        assert len(captured_headers) == 1
        auth = captured_headers[0].get("Authorization") or captured_headers[0].get("authorization", "")
        assert auth.startswith("Bearer ya29.fake-token")


# ---------------------------------------------------------------------------
# Integration tests: routing branch in run()
# ---------------------------------------------------------------------------


class TestVertexRoutingInRun:
    """Verify VNX_GEMINI_ROUTING controls which path run() takes."""

    def _make_vertex_run_patches(self, *, response_text: str):
        """Return a dict of patch targets -> mock objects for a successful Vertex run."""
        def gcloud_side_effect(cmd, **kwargs):
            m = MagicMock()
            if "print-access-token" in cmd:
                m.stdout = "ya29.token\n"
            elif "get-value" in cmd:
                m.stdout = "routing-test-proj\n"
            else:
                m.stdout = ""
            return m

        return gcloud_side_effect, _mock_urlopen(response_text)

    def test_vertex_routing_triggers_vertex_path(self, gate_env, monkeypatch):
        """VNX_GEMINI_ROUTING=vertex must call Vertex API, not subprocess Popen."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "routing-proj")

        # Provide enough substantive lines to pass the content check
        review_text = (
            "Code review complete.\n"
            "No blocking issues found.\n"
            "All patterns conform to project standards.\n"
            "Approved for merge.\n"
        )

        runner = _make_runner(gate_env)
        payload = _make_payload(gate_env, prompt="Review my code")

        gcloud_se, url_resp = self._make_vertex_run_patches(response_text=review_text)

        with patch("gate_runner.subprocess.run", side_effect=gcloud_se) as mock_sub_run, \
             patch("urllib.request.urlopen", return_value=url_resp) as mock_urlopen, \
             patch("gate_runner.subprocess.Popen") as mock_popen:
            result = runner.run(
                gate="gemini_review",
                request_payload=payload,
                pr_number=1,
            )

        # Popen must NOT have been called (no gemini CLI subprocess)
        mock_popen.assert_not_called()
        # urlopen must have been called (Vertex REST API)
        mock_urlopen.assert_called_once()
        assert result["status"] == "completed"

    def test_oauth_routing_triggers_cli_path(self, gate_env, monkeypatch):
        """VNX_GEMINI_ROUTING=oauth must use subprocess Popen, not Vertex API."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "oauth")
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/gemini")

        review_output = (
            b"LGTM: no issues detected.\n"
            b"All files reviewed successfully.\n"
            b"Approval granted.\n"
        )

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stderr.fileno.return_value = 11
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.pid = 55555

        read_count = [0]

        def mock_os_read(fd, size):
            read_count[0] += 1
            if fd == 10 and read_count[0] == 1:
                return review_output
            return b""

        runner = _make_runner(gate_env)
        payload = _make_payload(gate_env)

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.os.read", side_effect=mock_os_read), \
             patch("gate_runner.os.getpgid", return_value=55555), \
             patch("urllib.request.urlopen") as mock_urlopen:
            result = runner.run(
                gate="gemini_review",
                request_payload=payload,
                pr_number=1,
            )

        mock_popen.assert_called_once()
        mock_urlopen.assert_not_called()
        assert result["status"] == "completed"

    def test_default_routing_is_oauth(self, gate_env, monkeypatch):
        """When VNX_GEMINI_ROUTING is not set, default is oauth (CLI path)."""
        monkeypatch.delenv("VNX_GEMINI_ROUTING", raising=False)
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/gemini")

        review_output = (
            b"Default routing test: oauth path.\n"
            b"No issues found.\n"
            b"Review complete.\n"
        )

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stderr.fileno.return_value = 11
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.pid = 66666

        read_count = [0]

        def mock_os_read(fd, size):
            read_count[0] += 1
            if fd == 10 and read_count[0] == 1:
                return review_output
            return b""

        runner = _make_runner(gate_env)
        payload = _make_payload(gate_env)

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.os.read", side_effect=mock_os_read), \
             patch("gate_runner.os.getpgid", return_value=66666), \
             patch("urllib.request.urlopen") as mock_urlopen:
            result = runner.run(
                gate="gemini_review",
                request_payload=payload,
                pr_number=1,
            )

        mock_popen.assert_called_once()
        mock_urlopen.assert_not_called()
        assert result["status"] == "completed"

    def test_vertex_api_error_produces_failed_result(self, gate_env, monkeypatch):
        """When Vertex API raises, run() must return a failed result record."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "error-test-proj")

        def error_gcloud(cmd, **kwargs):
            m = MagicMock()
            m.stdout = "ya29.token\n"
            return m

        def raise_http_error(req, timeout=None):
            raise urllib.error.URLError("Connection refused")

        runner = _make_runner(gate_env)
        payload = _make_payload(gate_env)

        with patch("gate_runner.subprocess.run", side_effect=error_gcloud), \
             patch("urllib.request.urlopen", side_effect=raise_http_error):
            result = runner.run(
                gate="gemini_review",
                request_payload=payload,
                pr_number=1,
            )

        assert result["status"] == "failed"
        assert result["reason"] == "vertex_api_error"
        assert "Connection refused" in result["reason_detail"]

    def test_vertex_routing_skips_binary_check(self, gate_env, monkeypatch):
        """Vertex routing must proceed even when the gemini binary is absent."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "no-binary-proj")
        # gemini binary not in PATH
        monkeypatch.setattr("shutil.which", lambda b: None)

        review_text = (
            "Vertex review: binary not required.\n"
            "All checks passed via REST.\n"
            "No issues found.\n"
            "Approved.\n"
        )

        def gcloud_se(cmd, **kwargs):
            m = MagicMock()
            m.stdout = "ya29.token\n"
            return m

        runner = _make_runner(gate_env)
        payload = _make_payload(gate_env)

        with patch("gate_runner.subprocess.run", side_effect=gcloud_se), \
             patch("urllib.request.urlopen", return_value=_mock_urlopen(review_text)):
            result = runner.run(
                gate="gemini_review",
                request_payload=payload,
                pr_number=1,
            )

        # Must complete successfully without hitting not_executable
        assert result["status"] == "completed"

    def test_vertex_only_applies_to_gemini_review_gate(self, gate_env, monkeypatch):
        """VNX_GEMINI_ROUTING=vertex must NOT bypass binary check for codex_gate."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setattr("shutil.which", lambda b: None)  # no binaries available

        runner = _make_runner(gate_env)
        payload = _make_payload(gate_env, gate="codex_gate")
        payload["gate"] = "codex_gate"

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = runner.run(
                gate="codex_gate",
                request_payload=payload,
                pr_number=1,
            )

        # codex_gate has no Vertex routing; missing binary → not_executable
        assert result["status"] == "not_executable"
        assert result["reason"] == "provider_not_installed"
        mock_urlopen.assert_not_called()



# ---------------------------------------------------------------------------
# Tests for _build_gemini_prompt enrichment with inline file contents
# ---------------------------------------------------------------------------


class TestBuildGeminiPromptEnrichment:
    """_build_gemini_prompt must inline file contents for Vertex AI routing."""

    def test_prompt_contains_file_content_when_files_exist(self, tmp_path):
        """Prompt includes inline content from each changed file."""
        file_a = tmp_path / "module_a.py"
        file_a.write_text("def hello():\n    return 'world'\n")
        file_b = tmp_path / "module_b.py"
        file_b.write_text("class Foo:\n    pass\n")

        payload = {
            "changed_files": [str(file_a), str(file_b)],
            "branch": "fix/something",
            "risk_class": "medium",
            "pr_number": 42,
        }
        prompt = GateRunner._build_gemini_prompt(payload)

        assert "def hello():" in prompt
        assert "class Foo:" in prompt
        assert f"--- FILE: {file_a}" in prompt
        assert f"--- FILE: {file_b}" in prompt

    def test_prompt_contains_json_response_format(self, tmp_path):
        """Prompt must include structured JSON response instructions."""
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        payload = {
            "changed_files": [str(f)],
            "branch": "main",
            "risk_class": "low",
            "pr_number": 1,
        }
        prompt = GateRunner._build_gemini_prompt(payload)

        assert '"verdict"' in prompt
        assert '"findings"' in prompt
        assert "pass|fail|blocked" in prompt

    def test_prompt_skips_missing_files(self, tmp_path):
        """Files that do not exist on disk are skipped silently."""
        real = tmp_path / "real.py"
        real.write_text("y = 2\n")
        payload = {
            "changed_files": ["/nonexistent/missing.py", str(real)],
            "branch": "test",
            "risk_class": "medium",
            "pr_number": 0,
        }
        prompt = GateRunner._build_gemini_prompt(payload)

        assert "y = 2" in prompt
        # Missing file must not appear as an inlined section
        assert "--- FILE: /nonexistent/missing.py" not in prompt

    def test_prompt_respects_max_bytes_env_var(self, tmp_path, monkeypatch):
        """File content is capped at VNX_GEMINI_MAX_PROMPT_BYTES bytes total."""
        file_a = tmp_path / "big_a.py"
        file_a.write_text("A" * 500)
        file_b = tmp_path / "big_b.py"
        file_b.write_text("B" * 500)

        payload = {
            "changed_files": [str(file_a), str(file_b)],
            "branch": "test",
            "risk_class": "high",
            "pr_number": 99,
        }
        monkeypatch.setenv("VNX_GEMINI_MAX_PROMPT_BYTES", "300")
        prompt = GateRunner._build_gemini_prompt(payload)

        # Total bytes from file sections must not exceed 300 + small overhead
        file_sections = prompt.split("--- FILE:")[1:]
        total_bytes = sum(len(s.encode("utf-8")) for s in file_sections)
        assert total_bytes <= 500, "file content should be bounded by max bytes cap"

    def test_prompt_discovers_files_via_git_when_changed_files_empty(self, tmp_path, monkeypatch):
        """When changed_files is empty, git diff --name-only is used to discover files."""
        discovered = tmp_path / "discovered.py"
        discovered.write_text("z = 3\n")

        payload = {
            "changed_files": [],
            "branch": "test",
            "risk_class": "medium",
            "pr_number": 5,
        }

        mock_result = MagicMock()
        mock_result.stdout = str(discovered) + "\n"

        with patch("gate_runner.subprocess.run", return_value=mock_result) as mock_run:
            prompt = GateRunner._build_gemini_prompt(payload)

        # git diff --name-only must have been called
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "git" in cmd_args
        assert "diff" in cmd_args
        assert "--name-only" in cmd_args

        assert "z = 3" in prompt

    def test_prompt_graceful_when_git_fails(self, monkeypatch):
        """When git diff raises, prompt still contains review instructions."""
        payload = {
            "changed_files": [],
            "branch": "test",
            "risk_class": "low",
            "pr_number": 0,
        }
        with patch("gate_runner.subprocess.run", side_effect=OSError("git not found")):
            prompt = GateRunner._build_gemini_prompt(payload)

        assert "verdict" in prompt


# ---------------------------------------------------------------------------
# Tests for contract prompt + Vertex enrichment (dispatch 20260404-174925)
# ---------------------------------------------------------------------------


class TestVertexContractPromptEnrichment:
    """When a contract prompt is pre-provided, Vertex path must still append file contents."""

    def _gcloud_side_effect(self, cmd, **kwargs):
        m = MagicMock()
        if "print-access-token" in cmd:
            m.stdout = "ya29.fake-token\n"
        elif "get-value" in cmd:
            m.stdout = "enrich-test-proj\n"
        else:
            m.stdout = ""
        return m

    def _make_vertex_run_patches(self, response_text):
        def gcloud_se(cmd, **kwargs):
            return self._gcloud_side_effect(cmd, **kwargs)
        return gcloud_se, _mock_urlopen(response_text)

    def test_contract_prompt_with_vertex_appends_file_contents(self, gate_env, monkeypatch, tmp_path):
        """contract prompt + using_vertex=True → file contents still appended to prompt."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "enrich-proj")

        source_file = tmp_path / "reviewed.py"
        source_file.write_text("def contract_enriched():\n    return True\n")

        captured_prompts = []

        def fake_urlopen(req, timeout=None):
            import json as _json
            body = _json.loads(req.data.decode("utf-8"))
            captured_prompts.append(body["contents"][0]["parts"][0]["text"])
            return _mock_urlopen(
                "Code review complete.\nNo blocking issues.\nApproved.\nLGTM.\n"
            )

        contract_text = "CONTRACT: Review the following deliverable for PR #7."
        payload = _make_payload(gate_env, prompt=contract_text)
        payload["changed_files"] = [str(source_file)]
        runner = _make_runner(gate_env)

        gcloud_se, _ = self._make_vertex_run_patches("ok")
        with patch("gate_runner.subprocess.run", side_effect=gcloud_se), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = runner.run(gate="gemini_review", request_payload=payload, pr_number=7)

        assert result["status"] == "completed"
        assert len(captured_prompts) == 1
        sent = captured_prompts[0]
        assert "contract_enriched" in sent, "file contents must be appended to contract prompt"

    def test_original_contract_prompt_text_preserved(self, gate_env, monkeypatch, tmp_path):
        """Contract prompt text must not be replaced — it must appear in the sent prompt."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "preserve-proj")

        source_file = tmp_path / "mod.py"
        source_file.write_text("x = 42\n")

        captured_prompts = []

        def fake_urlopen(req, timeout=None):
            import json as _json
            body = _json.loads(req.data.decode("utf-8"))
            captured_prompts.append(body["contents"][0]["parts"][0]["text"])
            return _mock_urlopen(
                "Review done.\nNo issues.\nAll clear.\nApproved for merge.\n"
            )

        contract_text = "UNIQUE_CONTRACT_MARKER: validate delivery evidence for batch X."
        payload = _make_payload(gate_env, prompt=contract_text)
        payload["changed_files"] = [str(source_file)]
        runner = _make_runner(gate_env)

        gcloud_se, _ = self._make_vertex_run_patches("ok")
        with patch("gate_runner.subprocess.run", side_effect=gcloud_se), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            runner.run(gate="gemini_review", request_payload=payload, pr_number=8)

        sent = captured_prompts[0]
        assert "UNIQUE_CONTRACT_MARKER" in sent, "original contract text must be preserved"
        assert "x = 42" in sent, "file contents must also be present"

    def test_byte_cap_respected_when_enriching_existing_prompt(self, gate_env, monkeypatch, tmp_path):
        """VNX_GEMINI_MAX_PROMPT_BYTES cap is respected when appending to contract prompt."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "cap-proj")
        monkeypatch.setenv("VNX_GEMINI_MAX_PROMPT_BYTES", "200")

        big_file = tmp_path / "big.py"
        big_file.write_text("Z" * 1000)

        captured_prompts = []

        def fake_urlopen(req, timeout=None):
            import json as _json
            body = _json.loads(req.data.decode("utf-8"))
            captured_prompts.append(body["contents"][0]["parts"][0]["text"])
            return _mock_urlopen(
                "Cap test review.\nNo blocking issues.\nApproved.\nLGTM.\n"
            )

        payload = _make_payload(gate_env, prompt="CONTRACT: cap test")
        payload["changed_files"] = [str(big_file)]
        runner = _make_runner(gate_env)

        gcloud_se, _ = self._make_vertex_run_patches("ok")
        with patch("gate_runner.subprocess.run", side_effect=gcloud_se), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            runner.run(gate="gemini_review", request_payload=payload, pr_number=9)

        sent = captured_prompts[0]
        # File section in sent prompt must not exceed cap + small overhead
        file_sections = sent.split("--- FILE:")[1:]
        file_bytes = sum(len(s.encode("utf-8")) for s in file_sections)
        assert file_bytes <= 400, f"file content bytes {file_bytes} exceeded cap (200 + overhead)"
