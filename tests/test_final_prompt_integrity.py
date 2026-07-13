"""test_final_prompt_integrity.py — the input-side audit-closure regression suite.

Track: final-dispatch-persist-integrity (P1). Proves:

  1. The enriched final prompt is persisted (``final_prompt.md``) and its sha is
     pinned into ``dispatch-spec.json`` + matches the bytes on disk.
  2. Reconstruction SUCCEEDS for a real case: the raw instruction bytes and every
     recorded intelligence-injection item literally survive into the final body.
  3. A deliberately CORRUPTED / dropped injection makes ``injection_reconstructs``
     False and fails LOUD (ERROR log; strict mode raises).
  4. ``emit_dispatch_receipt`` carries the three integrity fields, and the envelope
     provider lane stamps ``injection_reconstructs`` onto its receipt end-to-end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import final_prompt_integrity as fpi  # noqa: E402
import provider_dispatch as pd  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(content: str, *, item_class: str = "proven_pattern", title: str = "T", item_id: str = "i1") -> dict:
    return {"item_id": item_id, "item_class": item_class, "title": title, "content": content}


def _render_final(raw: str, items: list) -> str:
    """A stand-in for the enriched body: intelligence section (rendered) + raw."""
    parts = ["## Relevant Intelligence (from past dispatches)", ""]
    for it in items:
        parts.append(f"- **{it['title']}**: {it['content']}")
    parts.append("\n---\n")
    parts.append(raw)
    return "\n".join(parts)


def _write_injection_row(state_dir: Path, dispatch_id: str, items: list, *, injection_point: str = "dispatch_create") -> None:
    """Insert one intelligence_injections row (minimal table) the loader can read."""
    db = state_dir / "runtime_coordination.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS intelligence_injections ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT, "
            "injection_point TEXT, items_json TEXT, injected_at TEXT)"
        )
        conn.execute(
            "INSERT INTO intelligence_injections (dispatch_id, injection_point, items_json, injected_at) "
            "VALUES (?, ?, ?, ?)",
            (dispatch_id, injection_point, json.dumps(items), "2026-07-11T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Pure reconstruction check
# ---------------------------------------------------------------------------

def test_verify_reconstructs_true_for_raw_and_items():
    raw = "Implement the widget loader and return exactly OK."
    items = [_item("Always guard against a null store handle", item_id="ap1", item_class="failure_prevention")]
    final = _render_final(raw, items)

    res = fpi.verify_injection_reconstructs(final, raw, items, dispatch_id="d-ok")

    assert res.reconstructs is True
    assert res.items_checked == 1
    assert res.missing == ()


def test_verify_true_is_whitespace_tolerant():
    raw = "line one\n  line two\tline three"
    items = [_item("multi\nline\ncontent here")]
    # final collapses/reflows whitespace differently — containment must still hold.
    final = "PREAMBLE  multi line content here MIDDLE line one line two   line three END"

    res = fpi.verify_injection_reconstructs(final, raw, items)
    assert res.reconstructs is True


def test_verify_true_when_item_content_is_a_prefix():
    # items_json stores content[:cap] (a prefix). A prefix of verbatim-rendered
    # content is still a substring, so it must reconstruct.
    raw = "do the thing"
    full = "the quick brown fox jumps over the lazy dog"
    stored_prefix = full[:20]  # "the quick brown fox "
    final = _render_final(raw, [_item(full)])
    res = fpi.verify_injection_reconstructs(final, raw, [_item(stored_prefix)])
    assert res.reconstructs is True


def test_verify_false_when_item_dropped_fails_loud(caplog):
    raw = "raw instruction body"
    good = _item("this proven pattern actually reached the model", item_id="sp-good")
    dropped = _item("THIS INJECTION NEVER MADE IT INTO THE PROMPT", item_id="sp-dropped")
    # final contains raw + the good item, but NOT the dropped item's content.
    final = _render_final(raw, [good])

    with caplog.at_level(logging.ERROR, logger="final_prompt_integrity"):
        res = fpi.verify_injection_reconstructs(final, raw, [good, dropped], dispatch_id="d-drop")

    assert res.reconstructs is False
    assert "sp-dropped" in res.missing
    assert "sp-good" not in res.missing
    # fail-loud: an ERROR was logged naming the dispatch.
    assert any("reconstruction FAILED" in r.message and "d-drop" in r.getMessage() for r in caplog.records)


def test_verify_false_when_raw_missing():
    raw = "the raw instruction that was silently swapped out"
    final = "## Intelligence\n\n- something else entirely\n"
    res = fpi.verify_injection_reconstructs(final, raw, [])
    assert res.reconstructs is False
    assert "raw-instruction" in res.missing


def test_verify_strict_raises_fail_closed():
    raw = "raw body"
    final = "nothing matching here"
    with pytest.raises(fpi.InjectionReconstructError):
        fpi.verify_injection_reconstructs(final, raw, [], strict=True)


def test_verify_strict_env_toggle(monkeypatch):
    monkeypatch.setenv("VNX_INJECTION_RECONSTRUCT_STRICT", "1")
    with pytest.raises(fpi.InjectionReconstructError):
        fpi.verify_injection_reconstructs("no match", "raw", [])


# ---------------------------------------------------------------------------
# 2. Persistence
# ---------------------------------------------------------------------------

def test_persist_writes_final_prompt_and_stamps_spec(tmp_path):
    bundle = tmp_path / "dispatches" / "pending" / "d-1"
    bundle.mkdir(parents=True)
    (bundle / "dispatch-spec.json").write_text(
        json.dumps({"schema_version": 1, "dispatch_id": "d-1", "instruction_sha256": "abc"}),
        encoding="utf-8",
    )
    final_prompt = "## enriched body\n\nthe full assembled prompt"

    path, sha = fpi.persist_final_prompt(bundle, final_prompt)

    assert path is not None and path.name == "final_prompt.md"
    assert path.read_text(encoding="utf-8") == final_prompt
    # sha matches the persisted bytes.
    assert sha == hashlib.sha256(final_prompt.encode("utf-8")).hexdigest()
    # spec now carries the pointer + hash, keeping instruction_sha256 intact.
    spec = json.loads((bundle / "dispatch-spec.json").read_text(encoding="utf-8"))
    assert spec["final_prompt_sha256"] == sha
    assert spec["final_prompt_path"].endswith("final_prompt.md")
    assert spec["instruction_sha256"] == "abc"


def test_persist_returns_sha_even_without_spec(tmp_path):
    bundle = tmp_path / "bundle"
    path, sha = fpi.persist_final_prompt(bundle, "body only")
    assert path is not None
    assert sha == hashlib.sha256(b"body only").hexdigest()


# ---------------------------------------------------------------------------
# 3. Injection loader
# ---------------------------------------------------------------------------

def test_load_injection_items_reads_coord_db(tmp_path):
    items = [_item("recorded content A"), _item("recorded content B", item_id="i2")]
    _write_injection_row(tmp_path, "d-load", items)
    loaded = fpi.load_injection_items(tmp_path, "d-load")
    assert [it["content"] for it in loaded] == ["recorded content A", "recorded content B"]


def test_load_injection_items_absent_db_is_empty(tmp_path):
    assert fpi.load_injection_items(tmp_path, "nope") == []


def test_load_injection_items_prefers_dispatch_create(tmp_path):
    _write_injection_row(tmp_path, "d-p", [_item("other point", item_id="x")], injection_point="closeout")
    _write_injection_row(tmp_path, "d-p", [_item("create point", item_id="y")], injection_point="dispatch_create")
    loaded = fpi.load_injection_items(tmp_path, "d-p")
    assert [it["item_id"] for it in loaded] == ["y"]


# ---------------------------------------------------------------------------
# 4. End-to-end record_final_prompt_integrity
# ---------------------------------------------------------------------------

def test_record_integrity_end_to_end_pass(tmp_path):
    dispatch_id = "d-e2e-ok"
    raw = "Build the loader. Return OK."
    items = [_item("proven: reuse the existing store handle", item_id="sp1")]
    _write_injection_row(tmp_path, dispatch_id, items)
    final_prompt = _render_final(raw, items)

    result = fpi.record_final_prompt_integrity(
        dispatch_id=dispatch_id,
        final_prompt=final_prompt,
        raw_instruction=raw,
        data_dir=tmp_path,
        state_dir=tmp_path,
    )

    assert result.injection_reconstructs is True
    assert result.items_checked == 1
    # final_prompt.md was written into the derived pending bundle, sha matches.
    persisted = Path(result.final_prompt_path)
    assert persisted.read_text(encoding="utf-8") == final_prompt
    assert result.final_prompt_sha256 == hashlib.sha256(final_prompt.encode("utf-8")).hexdigest()


def test_record_integrity_end_to_end_corrupted_fails_loud(tmp_path, caplog):
    """The regression this track exists to catch: a recorded injection that does
    NOT survive into the delivered prompt must flip injection_reconstructs False."""
    dispatch_id = "d-e2e-bad"
    raw = "Build the loader. Return OK."
    recorded = [_item("this recorded pattern was DROPPED from the prompt", item_id="sp-x")]
    _write_injection_row(tmp_path, dispatch_id, recorded)
    # The delivered final prompt omits the recorded item entirely.
    final_prompt = _render_final(raw, [])

    with caplog.at_level(logging.ERROR, logger="final_prompt_integrity"):
        result = fpi.record_final_prompt_integrity(
            dispatch_id=dispatch_id,
            final_prompt=final_prompt,
            raw_instruction=raw,
            data_dir=tmp_path,
            state_dir=tmp_path,
        )

    assert result.injection_reconstructs is False
    assert "sp-x" in result.missing
    assert any("reconstruction FAILED" in r.message for r in caplog.records)
    # Even on failure the prompt is still persisted + hashed (audit evidence).
    assert Path(result.final_prompt_path).exists()
    assert result.final_prompt_sha256


# ---------------------------------------------------------------------------
# 5. Receipt carriage
# ---------------------------------------------------------------------------

def test_emit_dispatch_receipt_carries_integrity_fields(tmp_path):
    from governance_emit import emit_dispatch_receipt  # noqa: PLC0415

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    emit_dispatch_receipt(
        dispatch_id="d-receipt",
        terminal_id="T1",
        provider="claude",
        model="sonnet",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=1.0,
        token_usage={"input": 1, "output": 1},
        cost_usd=None,
        state_dir=state_dir,
        final_prompt_path="/x/final_prompt.md",
        final_prompt_sha256="deadbeef",
        injection_reconstructs=False,
    )
    line = (state_dir / "t0_receipts.ndjson").read_text(encoding="utf-8").strip().splitlines()[-1]
    receipt = json.loads(line)
    assert receipt["final_prompt_path"] == "/x/final_prompt.md"
    assert receipt["final_prompt_sha256"] == "deadbeef"
    assert receipt["injection_reconstructs"] is False


def test_emit_dispatch_receipt_omits_integrity_when_unset(tmp_path):
    from governance_emit import emit_dispatch_receipt  # noqa: PLC0415

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    emit_dispatch_receipt(
        dispatch_id="d-plain",
        terminal_id="T1",
        provider="claude",
        model="sonnet",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=1.0,
        token_usage={},
        cost_usd=None,
        state_dir=state_dir,
    )
    receipt = json.loads((state_dir / "t0_receipts.ndjson").read_text(encoding="utf-8").strip())
    assert "injection_reconstructs" not in receipt
    assert "final_prompt_sha256" not in receipt


# ---------------------------------------------------------------------------
# 6. Envelope provider-lane integration (end-to-end receipt stamping)
# ---------------------------------------------------------------------------

@dataclass
class _FakeSpawnResult:
    returncode: int = 0
    completion_text: str = "OK"
    events_written: int = 1
    session_id: Optional[str] = None
    timed_out: bool = False
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    event_writer_failures: int = 0


def _make_provider_plan(tmp_path: Path, dispatch_id: str):
    from dispatch_plan import ExecutionPlan
    from dispatch_spec import Isolation, Provider

    bundle = tmp_path / "dispatches" / "pending" / dispatch_id
    bundle.mkdir(parents=True)
    instruction_file = bundle / "instruction.md"
    instruction_file.write_text("Reply with exactly the word OK.", encoding="utf-8")
    sha = hashlib.sha256(instruction_file.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    return ExecutionPlan(
        dispatch_id=dispatch_id,
        project_id="vnx-dev",
        provider=Provider.KIMI,
        model="default",
        lane="provider",
        adapter="provider",
        target_id="T1",
        billing="provider_metered",
        serialization_class=None,
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="n/a",
        deadline_seconds=3600,
        base_ref="main",
        dispatch_paths=(),
        instruction_file=instruction_file,
        route_reason="integrity-integration",
        instruction_sha256=sha,
    )


def test_run_envelope_plan_stamps_injection_reconstructs(tmp_path):
    """The real-case integration: run_envelope_plan persists final_prompt.md and
    stamps injection_reconstructs onto the receipt (regression guard for the wiring)."""
    from dispatch_envelope import run_envelope_plan
    from dispatch_internal import issue_permit

    dispatch_id = "d-env-integ"
    plan = _make_provider_plan(tmp_path, dispatch_id)
    permit = issue_permit(plan)

    state_dir = tmp_path / "state"
    data_dir = tmp_path
    state_dir.mkdir()

    fake_wt = tmp_path / "wt"
    fake_wt.mkdir()

    with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=_FakeSpawnResult()), \
         patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=fake_wt), \
         patch("dispatch_worktree_isolation.remove_dispatch_worktree"):
        result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

    assert result.status == "success", f"error={result.error}"

    # final_prompt.md persisted into the bundle, spec stamped with the sha.
    bundle = tmp_path / "dispatches" / "pending" / dispatch_id
    assert (bundle / "final_prompt.md").exists()

    # the receipt carries the integrity fields (raw ⊆ enriched, 0 items → True).
    receipt = json.loads((state_dir / "t0_receipts.ndjson").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "injection_reconstructs" in receipt, (
        "regression: run_envelope_plan did not stamp injection_reconstructs onto the receipt"
    )
    assert receipt["injection_reconstructs"] is True
    assert receipt["final_prompt_sha256"]


# ---------------------------------------------------------------------------
# 7. Provider-lane LEGACY path (kimi/codex/etc — the gap this dispatch closes)
#
# PR #1116 wired final_prompt_integrity into the tmux lane only. The provider
# lane's _enrich_instruction (codex, gemini, litellm, kimi, deepseek-harness,
# glm-harness) never persisted the enriched body. These tests exercise the
# REAL _dispatch_kimi entrypoint end-to-end (only the spawn + EventStore are
# mocked) to prove the wiring, not just the underlying module.
# ---------------------------------------------------------------------------

def _provider_args(**overrides) -> argparse.Namespace:
    defaults = {
        "provider": "kimi",
        "terminal_id": "T1",
        "dispatch_id": "d-provider-lane-ok",
        "instruction": "Build the loader. Return OK.",
        "model": "default",
        "max_retries": 3,
        "no_auto_commit": False,
        "gate": "",
        "dispatch_paths": "",
        "pr_id": None,
        "role": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _kimi_success_result():
    from provider_spawns.kimi_spawn import KimiSpawnResult  # noqa: PLC0415
    return KimiSpawnResult(
        returncode=0,
        completion_text="OK",
        events_written=1,
        session_id=None,
        timed_out=False,
        stopped_early=False,
        token_usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_creation_tokens": 0},
        error=None,
        event_writer_failures=0,
    )


def _last_receipt(state_dir: Path) -> dict:
    line = (state_dir / "t0_receipts.ndjson").read_text(encoding="utf-8").strip().splitlines()[-1]
    return json.loads(line)


def test_provider_lane_persists_final_prompt_and_stamps_receipt(tmp_path, monkeypatch):
    """The legacy provider-lane path (_dispatch_kimi, no envelope flag) must persist
    final_prompt.md + stamp final_prompt_path/sha/injection_reconstructs onto the
    receipt — the exact gap final_prompt_integrity's own docstring calls out.

    Explicitly monkeypatches _resolve_data_dir/_resolve_state_dir to tmp_path-based
    dirs rather than relying on ambient env-var isolation, so this test can never
    write into the operator's real central ~/.vnx-data store."""
    dispatch_id = "d-provider-lane-ok"
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    monkeypatch.setattr(pd, "_resolve_data_dir", lambda: data_dir)
    monkeypatch.setattr(pd, "_resolve_state_dir", lambda: state_dir)
    args = _provider_args(dispatch_id=dispatch_id)
    result = _kimi_success_result()

    with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result), \
         patch("event_store.EventStore", return_value=MagicMock()):
        rc = pd._dispatch_kimi(args)

    assert rc == 0

    bundle = data_dir / "dispatches" / "pending" / dispatch_id
    final_prompt_file = bundle / "final_prompt.md"
    assert final_prompt_file.exists()
    final_prompt = final_prompt_file.read_text(encoding="utf-8")
    assert "Build the loader. Return OK." in final_prompt

    receipt = _last_receipt(state_dir)
    assert receipt["injection_reconstructs"] is True
    assert receipt["final_prompt_sha256"] == hashlib.sha256(final_prompt.encode("utf-8")).hexdigest()
    assert receipt["final_prompt_path"] == str(final_prompt_file)


