"""Tests for scripts/probe_agent_skill_discovery.sh (agent-skill-discovery-binding, R7).
No test starts a real claude or codex (stub CLIs on PATH); a new script has no old-code red run (the plan
names that exception), so the red run is the missing PROBE file itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "scripts" / "probe_agent_skill_discovery.sh"
BASH = "/bin/bash"
DECISIVE = {"C1": True, "C2": False, "C3a": True, "C3b": True}
KEYS = {"cell", "decisive", "outcome", "reason", "command", "claude_version", "codex_version", "sha256"}
NEGATIVE = "vnx-probe-negative-control-not-in-tree"


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
if "--version" in argv:
    print("2.1.319 (Claude Code)"); raise SystemExit
sid = argv[argv.index("--session-id") + 1]
mode = os.environ.get("FAKE_CLAUDE", "ja")
if mode == "garbage": print("not json at all"); raise SystemExit
if mode != "noinit":
    source = {"key": "ANTHROPIC_API_KEY", "weird": "unknown-source", "nullsrc": None, "intsrc": 7, "emptysrc": ""}.get(mode, "none")
    print(json.dumps({"type": "system", "subtype": "init", "apiKeySource": source}))
print(json.dumps({"type": "result", "is_error": False, "result": "OK"}))
if mode == "exit":
    raise SystemExit(3)
room = Path.home() / ".claude" / "projects" / "-probe"; room.mkdir(parents=True, exist_ok=True)
found = sorted(p.name for p in Path(".claude/skills").iterdir()) if Path(".claude/skills").is_dir() else []
names = "not-a-list" if mode == "badnames" else found if mode not in ("nee", "noskills") else ["unrelated-skill"]
attachment = ({"attachment": {"type": "instructions", "files": []}} if mode == "noskills" else
              {"attachment": {"type": "skill_listing", "isInitial": True, "names": names,
                              "skillCount": len(names), "content": str(names)}})
if mode != "notranscript":
    (room / (sid + ".jsonl")).write_text(json.dumps(attachment) + "\n")
'''

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.164.0"); raise SystemExit
mode = os.environ.get("FAKE_CODEX", "ja")
if mode in ("exit", "empty"): raise SystemExit(4 if mode == "exit" else 0)
found = sorted(p.name for p in Path(".agents/skills").iterdir()) if Path(".agents/skills").is_dir() else []
names = {"neg": found + ["vnx-probe-negative-control-not-in-tree"], "noslug": ["unrelated-skill"]}.get(mode, found)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Skills: " + ", ".join(names)}}))
'''


def _run(tmp_path, *args, operator=True, fakes=True, extra=None):
    env = dict(os.environ, HOME=str(tmp_path), TMPDIR=str(tmp_path))
    env.pop("VNX_CLAUDE_PROJECTS_DIR", None); env.pop("VNX_PROBE_OPERATOR", None)
    if operator:
        env["VNX_PROBE_OPERATOR"] = "1"
    if fakes:
        bins = tmp_path / "bin"
        bins.mkdir(exist_ok=True)
        for name, text in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            (bins / name).write_text(text, encoding="utf-8"); (bins / name).chmod(0o755)
        (bins / "python3").unlink(missing_ok=True); (bins / "python3").symlink_to(sys.executable)  # the judges import PyYAML here
        env["PATH"] = os.pathsep.join([str(bins), str(Path(sys.executable).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    env.update(extra or {})
    return subprocess.run([BASH, str(PROBE), *args], capture_output=True, text=True, env=env, check=False)


def _cell(tmp_path, cell, **extra):
    proc = _run(tmp_path, "--run", "--cell", cell, extra=extra)
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()][0]


def test_contract_exists_and_self_check_reports_every_cell(tmp_path):
    assert PROBE.is_file()
    assert subprocess.run([BASH, "-n", str(PROBE)], check=False).returncode == 0
    proc = _run(tmp_path, "--self-check", fakes=False)
    assert proc.returncode == 0, proc.stderr
    rows = {row["cell"]: row for row in [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]}
    assert set(rows) == set(DECISIVE)
    for cell, decisive in DECISIVE.items():
        row = rows[cell]
        assert set(row) == KEYS and row["decisive"] is decisive
        assert row["outcome"] == "niet_gemeten" and row["reason"]
        assert row["sha256"] == "-" and row["claude_version"] == ""


def test_run_without_the_operator_marker_exits_three(tmp_path):
    proc = _run(tmp_path, "--run", operator=False)
    assert (proc.returncode, proc.stdout) == (3, "")
    assert len(proc.stderr.strip().splitlines()) == 1 and "VNX_PROBE_OPERATOR" in proc.stderr


def test_c1_ja_on_a_listing_with_the_probe_slug_and_sha_of_the_raw_output(tmp_path):
    row = _cell(tmp_path, "C1")
    assert (row["outcome"], row["decisive"]) == ("ja", True), row
    assert "env -u ANTHROPIC_API_KEY" in row["command"] and "--bare" not in row["command"]
    raw = "\n".join(json.dumps(e) for e in ({"type": "system", "subtype": "init", "apiKeySource": "none"}, {"type": "result", "is_error": False, "result": "OK"})) + "\n"
    assert row["sha256"] == hashlib.sha256(raw.encode()).hexdigest()


def test_c1_nee_when_the_listing_does_not_name_the_slug(tmp_path):
    row = _cell(tmp_path, "C1", FAKE_CLAUDE="nee")
    assert row["outcome"] == "nee" and "does not name" in row["reason"], row


def test_c1_stays_niet_gemeten_without_recorded_evidence(tmp_path):
    cases = {"noinit": "auth_source_unproven", "garbage": "auth_source_unproven", "key": "auth_source_api_key",
             "nullsrc": "auth_source_unproven", "intsrc": "auth_source_unproven", "emptysrc": "auth_source_unproven",
             "weird": "auth_source_api_key", "notranscript": "no_transcript", "noskills": "skill_listing_missing",
             "badnames": "skill_listing_malformed", "exit": "cli_exit_3"}
    for mode, reason in cases.items():
        row = _cell(tmp_path, "C1", FAKE_CLAUDE=mode)
        assert row["outcome"] == "niet_gemeten" and reason in row["reason"], (mode, row)


def test_c2_uses_bare_and_is_not_decisive(tmp_path):
    row = _cell(tmp_path, "C2")
    assert (row["outcome"], row["decisive"]) == ("ja", False), row
    assert "--bare" in row["command"]


def test_c3a_ja_when_the_destination_is_exactly_the_declared_set(tmp_path):
    row = _cell(tmp_path, "C3a")
    assert row["outcome"] == "ja" and "ls -1 .agents/skills" in row["command"], row
    assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"])


def test_c3a_judgement_needs_exact_set_equality(tmp_path):
    raw = tmp_path / "listing"

    def judge(text):
        raw.write_text(text, encoding="utf-8")
        env = dict(os.environ, HOME=str(tmp_path), TMPDIR=str(tmp_path))
        return subprocess.run([BASH, "-c", 'source "%s"\npy judge_listing "%s" probe-skill' % (PROBE, raw)],
                              capture_output=True, text=True, env=env, check=False).stdout.strip()

    assert judge("probe-skill\n").startswith("ja\t")
    extra = judge("probe-skill\nextra\n")
    assert extra.startswith("nee\t") and "extra" in extra
    missing = judge("")
    assert missing.startswith("nee\t") and "missing" in missing


def test_c3b_ja_only_when_the_cli_names_the_slug_and_not_the_negative_control(tmp_path):
    row = _cell(tmp_path, "C3b")
    assert row["outcome"] == "ja" and "codex exec" in row["command"], row
    for mode in ("neg", "noslug"):
        row = _cell(tmp_path, "C3b", FAKE_CODEX=mode)
        assert row["outcome"] == "nee", (mode, row)
    assert NEGATIVE in _cell(tmp_path, "C3b", FAKE_CODEX="neg")["reason"]


def test_c3b_stays_niet_gemeten_when_the_call_gave_no_evidence(tmp_path):
    for mode, reason in (("exit", "cli_exit_4"), ("empty", "empty_output")):
        row = _cell(tmp_path, "C3b", FAKE_CODEX=mode)
        assert row["outcome"] == "niet_gemeten" and reason in row["reason"], (mode, row)

