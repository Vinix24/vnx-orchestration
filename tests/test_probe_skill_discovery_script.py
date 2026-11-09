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
found = sorted(p.name for p in Path(".claude/skills").iterdir()) if Path(".claude/skills").is_dir() else []
if mode != "noinit":
    source = {"key": "ANTHROPIC_API_KEY", "weird": "unknown-source", "nullsrc": None, "intsrc": 7, "emptysrc": ""}.get(mode, "none")
    init = {"type": "system", "subtype": "init", "apiKeySource": source}
    if mode == "initmention": init.update(skills=found, slash_commands=found)
    print(json.dumps(init))
print(json.dumps({"type": "result", "is_error": False, "result": " ".join(["OK"] + found) if mode == "initmention" else "OK"}))
if mode == "badstream": print("oops, a stray line that is not json")
if mode == "exit":
    raise SystemExit(3)
room = Path.home() / ".claude" / "projects" / "-probe"; room.mkdir(parents=True, exist_ok=True)
names = ("not-a-list" if mode == "badnames" else
         ["unrelated-skill"] if mode in ("nee", "noskills", "initmention", "transcriptmention") else found)
if mode == "transcriptmention": names = names + ["The skill %s is not available to me." % " ".join(found)]
attachment = ({"attachment": {"type": "instructions", "files": []}} if mode == "noskills" else
              {"attachment": {"type": "skill_listing", "isInitial": True, "names": names,
                              "skillCount": len(names), "content": str(names)}})
if mode != "notranscript":
    extra = "this transcript line is not json\n" if mode == "badtranscript" else ""
    later = "".join(json.dumps(e) + "\n" for e in ({"type": "user", "message": "Use the skill " + " ".join(found)},
                                                     {"attachment": {"type": "skill_listing", "isInitial": False, "names": found}})
                    ) if mode == "transcriptmention" else ""
    (room / (sid + ".jsonl")).write_text(json.dumps({"type": "user", "message": "hi"}) + "\n" + extra + json.dumps(attachment) + "\n" + later)