def test_provider_lane_mismatch_fails_loud_and_stamps_false(tmp_path, monkeypatch, caplog):
    """A recorded intelligence_injections row whose content never actually reached
    the enriched prompt (the real, unmocked IntelligenceSelector finds nothing
    against a fresh DB, so enriched == raw instruction) must flip
    injection_reconstructs False on the receipt and log an ERROR — the regression
    this track exists to catch, exercised through the real dispatch entrypoint.

    Explicitly monkeypatches _resolve_data_dir/_resolve_state_dir to tmp_path-based
    dirs rather than relying on ambient env-var isolation, so this test can never
    write into the operator's real central ~/.vnx-data store."""
    dispatch_id = "d-provider-lane-mismatch"
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    monkeypatch.setattr(pd, "_resolve_data_dir", lambda: data_dir)
    monkeypatch.setattr(pd, "_resolve_state_dir", lambda: state_dir)
    args = _provider_args(dispatch_id=dispatch_id)
    _write_injection_row(
        state_dir, dispatch_id,
        [_item("THIS PATTERN WAS NEVER ACTUALLY INJECTED", item_id="phantom-1")],
    )
    result = _kimi_success_result()

    with caplog.at_level(logging.ERROR, logger="final_prompt_integrity"):
        with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result), \
             patch("event_store.EventStore", return_value=MagicMock()):
            rc = pd._dispatch_kimi(args)

    # the dispatch itself still succeeds — audit closure never blocks a dispatch.
    assert rc == 0
    receipt = _last_receipt(state_dir)
    assert receipt["injection_reconstructs"] is False
    assert any(
        "reconstruction FAILED" in r.message and "phantom-1" in r.getMessage()
        for r in caplog.records
    )


