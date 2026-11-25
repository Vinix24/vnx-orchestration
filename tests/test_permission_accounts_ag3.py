"""ADR-090: isolated account discovery, slot validation and settings evidence."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
import permission_account_dirs as accounts
import permission_applied_check as applied
import permission_events_sources as sources
from test_permission_events_scan import Env, FULL
from test_permission_applied_check import apply, digest, seed, w, write, RULE


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("CLAUDE_CONFIG_DIR", "VNX_ACCOUNT_DIRS", "VNX_OVERRIDE_ACCOUNT_DIRS"):
        monkeypatch.delenv(key, raising=False)


def test_ag3_config_dir_does_not_read_the_operators_second_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "configured"))
    monkeypatch.delenv("VNX_ACCOUNT_DIRS", raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_ACCOUNT_DIRS", raising=False)
    for name in (".claude-salesminds", "configured"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": name}}))
    code = """
import permission_events_sources as src
from test_permission_events_sources import use, result
opened, read = [], src._config_uuid
def observed(path):
    opened.append(str(path)); return read(path)
src._config_uuid = observed
def scan(owner):
    lines = [json.dumps(o) for o in [{"type": "session-owner", "accountUuid": owner}, use("t"), result("t", "config")]]
    return src.scan_transcript(lines, project_id="p", slug="-p", stem="s").events[0]["account"]
