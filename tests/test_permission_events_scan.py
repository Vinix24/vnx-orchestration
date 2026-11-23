"""permission_events_scan: binding, containment, tenant border, privacy and determinism (ADR-075)."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import operator_steering
import operator_steering_source as source
import permission_events_scan as scan
from test_permission_events_sources import UUID, asked, result, use

NOW = datetime.now(timezone.utc).isoformat()
OUTPUT = "Q7xk2mZp9RvL4tWb8NcY3hJd6FgS1aEu5oPi0KeT"  # 40 unique characters
EMAIL, PROMPT = "owner@example.invalid", "PROMPTTEXT-zebra-quokka-42"
SLUG = "-proj-root"


class Env:
    def __init__(self, tmp_path, monkeypatch, slug=SLUG, bind=True):
        monkeypatch.setenv("HOME", str(tmp_path))
        self.tmp, self.projects = tmp_path, tmp_path / "projects"
        self.state = tmp_path / ".vnx-data" / "vnx-dev" / "state"
        self.dir = self.projects / slug
        self.dir.mkdir(parents=True)
        (self.projects / "memory").mkdir()
        self.vnx, self.sm = tmp_path / "vnx.json", tmp_path / "sm.json"
        self.vnx.write_text(json.dumps({"oauthAccount": {"accountUuid": UUID, "emailAddress": EMAIL}}))
        self.sm.write_text("{}")
        if bind:
            source.bind(self.state, self.projects, [slug], self.projects / "memory")

    def transcript(self, objs, name="s1.jsonl"):
        (self.dir / name).write_text("\n".join(json.dumps(o) for o in objs) + "\n")

    def run(self, project="vnx-dev", state=None, extra=()):
        argv = ["--project-id", project, "--since", "7d", "--projects-root", str(self.projects), "--state-dir",
                str(state or self.state), "--vnx-config", str(self.vnx), "--sm-config", str(self.sm), *extra]
        return scan.main(argv)

    def ledger(self):
        path = self.state / "permission_events.ndjson"
        return path.read_bytes() if path.exists() else b""

    def coverage(self):
        return json.loads((self.state / "permission_coverage.json").read_text())["sources"]


FULL = [
    {"type": "session-owner", "accountUuid": UUID, "sessionId": "sess-1"},
    {"type": "user", "timestamp": NOW, "message": {"content": PROMPT}},
    use("t1", ts=NOW), result("t1", "config", "user-rejected", ts=NOW),
    use("t2", command="sed -i x f", ts=NOW), result("t2", "user_temporary", decision="accept", text=OUTPUT, ts=NOW),
    asked("Draai dit zelf:\n```bash\nvnx release publish --tag v1\n```", ts=NOW),
]


def test_scan_reads_b1_b2_b4a_and_writes_coverage(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.transcript(FULL)
    assert env.run() == 0
    events = [json.loads(line) for line in env.ledger().splitlines()]
    assert sorted((e["bron"], e["besluit"]) for e in events) == [
        ("B1", "refused_config"), ("B2", "approved_once"), ("B4a", "asked")]
    cov = env.coverage()
    assert {k: (v["state"], v["count"], v["files_read"]) for k, v in cov.items()} == {
        "B1": ("ok", 1, 1), "B2": ("ok", 1, 1), "B3": ("ok", 0, 1), "B4a": ("ok", 1, 1)}
    out = capsys.readouterr().out
    assert "B1 refused_config 1" in out and "pytest" not in out and "vnx release" not in out


def test_nothing_private_reaches_the_store(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.transcript(FULL)
    assert env.run() == 0
    blob = env.ledger() + (env.state / "permission_coverage.json").read_bytes()
    for value in (UUID, EMAIL, PROMPT, OUTPUT, SLUG):
        assert value.encode() not in blob
    assert {json.loads(line)["account"] for line in env.ledger().splitlines()} == {"vnx"}


def test_same_fixture_twice_gives_the_same_ledger(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.transcript(FULL)
    assert env.run() == 0
    first = env.ledger()
    assert env.run() == 0 and env.ledger() == first and first.count(b"\n") == 3


def test_events_outside_the_window_are_left_out(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.transcript([use("t1", ts="2020-01-01T00:00:00Z"), result("t1", "config", "permission-rule", ts="2020-01-01T00:00:00Z")])
    assert env.run() == 0 and env.ledger() == b""
    assert env.coverage()["B1"]["count"] == 0


def test_no_binding_is_unbound_and_writes_no_line(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, bind=False)
    env.transcript(FULL)
    assert env.run() == 0 and env.ledger() == b""
    cov = env.coverage()
    assert {cov[b]["state"] for b in ("B1", "B2", "B4a")} == {"unbound"} and cov["B1"]["count"] is None
    assert cov["B1"]["unreadable_reason"]


def test_symlink_outside_the_bound_dir_is_an_error_without_lines(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(FULL[2]) + "\n")
    os.symlink(outside, env.dir / "link.jsonl")
    assert env.run() == 0 and env.ledger() == b""
    cov = env.coverage()
    assert {cov[b]["state"] for b in ("B1", "B2", "B4a")} == {"error"} and cov["B1"]["count"] is None


def test_unreadable_transcript_is_unknown_with_a_reason(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.transcript(FULL)
    (env.dir / "locked.jsonl").write_text("{}\n")
    (env.dir / "locked.jsonl").chmod(0)
    try:
        assert env.run() == 0
    finally:
        (env.dir / "locked.jsonl").chmod(0o600)
    cov = env.coverage()
    for b in ("B1", "B2", "B4a"):
        assert cov[b]["state"] == "unknown" and cov[b]["count"] is None
        assert cov[b]["files_unreadable"] == 1 and cov[b]["unreadable_reason"] == "unreadable_file"


def test_a_scan_for_project_a_under_the_store_of_b_is_refused(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.transcript(FULL)
    foreign = tmp_path / ".vnx-data" / "other-proj" / "state"
    assert env.run(state=foreign) == 2
    assert not foreign.exists() and "store" in capsys.readouterr().err


@pytest.mark.parametrize("part", ["clients", "business", "salesminds"])
def test_forbidden_slugs_cannot_be_bound_or_scanned(tmp_path, monkeypatch, part, capsys):
    env = Env(tmp_path, monkeypatch, bind=False)
    slug = f"-Users-x-{part.upper()}-site"
    (env.projects / slug).mkdir()
    argv = ["--state-dir", str(env.state), "--projects-root", str(env.projects), "bind",
            f"--transcript-dir={slug}", "--memory-dir", str(env.projects / "memory")]
    assert operator_steering.main(argv) == 2
    # a hand-written binding with the same slug is refused by the scan as well
    env.state.mkdir(parents=True)
    (env.state / source.BINDING_FILE).write_text(json.dumps({"transcript_dirs": [slug]}))
    assert env.run() == 2 and env.ledger() == b"" and "binding refused" in capsys.readouterr().err


@pytest.mark.parametrize("since,hours", [("7d", 168), ("12h", 12), ("1d", 24)])
def test_parse_since(since, hours):
    assert scan.parse_since(since).total_seconds() == hours * 3600
    with pytest.raises(ValueError):
        scan.parse_since("soon")


def test_unreadable_vnx_config_is_counted_in_the_b1_coverage_without_a_path(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.transcript(FULL)
    env.vnx.unlink()
    env.vnx.mkdir()  # a directory where the config should be: reading it fails
    assert env.run() == 0
    assert env.coverage()["B1"]["skipped"]["config_unreadable_vnx"] >= 1
    raw = (env.state / "permission_coverage.json").read_text()
    assert str(tmp_path) not in raw and UUID not in raw and EMAIL not in raw


def test_unparseable_vnx_config_is_counted_not_zero(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.transcript(FULL)
    env.vnx.write_text("{not json")
    assert env.run() == 0
    assert env.coverage()["B1"]["skipped"]["config_unparseable_vnx"] >= 1


def test_event_with_a_timestamp_without_timezone_does_not_abort_the_scan(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.transcript([use("t1", ts="2026-11-15T10:00:00"), result("t1", "config", "permission-rule", ts="2026-11-15T10:00:00"),
                    *FULL])
    assert env.run() == 0
    cov = env.coverage()
    assert cov["B1"]["state"] == "ok" and cov["B1"]["skipped"].get("bad_timestamp", 0) >= 1


def test_b3_enters_the_scan_idempotently_without_secrets(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    cmd = "TOKEN=abc123def456 bin/vnx deliverable promote dlv-0123456789ab sk-ABCDEFGHIJKLMNOPQRSTUVWX"
    env.transcript([{"type": "session-owner", "accountUuid": UUID, "sessionId": "sess-1"},
                    {"type": "user", "timestamp": NOW, "message": {"content": f"<bash-input>{cmd}</bash-input>"}},
                    {"type": "user", "timestamp": NOW, "message": {"content": "<bash-stdout>out</bash-stdout>"}}])
    assert env.run() == 0
    first = env.ledger()
    events = [json.loads(line) for line in first.splitlines()]
    assert [(e["bron"], e["besluit"], e["account"]) for e in events] == [("B3", "executed", "vnx")]
    assert env.coverage()["B3"]["state"] == "ok" and env.coverage()["B3"]["count"] == 1
    blob = first + (env.state / "permission_coverage.json").read_bytes()
    assert b"abc123def456" not in blob and b"sk-ABCDEF" not in blob
    assert env.run() == 0 and env.ledger() == first


def test_b3_unreadable_file_is_unknown_not_zero(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.transcript(FULL)
    (env.dir / "locked.jsonl").write_text("{}\n")
    (env.dir / "locked.jsonl").chmod(0)
    try:
        assert env.run() == 0
    finally:
        (env.dir / "locked.jsonl").chmod(0o600)
    cov = env.coverage()["B3"]
    assert cov["state"] == "unknown" and cov["count"] is None and cov["files_unreadable"] == 1
