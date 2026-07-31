"""test_token_harvest.py — receipt-quality PR-B1 token-capture tests.

Covers the transcript harvester (scripts/lib/token_harvest.py) in isolation,
plus its wiring into governance_emit.emit_dispatch_receipt for the claude
lane.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from token_harvest import harvest_session_tokens  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _assistant_line(message_id: str, usage: dict) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "id": message_id, "usage": usage},
    })


def _write_transcript(projects_dir: Path, session_id: str, lines: list) -> Path:
    project_dir = projects_dir / "-Users-test-some-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    transcript = project_dir / f"{session_id}.jsonl"
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return transcript


_FULL_USAGE = {
    "input_tokens": 100,
    "output_tokens": 20,
    "cache_read_input_tokens": 2,
    "cache_creation_input_tokens": 15,
    "cache_creation": {
        "ephemeral_5m_input_tokens": 10,
        "ephemeral_1h_input_tokens": 5,
    },
}


# ---------------------------------------------------------------------------
# harvest_session_tokens — sums + dedups correctly
# ---------------------------------------------------------------------------

def test_harvest_sums_single_message(tmp_path):
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-1", [
        _assistant_line("msg_1", _FULL_USAGE),
    ])
    result = harvest_session_tokens("session-1", claude_projects_dir=projects_dir)
    assert result == {
        "input": 100,
        "output": 20,
        "cache_creation_5m": 10,
        "cache_creation_1h": 5,
        "cache_read": 2,
    }


def test_harvest_dedups_by_message_id(tmp_path):
    """Claude Code rewrites the same message repeatedly while streaming —
    each rewrite carries the SAME cumulative usage. Naive per-line summing
    would multiply-count every redraw; only the last write per message.id
    must be counted once."""
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-2", [
        _assistant_line("msg_1", _FULL_USAGE),
        _assistant_line("msg_1", _FULL_USAGE),
        _assistant_line("msg_1", _FULL_USAGE),
        _assistant_line("msg_1", _FULL_USAGE),
    ])
    result = harvest_session_tokens("session-2", claude_projects_dir=projects_dir)
    assert result["input"] == 100
    assert result["output"] == 20
    assert result["cache_creation_5m"] == 10
    assert result["cache_creation_1h"] == 5
    assert result["cache_read"] == 2
    assert "unavailable" not in result


def test_harvest_sums_across_distinct_messages(tmp_path):
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-3", [
        _assistant_line("msg_1", _FULL_USAGE),
        _assistant_line("msg_1", _FULL_USAGE),  # duplicate rewrite of msg_1
        _assistant_line("msg_2", {
            "input_tokens": 50,
            "output_tokens": 5,
            "cache_read_input_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        }),
    ])
    result = harvest_session_tokens("session-3", claude_projects_dir=projects_dir)
    assert result == {
        "input": 150,
        "output": 25,
        "cache_creation_5m": 10,
        "cache_creation_1h": 5,
        "cache_read": 3,
    }


def test_harvest_legacy_flat_cache_creation_bucketed_as_5m(tmp_path):
    """Pre-split transcripts (no cache_creation sub-object) bucket the flat
    cache_creation_input_tokens total under the 5m TTL (the API default)."""
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-4", [
        _assistant_line("msg_1", {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 7,
        }),
    ])
    result = harvest_session_tokens("session-4", claude_projects_dir=projects_dir)
    assert result["cache_creation_5m"] == 7
    assert result["cache_creation_1h"] == 0


def test_harvest_ignores_non_assistant_and_malformed_lines(tmp_path):
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-5", [
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        "not-json-at-all {{{",
        json.dumps({"type": "assistant", "message": {"role": "assistant", "id": "msg_no_usage"}}),
        _assistant_line("msg_1", {
            "input_tokens": 5, "output_tokens": 1,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }),
        "",
    ])
    result = harvest_session_tokens("session-5", claude_projects_dir=projects_dir)
    assert result["input"] == 5
    assert result["output"] == 1
    assert "unavailable" not in result


# ---------------------------------------------------------------------------
# Fail-open unavailable marker
# ---------------------------------------------------------------------------

def test_harvest_unavailable_when_session_id_empty(tmp_path):
    result = harvest_session_tokens("", claude_projects_dir=tmp_path / "projects")
    assert result["unavailable"] is True
    assert result["input"] == 0


def test_harvest_unavailable_when_session_id_none(tmp_path):
    result = harvest_session_tokens(None, claude_projects_dir=tmp_path / "projects")
    assert result["unavailable"] is True


def test_harvest_unavailable_when_projects_dir_missing(tmp_path):
    result = harvest_session_tokens(
        "session-x", claude_projects_dir=tmp_path / "does-not-exist"
    )
    assert result["unavailable"] is True


def test_harvest_unavailable_when_no_matching_transcript(tmp_path):
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-real", [_assistant_line("msg_1", _FULL_USAGE)])
    result = harvest_session_tokens("session-does-not-exist", claude_projects_dir=projects_dir)
    assert result["unavailable"] is True


def test_harvest_unavailable_when_transcript_has_no_usage(tmp_path):
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-6", [
        json.dumps({"type": "assistant", "message": {"role": "assistant", "id": "m1"}}),
    ])
    result = harvest_session_tokens("session-6", claude_projects_dir=projects_dir)
    assert result["unavailable"] is True


def test_harvest_kimi_style_no_transcript_is_unavailable(tmp_path):
    """Kimi runs outside Claude Code and never produces a transcript — this
    is the same code path as 'no matching transcript found'."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)
    result = harvest_session_tokens("kimi-session-abc", claude_projects_dir=projects_dir)
    assert result["unavailable"] is True


