#!/usr/bin/env python3
"""Validation tests for Vertex AI routing path (VNX_GEMINI_ROUTING=vertex).

Covers:
- Service account credential flow: GOOGLE_APPLICATION_CREDENTIALS → gcloud token
- gate_runner routes to Vertex when VNX_GEMINI_ROUTING=vertex
- gate_runner routes to CLI when VNX_GEMINI_ROUTING is unset
- Vertex runner produces the same result schema as the CLI runner
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_runner
from gate_runner import GateRunner
import vertex_ai_runner as _vtx


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_gate_worktree(tmp_path, monkeypatch):
    """Default OI-708 worktree checkout to a no-op fake — see test_gate_runner.py."""
    fake_path = tmp_path / "_fake_gate_worktree"
    monkeypatch.setattr(gate_runner, "create_gate_worktree", lambda **kw: fake_path)
    monkeypatch.setattr(gate_runner, "remove_gate_worktree", lambda *a, **kw: None)
    return fake_path


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
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
    }


def _make_runner(gate_env):
    return GateRunner(
        state_dir=gate_env["state_dir"],
        reports_dir=gate_env["reports_dir"],
    )


def _make_payload(gate_env, *, pr_number=42, prompt="Review this code for correctness"):
    return {
        "gate": "gemini_review",
        "status": "requested",
        "branch": "feat/vertex-test",
        "pr_number": pr_number,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/gate_runner.py"],
        "requested_at": "2026-05-13T10:00:00Z",
        "report_path": str(gate_env["reports_dir"] / "vertex-routing-test.md"),
        "prompt": prompt,
    }


def _make_vertex_response(text: str) -> bytes:
    return json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": text}], "role": "model"},
            "finishReason": "STOP",
        }]
    }).encode("utf-8")


def _mock_urlopen(response_text: str):
    resp = MagicMock()
    resp.read.return_value = _make_vertex_response(response_text)
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _gcloud_sa_side_effect(cmd, **kwargs):
    """Simulate gcloud behaviour when GOOGLE_APPLICATION_CREDENTIALS is set.

    When a service-account JSON is present, `gcloud auth print-access-token`
    returns a bearer token derived from that credential.  This mock simulates
    a successful SA-based token fetch and a valid project lookup.
    """
    m = MagicMock()
    if "print-access-token" in cmd:
        m.stdout = "ya29.sa-derived-token\n"
    elif "get-value" in cmd:
        m.stdout = "sa-test-project\n"
    else:
        m.stdout = ""
    return m


# ---------------------------------------------------------------------------
# Test 1: service-account credential path
# ---------------------------------------------------------------------------


class TestVertexRunnerUsesServiceAccountCredentials:
    """GOOGLE_APPLICATION_CREDENTIALS → gcloud derives SA-based bearer token."""

    def test_vertex_runner_uses_service_account_credentials(
        self, gate_env, tmp_path, monkeypatch
    ):
        """When GOOGLE_APPLICATION_CREDENTIALS is set, gcloud auth print-access-token
        is called and the resulting SA-derived token is used in the Authorization header.

        The runner delegates token acquisition to the gcloud subprocess, which
        automatically picks up GOOGLE_APPLICATION_CREDENTIALS — the same path used
        by Vertex operator setup (service-account JSON key).
        """
        sa_json = tmp_path / "service-account.json"
        sa_json.write_text('{"type":"service_account","project_id":"sa-test-project"}')

        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(sa_json))
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "sa-test-project")
        monkeypatch.delenv("VNX_VERTEX_REGION", raising=False)
        monkeypatch.delenv("VNX_VERTEX_MODEL", raising=False)

        captured_auth_headers = []
        gcloud_calls = []

        def tracking_gcloud(cmd, **kwargs):
            gcloud_calls.append(list(cmd))
            return _gcloud_sa_side_effect(cmd, **kwargs)

        def fake_urlopen(req, timeout=None):
            captured_auth_headers.append(
                req.get_header("Authorization") or req.get_header("authorization") or ""
            )
            return _mock_urlopen(
                "Service account review complete.\n"
                "No blocking findings.\n"
                "All checks passed.\n"
            )

        with patch("gate_runner.subprocess.run", side_effect=tracking_gcloud), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            runner = _make_runner(gate_env)
            text = runner._run_vertex_ai("Review this PR for correctness")

        # gcloud auth print-access-token must have been invoked
        token_calls = [c for c in gcloud_calls if "print-access-token" in c]
        assert len(token_calls) == 1, "gcloud auth print-access-token must be called once"

        # SA-derived bearer token must appear in Authorization header
        assert len(captured_auth_headers) == 1
        assert captured_auth_headers[0].startswith("Bearer ya29.sa-derived-token"), (
            f"Expected SA-derived Bearer token, got: {captured_auth_headers[0]}"
        )

        assert "complete" in text.lower()

    def test_missing_sa_file_does_not_block_gcloud_invocation(
        self, gate_env, monkeypatch
    ):
        """GOOGLE_APPLICATION_CREDENTIALS set to non-existent path does not prevent
        gcloud subprocess call; authentication outcome is gcloud's responsibility."""
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/sa.json")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "fallback-proj")

        gcloud_invoked = []

        def mock_gcloud(cmd, **kwargs):
            gcloud_invoked.append(list(cmd))
            return _gcloud_sa_side_effect(cmd, **kwargs)

        with patch("gate_runner.subprocess.run", side_effect=mock_gcloud), \
             patch("urllib.request.urlopen", return_value=_mock_urlopen(
                 "Fallback review.\nNo issues.\nAll clear.\n"
             )):
            runner = _make_runner(gate_env)
            runner._run_vertex_ai("test prompt")

        assert any("print-access-token" in c for c in gcloud_invoked), (
            "gcloud must still be invoked; validation of the SA path is gcloud's job"
        )