print(json.dumps([[scan(".claude-salesminds"), scan("configured")], opened]))
"""
    root = Path(__file__).resolve().parents[1]
    run = subprocess.run([sys.executable, "-c", "import json\n" + code], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join(str(root / p) for p in ("scripts/lib", "tests"))})
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [["unknown", "unknown"], [str(tmp_path / p) for p in
                                     (".claude.json", "configured/.claude.json")] * 2]


@pytest.mark.parametrize("name", [None, ".claude-salesminds", "another-account"])
def test_ag3_global_mapping_events_and_applied_paths(tmp_path, monkeypatch, w, name):
    monkeypatch.setenv("HOME", str(w.home))
    rows = [] if name is None else [{"map": f"~/{name}", "slot": "sm"}]
    config = write(tmp_path / "global.json", {"version": 1, "values": {"VNX_ACCOUNT_DIRS": {"value": json.dumps(rows)}}})
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(config))
    monkeypatch.setenv("VNX_ACCOUNT_DIRS", "invalid lower priority value")
    expected = {"vnx": w.home / ".claude"}
    if name:
        expected["sm"] = w.home / name
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"~/{name}")
    assert accounts.account_dirs() == expected
    for slot, folder in expected.items():
        write(w.home / ".claude.json" if slot == "vnx" else folder / ".claude.json",
              {"oauthAccount": {"accountUuid": f"uuid-{slot}", "emailAddress": "private@example.invalid"}})
        lines = [json.dumps({**FULL[0], "accountUuid": f"uuid-{slot}"}), *(json.dumps(o) for o in FULL[1:])]
        events = sources.scan_transcript(lines, project_id="p", slug="-p", stem="s").events
        assert events and {e["account"] for e in events} == {slot}
        assert all(private not in json.dumps(events) for private in (str(w.home), "uuid-", "private@example.invalid"))
        settings = write(folder / "settings.json", {"permissions": {"allow": [RULE]}})
        assert applied.allowed_paths("user", slot, w.repo, w.home) == (str(settings),)
        assert apply(w, f"settings:{settings}#{digest()}", seed(w, account=slot, scope="user"))["applied_evidence"]["path"] == str(settings)


@pytest.mark.parametrize("raw", ["{", "null", "{}", '"unknown"', "[null]", '[{}]',
    '[{"map":"~/a"}]', '[{"slot":"sm"}]', '[{"map":null,"slot":"sm"}]', '[{"map":"","slot":"sm"}]',
    '[{"map":"~/a","slot":null}]', '[{"map":"~/a","slot":"other"}]',
    '[{"map":"~/a","slot":"vnx"}]', '[{"map":"~/a","slot":"sm"},{"map":"~/b","slot":"sm"}]',
    '[{"map":"~/a","slot":"sm"},{"map":"~/a/../a","slot":"vnx"}]'])
def test_ag3_bad_mapping_refuses_scan_and_applied_without_writes(tmp_path, monkeypatch, capsys, raw):
    env = Env(tmp_path, monkeypatch)
    env.transcript(FULL)
    monkeypatch.setenv("VNX_ACCOUNT_DIRS", raw)
    assert env.run() == 2 and env.ledger() == b""
    error = capsys.readouterr().err
    assert "VNX_ACCOUNT_DIRS" in error and repr(raw) in error
    with pytest.raises(applied.Refused) as exc:
        applied.allowed_paths("user", "vnx", tmp_path, tmp_path)
    assert "VNX_ACCOUNT_DIRS" in str(exc.value) and repr(raw) in str(exc.value)


def test_ag3_resolved_aliases_override_default_and_missing_slot_refuses(tmp_path, monkeypatch):
    (tmp_path / "alias").symlink_to(tmp_path / ".claude", target_is_directory=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "alias"))
    assert accounts.account_dirs() == {"vnx": tmp_path / ".claude"}
    monkeypatch.setenv("VNX_ACCOUNT_DIRS", '[{"map":"~/alias","slot":"sm"}]')
    assert sources.default_account_configs() == {"sm": tmp_path / ".claude.json"}
    for slot in ("vnx", "unknown"):
        with pytest.raises(applied.Refused, match="no mapped folder"):
            applied.allowed_paths("user", slot, tmp_path, tmp_path)
    monkeypatch.setenv("VNX_ACCOUNT_DIRS", '[{"map":"~/alias","slot":"sm"},{"map":"~/.claude","slot":"vnx"}]')
    with pytest.raises(ValueError, match="VNX_ACCOUNT_DIRS=.*duplicate folder"):
        accounts.account_dirs()


def test_ag3_scan_loads_registry_once(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.transcript(FULL)
    env.transcript(FULL, "s2.jsonl")
    calls, get = [], accounts.config_runtime.get
    def counted(key, **kwargs):
        calls.append(key)
        return get(key, **kwargs)
    monkeypatch.setattr(accounts.config_runtime, "get", counted)
    assert env.run() == 0 and calls == ["VNX_ACCOUNT_DIRS"]


@pytest.mark.parametrize("failure", [OSError, RuntimeError])
def test_ag3_resolution_error_is_visible(tmp_path, monkeypatch, failure):
    def fail(path):
        raise failure("cannot resolve folder")
    monkeypatch.setattr(Path, "resolve", fail)
    with pytest.raises(ValueError, match="VNX_ACCOUNT_DIRS=.*cannot resolve folder"):
        accounts.account_dirs()


@pytest.mark.parametrize("body", [b"{", b'{"version":99,"values":{}}', None])
def test_ag3_unreadable_global_config_never_falls_back(tmp_path, monkeypatch, body):
    path = tmp_path / "broken.json"
    path.mkdir() if body is None else path.write_bytes(body)
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(path))
    with pytest.raises(ValueError, match="VNX_ACCOUNT_DIRS=None: global_config_error"):
        accounts.account_dirs()
    with pytest.raises(applied.Refused, match="global_config_error"):
        applied.allowed_paths("user", "vnx", tmp_path, tmp_path)


def test_ag3_chain_reports_refusal_without_private_mapping(tmp_path, monkeypatch):
    import permission_chain
    monkeypatch.setenv("VNX_ACCOUNT_DIRS", "private-invalid-value")
    report = permission_chain.run_permission_chain(tmp_path / "state", "p", tmp_path / "projects")
    assert (report["status"], report["reden"]) == ("error", "scan: ValueError")
    assert "private-invalid-value" not in json.dumps(report)
    assert not (tmp_path / "state" / "permission_events.ndjson").exists()
