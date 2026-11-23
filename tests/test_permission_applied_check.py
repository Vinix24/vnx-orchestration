"""ADR-086 decision 2 (permissieblokkades-leren L7a): ``applied`` on a ``permissie`` proposal checks the settings
file read-only first. Every settings file is built under tmp_path; no real settings file is read or written."""

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import steering_proposal_store as store  # noqa: E402

PID, T0 = "vnx-dev", "2026-11-17T08:00:00Z"
RULE = "Bash(git log:*)"


def digest(rule=RULE):
    return hashlib.sha256(rule.encode("utf-8")).hexdigest()[:16]


def voorstel(rule=RULE):
    return (f"Add `{rule}` via /permissions, scope project (.claude/settings.local.json), account vnx, class K1: "
            "a read-only verb (3 sessions). Way back: remove the rule from .claude/settings.local.json.")


def write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    os.utime(path, (1_700_000_000, 1_700_000_000))
    return path


def seed(w, account="vnx", scope="project", kind="permissie", text=None, oorzaak=None):
    item = {"oorzaak": oorzaak or f"permission_form:{kind}:{account}:{scope}:{'a' * 16}", "artefact_soort": kind,
            "doelpad": ".claude/settings.local.json", "voorstel": text or voorstel(), "handelingsklasse": "K1",
            "evidence": [{"prompt_id": "p1", "ts": T0}]}
    pid = store.add_revision(w.state, PID, item, now=T0)["proposal_id"]
    store.decide(w.state, PID, pid, "approved", reason="ja", decided_by="op", now=T0)
    return pid


@pytest.fixture()
def w(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / "home"
    ns = SimpleNamespace(repo=repo, home=home, state=tmp_path / "state",
                         settings=write(repo / ".claude" / "settings.local.json", {"permissions": {"allow": ["Bash(ls)", RULE]}}))
    ns.pid = seed(ns)
    return ns


def snap(*paths):
    return [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths if p.exists() and not p.is_symlink()]


def apply(w, ref, pid=None, now=T0):
    return store.decide(w.state, PID, pid or w.pid, "applied", reason="toegepast", decided_by="op", applied_ref=ref,
                        settings_roots=(w.repo, w.home), now=now)


def refused(w, ref, check, pid=None):
    files, before = snap(w.settings), len(store.decisions(w.state, PID))
    with pytest.raises(store.DecisionRefused) as info:
        apply(w, ref, pid)
    assert str(info.value).startswith(f"applied refused, {check}:") and "\n" not in str(info.value)
    assert len(store.decisions(w.state, PID)) == before and store.get(w.state, PID, pid or w.pid)["status"] == "approved"
    assert snap(w.settings) == files


def test_a_passing_check_writes_the_record_with_path_rule_sha_and_mtime_and_leaves_the_file(w):
    files, start = snap(w.settings), datetime.now(timezone.utc) - timedelta(seconds=1)
    record = apply(w, f"settings:{w.settings}#{digest()}", now=None)
    ev = record["applied_evidence"]
    assert (ev["path"], ev["rule"], ev["rule_sha256"]) == (str(w.settings), RULE, hashlib.sha256(RULE.encode()).hexdigest())
    assert ev["file_sha256"] == hashlib.sha256(w.settings.read_bytes()).hexdigest()
    assert ev["mtime"] == datetime.fromtimestamp(1_700_000_000, timezone.utc).isoformat()
    assert record["applied_at"] == record["decided_at"] and store._parse_ts(record["applied_at"]) >= start
    assert store.get(w.state, PID, w.pid)["status"] == "applied" and snap(w.settings) == files


@pytest.mark.parametrize("ref", ["", None, "settings:/x#nothex0000000000", "PR #2690", "settings:#" + "a" * 16])
def test_applied_ref_missing_none_malformed_or_unknown_is_refused(w, ref):
    refused(w, ref, "applied_ref")


@pytest.mark.parametrize("oorzaak,text", [("permission_form:x", None), ("permission_form:permissie:xx:project:" + "a" * 16, None),
                                          ("permission_form:permissie:vnx:project:" + "b" * 16, "Voeg Bash(ls) toe"),
                                          ("permission_form:permissie:vnx:project:" + "c" * 16, "Add `` via /permissions, scope project")])
def test_a_proposal_without_account_scope_or_rule_is_refused(w, oorzaak, text):
    refused(w, f"settings:{w.settings}#{digest()}", "proposal", seed(w, oorzaak=oorzaak, text=text))


def test_a_path_outside_scope_or_account_is_refused(w):
    other = write(w.home / ".claude" / "settings.json", {"permissions": {"allow": [RULE]}})
    for path in (other, "~/.claude/settings.json", ".claude/settings.local.json", f"{w.repo}/.claude/../.claude/settings.local.json",
                 w.repo / "settings.local.json", "/etc/passwd"):
        refused(w, f"settings:{path}#{digest()}", "path")
    sm = seed(w, account="sm", scope="user")
    refused(w, f"settings:{other}#{digest()}", "path", sm)
    write(w.home / ".claude-salesminds" / "settings.json", {"permissions": {"allow": [RULE]}})
    assert apply(w, f"settings:~/.claude-salesminds/settings.json#{digest()}", sm)["applied_evidence"]["rule"] == RULE
    assert snap(other)[0][0] == json.dumps({"permissions": {"allow": [RULE]}}).encode()


def test_a_hash_that_is_not_the_rule_is_refused(w):
    refused(w, f"settings:{w.settings}#{digest('Bash(ls)')}", "hash")


def test_a_missing_file_a_symlink_or_a_directory_is_refused(w, tmp_path):
    shared = w.repo / ".claude" / "settings.json"
    refused(w, f"settings:{shared}#{digest()}", "file")
    shared.mkdir()
    refused(w, f"settings:{shared}#{digest()}", "file")
    shared.rmdir()
    shared.symlink_to(w.settings)
    refused(w, f"settings:{shared}#{digest()}", "file")


@pytest.mark.parametrize("body", [b"", b"{", b"\xff\xfe{}", b"{'permissions': {}}"])
def test_a_file_that_is_not_json_is_refused(w, body):
    write(w.settings, body)
    refused(w, f"settings:{w.settings}#{digest()}", "json")


@pytest.mark.parametrize("body", [None, {}, {"permissions": None}, {"permissions": {}}, {"permissions": {"allow": None}},
                                  {"permissions": {"allow": RULE}}, {"permissions": {"allow": {RULE: 1}}}, [RULE],
                                  {"permissions": [RULE]}])
def test_no_permissions_allow_list_is_refused(w, body):
    write(w.settings, body)
    refused(w, f"settings:{w.settings}#{digest()}", "list")


@pytest.mark.parametrize("allow", [[], [None], [f" {RULE}"], ["Bash(git log)"], [RULE.lower()]])
def test_a_list_without_the_exact_rule_is_refused(w, allow):
    write(w.settings, {"permissions": {"allow": allow, "deny": [RULE]}})
    refused(w, f"settings:{w.settings}#{digest()}", "rule")


@pytest.mark.parametrize("kind", ["bouwer_allowlist", "bouwgewoonte"])
def test_other_kinds_keep_a_pr_or_commit_ref(w, kind):
    pid = seed(w, kind=kind)
    assert apply(w, "PR #2690", pid)["applied_ref"] == "PR #2690" and "applied_evidence" not in store.decisions(w.state, PID)[-1]
    assert store.get(w.state, PID, pid)["status"] == "applied"