# ---------------------------------------------------------------------------
# Test 2: gate_runner routes to Vertex when VNX_GEMINI_ROUTING=vertex
# ---------------------------------------------------------------------------


class TestGateRunnerRoutesToVertexWhenEnvSet:
    """VNX_GEMINI_ROUTING=vertex → Vertex REST path, no Popen."""

    def test_gate_runner_routes_to_vertex_when_env_set(self, gate_env, monkeypatch):
        """Setting VNX_GEMINI_ROUTING=vertex must route the gemini_review gate
        through _run_vertex_path() — Popen must not be called."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "vertex-routing-proj")

        review_text = (
            "Code review via Vertex AI.\n"
            "No blocking findings detected.\n"
            "All patterns conform to project standards.\n"
            "Approved for merge.\n"
        )

        with patch("gate_runner.subprocess.run", side_effect=_gcloud_sa_side_effect), \
             patch("urllib.request.urlopen", return_value=_mock_urlopen(review_text)) as mock_urlopen, \
             patch("gate_runner.subprocess.Popen") as mock_popen:
            runner = _make_runner(gate_env)
            result = runner.run(
                gate="gemini_review",
                request_payload=_make_payload(gate_env),
                pr_number=42,
            )

        mock_popen.assert_not_called()
        mock_urlopen.assert_called_once()
        assert result["status"] == "completed"
        assert result["gate"] == "gemini_review"

    def test_vertex_routing_bypasses_binary_check(self, gate_env, monkeypatch):
        """Vertex path must proceed even when the `gemini` binary is absent from PATH."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "no-binary-proj")
        monkeypatch.setattr("shutil.which", lambda _: None)

        review_text = (
            "Vertex review: binary-free path.\n"
            "All checks passed via REST.\n"
            "No blocking issues found.\n"
            "Approved.\n"
        )

        with patch("gate_runner.subprocess.run", side_effect=_gcloud_sa_side_effect), \
             patch("urllib.request.urlopen", return_value=_mock_urlopen(review_text)):
            runner = _make_runner(gate_env)
            result = runner.run(
                gate="gemini_review",
                request_payload=_make_payload(gate_env),
                pr_number=42,
            )

        assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# Test 3: gate_runner routes to CLI when VNX_GEMINI_ROUTING is unset
# ---------------------------------------------------------------------------


