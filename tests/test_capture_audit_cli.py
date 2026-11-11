"""Capture PR-4: the ``report`` and ``dismiss`` subcommands of scripts/capture_audit.py.

Every store lives under tmp_path with HOME pointing there (never ~/.claude or ~/.vnx-data).
The CLI runs as a subprocess, the ledger reader is the real ``capture_audit`` lib.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import capture_audit as ca

SCRIPT = _REPO_ROOT / "scripts" / "capture_audit.py"
SINCE, UNTIL = "2026-11-09T12:00:00Z", "2026-11-10T12:00:00Z"
PAIR = "0123456789abcdef"


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    state = home / ".vnx-data" / "proj-a" / "state"
    memory = home / ".claude" / "projects" / "-proj-a" / "memory"
    bucket = home / "bucket"
    for d in (state, memory, bucket):
        d.mkdir(parents=True)
    (state / "capture_sources.json").write_text(json.dumps({
        "schema": "capture_sources/1", "project_id": "proj-a", "memory_dir": str(memory),
        "bucket_dir": str(bucket), "bucket_slug": "mine", "bound_at": "2026-11-10T08:00:00+00:00"}))
    (state / "open_items.json").write_text(json.dumps({"items": [{
        "id": "OI-7", "status": "open", "title": "merge lock blijft achter na afgebroken lus",
        "created_at": "2026-10-01T10:00:00"}]}))
    (memory / "merge-lock-note.md").write_text(
        "---\nname: merge-lock-note\ndescription: afgebroken merge lus laat lock achter\n"
        "modified: 2026-11-10T08:00:00+00:00\n---\ntekst\n")
    (bucket / "2026-10.md").write_text("### 2026-10-01 · mine · koffie-zetten-ochtend\n- x\n\n")
    return {"home": home, "state": state}


def _run(*args, home):
    drop = ("VNX_PROJECT_ID", "VNX_DISPATCH_PATHS", "VNX_ENFORCE_WORKER_PERMISSIONS")
    proc_env = {**{k: v for k, v in os.environ.items() if k not in drop}, "HOME": str(home)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=proc_env, cwd=str(home))


def _audit(env):
    proc = _run("audit", "--state-dir", str(env["state"]), "--since", SINCE, "--until", UNTIL, home=env["home"])
    assert proc.returncode == 0, proc.stderr
    return json.loads((env["state"] / ca.AUDIT_FILENAME).read_text())


def _dismiss(env, pair=PAIR, reason="bewust twee verhalen", *extra):
    return _run("dismiss", "--state-dir", str(env["state"]), "--pair", pair, "--reason", reason, *extra,
                home=env["home"])


def _ledger(env) -> Path:
    return env["state"] / ca.LEDGER_FILENAME


def test_dismiss_appends_a_line_the_reader_accepts(env):
    proc = _dismiss(env, PAIR, "bewust twee verhalen", "--by", "vincent")
    assert proc.returncode == 0, proc.stderr
    [line] = _ledger(env).read_text().splitlines()
    row = json.loads(line)
    assert (row["pair_id"], row["reason"], row["by"]) == (PAIR, "bewust twee verhalen", "vincent")
    assert ca.valid_dismissal(row)
    assert ca.read_dismissed(env["state"]) == frozenset({PAIR})


def test_dismiss_closes_an_unterminated_last_line_before_appending(env):
    _ledger(env).write_text('{"kapotte regel')
    assert _dismiss(env, PAIR, "r", "--by", "v").returncode == 0
    lines = _ledger(env).read_text().splitlines()
    assert lines[0] == '{"kapotte regel' and len(lines) == 2
    assert ca.read_dismissed(env["state"]) == frozenset({PAIR})


@pytest.mark.parametrize("pair,reason,by", [
    (PAIR, "", "v"), (PAIR, "   ", "v"), (PAIR, "r", " "), ("", "r", "v"), ("abc", "r", "v"),
    (PAIR.upper(), "r", "v"), (PAIR + "0", "r", "v"), ("zzzzzzzzzzzzzzzz", "r", "v")])
def test_dismiss_refuses_what_the_reader_rejects_and_writes_nothing(env, pair, reason, by):
    proc = _dismiss(env, pair, reason, "--by", by)
    assert proc.returncode == 2
    assert "nothing written" in proc.stderr
    assert not _ledger(env).exists()


def test_dismiss_refuses_a_missing_state_dir(env):
    proc = _run("dismiss", "--state-dir", str(env["home"] / "nope"), "--pair", PAIR, "--reason", "r", "--by", "v",
                home=env["home"])
    assert proc.returncode == 2 and not (env["home"] / "nope").exists()


def test_dismiss_reports_an_unwritable_ledger_as_failed(env):
    _ledger(env).mkdir()
    proc = _dismiss(env, PAIR, "r", "--by", "v")
    assert proc.returncode == 1 and "dismiss failed" in proc.stderr


def test_dismissed_pair_is_dismissed_in_the_next_audit(env):
    [pair] = _audit(env)["pairs"]
    assert pair["disposition"] == "open"
    assert _dismiss(env, pair["pair_id"], "geen duplicaat", "--by", "v").returncode == 0
    audit = _audit(env)
    assert [p["disposition"] for p in audit["pairs"]] == ["dismissed"]
    assert audit["open_pairs"] == 0


def test_report_lists_the_open_pair_in_text_and_json(env):
    audit = _audit(env)
    proc = _run("report", "--state-dir", str(env["state"]), home=env["home"])
    assert proc.returncode == 0, proc.stderr
    assert "open_pairs=1" in proc.stdout
    assert f"open {audit['pairs'][0]['pair_id']} oi_mem anchor=memory in_window=True" in proc.stdout
    assert "merge lock" not in proc.stdout and "OI-7" not in proc.stdout
    as_json = json.loads(_run("report", "--state-dir", str(env["state"]), "--json", home=env["home"]).stdout)
    assert as_json["pairs"] == audit["pairs"] and as_json["dismissal_ledger"]["state"] == "absent"


def test_report_show_resolves_names_to_the_terminal_only(env):
    _audit(env)
    before = (env["state"] / ca.AUDIT_FILENAME).read_text()
    proc = _run("report", "--state-dir", str(env["state"]), "--show", home=env["home"])
    assert proc.returncode == 0, proc.stderr
    assert "OI-7 merge lock blijft achter na afgebroken lus" in proc.stdout and "[[merge-lock-note]]" in proc.stdout
    assert (env["state"] / ca.AUDIT_FILENAME).read_text() == before


def test_report_show_with_json_is_refused(env):
    _audit(env)
    assert _run("report", "--state-dir", str(env["state"]), "--show", "--json", home=env["home"]).returncode == 2


def test_report_names_an_unreadable_ledger(env):
    _audit(env)
    _ledger(env).mkdir()
    proc = _run("report", "--state-dir", str(env["state"]), home=env["home"])
    assert proc.returncode == 0
    assert f"dismissal ledger {_ledger(env)}: unreadable" in proc.stdout and "open_pairs=1" in proc.stdout
    as_json = json.loads(_run("report", "--state-dir", str(env["state"]), "--json", home=env["home"]).stdout)
    assert as_json["dismissal_ledger"] == {"path": str(_ledger(env)), "state": "unreadable"}


def test_report_is_quiet_about_a_readable_ledger(env):
    _audit(env)
    assert _dismiss(env, PAIR, "r", "--by", "v").returncode == 0
    assert "unreadable" not in _run("report", "--state-dir", str(env["state"]), home=env["home"]).stdout


@pytest.mark.parametrize("content", [None, "{niet json", "[]", json.dumps({"schema": "x/9", "pairs": []}),
                                     json.dumps({"schema": ca.AUDIT_SCHEMA, "pairs": None})])
def test_report_without_a_usable_audit_file_exits_2(env, content):
    if content is not None:
        (env["state"] / ca.AUDIT_FILENAME).write_text(content)
    proc = _run("report", "--state-dir", str(env["state"]), home=env["home"])
    assert proc.returncode == 2 and "capture_audit report:" in proc.stderr and proc.stdout == ""


def test_report_prints_null_for_no_measurement(env):
    (env["state"] / "capture_sources.json").unlink()
    _audit(env)
    out = _run("report", "--state-dir", str(env["state"]), home=env["home"]).stdout
    assert "open_pairs=None" in out and "open_pairs=0" not in out
