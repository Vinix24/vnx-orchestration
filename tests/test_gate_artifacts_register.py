"""Tests for gate_artifacts → dispatch_register emit (codex_gate only)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_artifacts import materialize_artifacts


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def artifact_env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    # Redirect dispatch_register writes to tmp_path so they don't hit production state
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    return {
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": requests_dir,
        "results_dir": results_dir,
    }


# Prose stdout. Fine for the gates that are NOT held to a verdict (gemini_review,
# claude_github_optional); codex_gate is refused on it (OI-1770), see _codex_stream.
_STDOUT = "Review line one.\nReview line two.\nReview line three.\n"


def _codex_stream(verdict: dict) -> str:
    """A codex ``exec --json`` run that looked once and ended in *verdict*, bare.

    The event vocabulary is the one a real run emits (see
    tests/fixtures/gate_verdict/pr-1869-codex_gate-bare-verdict.ndjson). The
    verdict is written the way codex writes it: an indented object in an
    ``agent_message``, no markdown fence.
    """
    events = [
        {"type": "thread.started", "thread_id": "01a0-register-test"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "item_0", "type": "command_execution",
            "command": "/bin/zsh -lc 'git diff --stat'",
            "aggregated_output": "", "exit_code": 0, "status": "completed",
        }},
        {"type": "item.completed", "item": {
            "id": "item_1", "type": "agent_message", "text": json.dumps(verdict, indent=2),
        }},
        {"type": "turn.completed"},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


_PASS = {"verdict": "pass", "findings": [], "residual_risk": None}


def _make_payload(gate="codex_gate", pr_number=42, pr_id="", dispatch_id="test-dispatch-pr4b4", **kw):
    base = {
        "gate": gate,
        "status": "requested",
        "provider": "codex",
        "branch": "feat/test",
        "pr_number": pr_number,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/foo.py"],
        "requested_at": "20260428T100000Z",
        "prompt": "Review this code",
        "dispatch_id": dispatch_id,
    }
    if pr_id:
        base["pr_id"] = pr_id
    base.update(kw)
    return base


def _run(env, payload, stdout=_STDOUT):
    report_file = env["reports_dir"] / "test-report.md"
    payload.setdefault("report_path", str(report_file))
    return materialize_artifacts(
        gate=payload["gate"],
        pr_number=payload.get("pr_number"),
        pr_id=payload.get("pr_id", ""),
        stdout=stdout,
        request_payload=payload,
        duration_seconds=1.5,
        requests_dir=env["requests_dir"],
        results_dir=env["results_dir"],
        reports_dir=env["reports_dir"],
    )


def _read_register_events(env) -> list[dict]:
    reg = env["state_dir"] / "dispatch_register.ndjson"
    if not reg.exists():
        return []
    return [json.loads(ln) for ln in reg.read_text().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGateArtifactsRegisterEmit:

    def test_codex_gate_no_blocking_emits_gate_passed(self, artifact_env):
        """codex_gate with no blocking findings emits gate_passed to register."""
        payload = _make_payload(gate="codex_gate", pr_number=42)
        result = _run(artifact_env, payload, stdout=_codex_stream(_PASS))

        assert result["status"] == "completed"
        events = _read_register_events(artifact_env)
        assert len(events) == 1
        assert events[0]["event"] == "gate_passed"
        assert events[0]["gate"] == "codex_gate"

    def test_codex_gate_blocking_emits_gate_failed(self, artifact_env):
        """codex_gate with blocking findings emits gate_failed to register."""
        payload = _make_payload(gate="codex_gate", pr_number=42)
        stdout = _codex_stream({
            "verdict": "fail",
            "findings": [{"severity": "error", "message": "Critical security issue"}],
            "residual_risk": "High",
        })
        result = _run(artifact_env, payload, stdout=stdout)

        assert result["status"] == "completed"
        assert [f["message"] for f in result["blocking_findings"]] == ["Critical security issue"]
        events = _read_register_events(artifact_env)
        assert len(events) == 1
        assert events[0]["event"] == "gate_failed"
        assert events[0]["gate"] == "codex_gate"

    def test_gemini_review_no_register_entry(self, artifact_env):
        """gemini_review gate must NOT emit any register event."""
        payload = _make_payload(gate="gemini_review", pr_number=42)
        result = _run(artifact_env, payload)

        assert result["status"] == "completed"
        events = _read_register_events(artifact_env)
        assert events == []

    def test_claude_github_optional_no_register_entry(self, artifact_env):
        """claude_github_optional gate must NOT emit any register event."""
        payload = _make_payload(gate="claude_github_optional", pr_number=42)
        result = _run(artifact_env, payload)

        assert result["status"] == "completed"
        events = _read_register_events(artifact_env)
        assert events == []

    def test_codex_gate_numeric_pr_id_resolves_to_pr_number(self, artifact_env):
        """pr_id='276' (numeric string) should resolve to pr_number=276 in register."""
        payload = _make_payload(gate="codex_gate", pr_number=None, pr_id="276")
        result = _run(artifact_env, payload, stdout=_codex_stream(_PASS))

        assert result["status"] == "completed"
        events = _read_register_events(artifact_env)
        assert len(events) == 1
        assert events[0].get("pr_number") == 276
        assert "feature_id" not in events[0]

    def test_codex_gate_non_numeric_pr_id_resolves_to_feature_id(self, artifact_env):
        """pr_id='PR-6' (non-numeric) should resolve to feature_id='PR-6' in register."""
        payload = _make_payload(gate="codex_gate", pr_number=None, pr_id="PR-6")
        result = _run(artifact_env, payload, stdout=_codex_stream(_PASS))

        assert result["status"] == "completed"
        events = _read_register_events(artifact_env)
        assert len(events) == 1
        assert events[0].get("feature_id") == "PR-6"
        assert "pr_number" not in events[0]

    def test_codex_gate_verdict_pass_overrides_blocking_findings(self, artifact_env):
        """codex verdict='pass' must emit gate_passed even when severity-derived result would be gate_failed."""
        payload = _make_payload(gate="codex_gate", pr_number=42)
        stdout = _codex_stream({
            "verdict": "pass",
            "findings": [{"severity": "error", "message": "Flagged but verdict overrides"}],
            "residual_risk": "Low",
        })
        result = _run(artifact_env, payload, stdout=stdout)

        assert result["status"] == "completed"
        assert len(result["blocking_findings"]) == 1, "the finding must still be booked as blocking"
        events = _read_register_events(artifact_env)
        assert len(events) == 1, f"Expected 1 event; got: {events}"
        assert events[0]["event"] == "gate_passed", (
            "Explicit verdict='pass' must override severity-derived gate_failed"
        )

    def test_codex_gate_verdict_fail_with_no_findings_emits_gate_failed(self, artifact_env):
        """codex verdict='fail' must emit gate_failed even when there are no findings."""
        payload = _make_payload(gate="codex_gate", pr_number=42)
        stdout = _codex_stream({"verdict": "fail", "findings": [], "residual_risk": None})
        result = _run(artifact_env, payload, stdout=stdout)

        assert result["status"] == "completed"
        events = _read_register_events(artifact_env)
        assert len(events) == 1, f"Expected 1 event; got: {events}"
        assert events[0]["event"] == "gate_failed", (
            "Explicit verdict='fail' must emit gate_failed even with no findings"
        )

    def test_codex_gate_missing_verdict_falls_back_to_severity(self, artifact_env):
        """When the findings reader returns no verdict object, classification falls back to severity.

        The RUN still has to carry a verdict: codex_gate is refused without one
        (OI-1770), so the stdout here is a real run that ends in a verdict. What
        is pinned is the register's fallback when ``parse_codex_findings`` hands
        back none, which is why that one call stays mocked.
        """
        payload = _make_payload(gate="codex_gate", pr_number=42)
        blocking = [{"severity": "error", "message": "Critical issue"}]
        with patch("gate_artifacts.parse_codex_findings", return_value={
            "findings": blocking,
            "residual_risk": "",
            "verdict": {},
            "raw_text": "",
        }):
            result = _run(artifact_env, payload, stdout=_codex_stream(_PASS))

        assert result["status"] == "completed"
        events = _read_register_events(artifact_env)
        assert len(events) == 1
        assert events[0]["event"] == "gate_failed", (
            "Missing verdict with blocking severity must fall back to gate_failed"
        )