class TestGateRunnerRoutesToCliWhenEnvUnset:
    """VNX_GEMINI_ROUTING unset → CLI subprocess path, urlopen not called."""

    def _make_mock_proc(self, review_output: bytes):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stderr.fileno.return_value = 11
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.pid = 77777

        read_count = [0]

        def mock_os_read(fd, size):
            read_count[0] += 1
            if fd == 10 and read_count[0] == 1:
                return review_output
            return b""

        return mock_proc, mock_os_read

    def test_gate_runner_routes_to_cli_when_env_unset(self, gate_env, monkeypatch):
        """Unset VNX_GEMINI_ROUTING must use the subprocess CLI path."""
        monkeypatch.delenv("VNX_GEMINI_ROUTING", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gemini")

        review_output = (
            b"CLI path review: no issues found.\n"
            b"All files reviewed successfully.\n"
            b"Approval granted.\n"
        )
        mock_proc, mock_os_read = self._make_mock_proc(review_output)

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.os.read", side_effect=mock_os_read), \
             patch("gate_runner.os.getpgid", return_value=77777), \
             patch("urllib.request.urlopen") as mock_urlopen:
            runner = _make_runner(gate_env)
            result = runner.run(
                gate="gemini_review",
                request_payload=_make_payload(gate_env),
                pr_number=42,
            )

        mock_popen.assert_called_once()
        mock_urlopen.assert_not_called()
        assert result["status"] == "completed"

    def test_gate_runner_routes_to_cli_when_env_set_to_oauth(self, gate_env, monkeypatch):
        """Explicit VNX_GEMINI_ROUTING=oauth must also use the CLI path."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "oauth")
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gemini")

        review_output = (
            b"OAuth CLI path: no issues.\n"
            b"Review complete via subprocess.\n"
            b"All clear.\n"
        )
        mock_proc, mock_os_read = self._make_mock_proc(review_output)

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.os.read", side_effect=mock_os_read), \
             patch("gate_runner.os.getpgid", return_value=77777), \
             patch("urllib.request.urlopen") as mock_urlopen:
            runner = _make_runner(gate_env)
            result = runner.run(
                gate="gemini_review",
                request_payload=_make_payload(gate_env),
                pr_number=42,
            )

        mock_popen.assert_called_once()
        mock_urlopen.assert_not_called()
        assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# Test 4: Vertex runner output schema matches CLI runner output
# ---------------------------------------------------------------------------


class TestVertexRunnerReturnsGateResultShape:
    """Vertex path result record has the same schema as CLI runner output.

    gate_recorder / gate_artifacts produce a canonical result shape.  The
    Vertex path feeds raw text to the same materialize_artifacts() pipeline,
    so the output schema must match what the CLI path would produce.
    """

    _REQUIRED_KEYS = {
        "gate",
        "pr_id",
        "pr_number",
        "status",
        "contract_hash",
        "report_path",
        "findings",
        "blocking_findings",
        "advisory_findings",
        "required_reruns",
        "duration_seconds",
        "recorded_at",
    }

    def test_vertex_runner_returns_gate_result_shape(self, gate_env, monkeypatch):
        """Result dict from Vertex path must contain all required schema keys."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "schema-test-proj")

        review_text = (
            "Schema validation review.\n"
            "No blocking findings detected.\n"
            "All required fields present.\n"
            "Approved for merge.\n"
        )

        with patch("gate_runner.subprocess.run", side_effect=_gcloud_sa_side_effect), \
             patch("urllib.request.urlopen", return_value=_mock_urlopen(review_text)):
            runner = _make_runner(gate_env)
            result = runner.run(
                gate="gemini_review",
                request_payload=_make_payload(gate_env),
                pr_number=42,
            )

        assert result["status"] == "completed"
        missing = self._REQUIRED_KEYS - set(result.keys())
        assert not missing, f"Result record missing required keys: {missing}"

    def test_vertex_result_status_field_matches_cli_path_values(
        self, gate_env, monkeypatch
    ):
        """status field must be 'completed' on success — same as CLI path."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "status-test-proj")

        review_text = (
            "Status field test.\n"
            "Review complete via Vertex AI REST.\n"
            "No blocking issues.\n"
            "Approved.\n"
        )

        with patch("gate_runner.subprocess.run", side_effect=_gcloud_sa_side_effect), \
             patch("urllib.request.urlopen", return_value=_mock_urlopen(review_text)):
            runner = _make_runner(gate_env)
            result = runner.run(
                gate="gemini_review",
                request_payload=_make_payload(gate_env),
                pr_number=42,
            )

        assert result["status"] == "completed"
        assert result["gate"] == "gemini_review"
        assert result["pr_number"] == 42
        assert isinstance(result["findings"], list)
        assert isinstance(result["blocking_findings"], list)
        assert isinstance(result["duration_seconds"], float)

    def test_vertex_failure_result_matches_failed_shape(self, gate_env, monkeypatch):
        """Vertex API error must produce a result with status='failed' and reason field."""
        monkeypatch.setenv("VNX_GEMINI_ROUTING", "vertex")
        monkeypatch.setenv("VNX_VERTEX_PROJECT", "failure-shape-proj")

        def raise_on_urlopen(req, timeout=None):
            raise ConnectionError("Simulated Vertex quota error")

        with patch("gate_runner.subprocess.run", side_effect=_gcloud_sa_side_effect), \
             patch("urllib.request.urlopen", side_effect=raise_on_urlopen):
            runner = _make_runner(gate_env)
            result = runner.run(
                gate="gemini_review",
                request_payload=_make_payload(gate_env),
                pr_number=42,
            )

        assert result["status"] == "failed"
        assert "reason" in result
        assert result["reason"] == "vertex_api_error"
        assert "Simulated Vertex quota error" in result.get("reason_detail", "")