# ---------------------------------------------------------------------------
# Wiring: governance_emit.emit_dispatch_receipt backfills claude receipts
# ---------------------------------------------------------------------------

def test_claude_receipt_carries_harvested_token_usage(tmp_path, monkeypatch):
    """A claude-lane receipt emitted with an 'unavailable' token_usage marker
    and a resolvable session_id now carries real harvested counts."""
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-receipt-1", [
        _assistant_line("msg_1", _FULL_USAGE),
    ])
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(projects_dir))

    from governance_emit import emit_dispatch_receipt

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    emit_dispatch_receipt(
        dispatch_id="dispatch-b1-test",
        terminal_id="T1",
        provider="claude",
        model="claude-sonnet-4-6",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=3.5,
        token_usage={"unavailable": True},
        cost_usd=None,
        state_dir=state_dir,
        receipt_kind="dispatch",
        session_id="session-receipt-1",
    )
    line = (state_dir / "t0_receipts.ndjson").read_text().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["token_usage"] == {
        "input": 100,
        "output": 20,
        "cache_creation_5m": 10,
        "cache_creation_1h": 5,
        "cache_read": 2,
    }


def test_claude_receipt_keeps_real_token_usage_when_already_present(tmp_path, monkeypatch):
    """A caller-supplied real token_usage is never overwritten by the harvester,
    even when a (different) transcript exists for the session_id."""
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-receipt-2", [
        _assistant_line("msg_1", _FULL_USAGE),
    ])
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(projects_dir))

    from governance_emit import emit_dispatch_receipt

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    real_usage = {"input": 9, "output": 3, "cache_hit": 0}
    emit_dispatch_receipt(
        dispatch_id="dispatch-b1-test-2",
        terminal_id="T1",
        provider="claude",
        model="claude-sonnet-4-6",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=3.5,
        token_usage=real_usage,
        cost_usd=None,
        state_dir=state_dir,
        receipt_kind="dispatch",
        session_id="session-receipt-2",
    )
    line = (state_dir / "t0_receipts.ndjson").read_text().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["token_usage"] == real_usage


def test_kimi_receipt_never_harvests(tmp_path, monkeypatch):
    """Kimi stays 'unavailable' in this PR — the harvest guard is claude-only."""
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-kimi", [
        _assistant_line("msg_1", _FULL_USAGE),
    ])
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(projects_dir))

    from governance_emit import emit_dispatch_receipt

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    emit_dispatch_receipt(
        dispatch_id="dispatch-b1-kimi",
        terminal_id="T1",
        provider="kimi",
        model="kimi-k3",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=3.5,
        token_usage={"unavailable": True},
        cost_usd=None,
        state_dir=state_dir,
        receipt_kind="dispatch",
        session_id="session-kimi",
    )
    line = (state_dir / "t0_receipts.ndjson").read_text().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["token_usage"] == {"unavailable": True}


def test_claude_receipt_no_session_id_keeps_marker(tmp_path):
    """No session_id supplied — the harvest guard never fires (old behavior)."""
    from governance_emit import emit_dispatch_receipt

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    emit_dispatch_receipt(
        dispatch_id="dispatch-b1-no-session",
        terminal_id="T1",
        provider="claude",
        model="claude-sonnet-4-6",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=3.5,
        token_usage={"unavailable": True},
        cost_usd=None,
        state_dir=state_dir,
        receipt_kind="dispatch",
    )
    line = (state_dir / "t0_receipts.ndjson").read_text().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["token_usage"] == {"unavailable": True}


