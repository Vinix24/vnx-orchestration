"""context_manifest.capture and the transcript locator (agents-als-map PR-4a1).

The fixture is a synthetic 2.1.287 transcript: ``{HOME}`` and ``{REPO}`` are
replaced per test with temp dirs, so no path of this machine is baked in.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

FIXTURE = Path(__file__).parent / "fixtures" / "context_manifest" / "transcript_2_1_287.jsonl"
SESSION = "sess-fixture-1"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    projects = tmp_path / "projects"
    for d in (home, repo, projects / "-enc"):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VNX_CLAUDE_PROJECTS_DIR", raising=False)
    text = FIXTURE.read_text(encoding="utf-8").replace("{HOME}", str(home)).replace("{REPO}", str(repo))
    transcript = projects / "-enc" / f"{SESSION}.jsonl"
    transcript.write_text(text, encoding="utf-8")
    return {"home": home, "repo": repo, "projects": projects, "transcript": transcript}


def _capture(env, **kw):
    import context_manifest
    return context_manifest.capture(
        SESSION, claude_projects_dir=env["projects"], repo_root=env["repo"], **kw
    )


# --- locator -------------------------------------------------------------

def test_harvest_reads_the_transcript_through_the_locator(env, monkeypatch):
    import token_harvest

    calls = []

    def spy(session_id, *, claude_projects_dir=None):
        calls.append((session_id, claude_projects_dir))
        return env["transcript"]

    monkeypatch.setattr(token_harvest, "locate_session_transcript", spy, raising=False)
    # A projects dir that holds nothing: only the locator can find the file now.
    empty = env["projects"].parent / "empty"
    empty.mkdir()
    result = token_harvest.harvest_session_tokens(SESSION, claude_projects_dir=empty)
    assert calls == [(SESSION, empty)]
    assert result == {"input": 100, "output": 20, "cache_creation_5m": 10,
                      "cache_creation_1h": 5, "cache_read": 2}


def test_locator_finds_the_same_file_harvest_reads(env):
    import token_harvest

    found = token_harvest.locate_session_transcript(SESSION, claude_projects_dir=env["projects"])
    assert found == env["transcript"]
    assert token_harvest.harvest_session_tokens(SESSION, claude_projects_dir=env["projects"])["input"] == 100


@pytest.mark.parametrize("session_id", [None, "", "   ", "nope"])
def test_locator_returns_none_without_a_match(env, session_id):
    import token_harvest

    assert token_harvest.locate_session_transcript(session_id, claude_projects_dir=env["projects"]) is None


# --- capture: happy path ---------------------------------------------------

def test_capture_instructions_are_paths_types_lengths_hashes(env):
    result = _capture(env)
    assert result["status"] == "ok"
    inst = result["instructions"]
    files = {f["path"]: f for f in inst["files"]}
    assert set(files) == {
        "~/.claude-alt/CLAUDE.md", "~/.claude/rules/style.md",
        str(env["repo"] / "CLAUDE.md"), "~/.claude-alt/projects/enc/memory/MEMORY.md",
        str(env["repo"] / "sub" / "CLAUDE.md"),
    }
    user = files["~/.claude-alt/CLAUDE.md"]
    assert user == {"path": "~/.claude-alt/CLAUDE.md", "type": "User",
                    "chars": len("SENTINEL-CONTENT user instructions"),
                    "sha256": _digest("SENTINEL-CONTENT user instructions"),
                    "in_config_home": True, "seen": "initial"}
    assert files[str(env["repo"] / "CLAUDE.md")]["in_config_home"] is False
    assert files[str(env["repo"] / "CLAUDE.md")]["seen"] == "initial"
    assert files[str(env["repo"] / "sub" / "CLAUDE.md")]["seen"] == "later"
    assert inst["by_type"] == {"User": 1, "Project": 3, "AutoMem": 1}
    assert inst["attachments"] == 2
    assert inst["total_chars"] == sum(f["chars"] for f in inst["files"])
    assert result["cli_version"] == "2.1.287"


def test_capture_auto_memory(env):
    mem = _capture(env)["auto_memory"]
    assert mem == {"loaded": True, "path": "~/.claude-alt/projects/enc/memory/MEMORY.md",
                   "chars": len("SENTINEL-CONTENT memory index")}


def test_capture_skills_split_by_origin(env):
    (env["repo"] / ".claude" / "skills" / "repo-skill").mkdir(parents=True)
    (env["repo"] / ".claude" / "skills" / "repo-skill" / "SKILL.md").write_text("x")
    (env["home"] / ".claude-alt" / "skills" / "user-skill").mkdir(parents=True)
    (env["home"] / ".claude-alt" / "skills" / "user-skill" / "SKILL.md").write_text("x")
    skills = _capture(env)["skills"]
    assert skills["initial_count"] == 6
    assert skills["initial_chars"] > 0
    assert skills["initial"] == {
        "synced": ["anthropic-skills:docx"], "namespaced": ["plugin:thing"],
        "repo": ["repo-skill"], "user": ["user-skill"], "other": ["builtin", "dup"],
    }
    assert skills["later"] == ["later-skill"]


def test_capture_output_has_no_content_and_no_address(env):
    blob = json.dumps(_capture(env))
    assert "SENTINEL-CONTENT" not in blob
    assert "@" not in blob
    assert "example.invalid" not in blob


# --- capture: hooks, account, config dir, provider (PR-4a2) -------------------

def _walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk_strings(value)


def test_capture_hooks_event_name_path_count(env):
    hooks = {(h["event"], h["name"], h["command_path"]): h for h in _capture(env)["hooks"]["hooks"]}
    guard = hooks[("PreToolUse", "PreToolUse:Bash", str(env["repo"] / "scripts" / "hooks" / "guard.sh"))]
    assert guard["count"] == 2 and guard["in_config_home"] is False
    build = hooks[("SessionStart", "SessionStart:startup", "~/.claude-alt/hooks/build_state.sh")]
    assert build["count"] == 1 and build["in_config_home"] is True
    inline = hooks[("SessionStart", "SessionStart:startup", None)]
    assert inline["in_config_home"] is False and len(inline["command_sha256"]) == 16
    assert _capture(env)["hooks"]["total"] == 5 and _capture(env)["hooks"]["skipped"] == 0


def test_hook_with_an_address_in_its_path_keeps_no_path(env):
    hooks = _capture(env)["hooks"]["hooks"]
    notify = next(h for h in hooks if h["event"] == "PostToolUse")
    assert notify["command_path"] is None and notify["in_config_home"] is False


def test_capture_hooks_keep_no_arguments(env):
    assert "SENTINEL-ARG" not in json.dumps(_capture(env))


def test_capture_account_is_auth_kind_and_org_hash_only(env):
    org = "00000000-0000-4000-8000-000000000000"
    assert _capture(env)["account"] == {
        "auth": "oauth", "org_sha256": hashlib.sha256(org.encode("utf-8")).hexdigest()}
    assert org not in json.dumps(_capture(env))


def test_no_value_anywhere_contains_an_at_sign(env):
    result = _capture(env)
    assert result["status"] == "ok"
    offenders = [s for s in _walk_strings(result) if "@" in s]
    assert offenders == []
    assert "example.invalid" not in json.dumps(result)


def test_an_address_in_the_account_source_is_absent(env):
    line = {"type": "attachment", "version": "2.1.287", "attachment": {
        "type": "credential_org", "organizationUuid": "org-1", "email": "who@example.invalid",
        "userEmail": "who@example.invalid"}}
    env["transcript"].write_text(json.dumps(line) + "\n", encoding="utf-8")
    result = _capture(env)
    assert result["account"]["auth"] == "oauth"
    assert "example.invalid" not in json.dumps(result) and not [s for s in _walk_strings(result) if "@" in s]


def test_missing_credential_record_is_unknown_and_partial(env):
    lines = [l for l in env["transcript"].read_text(encoding="utf-8").splitlines() if "credential_org" not in l]
    env["transcript"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _capture(env)
    assert result["account"] == {"auth": "unknown", "org_sha256": None}
    assert result["status"] == "partial"


@pytest.mark.parametrize("bad", [
    {"hookEvent": "PreToolUse", "hookName": "PreToolUse:Bash"},
    {"hookEvent": "PreToolUse", "command": "bash /x/y.sh"},
    {"hookName": "PreToolUse:Bash", "command": "bash /x/y.sh"},
    {"hookEvent": None, "hookName": "n", "command": "bash /x/y.sh"},
    {"hookEvent": "e", "hookName": "n", "command": ""},
    {"hookEvent": "e", "hookName": "n", "command": 7},
])
def test_malformed_hook_record_is_skipped_and_marks_partial(env, bad):
    lines = [l for l in env["transcript"].read_text(encoding="utf-8").splitlines() if "hook_success" not in l]
    lines.append(json.dumps({"type": "attachment", "version": "2.1.287", "attachment": {"type": "hook_success", **bad}}))
    env["transcript"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _capture(env)
    assert result["hooks"] == {"hooks": [], "total": 0, "skipped": 1}
    assert result["status"] == "partial"


def test_no_hooks_at_all_is_a_complete_measurement(env):
    lines = [l for l in env["transcript"].read_text(encoding="utf-8").splitlines() if "hook_success" not in l]
    env["transcript"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _capture(env)
    assert result["hooks"] == {"hooks": [], "total": 0, "skipped": 0}
    assert result["status"] == "ok"


def test_other_hook_attachments_are_not_counted(env):
    extra = {"type": "attachment", "version": "2.1.287", "attachment": {
        "type": "hook_additional_context", "hookName": "SessionStart:startup", "hookEvent": "SessionStart", "content": ["x"]}}
    with env["transcript"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(extra) + "\n")
    assert _capture(env)["hooks"]["total"] == 5


def test_config_dir_comes_from_the_transcript_location(env):
    alt = env["home"] / ".claude-alt" / "projects" / "-enc"
    alt.mkdir(parents=True)
    (alt / f"{SESSION}.jsonl").write_text(env["transcript"].read_text(encoding="utf-8"), encoding="utf-8")
    import context_manifest
    result = context_manifest.capture(SESSION, claude_projects_dir=alt.parent, repo_root=env["repo"])
    assert result["config_dir"] == {"path": "~/.claude-alt", "default": False, "source": "transcript"}


def test_config_dir_default_home_is_flagged_default(env):
    default = env["home"] / ".claude" / "projects" / "-enc"
    default.mkdir(parents=True)
    (default / f"{SESSION}.jsonl").write_text(env["transcript"].read_text(encoding="utf-8"), encoding="utf-8")
    import context_manifest
    result = context_manifest.capture(SESSION, claude_projects_dir=default.parent, repo_root=env["repo"])
    assert result["config_dir"] == {"path": "~/.claude", "default": True, "source": "transcript"}


def test_config_dir_falls_back_to_the_environment(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env["home"] / ".claude-sm"))
    assert _capture(env)["config_dir"] == {"path": "~/.claude-sm", "default": False, "source": "env"}
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert _capture(env)["config_dir"] == {"path": "~/.claude", "default": True, "source": "env"}


@pytest.mark.parametrize("provider", ["kimi", "codex", "", "KIMI "])
def test_other_providers_are_unsupported_and_never_touch_the_file_system(env, monkeypatch, provider):
    import context_manifest

    def locator_must_not_run(*a, **k):
        pytest.fail("locate_session_transcript was called for an unsupported provider")

    monkeypatch.setattr(context_manifest, "locate_session_transcript", locator_must_not_run)
    result = context_manifest.capture(SESSION, provider=provider, claude_projects_dir=env["projects"])
    assert result["status"] == "unsupported"
    assert result["reason"] == f"provider_not_supported:{provider.strip().lower()}"
    assert "instructions" not in result


def test_provider_claude_is_the_default_and_is_case_insensitive(env):
    import context_manifest

    explicit = context_manifest.capture(SESSION, provider=" Claude", claude_projects_dir=env["projects"], repo_root=env["repo"])
    assert explicit["status"] == "ok" and explicit == _capture(env)


# --- capture: failure and edge paths ---------------------------------------

def test_capture_without_session_id_is_unavailable(env):
    import context_manifest

    result = context_manifest.capture("  ", claude_projects_dir=env["projects"])
    assert result["status"] == "unavailable" and result["reason"] == "no_session_id"


def test_capture_unknown_session_is_unavailable(env):
    import context_manifest

    result = context_manifest.capture("other", claude_projects_dir=env["projects"])
    assert result["status"] == "unavailable" and result["reason"] == "transcript_not_found"


def test_transcript_without_context_records_is_partial(env):
    env["transcript"].write_text(json.dumps({"type": "user", "version": "2.1.287"}) + "\n", encoding="utf-8")
    result = _capture(env)
    assert result["status"] == "partial"
    assert result["instructions"]["files"] == []
    assert result["auto_memory"]["loaded"] is False
    assert result["skills"]["initial_count"] is None


@pytest.mark.parametrize("bad_entry", [
    {"path": "/x/CLAUDE.md", "type": "User", "content": None},
    {"path": "/x/CLAUDE.md", "type": None, "content": "c"},
    {"path": "", "type": "User", "content": "c"},
    {"type": "User", "content": "c"},
    "not-a-dict",
])
def test_malformed_instruction_entry_is_skipped_and_marks_partial(env, bad_entry):
    line = {"type": "attachment", "version": "2.1.287",
            "attachment": {"type": "instructions", "files": [bad_entry]}}
    env["transcript"].write_text(json.dumps(line) + "\n", encoding="utf-8")
    result = _capture(env)
    assert result["status"] == "partial"
    assert result["instructions"]["files"] == []
    assert result["instructions"]["skipped"] == 1


def test_unparseable_lines_are_ignored(env):
    text = env["transcript"].read_text(encoding="utf-8")
    env["transcript"].write_text('{"attachment": broken\n' + text, encoding="utf-8")
    assert _capture(env)["status"] == "ok"


def test_byte_budget_gives_partial(env, monkeypatch):
    import context_manifest

    monkeypatch.setattr(context_manifest, "MAX_SCAN_BYTES", 10)
    result = _capture(env)
    assert result["status"] == "partial"
    assert result["transcript"]["budget_hit"] is True


def test_time_budget_gives_partial(env, monkeypatch):
    import context_manifest

    monkeypatch.setattr(context_manifest, "MAX_SCAN_SECONDS", -1.0)
    result = _capture(env)
    assert result["status"] == "partial"
    assert result["transcript"]["budget_hit"] is True


def test_capture_never_raises(env, monkeypatch):
    import context_manifest

    def boom(*a, **k):
        raise RuntimeError("scan broke")

    monkeypatch.setattr(context_manifest, "_scan_transcript", boom)
    result = _capture(env)
    assert result["status"] == "error" and result["reason"] == "extract_failed:RuntimeError"


def _hooks_for(env, command):
    lines = [l for l in env["transcript"].read_text(encoding="utf-8").splitlines() if "hook_success" not in l]
    lines.append(json.dumps({"type": "attachment", "version": "2.1.287", "attachment": {
        "type": "hook_success", "hookEvent": "PreToolUse", "hookName": "PreToolUse:Bash", "command": command}}))
    env["transcript"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _capture(env)
    assert "SENTINEL-SECRET" not in json.dumps(result)
    return result["hooks"]["hooks"]


@pytest.mark.parametrize("command,expected", [
    ("bash -c '/opt/hooks/start.sh --token SENTINEL-SECRET'", "/opt/hooks/start.sh"),
    ('/usr/bin/env bash -lc "/opt/x.sh SENTINEL-SECRET"', "/opt/x.sh"),
    ("sh -c '/opt/a.sh; curl -H SENTINEL-SECRET'", None),
    ("/bin/bash -c /opt/hooks/start.sh", "/opt/hooks/start.sh"),
    ("/opt/run.sh --token=/SENTINEL-SECRET", "/opt/run.sh"),
    ("TOKEN=/SENTINEL-SECRET /opt/run.sh", "/opt/run.sh"),
    ("--token=/SENTINEL-SECRET", None),
    ("/opt/plain.sh", "/opt/plain.sh"),
])
def test_command_path_never_carries_a_script_or_an_argument(env, command, expected):
    hooks = _hooks_for(env, command)
    assert len(hooks) == 1 and hooks[0]["command_path"] == expected


@pytest.mark.parametrize("command,expected", [
    ("curl --token /SENTINEL-SECRET", None),
    ("bash hooks/run.sh /SENTINEL-SECRET", None),
    ('python -c "print(1)" /SENTINEL-SECRET', None),
    ("python3 -m tool /SENTINEL-SECRET", None),
    ("/opt/hooks/run.sh /SENTINEL-SECRET", "/opt/hooks/run.sh"),
    ("bash /opt/hooks/run.sh /SENTINEL-SECRET", "/opt/hooks/run.sh"),
    ("/usr/bin/env FOO=bar python3 /opt/hooks/check.py --key /SENTINEL-SECRET", "/opt/hooks/check.py"),
    ("node ~/hooks/x.js /SENTINEL-SECRET", "~/hooks/x.js"),
    ("bash -e /opt/hooks/run.sh /SENTINEL-SECRET", "/opt/hooks/run.sh"),
    ("env -i FOO=bar curl /SENTINEL-SECRET", None),
    ("python3.12 relative.py /SENTINEL-SECRET", None),
    ("FOO=bar bash", None),
    ("env", None),
])
def test_command_path_reads_the_executable_or_script_position_only(env, command, expected):
    hooks = _hooks_for(env, command)
    assert len(hooks) == 1 and hooks[0]["command_path"] == expected
    assert "SENTINEL-SECRET" not in json.dumps(hooks)


def test_command_path_expands_home_for_a_bare_tilde_path(env):
    hooks = _hooks_for(env, "~/hooks/a.sh")
    assert hooks[0]["command_path"] == "~/hooks/a.sh"


@pytest.mark.parametrize("command", ["bash -c", "bash -c '/opt/a.sh", "bash -c ''"])
def test_command_path_malformed_inline_script_is_none(env, command):
    assert _hooks_for(env, command)[0]["command_path"] is None