def test_provider_lane_strict_mode_raises_and_propagates(tmp_path, monkeypatch):
    """VNX_INJECTION_RECONSTRUCT_STRICT=1 must fail-closed: the raised
    InjectionReconstructError propagates out of _enrich_instruction (and thus out
    of the dispatch handler) instead of being swallowed like every other error in
    the audit-closure step.

    Explicitly monkeypatches _resolve_data_dir/_resolve_state_dir to tmp_path-based
    dirs rather than relying on ambient env-var isolation, so this test can never
    write into the operator's real central ~/.vnx-data store."""
    monkeypatch.setenv("VNX_INJECTION_RECONSTRUCT_STRICT", "1")
    state_dir = tmp_path / "state"
    monkeypatch.setattr(pd, "_resolve_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(pd, "_resolve_state_dir", lambda: state_dir)
    dispatch_id = "d-provider-lane-strict"
    args = _provider_args(dispatch_id=dispatch_id, instruction="Build the loader. Return OK.")
    _write_injection_row(
        state_dir, dispatch_id,
        [_item("NEVER INJECTED", item_id="phantom-strict")],
    )

    with pytest.raises(fpi.InjectionReconstructError):
        pd._enrich_instruction(args)


def test_provider_lane_records_exactly_once(tmp_path, monkeypatch):
    """_enrich_instruction must call record_final_prompt_integrity exactly once per
    dispatch — no double-recording within the legacy provider-lane call chain.

    Explicitly monkeypatches _resolve_data_dir/_resolve_state_dir to tmp_path-based
    dirs rather than relying on ambient env-var isolation, so this test can never
    write into the operator's real central ~/.vnx-data store."""
    dispatch_id = "d-provider-lane-once"
    monkeypatch.setattr(pd, "_resolve_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(pd, "_resolve_state_dir", lambda: tmp_path / "state")
    args = _provider_args(dispatch_id=dispatch_id)
    result = _kimi_success_result()

    with patch(
        "final_prompt_integrity.record_final_prompt_integrity",
        wraps=fpi.record_final_prompt_integrity,
    ) as mock_record, \
         patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result), \
         patch("event_store.EventStore", return_value=MagicMock()):
        rc = pd._dispatch_kimi(args)

    assert rc == 0
    mock_record.assert_called_once()
    assert mock_record.call_args.kwargs["dispatch_id"] == dispatch_id


def test_emit_governance_tolerates_magicmock_args_without_integrity():
    """Regression guard: _emit_governance must read back _final_prompt_integrity via
    vars(args).get(...), not getattr(args, ..., None). A bare MagicMock args (as
    legacy tests widely use) auto-vivifies unset attributes via getattr, which would
    otherwise smuggle a non-serializable Mock into emit_dispatch_receipt's kwargs
    for every caller that skips _enrich_instruction (e.g. the claude
    subprocess-delegate path)."""
    from dataclasses import dataclass as _dc
    from datetime import datetime, timezone

    @_dc
    class _Result:
        completion_text: str = "OK"
        token_usage: Optional[dict] = None

    args = MagicMock()
    args.dispatch_id = "d-magicmock-no-integrity"
    args.terminal_id = "T1"
    args.instruction = "irrelevant"
    args.pr_id = None
    # NOTE: args._final_prompt_integrity is deliberately never set — this is the
    # exact condition the vars(args).get(...) fix guards against. governance_emit's
    # own functions are mocked out here (report YAML frontmatter / mandate_id
    # serialization are unrelated pre-existing gaps, not this track's concern);
    # this test isolates exactly the kwargs _emit_governance computes.
    start = end = datetime.now(timezone.utc)

    with patch("governance_emit.emit_dispatch_receipt") as mock_receipt, \
         patch("governance_emit.emit_unified_report") as mock_report:
        mock_receipt.return_value = Path("/tmp/receipt.ndjson")
        mock_report.return_value = Path("/tmp/report.md")
        # Must not raise while computing the integrity kwargs.
        pd._emit_governance(args, "claude", "sonnet", _Result(), start, end, "success")

    call_kwargs = mock_receipt.call_args.kwargs
    assert call_kwargs["final_prompt_path"] is None
    assert call_kwargs["final_prompt_sha256"] is None
    assert call_kwargs["injection_reconstructs"] is None