'''

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.164.0"); raise SystemExit
mode = os.environ.get("FAKE_CODEX", "ja"); answer = os.environ.get("FAKE_CODEX_ANSWER")
if mode in ("exit", "empty"): raise SystemExit(4 if mode == "exit" else 0)
found = sorted(p.name for p in Path(".agents/skills").iterdir()) if Path(".agents/skills").is_dir() else []
names = {"neg": found + ["vnx-probe-negative-control-not-in-tree"], "noslug": ["unrelated-skill"]}.get(mode, found)
def emit(event): print(json.dumps(event))
if mode == "malformed":
    emit({"type": "thread.started"}); print("oops not json " + ", ".join(found)); emit({"type": "turn.completed"}); raise SystemExit
emit({"type": "thread.started"}); emit({"type": "turn.started"})
if mode == "cmdonly":
    emit({"type": "item.completed", "item": {"type": "command_execution", "command": "ls .agents/skills", "aggregated_output": "\n".join(found)}})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "I could not tell."}})
elif mode != "noanswer":
    emit({"type": "item.completed", "item": {"type": "reasoning", "text": "\n".join(found)}})
    if mode == "blocks":
        emit({"type": "item.completed", "item": {"type": "agent_message", "content": [{"text": "- `" + n + "`"} for n in names]}})
    else:
        text = "\n".join("- " + n for n in names) if answer is None else answer.replace("{slug}", found[0]).replace("{neg}", "vnx-probe-negative-control-not-in-tree")
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
if mode != "noturn": emit({"type": "turn.completed"})
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
    assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) and row["sha256"] != hashlib.sha256(raw.encode()).hexdigest()


def test_c1_nee_when_the_listing_does_not_name_the_slug(tmp_path):
    row = _cell(tmp_path, "C1", FAKE_CLAUDE="nee")
    assert row["outcome"] == "nee" and "does not name" in row["reason"], row


def test_c1_stays_niet_gemeten_without_recorded_evidence(tmp_path):
    cases = {"noinit": "auth_source_unproven", "garbage": "stream_malformed: line 1", "key": "auth_source_api_key",
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


def test_c3b_ja_on_a_content_block_answer_and_the_last_message_wins(tmp_path):
    assert _cell(tmp_path, "C3b", FAKE_CODEX="blocks")["outcome"] == "ja"


def test_c3b_malformed_stream_is_niet_gemeten_even_when_the_slug_is_in_it(tmp_path):
    row = _cell(tmp_path, "C3b", FAKE_CODEX="malformed")
    assert row["outcome"] == "niet_gemeten" and "stream_malformed: line 2" in row["reason"], row


def test_c3b_slug_only_in_a_command_execution_item_is_not_a_ja(tmp_path):
    row = _cell(tmp_path, "C3b", FAKE_CODEX="cmdonly")  # the answer "I could not tell." is prose, not a list
    assert row["outcome"] == "niet_gemeten" and "answer_not_a_list: line 1" in row["reason"], row


def test_c3b_counts_an_answer_only_as_a_list_of_bare_names(tmp_path):
    cases = {"The skill {slug} is not available to me.": ("niet_gemeten", "answer_not_a_list: line 1 is not a bare skill name"),
             "- {slug}\nThese are all the skills I can see.": ("niet_gemeten", "answer_not_a_list: line 2 is not a bare skill name"),
             "- {slug}-old\n- other-skill": ("nee", "does not list"),
             "- `{slug}`\n\n- other-skill": ("ja", "lists vnx-probe-skill-disc-"),
             "{slug}\n{neg}": ("nee", NEGATIVE)}
    for answer, (outcome, reason) in cases.items():
        row = _cell(tmp_path, "C3b", FAKE_CODEX_ANSWER=answer)
        assert row["outcome"] == outcome and reason in row["reason"], (answer, row)


def test_c3b_a_mention_of_the_slug_outside_a_list_is_never_a_ja(tmp_path):
    for answer in ("{slug} is available", "Skills: {slug}", "I can see `{slug}` in .agents/skills.", "- {slug} (from .agents/skills)"):
        row = _cell(tmp_path, "C3b", FAKE_CODEX_ANSWER=answer)
        assert row["outcome"] == "niet_gemeten" and "answer_not_a_list: line 1" in row["reason"], (answer, row)


def _judge(tmp_path, *args):
    env = dict(os.environ, HOME=str(tmp_path), TMPDIR=str(tmp_path))
    script = 'source "%s"\npy %s' % (PROBE, " ".join('"%s"' % arg for arg in args))
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, check=False).stdout.strip()


def test_c3a_a_mention_of_the_slug_in_the_listing_output_is_not_a_ja(tmp_path):
    raw = tmp_path / "listing"
    for text in ("The skill probe-skill is not available to me.\n", "probe-skill is here\n", "probe-skill-old\n"):
        raw.write_text(text, encoding="utf-8")
        assert _judge(tmp_path, "judge_listing", raw, "probe-skill").startswith("nee\t"), text


def test_c1_the_init_event_naming_the_slug_is_not_a_listing(tmp_path):
    row = _cell(tmp_path, "C1", FAKE_CLAUDE="initmention")
    assert row["outcome"] == "nee" and "does not name" in row["reason"], row
    slug = re.search(r"does not name (\S+);", row["reason"]).group(1)
    transcript = next((tmp_path / ".claude" / "projects" / "-probe").glob("*.jsonl"))
    stream = "\n".join(json.dumps(e) for e in (
        {"type": "system", "subtype": "init", "apiKeySource": "none", "skills": [slug], "slash_commands": [slug]},
        {"type": "result", "is_error": False, "result": "OK " + slug})) + "\n"
    judged = stream.encode() + ("--- transcript %s ---\n" % transcript).encode() + transcript.read_bytes()
    assert row["sha256"] == hashlib.sha256(judged).hexdigest()  # the judged stream named the slug in the init event


def test_c1_a_transcript_mention_outside_the_initial_listing_names_is_not_a_ja(tmp_path):
    row = _cell(tmp_path, "C1", FAKE_CLAUDE="transcriptmention")
    assert row["outcome"] == "nee" and "does not name" in row["reason"], row
    slug = re.search(r"does not name (\S+);", row["reason"]).group(1)
    transcript = next((tmp_path / ".claude" / "projects" / "-probe").glob("*.jsonl")).read_text(encoding="utf-8")
    assert transcript.count(slug) == 4  # a user message, a prose name and the content of the initial listing, a later listing


def test_c3b_needs_an_agent_answer_and_a_completed_turn(tmp_path):
    row = _cell(tmp_path, "C3b", FAKE_CODEX="noanswer")
    assert row["outcome"] == "niet_gemeten" and "no_agent_answer" in row["reason"], row
    row = _cell(tmp_path, "C3b", FAKE_CODEX="noturn")
    assert row["outcome"] == "niet_gemeten" and "turn_incomplete" in row["reason"], row


def test_c3b_judge_stays_niet_gemeten_on_odd_evidence(tmp_path):
    raw = tmp_path / "stream"

    def judge(text):
        raw.write_text(text, encoding="utf-8")
        env = dict(os.environ, HOME=str(tmp_path), TMPDIR=str(tmp_path))
        return subprocess.run([BASH, "-c", 'source "%s"\npy judge_codex "%s" probe-skill %s' % (PROBE, raw, NEGATIVE)],
                              capture_output=True, text=True, env=env, check=False).stdout.strip()

    done = json.dumps({"type": "turn.completed"})
    msg = lambda item: json.dumps({"type": "item.completed", "item": item})
    assert judge('[1]\n' + done).startswith("niet_gemeten\tstream_malformed")
    assert judge('"probe-skill"\n' + done).startswith("niet_gemeten\tstream_malformed")
    for item in ({"type": "agent_message"}, {"type": "agent_message", "text": None}, {"type": "agent_message", "text": 7},
                 {"type": "agent_message", "text": "  "}, {"type": "agent_message", "content": [{"x": 1}]}, "agent_message"):
        assert judge(msg(item) + "\n" + done).startswith("niet_gemeten\tno_agent_answer"), item
    assert judge(json.dumps({"type": "turn.completed", "item": 1}).replace("turn.completed", "turn.failed") + "\n"
                 + msg({"type": "agent_message", "text": "probe-skill"})).startswith("niet_gemeten\tturn_incomplete")


def test_c1_sha256_covers_the_cli_stream_and_the_transcript_that_was_judged(tmp_path):
    rows = {}
    for mode in ("ja", "nee"):
        home = tmp_path / mode
        home.mkdir()
        rows[mode] = _cell(home, "C1", FAKE_CLAUDE=mode)
        transcript = next((home / ".claude" / "projects" / "-probe").glob("*.jsonl"))
        stdout = "\n".join(json.dumps(e) for e in ({"type": "system", "subtype": "init", "apiKeySource": "none"},
                                                    {"type": "result", "is_error": False, "result": "OK"})) + "\n"
        evidence = stdout.encode() + ("--- transcript %s ---\n" % transcript).encode() + transcript.read_bytes()
        assert rows[mode]["sha256"] == hashlib.sha256(evidence).hexdigest(), mode
    assert (rows["ja"]["outcome"], rows["nee"]["outcome"]) == ("ja", "nee")
    assert rows["ja"]["sha256"] != rows["nee"]["sha256"]


def test_c1_without_a_transcript_hashes_the_stream_plus_the_none_separator(tmp_path):
    row = _cell(tmp_path, "C1", FAKE_CLAUDE="notranscript")
    stdout = "\n".join(json.dumps(e) for e in ({"type": "system", "subtype": "init", "apiKeySource": "none"},
                                                {"type": "result", "is_error": False, "result": "OK"})) + "\n"
    assert row["outcome"] == "niet_gemeten" and "no_transcript" in row["reason"]
    assert row["sha256"] == hashlib.sha256((stdout + "--- transcript <none> ---\n").encode()).hexdigest()


def test_c1_malformed_stream_line_is_niet_gemeten_even_with_a_listing_that_names_the_slug(tmp_path):
    row = _cell(tmp_path, "C1", FAKE_CLAUDE="badstream")
    assert row["outcome"] == "niet_gemeten" and "stream_malformed: line 3 is not a JSON object" in row["reason"], row


def test_c1_malformed_transcript_line_is_niet_gemeten_but_other_entry_types_are_skipped(tmp_path):
    row = _cell(tmp_path, "C1", FAKE_CLAUDE="badtranscript")
    assert row["outcome"] == "niet_gemeten" and "transcript_malformed: line 2" in row["reason"], row
    (tmp_path / "ok").mkdir()
    assert _cell(tmp_path / "ok", "C1")["outcome"] == "ja"  # the user entry in the transcript is valid and skipped


def test_c1_judge_rejects_a_non_object_json_line_in_stream_and_transcript(tmp_path):
    def judge(stream, transcript):
        raw = tmp_path / "stream"; raw.write_text(stream, encoding="utf-8")
        home = tmp_path / "home"; room = home / ".claude" / "projects" / "-p"; room.mkdir(parents=True, exist_ok=True)
        (room / "sid.jsonl").write_text(transcript, encoding="utf-8")
        env = dict(os.environ, HOME=str(home), TMPDIR=str(tmp_path))
        env.pop("VNX_CLAUDE_PROJECTS_DIR", None)
        return subprocess.run([BASH, "-c", 'source "%s"\npy judge_claude "%s" sid "%s" probe-skill "%s"' % (PROBE, raw, tmp_path, tmp_path / "ev")],
                              capture_output=True, text=True, env=env, check=False).stdout.strip()

    init = json.dumps({"type": "system", "subtype": "init", "apiKeySource": "none"})
    for bad in ("[1]", '"probe-skill"', "7", "null"):
        assert judge(init + "\n" + bad + "\n", "").startswith("niet_gemeten\tstream_malformed: line 2"), bad
        assert judge(bad + "\n" + init + "\n", "").startswith("niet_gemeten\tstream_malformed: line 1"), bad
        assert judge(init + "\n", bad + "\n").startswith("niet_gemeten\ttranscript_malformed: line 1"), bad
