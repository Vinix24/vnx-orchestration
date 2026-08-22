"""tests/test_oi1433_lane_log_lift.py — an empty response gets one honest look at the
raw lane log before governance_emit falls back to "(no response captured)" (OI-1433).

Measured 22-08 on the central store: 20 of 20 empty-response reports with a 403 body
sitting in their per-dispatch raw lane log (``logs/conversations/<dispatch_id>.log``)
said "(no response captured)" — the operator read "unknown error" where the log said
the provider account was out of quota.

These tests are RED against the pre-fix ``emit_unified_report`` (the fixed body is a
new code path added in this same PR, not present before it — see the dispatch report
for the before/after pytest run demonstrating this).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for _p in (str(_LIB), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from governance_emit import (  # noqa: E402
    emit_unified_report,
    _classify_lane_log_text,
    _read_lane_log_text,
    _resolve_lane_log_path,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lane_logs"


@pytest.fixture()
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


def _write_log(data_dir: Path, dispatch_id: str, fixture_name: str) -> Path:
    log_dir = data_dir / "logs" / "conversations"
    log_dir.mkdir(parents=True, exist_ok=True)
    content = (_FIXTURES / fixture_name).read_bytes()
    log_path = log_dir / f"{dispatch_id}.log"
    log_path.write_bytes(content)
    return log_path


def _emit_kwargs(data_dir, dispatch_id, **overrides):
    kwargs = dict(
        dispatch_id=dispatch_id,
        terminal_id="T1",
        provider="kimi",
        instruction="do the thing",
        response_text="",
        findings=None,
        duration_seconds=3.2,
        data_dir=data_dir,
    )
    kwargs.update(overrides)
    return kwargs


# ── State 1: lane_exhausted — a 403/402 billing/quota body sitting in the log ──────


def test_empty_response_with_403_log_lifts_body_as_lane_exhausted(data_dir):
    dispatch_id = "20260822-oi1433-quota"
    _write_log(data_dir, dispatch_id, "kimi_403_quota.log")
    frontmatter = {}

    path = emit_unified_report(**_emit_kwargs(data_dir, dispatch_id, frontmatter=frontmatter))
    content = path.read_text(encoding="utf-8")

    assert "(no response captured)" not in content, (
        "an empty response with a real 403 body sitting in the lane log must not fall "
        "back to the contentless placeholder"
    )
    assert "access_terminated_error" in content, (
        "the raw 403 body must be lifted verbatim into the report body"
    )
    assert "NOT a model reply" in content, (
        "the lifted text must be labeled as coming from the lane log, never as a model reply"
    )
    assert frontmatter["empty_response_state"] == "lane_exhausted"
    assert "usage limit" in frontmatter["failure_reason"].lower(), (
        "the canonical failure_reason field (OI-1415/#1666) must carry the real quota "
        "reason extracted from the log, not a lane-log-only field name"
    )


def test_lane_exhausted_reads_reason_label_not_msg_field(data_dir):
    """Regression for the specific kimi trap: the raw log line that carries the real
    403 body ALSO trips a generic JSON-decode-failure message elsewhere in the
    pipeline (msg='Expecting value: line 1...'). The classifier must never be fooled
    by that noise into downgrading a real quota rejection to 'unreadable_verdict'.
    """
    text = (
        "[quota_or_auth] provider=kimi reason=quota_or_auth "
        "msg='Expecting value: line 1 column 1 (char 0)' "
        "raw='Error code: 403 - {\\'error\\': {\\'message\\': \"you are done\", "
        "\\'type\\': \\'access_terminated_error\\'}}'"
    )
    state, reason = _classify_lane_log_text(text)
    assert state == "lane_exhausted"
    assert reason is not None


# ── State 3: no_response — log missing or empty, cause genuinely unknown ───────────


def test_empty_response_with_empty_log_keeps_placeholder_and_no_response_state(data_dir):
    dispatch_id = "20260822-oi1433-empty"
    _write_log(data_dir, dispatch_id, "empty.log")
    frontmatter = {}

    path = emit_unified_report(**_emit_kwargs(data_dir, dispatch_id, frontmatter=frontmatter))
    content = path.read_text(encoding="utf-8")

    assert "(no response captured)" in content, (
        "an empty log must never be presented as if something explanatory was found"
    )
    assert frontmatter["empty_response_state"] == "no_response"
    assert "failure_reason" not in frontmatter


def test_empty_response_with_no_log_file_at_all_keeps_placeholder(data_dir):
    dispatch_id = "20260822-oi1433-missing"
    frontmatter = {}

    path = emit_unified_report(**_emit_kwargs(data_dir, dispatch_id, frontmatter=frontmatter))
    content = path.read_text(encoding="utf-8")

    assert "(no response captured)" in content
    assert frontmatter["empty_response_state"] == "no_response"


# ── State 2: unreadable_verdict — content exists, but no billing/quota signal ──────


def test_empty_response_with_content_but_no_verdict(data_dir):
    dispatch_id = "20260822-oi1433-noverdict"
    _write_log(data_dir, dispatch_id, "content_no_verdict.log")
    frontmatter = {}

    path = emit_unified_report(**_emit_kwargs(data_dir, dispatch_id, frontmatter=frontmatter))
    content = path.read_text(encoding="utf-8")

    assert "(no response captured)" not in content
    assert "Let me check the file structure first." in content
    assert "NOT a model reply" in content
    assert frontmatter["empty_response_state"] == "unreadable_verdict"
    assert "failure_reason" not in frontmatter, (
        "unreadable_verdict has no extracted reason — the canonical field must stay untouched"
    )


# ── State 4: a real response — unchanged behavior, log is never read ──────────────


def test_non_empty_response_never_reads_the_log(data_dir):
    dispatch_id = "20260822-oi1433-realresponse"
    _write_log(data_dir, dispatch_id, "kimi_403_quota.log")
    frontmatter = {}
    real_response = "The implementation is complete and tests pass."

    path = emit_unified_report(
        **_emit_kwargs(
            data_dir, dispatch_id, response_text=real_response, frontmatter=frontmatter,
        )
    )
    content = path.read_text(encoding="utf-8")

    assert real_response in content
    assert "access_terminated_error" not in content, (
        "a non-empty response must never trigger a lane-log read, even when a "
        "quota-shaped log happens to exist for the same dispatch_id"
    )
    assert "empty_response_state" not in frontmatter


# ── State 5: dispatch_id trying to escape the log directory ───────────────────────


def test_path_escaping_dispatch_id_is_refused_no_read(data_dir, caplog):
    log_dir = data_dir / "logs" / "conversations"
    log_dir.mkdir(parents=True, exist_ok=True)
    # A sibling file that a naive join could be tricked into reaching.
    secret = data_dir / "logs" / "secret.log"
    secret.write_text("top secret content", encoding="utf-8")

    evil_id = "../secret"
    with caplog.at_level("WARNING", logger="governance_emit"):
        assert _resolve_lane_log_path(evil_id, data_dir) is None
        assert _read_lane_log_text(evil_id, data_dir) is None

    assert any("refused" in record.getMessage() for record in caplog.records), (
        "a refused lane-log lookup must be logged loudly, not silently swallowed"
    )


def test_unsafe_dispatch_id_characters_are_refused(data_dir):
    for evil_id in ("../../etc/passwd", "foo/bar", "foo\x00bar", ""):
        assert _resolve_lane_log_path(evil_id, data_dir) is None


# ── State 6: a log bigger than the display limit is truncated, visibly ────────────


def test_oversized_log_is_truncated_with_visible_notice(data_dir):
    dispatch_id = "20260822-oi1433-huge"
    log_dir = data_dir / "logs" / "conversations"
    log_dir.mkdir(parents=True, exist_ok=True)
    huge_text = "filler line without any quota keywords in it\n" * 400  # > 4000 chars
    (log_dir / f"{dispatch_id}.log").write_text(huge_text, encoding="utf-8")
    frontmatter = {}

    path = emit_unified_report(**_emit_kwargs(data_dir, dispatch_id, frontmatter=frontmatter))
    content = path.read_text(encoding="utf-8")

    assert "truncated" in content.lower(), (
        "a log larger than the display limit must carry a visible truncation notice"
    )
    assert "additional characters omitted" in content
    assert frontmatter["empty_response_state"] == "unreadable_verdict"


def test_unreadable_log_never_crashes_report_emission(data_dir, monkeypatch):
    dispatch_id = "20260822-oi1433-unreadable"
    log_dir = data_dir / "logs" / "conversations"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{dispatch_id}.log"
    log_path.write_text("some content", encoding="utf-8")

    import governance_emit

    def _boom(*_a, **_kw):
        raise OSError("simulated unreadable file")

    monkeypatch.setattr(governance_emit.Path, "read_bytes", _boom)

    path = emit_unified_report(**_emit_kwargs(data_dir, dispatch_id))
    content = path.read_text(encoding="utf-8")
    assert "(no response captured)" in content
