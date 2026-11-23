"""permission_events_sources: B1, B2 and B4a readers, origin and account (ADR-075 decisions 2, 5, 8)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import permission_events_sources as src

TS = "2026-11-14T10:00:00.000Z"
UUID = "0a1b2c3d-1111-2222-3333-444455556666"


def use(tid, name="Bash", command="pytest -q", ts=TS):
    block = {"type": "tool_use", "id": tid, "name": name, "input": {"command": command} if command else {}}
    return {"type": "assistant", "timestamp": ts, "message": {"content": [block]}}


def result(tid, source, kind=None, code="rule:cliArg", decision="reject", text="denied", ts=TS):
    block = {"type": "tool_result", "tool_use_id": tid, "content": text,
             "permissionDecision": {"decision": decision, "source": source, "reasonCode": code}}
    if kind:
        block["toolDenialKind"] = kind
    return {"type": "user", "timestamp": ts, "message": {"content": [block]}}


def asked(text, ts=TS):
    return {"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "text", "text": text}]}}


def reasons(counter):
    return {k: v for k, v in counter.items() if not k.startswith("config_")}


def scan(objs, slug="-root", configs=("/nonexistent/a", "/nonexistent/b")):
    lines = [o if isinstance(o, str) else json.dumps(o) for o in objs]
    return src.scan_transcript(lines, project_id="vnx-dev", slug=slug, stem="sess1", account_configs=configs)


def one(objs, **kw):
    events = scan(objs, **kw).events
    assert len(events) == 1
    return events[0]


def test_b1_config_is_never_a_user_decision():
    ev = one([use("t1"), result("t1", "config", "user-rejected")])
    assert (ev["bron"], ev["besluit"], ev["vorm"]) == ("B1", "refused_config", "pytest -q")
    assert ev["reden"].startswith("rule:cliArg") and ev["session_id"] == "sess1"


@pytest.mark.parametrize("source,kind,code,besluit", [
    ("config", "permission-rule", "rule:cliArg", "refused_config"),
    ("config", "automode-blocked", "classifier:auto-mode", "refused_classifier"),
    ("classifier", "automode-blocked", "classifier:auto-mode", "refused_classifier"),
    ("config", "permission-rule", "hook:PreToolUse", "refused_hook"),
    ("", "permission-rule", "rule:userSettings", "refused_config"),
])
def test_b1_mapping(source, kind, code, besluit):
    ev = one([use("t1"), result("t1", source, kind, code)])
    assert (ev["bron"], ev["besluit"]) == ("B1", besluit)


@pytest.mark.parametrize("source,besluit", [
    ("user_temporary", "approved_once"), ("user_permanent", "approved_permanent"), ("user_reject", "refused_user"),
])
def test_b2_mapping(source, besluit):
    ev = one([use("t1"), result("t1", source, None, "safetyCheck", "accept")])
    assert (ev["bron"], ev["besluit"]) == ("B2", besluit)


def test_non_bash_tool_uses_the_tool_name():
    assert one([use("t1", "Write", ""), result("t1", "user_temporary", decision="accept")])["vorm"] == "Write"


@pytest.mark.parametrize("bad", [
    result("t1", "user-ish-new-thing", "permission-rule"),   # unknown source
    result("t1", "", "user-rejected"),                        # a human rejection that cannot be proven
    result("t9", "config", "permission-rule"),                # no matching tool_use
    result("t1", "config", "permission-rule", ts="not a time"),
])
def test_unmappable_lines_are_skipped_and_counted(bad):
    out = scan([use("t1"), bad])
    assert out.events == [] and sum(reasons(out.skipped["B1"]).values()) == 1


def test_automatic_accepts_and_plain_results_are_ignored():
    out = scan([use("t1"), result("t1", "config", None, "rule", "accept"), "not json"])
    assert out.events == [] and reasons(out.skipped["B1"]) == {"malformed_json": 1}


def test_b4a_copy_block_needs_an_ask_outside_the_block():
    text = "Draai dit in een eigen terminal:\n```bash\n# comment\n! cd /x && vnx deliverable promote dlv-4c83df210cdf\nls\n```"
    ev = one([asked(text)])
    assert (ev["bron"], ev["besluit"], ev["vorm"], ev["dispatch_id"]) == ("B4a", "asked", "vnx deliverable promote <dlv>", "dlv-4c83df210cdf")
    assert scan([asked("Hier is code:\n```bash\nls\n```")]).events == []
    assert scan([asked("```bash\n# draai dit\nls\n```")]).events == []
    assert len(scan([asked("jij moet dit doen:\n```\nls -la\n```\nen ook\n```\npwd\n```")]).events) == 2


@pytest.mark.parametrize("slug,origin", [
    ("-Users-x-Development-vnx-orchestration--vnx-data-worktrees-dispatch-dlv-4c83df210cdf", "worker"),
    ("-Users-x-repo-.vnx-data-worktrees-dispatch-a", "worker"),
    ("-Users-x-Development-vnx-orchestration--claude-terminals-T0", "t0"),
    ("-Users-x-T0", "t0"),
    ("-Users-x-Development-vnx-orchestration", "operator"),
    ("-tmp-claude-worktrees-foo", "unknown"),
])
def test_origin_and_slug_never_in_the_event(slug, origin):
    assert src.origin_of(slug) == origin
    ev = one([use("t1"), result("t1", "config", "permission-rule")], slug=slug)
    assert ev["origin"] == origin and slug not in json.dumps(ev)


def test_dispatch_id_from_a_worktree_slug():
    ev = one([use("t1"), result("t1", "config", "permission-rule")], slug="-a--vnx-data-worktrees-dispatch-dlv-4c83df210cdf")
    assert ev["dispatch_id"] == "dlv-4c83df210cdf"


def test_account_is_a_slot_and_the_uuid_stays_inside(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"oauthAccount": {"accountUuid": "other", "emailAddress": "a@example.invalid"}}))
    b.write_text(json.dumps({"oauthAccount": {"accountUuid": UUID, "emailAddress": "b@example.invalid"}}))
    owner = {"type": "session-owner", "accountUuid": UUID, "sessionId": "sess-from-owner"}
    ev = one([owner, use("t1"), result("t1", "config", "permission-rule")], configs=(a, b))
    assert ev["account"] == "sm" and ev["session_id"] == "sess-from-owner"
    assert UUID not in json.dumps(ev) and "example.invalid" not in json.dumps(ev)
    assert src.resolve_account(UUID, (b, a)) == "vnx"
    for owner_uuid in (None, "", "nobody"):
        assert src.resolve_account(owner_uuid, (a, b)) == "unknown"
    a.write_text("{broken")
    assert src.resolve_account(UUID, (a, tmp_path / "missing.json")) == "unknown"


def _cfg(tmp_path, name, content):
    (tmp_path / name).write_bytes(content if isinstance(content, bytes) else content.encode())
    return tmp_path / name


@pytest.mark.parametrize("name,content,reason", [
    ("bad.json", "{broken", "config_unparseable_vnx"),
    ("bin.json", b"\xff\xfe\x00", "config_unreadable_vnx"),
    ("list.json", "[1]", "config_wrong_type_vnx"),
    ("acct.json", json.dumps({"oauthAccount": "x@example.invalid"}), "config_wrong_type_vnx"),
    ("uuid.json", json.dumps({"oauthAccount": {"accountUuid": 7}}), "config_wrong_type_vnx"),
])
def test_config_failure_is_a_counted_reason_distinct_from_no_match(tmp_path, name, content, reason):
    good = _cfg(tmp_path, "good.json", json.dumps({"oauthAccount": {"accountUuid": "other", "emailAddress": "g@example.invalid"}}))
    bad = _cfg(tmp_path, name, content)
    assert src.resolve_account_checked(UUID, (bad, good)) == ("unknown", [reason])
    assert src.resolve_account_checked(UUID, (good, good)) == ("unknown", [])
    out = scan([{"type": "session-owner", "accountUuid": UUID}], configs=(bad, good))
    assert out.skipped["B1"] == {reason: 1}


def test_unreadable_config_names_slot_not_path(tmp_path):
    missing = tmp_path / "secret-home" / "missing.json"
    assert src.resolve_account_checked(UUID, (missing, missing)) == ("unknown", ["config_unreadable_vnx", "config_unreadable_sm"])


@pytest.mark.parametrize("decision", ["reject", ["reject"], 7])
def test_malformed_permission_decision_is_skipped_not_fatal(decision):
    bad = result("t2", "config", "permission-rule")
    bad["message"]["content"][0]["permissionDecision"] = decision
    out = scan([use("t1"), use("t2"), result("t1", "config", "permission-rule"), bad])
    assert len(out.events) == 1
    assert out.skipped["B1"]["malformed_permission_metadata"] == 1


def test_malformed_denial_kind_is_skipped_not_fatal():
    bad = result("t2", "config", None)
    bad["message"]["content"][0]["toolDenialKind"] = ["permission-rule"]
    out = scan([use("t1"), use("t2"), result("t1", "config", "permission-rule"), bad])
    assert len(out.events) == 1 and out.skipped["B1"]["malformed_permission_metadata"] == 1


def test_ledger_and_coverage_hold_no_uuid_email_or_home_path(tmp_path):
    import permission_events as pe
    a = _cfg(tmp_path, "a.json", json.dumps({"oauthAccount": {"accountUuid": UUID, "emailAddress": "a@example.invalid"}}))
    b = _cfg(tmp_path, "b.json", "{broken")
    owner = {"type": "session-owner", "accountUuid": UUID, "sessionId": "s"}
    out = scan([owner, use("t1"), result("t1", "config", "permission-rule")], configs=(a, b))
    state = tmp_path / "state"
    pe.write_events(out.events, "vnx-dev", state)
    pe.write_coverage("vnx-dev", "B1", state="ok", files_read=1, window="test", count=1, state_dir=state,
                      skipped=dict(out.skipped["B1"]))
    cov = json.loads((state / "permission_coverage.json").read_text())
    assert cov["sources"]["B1"]["skipped"] == {"config_unparseable_sm": 1}
    text = "".join(p.read_text() for p in state.rglob("*") if p.is_file())
    assert text and UUID not in text and "example.invalid" not in text and str(tmp_path) not in text


def bash_input(cmd, ts=TS, tag="bash-input"):
    return {"type": "user", "timestamp": ts, "message": {"content": f"<{tag}>{cmd}</{tag}>"}}


def test_b3_one_executed_event_per_bash_input_line() -> None:
    got = scan([bash_input("bin/vnx deliverable promote dlv-0123456789ab"), bash_input("ok", tag="bash-stdout"),
                bash_input("boom", tag="bash-stderr")])
    b3 = [e for e in got.events if e["bron"] == "B3"]
    assert len(b3) == 1
    assert (b3[0]["besluit"], b3[0]["ts"], b3[0]["dispatch_id"]) == ("executed", TS, "dlv-0123456789ab")
    assert b3[0]["vorm"] == "bin/vnx deliverable promote <dlv>"


def test_b3_masks_a_secret_and_skips_an_empty_command() -> None:
    got = scan([bash_input("TOKEN=abc123def456 bin/vnx doctor sk-ABCDEFGHIJKLMNOPQRSTUVWX"), bash_input("  ")])
    blob = json.dumps(got.events)
    assert "abc123def456" not in blob and "sk-ABCDEF" not in blob
    assert len([e for e in got.events if e["bron"] == "B3"]) == 1
    assert got.skipped["B3"]["no_command_line"] == 1


def test_b3_bad_timestamp_is_counted_not_raised() -> None:
    got = scan([bash_input("ls", ts="not-a-time")])
    assert [e for e in got.events if e["bron"] == "B3"] == [] and got.skipped["B3"]["bad_timestamp"] == 1
