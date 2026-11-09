"""The dispatch receipt carries a context_manifest reference, the manifest lands in a sidecar first.

agents-als-map PR-4b, ADR-050 contract 6. The transcript is the synthetic fixture of
``test_context_manifest.py``; every path is a temp dir.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import context_manifest
import governance_emit
from governance_emit import emit_dispatch_receipt
from receipt_schema import ReceiptV2

FIXTURE = Path(__file__).parent / "fixtures" / "context_manifest" / "transcript_2_1_287.jsonl"
SESSION = "sess-fixture-1"
LEDGER = "context_manifests.ndjson"

# The receipt origin/main (94ecddd1f) emits for _kwargs(), timestamp replaced.
GOLDEN_OFF = (
    '{"schema_version":2,"dispatch_id":"d-1","terminal_id":"T1","provider":"claude",'
    '"model":"sonnet-4-6","role":null,"receipt_kind":"dispatch","status":"success",'
    '"event_type":"task_complete","completion_pct":100,"risk":0.0,"duration_seconds":3.5,'
    '"token_usage":{"input":100,"output":50,"cache_hit":0},"cost_usd":null,"findings":[],'
    '"pr_id":null,"report_path":null,"events_path":null,"timestamp":"T",'
    '"outcome_id":"d1b35096bff3a8ed780a580461f40ee6be3e48bef35a58dd3111515042ccc09f",'
    '"verdict":{"decision":"investigate","reason":"status=\'success\' claims success but '
    'verification test evidence is absent or incomplete","evidence_complete":true}}'
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, repo, projects, state = (tmp_path / n for n in ("home", "repo", "projects", "state"))
    for d in (home, repo, projects / "-enc", state):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VNX_PROJECT_ID", "proj-a")
    for name in ("VNX_CLAUDE_PROJECTS_DIR", "VNX_AGENT_LAYOUT", "VNX_TASK_CLASS", "VNX_TIER_FROM",
                 "VNX_TIER_TO", "VNX_PARENT_DISPATCH"):
        monkeypatch.delenv(name, raising=False)
    text = FIXTURE.read_text(encoding="utf-8").replace("{HOME}", str(home)).replace("{REPO}", str(repo))
    transcript = projects / "-enc" / f"{SESSION}.jsonl"
    transcript.write_text(text, encoding="utf-8")
    real = context_manifest.capture
    monkeypatch.setattr(
        context_manifest, "capture",
        lambda sid, **kw: real(sid, claude_projects_dir=projects, repo_root=repo, **kw))
    return {"state": state, "transcript": transcript}


def _kwargs(env, **over):
    base = dict(
        dispatch_id="d-1", terminal_id="T1", provider="claude", model="sonnet-4-6", pr_id=None,
        status="success", completion_pct=100, risk=0.0, findings=[], duration_seconds=3.5,
        token_usage={"input": 100, "output": 50, "cache_hit": 0}, cost_usd=None,
        state_dir=env["state"], receipt_kind="dispatch", session_id=SESSION,
    )
    base.update(over)
    return base


def _receipts(env):
    return [json.loads(l) for l in (env["state"] / "t0_receipts.ndjson").read_text().splitlines()]


def _sidecar(env):
    path = env["state"] / LEDGER
    return path.read_text().splitlines() if path.exists() else []


def _shadow(monkeypatch):
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")


def test_shadow_writes_the_sidecar_line_first_and_the_receipt_carries_its_hash(env, monkeypatch):
    _shadow(monkeypatch)
    real_write = governance_emit._write_receipt_under_lock
    seen = []

    def spy(*a, **k):
        seen.append(_sidecar(env))  # what is already durable the moment the receipt is written
        return real_write(*a, **k)
    monkeypatch.setattr(governance_emit, "_write_receipt_under_lock", spy)
    emit_dispatch_receipt(**_kwargs(env))
    [line] = _sidecar(env)
    assert seen == [[line]]
    ref = _receipts(env)[0]["context_manifest"]
    assert ref == {"schema": 1, "status": "ok", "sha256": hashlib.sha256(line.encode()).hexdigest()}
    row = json.loads(line)
    assert (row["dispatch_id"], row["session_id"], row["project_id"]) == ("d-1", SESSION, "proj-a")
    assert row["manifest"]["status"] == "ok"


def test_second_emit_with_the_same_three_keys_writes_no_second_line(env, monkeypatch):
    _shadow(monkeypatch)
    emit_dispatch_receipt(**_kwargs(env))
    emit_dispatch_receipt(**_kwargs(env))
    assert len(_sidecar(env)) == 1


def test_a_new_attempt_in_another_session_gets_its_own_line(env, monkeypatch):
    _shadow(monkeypatch)
    emit_dispatch_receipt(**_kwargs(env))
    second = env["transcript"].with_name("sess-fixture-2.jsonl")
    second.write_text(env["transcript"].read_text(encoding="utf-8"), encoding="utf-8")
    emit_dispatch_receipt(**_kwargs(env, session_id="sess-fixture-2"))
    rows = [json.loads(l) for l in _sidecar(env)]
    assert [r["session_id"] for r in rows] == [SESSION, "sess-fixture-2"]
    assert {r["dispatch_id"] for r in rows} == {"d-1"}


def test_failing_sidecar_stamps_an_error_without_a_hash_and_the_receipt_lands(env, monkeypatch):
    _shadow(monkeypatch)
    (env["state"] / LEDGER).mkdir()  # a directory where the ledger file belongs
    emit_dispatch_receipt(**_kwargs(env))
    ref = _receipts(env)[0]["context_manifest"]
    assert ref == {"schema": 1, "status": "error", "reason": "ledger_write_failed:IsADirectoryError"}
    assert "sha256" not in ref


def test_fsync_failure_gives_an_error_without_a_hash(env, monkeypatch):
    _shadow(monkeypatch)
    monkeypatch.setattr(governance_emit.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    emit_dispatch_receipt(**_kwargs(env))
    assert _receipts(env)[0]["context_manifest"] == {
        "schema": 1, "status": "error", "reason": "ledger_write_failed:OSError"}


def test_unresolvable_project_fails_closed_without_a_line(env, monkeypatch):
    _shadow(monkeypatch)
    monkeypatch.delenv("VNX_PROJECT_ID")
    emit_dispatch_receipt(**_kwargs(env))
    ref = _receipts(env)[0]["context_manifest"]
    assert ref["status"] == "error" and ref["reason"].startswith("ledger_write_failed:") and "sha256" not in ref
    assert _sidecar(env) == []


@pytest.mark.parametrize("kwargs,status,reason", [
    ({"session_id": None}, "unavailable", "no_session_id"),
    ({"session_id": "no-such-session"}, "unavailable", "transcript_not_found"),
    ({"provider": "kimi"}, "unsupported", "provider_not_supported:kimi"),
])
def test_unusable_capture_is_stamped_with_its_reason_and_writes_no_line(env, monkeypatch, kwargs, status, reason):
    _shadow(monkeypatch)
    emit_dispatch_receipt(**_kwargs(env, **kwargs))
    assert _receipts(env)[0]["context_manifest"] == {"schema": 1, "status": status, "reason": reason}
    assert _sidecar(env) == []


def test_capture_error_is_stamped_with_its_reason_and_writes_no_line(env, monkeypatch):
    _shadow(monkeypatch)
    monkeypatch.setattr(context_manifest, "capture", lambda *a, **k: {"schema": 1, "status": "error", "reason": "extract_failed:ValueError"})
    emit_dispatch_receipt(**_kwargs(env))
    assert _receipts(env)[0]["context_manifest"] == {"schema": 1, "status": "error", "reason": "extract_failed:ValueError"}
    assert _sidecar(env) == []


def test_a_partial_capture_is_stamped_partial_with_a_hash(env, monkeypatch):
    _shadow(monkeypatch)
    lines = [l for l in env["transcript"].read_text(encoding="utf-8").splitlines() if "credential_org" not in l]
    env["transcript"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    emit_dispatch_receipt(**_kwargs(env))
    ref = _receipts(env)[0]["context_manifest"]
    assert ref["status"] == "partial" and len(ref["sha256"]) == 64
    assert len(_sidecar(env)) == 1


def test_a_capture_that_raises_never_fails_the_receipt(env, monkeypatch):
    _shadow(monkeypatch)
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(context_manifest, "capture", boom)
    emit_dispatch_receipt(**_kwargs(env))
    assert _receipts(env)[0]["context_manifest"] == {"schema": 1, "status": "error", "reason": "extract_failed:RuntimeError"}


@pytest.mark.parametrize("value", [None, "", "off", "bogus"])
def test_off_and_unrecognised_modes_are_byte_equal_to_origin_main(env, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("VNX_AGENT_LAYOUT", value)

    def boom(*a, **k):
        raise AssertionError("capture must not run")
    monkeypatch.setattr(context_manifest, "capture", boom)
    emit_dispatch_receipt(**_kwargs(env))
    raw = (env["state"] / "t0_receipts.ndjson").read_text().strip()
    receipt = json.loads(raw)
    receipt["timestamp"] = "T"
    assert json.dumps(receipt, separators=(",", ":")) == GOLDEN_OFF
    assert not (env["state"] / LEDGER).exists()


def test_no_secret_and_no_address_reach_the_sidecar_or_the_receipt(env, monkeypatch):
    _shadow(monkeypatch)
    extra = [
        {"type": "attachment", "version": "2.1.287", "attachment": {
            "type": "hook_success", "hookEvent": "PreToolUse", "hookName": "PreToolUse:Bash",
            "command": "/opt/hooks/check.sh SENTINEL-SECRET --token SENTINEL-SECRET"}},
        {"type": "attachment", "version": "2.1.287", "attachment": {
            "type": "credential_org", "organizationUuid": "org-1", "email": "who@example.invalid"}},
    ]
    with env["transcript"].open("a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(e) + "\n" for e in extra))
    emit_dispatch_receipt(**_kwargs(env))
    blob = (env["state"] / LEDGER).read_text() + (env["state"] / "t0_receipts.ndjson").read_text()
    assert _sidecar(env)
    for forbidden in ("SENTINEL-SECRET", "SENTINEL-CONTENT", "example.invalid", "@"):
        assert forbidden not in blob


def test_context_manifest_is_stamped_only_when_set_and_is_not_an_idempotency_field():
    from append_receipt_internals.idempotency import IDEMPOTENCY_FIELDS
    kw = dict(dispatch_id="d", terminal_id="T1", provider="claude", model="m", event_type="task_complete",
              status="success", completion_pct=100, risk=0.0, findings=[], duration_seconds=1,
              token_usage={}, cost_usd=None, pr_id=None, role=None, receipt_kind="dispatch")
    assert "context_manifest" not in ReceiptV2(**kw).to_dict()
    ref = {"schema": 1, "status": "ok", "sha256": "ab"}
    assert ReceiptV2(**kw, context_manifest=ref).to_dict()["context_manifest"] == ref
    assert "context_manifest" not in IDEMPOTENCY_FIELDS


def test_the_ledger_helper_dedups_on_the_whole_line_and_ignores_malformed_lines(tmp_path):
    path = tmp_path / LEDGER
    path.write_text("not json\n", encoding="utf-8")
    line = governance_emit._manifest_line("d", "s", "p", {"status": "ok"})
    governance_emit._append_manifest_line(path, line)
    governance_emit._append_manifest_line(path, line)
    other = governance_emit._manifest_line("d", "s2", "p", {"status": "ok"})
    governance_emit._append_manifest_line(path, other)
    assert path.read_text().splitlines() == ["not json", line, other]


def _emit_stamp(env):
    (env["state"] / "t0_receipts.ndjson").unlink(missing_ok=True)  # a fresh receipt each time, the sidecar stays
    emit_dispatch_receipt(**_kwargs(env))
    return _receipts(env)[-1]["context_manifest"]


def _complete_lines(env):
    raw = (env["state"] / LEDGER).read_bytes()
    assert raw.endswith(b"\n")
    return [json.loads(l) for l in raw.decode().splitlines()]


def test_an_unterminated_tail_is_dropped_counted_and_the_next_line_matches_its_hash(env, monkeypatch, caplog):
    _shadow(monkeypatch)
    (env["state"] / LEDGER).write_bytes(b'{"a":1}\n{"dispatch_id":"x","sess')
    with caplog.at_level("WARNING"):
        ref = _emit_stamp(env)
    assert "dropped 24 bytes" in caplog.text and '"sess' not in caplog.text
    rows = _sidecar(env)
    assert rows[0] == '{"a":1}' and len(rows) == 2
    assert [json.loads(r) for r in rows]
    assert ref["status"] == "ok" and ref["sha256"] == hashlib.sha256(rows[1].encode()).hexdigest()


def test_a_short_write_that_finishes_in_the_loop_gives_one_complete_line(env, monkeypatch):
    _shadow(monkeypatch)
    real, calls = governance_emit.os.write, []

    def short(fd, data):
        calls.append(len(data))
        return real(fd, bytes(data)[: max(1, len(data) // 2)]) if len(calls) == 1 else real(fd, data)
    monkeypatch.setattr(governance_emit.os, "write", short)
    ref = _emit_stamp(env)
    assert len(calls) == 2
    [line] = _sidecar(env)
    assert ref["sha256"] == hashlib.sha256(line.encode()).hexdigest()
    assert len(_complete_lines(env)) == 1


def test_a_write_that_fails_halfway_leaves_the_ledger_byte_equal_and_no_hash(env, monkeypatch):
    _shadow(monkeypatch)
    ledger = env["state"] / LEDGER
    ledger.write_bytes(b'{"a":1}\n')
    before = ledger.read_bytes()
    real, calls = governance_emit.os.write, []

    def half_then_fail(fd, data):
        calls.append(1)
        if len(calls) == 1:
            return real(fd, bytes(data)[: len(data) // 2])
        raise OSError("disk full")
    monkeypatch.setattr(governance_emit.os, "write", half_then_fail)
    ref = _emit_stamp(env)
    assert ledger.read_bytes() == before
    assert ref == {"schema": 1, "status": "error", "reason": "ledger_write_failed:OSError"}


def test_a_zero_byte_write_rolls_back_and_fails(env, monkeypatch):
    _shadow(monkeypatch)
    monkeypatch.setattr(governance_emit.os, "write", lambda fd, data: 0)
    ref = _emit_stamp(env)
    assert _sidecar(env) == [] and "sha256" not in ref and ref["status"] == "error"


def test_a_duplicate_with_a_failing_fsync_gives_no_hash_and_a_working_one_the_same_hash(env, monkeypatch):
    _shadow(monkeypatch)
    manifest = context_manifest.capture(SESSION, provider="claude")
    args = ("d-1", SESSION, env["state"], manifest)
    first = governance_emit._write_manifest_sidecar(*args)
    assert first["status"] == "ok" and governance_emit._write_manifest_sidecar(*args) == first
    monkeypatch.setattr(governance_emit.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    assert governance_emit._write_manifest_sidecar(*args) == {
        "schema": 1, "status": "error", "reason": "ledger_write_failed:OSError"}
    assert len(_sidecar(env)) == 1


def _short_pread_once(monkeypatch):
    real, calls = governance_emit.os.pread, []

    def short(fd, n, offset):
        calls.append((n, offset))
        data = real(fd, n, offset)
        return data[: len(data) // 2] if len(calls) == 1 else data
    monkeypatch.setattr(governance_emit.os, "pread", short)
    return calls


def test_a_short_read_that_completes_in_the_loop_keeps_every_complete_line(env, monkeypatch):
    _shadow(monkeypatch)
    ledger = env["state"] / LEDGER
    ledger.write_bytes(b'{"a":1}\n{"bbbbbbbbbbbb":2}\n')
    calls = _short_pread_once(monkeypatch)
    ref = _emit_stamp(env)
    assert len(calls) >= 2 and calls[1][1] > 0  # the second read resumes at the running offset
    rows = _sidecar(env)
    assert rows[:2] == ['{"a":1}', '{"bbbbbbbbbbbb":2}'] and len(rows) == 3
    assert ref["status"] == "ok" and ref["sha256"] == hashlib.sha256(rows[2].encode()).hexdigest()


def test_a_short_read_drops_only_the_real_fragment(env, monkeypatch):
    _shadow(monkeypatch)
    ledger = env["state"] / LEDGER
    ledger.write_bytes(b'{"a":1}\n{"bbbbbbbbbbbb":2}\n{"dispatch_id":"x","sess')
    _short_pread_once(monkeypatch)
    _emit_stamp(env)
    rows = _sidecar(env)
    assert rows[:2] == ['{"a":1}', '{"bbbbbbbbbbbb":2}'] and len(rows) == 3


def test_an_incomplete_read_leaves_the_ledger_byte_equal_and_no_hash(env, monkeypatch):
    _shadow(monkeypatch)
    ledger = env["state"] / LEDGER
    ledger.write_bytes(b'{"a":1}\n{"bbbbbbbbbbbb":2}\n{"dispatch_id":"x","sess')
    before = ledger.read_bytes()
    monkeypatch.setattr(governance_emit.os, "pread", lambda fd, n, offset: b"")
    ref = _emit_stamp(env)
    assert ledger.read_bytes() == before
    assert ref == {"schema": 1, "status": "error", "reason": "ledger_write_failed:OSError"}


def test_a_read_that_raises_leaves_the_ledger_byte_equal_and_no_hash(env, monkeypatch):
    _shadow(monkeypatch)
    ledger = env["state"] / LEDGER
    ledger.write_bytes(b'{"a":1}\n{"bbbbbbbbbbbb":2}\n{"frag')
    before = ledger.read_bytes()
    monkeypatch.setattr(governance_emit.os, "pread", lambda fd, n, offset: (_ for _ in ()).throw(OSError("io")))
    ref = _emit_stamp(env)
    assert ledger.read_bytes() == before
    assert ref == {"schema": 1, "status": "error", "reason": "ledger_write_failed:OSError"}