# ---------------------------------------------------------------------------
# OI-884: deepseek-harness / glm-harness run through the Claude Code harness
# (claude_harness_keyed, redirected ANTHROPIC_BASE_URL) and leave a full
# transcript — their receipts must harvest exactly like the native claude lane.
# ---------------------------------------------------------------------------

def test_deepseek_harness_receipt_carries_harvested_token_usage(tmp_path, monkeypatch):
    """A deepseek-harness receipt with an 'unavailable' marker and a resolvable
    session_id carries real harvested counts (core OI-884 DoD)."""
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-receipt-ds", [
        _assistant_line("msg_1", _FULL_USAGE),
    ])
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(projects_dir))

    from governance_emit import emit_dispatch_receipt

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    emit_dispatch_receipt(
        dispatch_id="dispatch-b1-deepseek",
        terminal_id="T1",
        provider="deepseek-harness",
        model="deepseek-v4-flash",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=3.5,
        token_usage={"unavailable": True},
        cost_usd=None,
        state_dir=state_dir,
        receipt_kind="dispatch",
        session_id="session-receipt-ds",
    )
    line = (state_dir / "t0_receipts.ndjson").read_text().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["token_usage"] == {
        "input": 100,
        "output": 20,
        "cache_creation_5m": 10,
        "cache_creation_1h": 5,
        "cache_read": 2,
    }


def test_glm_harness_receipt_carries_harvested_token_usage(tmp_path, monkeypatch):
    """A glm-harness receipt with an 'unavailable' marker and a resolvable
    session_id carries real harvested counts (core OI-884 DoD)."""
    projects_dir = tmp_path / "projects"
    _write_transcript(projects_dir, "session-receipt-glm", [
        _assistant_line("msg_1", _FULL_USAGE),
    ])
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(projects_dir))

    from governance_emit import emit_dispatch_receipt

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    emit_dispatch_receipt(
        dispatch_id="dispatch-b1-glm",
        terminal_id="T1",
        provider="glm-harness",
        model="glm-5.2",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=3.5,
        token_usage={"unavailable": True},
        cost_usd=None,
        state_dir=state_dir,
        receipt_kind="dispatch",
        session_id="session-receipt-glm",
    )
    line = (state_dir / "t0_receipts.ndjson").read_text().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["token_usage"] == {
        "input": 100,
        "output": 20,
        "cache_creation_5m": 10,
        "cache_creation_1h": 5,
        "cache_read": 2,
    }


def test_deepseek_harness_missing_transcript_fails_open(tmp_path, monkeypatch):
    """Fail-open: a deepseek-harness receipt whose transcript is missing still
    emits. The guard now fires for harness providers (the OI-884 broadening),
    harvest resolves to unavailable, and the caller-supplied marker is
    preserved — the receipt is never broken by a missing transcript."""
    import token_harvest as th

    calls = []
    real = th.harvest_session_tokens

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(th, "harvest_session_tokens", spy)

    from governance_emit import emit_dispatch_receipt

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    emit_dispatch_receipt(
        dispatch_id="dispatch-b1-ds-missing",
        terminal_id="T1",
        provider="deepseek-harness",
        model="deepseek-v4-flash",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=3.5,
        token_usage={"unavailable": True},
        cost_usd=None,
        state_dir=state_dir,
        receipt_kind="dispatch",
        session_id="session-does-not-exist",
    )
    line = (state_dir / "t0_receipts.ndjson").read_text().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["token_usage"] == {"unavailable": True}
    assert len(calls) == 1  # the guard fired for a harness provider


def test_emit_and_backfill_share_one_harness_provider_set():
    """OI-884: both consumers gate on the exact same single source of truth,
    so the nightly backfill can never cover a different set than the emit
    path. A third harness provider only needs adding in token_harvest."""
    os.environ.setdefault("VNX_PROJECT_ID", "vnx-dev")
    import governance_emit
    import link_sessions_dispatches
    import token_harvest

    assert governance_emit.CLAUDE_HARNESS_PROVIDERS is token_harvest.CLAUDE_HARNESS_PROVIDERS
    assert link_sessions_dispatches.CLAUDE_HARNESS_PROVIDERS is token_harvest.CLAUDE_HARNESS_PROVIDERS
    assert token_harvest.CLAUDE_HARNESS_PROVIDERS == frozenset(
        {"claude", "deepseek-harness", "glm-harness"}
    )
    assert "kimi" not in token_harvest.CLAUDE_HARNESS_PROVIDERS
