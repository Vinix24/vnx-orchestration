"""tests/test_vnx_guard_install.py — `vnx guard install` writes the user-level guard safely.

Plan t0-guard-gebruikersniveau, § "User-level registration", deliverable G2. Every case runs in a
tmp ``HOME``: nothing here opens the real ``~/.claude``, ``~/.claude-*`` or ``~/.vnx``. The
commands are the real functions; the concurrent-write cases use the two seams of
``write_settings`` (``on_compare``, ``before_replace``) to drive a write into exactly the place
the plan names, or wrap the call that puts the scratch file at the target to land a write right
before it.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import atomic_swap
import guard_install as gi
import hook_coverage as hc

GUARD = hc.GUARD_ENTRY
# The real call, for the other writer: a test may wrap `os.replace` itself.
_REAL_REPLACE = os.replace
EXPECTED_COMMAND = hc.registered_command(GUARD, "user")
OPERATOR_SETTINGS = {
    "permissions": {"allow": ["Bash(make *)"], "deny": ["Bash(rm -rf*)"]},
    "model": "opus",
    "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo mine"}]}]},
}


def _make_current(home: Path, *, shim: bool = True, guard: bool = True) -> Path:
    hooks = home / ".vnx-system" / "current" / "scripts" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    if shim:
        shutil.copy(REPO_ROOT / "scripts" / "hooks" / hc.SHIM_NAME, hooks / hc.SHIM_NAME)
    if guard:
        (hooks / GUARD).write_text("# guard\n", encoding="utf-8")
    return hooks


@pytest.fixture()
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    _make_current(path)
    return path


def _account(home: Path, name: str = ".claude", settings=OPERATOR_SETTINGS) -> Path:
    directory = home / name
    directory.mkdir(exist_ok=True)
    if settings is not None:
        (directory / "settings.json").write_text(json.dumps(settings, indent=2) + "\n",
                                                 encoding="utf-8")
    return directory


def _install(home: Path, *, config_dirs=(), dry_run=False):
    out = io.StringIO()
    code = gi.run_install(home=home, config_dirs=list(config_dirs), dry_run=dry_run, out=out)
    return code, out.getvalue()


def _settings(account: Path) -> dict:
    return json.loads((account / "settings.json").read_text(encoding="utf-8"))


def _guard_hooks(settings: dict):
    for event, groups in settings.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                if GUARD in hook.get("command", ""):
                    yield event, group, hook


def _files(account: Path):
    return sorted(p.name for p in account.iterdir())


# ── discovery ────────────────────────────────────────────────────────────────


def test_discovery_finds_the_default_account_and_every_claude_dash_account(home):
    _account(home)
    _account(home, ".claude-x")
    _account(home, ".claude-only-login", settings=None)
    (home / ".claude-only-login" / ".claude.json").write_text("{}", encoding="utf-8")
    _account(home, ".claude-empty", settings=None)
    (home / ".claudeish").mkdir()
    (home / ".claude-file").write_text("not a directory", encoding="utf-8")
    names = [p.name for p in gi.discover_accounts(home)]
    assert names == [".claude", ".claude-only-login", ".claude-x"]
    assert ".claude-empty" not in names and ".claudeish" not in names


def test_the_code_names_no_account():
    source = (REPO_ROOT / "scripts" / "lib" / "guard_install.py").read_text(encoding="utf-8")
    assert "salesminds" not in source.lower()


# ── the entries ──────────────────────────────────────────────────────────────


def test_install_writes_exactly_three_inline_entries_and_changes_nothing_else(home):
    account = _account(home)
    before = _settings(account)
    code, out = _install(home)
    assert code == 0, out
    after = _settings(account)

    found = {event: (group, hook) for event, group, hook in _guard_hooks(after)}
    assert set(found) == {"UserPromptSubmit", "PreToolUse", "Stop"}
    assert len(list(_guard_hooks(after))) == 3
    for event, (group, hook) in found.items():
        assert hook == {"type": "command", "command": EXPECTED_COMMAND, "timeout": 10}
        assert group["matcher"] == ("Bash|Skill|SlashCommand" if event == "PreToolUse" else "")
    # nothing else changed: every other key, and the operator's own Stop hook
    assert {k: v for k, v in after.items() if k != "hooks"} == \
        {k: v for k, v in before.items() if k != "hooks"}
    assert after["hooks"]["Stop"][0] == before["hooks"]["Stop"][0]


def test_install_keeps_the_original_as_a_timestamped_backup(home):
    account = _account(home)
    original = (account / "settings.json").read_bytes()
    _install(home)
    backups = [p for p in account.iterdir() if ".vnx-backup-" in p.name]
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_a_second_run_is_a_no_op(home):
    account = _account(home)
    _install(home)
    settled = (account / "settings.json").read_bytes()
    files = _files(account)
    code, out = _install(home)
    assert code == 0
    assert (account / "settings.json").read_bytes() == settled
    assert _files(account) == files, "a no-op run must not leave a backup or a scratch file"
    assert "unchanged" in out


def test_an_entry_of_ours_in_another_form_is_replaced_not_duplicated(home):
    stale = json.loads(json.dumps(OPERATOR_SETTINGS))
    stale["hooks"]["Stop"].append({"matcher": "", "hooks": [{
        "type": "command", "timeout": 5000,
        "command": EXPECTED_COMMAND}]})
    account = _account(home, settings=stale)
    code, _ = _install(home)
    assert code == 0
    hooks = list(_guard_hooks(_settings(account)))
    assert len(hooks) == 3 and all(h["timeout"] == 10 for _, _, h in hooks)
    assert _settings(account)["hooks"]["Stop"][0] == OPERATOR_SETTINGS["hooks"]["Stop"][0]


def test_a_settings_file_without_hooks_gets_them(home):
    account = _account(home, settings={"model": "opus"})
    assert _install(home)[0] == 0
    assert {e for e, _, _ in _guard_hooks(_settings(account))} == \
        {"UserPromptSubmit", "PreToolUse", "Stop"}


def test_an_account_with_only_a_login_file_gets_a_new_settings_file(home):
    account = _account(home, ".claude-solo", settings=None)
    (account / ".claude.json").write_text("{}", encoding="utf-8")
    assert _install(home)[0] == 0
    assert len(list(_guard_hooks(_settings(account)))) == 3
    assert not [p for p in account.iterdir() if ".vnx-backup-" in p.name]


def test_every_account_is_written(home):
    first, second = _account(home), _account(home, ".claude-x")
    assert _install(home)[0] == 0
    for account in (first, second):
        assert len(list(_guard_hooks(_settings(account)))) == 3


def test_the_file_mode_survives_the_write(home):
    account = _account(home)
    os.chmod(account / "settings.json", 0o640)
    _install(home)
    assert (account / "settings.json").stat().st_mode & 0o777 == 0o640


def test_a_symlinked_settings_file_stays_a_symlink(home, tmp_path):
    account = _account(home, settings=None)
    real = tmp_path / "shared-settings.json"
    real.write_text(json.dumps(OPERATOR_SETTINGS), encoding="utf-8")
    (account / "settings.json").symlink_to(real)
    assert _install(home)[0] == 0
    assert (account / "settings.json").is_symlink()
    assert len(list(_guard_hooks(json.loads(real.read_text(encoding="utf-8"))))) == 3


def test_the_indent_of_the_original_is_kept(home):
    account = _account(home, settings=None)
    (account / "settings.json").write_text(json.dumps({"model": "opus"}, indent=4) + "\n",
                                           encoding="utf-8")
    _install(home)
    assert '\n    "model"' in (account / "settings.json").read_text(encoding="utf-8")


# ── dry run ──────────────────────────────────────────────────────────────────


def test_dry_run_lists_the_accounts_prints_the_diff_and_writes_nothing(home):
    first, second = _account(home), _account(home, ".claude-x")
    snapshot = {a: ((a / "settings.json").read_bytes(), _files(a)) for a in (first, second)}
    code, out = _install(home, dry_run=True)
    assert code == 0
    assert str(first) in out and str(second) in out
    assert out.count("+++ ") == 2 and "vnx_hook_shim.sh" in out
    assert "nothing was written" in out
    for account, (content, names) in snapshot.items():
        assert (account / "settings.json").read_bytes() == content
        assert _files(account) == names


def test_dry_run_on_an_installed_account_says_unchanged(home):
    _account(home)
    _install(home)
    code, out = _install(home, dry_run=True)
    assert code == 0 and "unchanged" in out


# ── refusals ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("content", ["{not json", "[]", '"text"', ""])
def test_invalid_json_is_never_written(home, content):
    account = _account(home, settings=None)
    (account / "settings.json").write_text(content, encoding="utf-8")
    code, out = _install(home)
    assert code == 1
    assert (account / "settings.json").read_text(encoding="utf-8") == content
    assert _files(account) == ["settings.json"]
    assert "nothing was written" in out or "refused" in out


def test_a_malformed_hooks_tree_is_refused_rather_than_guessed(home):
    account = _account(home, settings={"hooks": {"Stop": "not a list"}})
    before = (account / "settings.json").read_bytes()
    code, out = _install(home)
    assert code == 1 and "refused" in out
    assert (account / "settings.json").read_bytes() == before


# Every node of the hooks tree, with the path the refusal names.
MALFORMED_TREES = [
    pytest.param({"hooks": []}, "hooks is not an object", id="hooks-a-list"),
    pytest.param({"hooks": {"SessionStart": "x"}}, "hooks.SessionStart is not a list", id="event"),
    pytest.param({"hooks": {"Stop": ["x"]}}, "hooks.Stop[0] is not an object", id="group"),
    pytest.param({"hooks": {"Stop": [{"hooks": "not a list"}]}}, "hooks.Stop[0].hooks is not a list",
                 id="group-hooks-a-string"),
    pytest.param({"hooks": {"Stop": [{"hooks": 17}]}}, "hooks.Stop[0].hooks is not a list",
                 id="group-hooks-an-integer"),
    pytest.param({"hooks": {"Stop": [{"matcher": 5, "hooks": []}]}}, "hooks.Stop[0].matcher is not a string",
                 id="matcher"),
    pytest.param({"hooks": {"Stop": [{"hooks": [], "matcher": ""}, {"hooks": [3]}]}},
                 "hooks.Stop[1].hooks[0] is not an object", id="hook"),
    pytest.param({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": 7}]}]}},
                 "hooks.Stop[0].hooks[0].command is not a string", id="command"),
]


@pytest.mark.parametrize("settings, where", MALFORMED_TREES)
def test_a_malformed_node_anywhere_in_the_hooks_tree_refuses_with_its_path(home, settings, where):
    """codex on #2078: `{"hooks":{"Stop":[{"hooks":"not a list"}]}}` installed as written, and an
    integer there raised an uncaught TypeError. Every node is validated before the write."""
    account = _account(home, settings=settings)
    before = (account / "settings.json").read_bytes()
    code, out = _install(home)
    assert code == 1 and "refused" in out and where in out, out
    assert (account / "settings.json").read_bytes() == before
    assert _files(account) == ["settings.json"], "a refused file leaves no backup and no scratch"


@pytest.mark.parametrize("settings, where", MALFORMED_TREES)
def test_the_dry_run_refuses_a_malformed_tree_with_the_same_path(home, settings, where):
    _account(home, settings=settings)
    code, out = _install(home, dry_run=True)
    assert code == 1 and where in out


@pytest.mark.parametrize("settings", [p.values[0] for p in MALFORMED_TREES] + [
    {}, {"hooks": {}}, {"hooks": {"Stop": []}}, {"hooks": {"Stop": [{}]}},
    {"hooks": {"Stop": [{"matcher": None, "hooks": [{"type": "prompt", "prompt": 1}]}]}},
    OPERATOR_SETTINGS,
])
def test_the_tree_rules_agree_with_hook_coverage(tmp_path, settings):
    """One shape: what the install refuses is what `hook_coverage` cannot load (UNKNOWN)."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    unloadable = hc.coverage(project, "Stop", env={"HOME": str(tmp_path)}).state == hc.UNKNOWN
    assert (gi.hooks_tree_problem(settings) is not None) == unloadable


def test_one_bad_account_does_not_stop_the_others_but_fails_the_run(home):
    bad = _account(home, settings=None)
    (bad / "settings.json").write_text("{oops", encoding="utf-8")
    good = _account(home, ".claude-x")
    code, _ = _install(home)
    assert code == 1
    assert len(list(_guard_hooks(_settings(good)))) == 3


@pytest.mark.parametrize("missing", ["shim", "guard"])
def test_install_refuses_without_the_shim_or_the_guard_in_current(tmp_path, monkeypatch, missing):
    bare = tmp_path / "bare-home"
    bare.mkdir()
    monkeypatch.setenv("HOME", str(bare))
    _make_current(bare, shim=missing != "shim", guard=missing != "guard")
    account = _account(bare)
    before = (account / "settings.json").read_bytes()
    code, out = _install(bare)
    assert code == 1
    assert ("vnx_hook_shim.sh" if missing == "shim" else GUARD) in out and "refused" in out
    assert (account / "settings.json").read_bytes() == before
    assert _files(account) == ["settings.json"]


def test_install_refuses_when_there_is_no_account(home):
    code, out = _install(home)
    assert code == 1 and "no Claude Code account" in out


def test_config_dir_must_exist_and_replaces_discovery(home, tmp_path):
    _account(home)
    code, out = _install(home, config_dirs=[str(tmp_path / "nope")])
    assert code == 1 and "not a directory" in out
    custom = tmp_path / "custom-config"
    custom.mkdir()
    code, _ = _install(home, config_dirs=[str(custom)])
    assert code == 0 and len(list(_guard_hooks(_settings(custom)))) == 3
    assert list(_guard_hooks(_settings(home / ".claude"))) == []


# ── the write procedure ──────────────────────────────────────────────────────


def _write_like_claude_code(path: Path, **extra) -> None:
    """A rename-style write, as the CLI does for a permission grant."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(extra)
    scratch = path.with_name(path.name + ".claude-scratch")
    scratch.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _REAL_REPLACE(scratch, path)


def _write_in_place(path: Path, **extra) -> None:
    """An in-place write: open with truncation, then write."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(extra)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")


def _just_before_the_replace(monkeypatch, path: Path, write) -> list:
    """Run ``write`` once, right before the call that puts the scratch file at ``path``,
    whichever call that is: ``os.replace`` or the exchange of ``atomic_swap``. Returns the
    calls that fired, so a test can assert the write landed in that window."""
    target = os.path.realpath(path)
    fired: list = []

    def wrap(real):
        def call(src, dst, *args, **kwargs):
            if not fired and os.path.realpath(dst) == target:
                fired.append(real.__name__)
                write()
            return real(src, dst, *args, **kwargs)
        return call

    monkeypatch.setattr(os, "replace", wrap(os.replace))
    monkeypatch.setattr(atomic_swap, "exchange", wrap(atomic_swap.exchange))
    return fired


def test_a_change_before_the_last_comparison_is_re_read_once(home):
    account = _account(home)
    path = account / "settings.json"
    calls = []

    def concurrent():
        calls.append(1)
        if len(calls) == 1:
            _write_like_claude_code(path, extraKnownMarketplaces={"a": 1})

    result = gi.write_settings(path, on_compare=concurrent)
    assert result.status == "written", result.detail
    assert len(calls) == 2, "the command starts over exactly once"
    final = _settings(account)
    assert final["extraKnownMarketplaces"] == {"a": 1}, "the re-read picked the change up"
    assert len(list(_guard_hooks(final))) == 3
    assert not [n for n in _files(account) if n.endswith(".vnx-tmp")]


def test_a_second_change_refuses_and_leaves_the_file_as_the_other_writer_left_it(home):
    account = _account(home)
    path = account / "settings.json"
    counter = iter(range(100))

    def concurrent():
        _write_like_claude_code(path, bump=next(counter))

    result = gi.write_settings(path, on_compare=concurrent)
    assert result.status == "refused" and "changed twice" in result.detail
    final = _settings(account)
    assert "bump" in final and list(_guard_hooks(final)) == []
    assert not [n for n in _files(account) if n.endswith(".vnx-tmp")]


def test_a_write_between_the_last_comparison_and_the_replace_is_reported(home):
    """The write lands after the comparison and before the exchange, so the exchange moves it
    aside. The backup holds the pre-state, the overwritten file is kept and the post-check names
    the difference."""
    account = _account(home)
    path = account / "settings.json"
    pre_state = path.read_bytes()

    def concurrent():
        _write_like_claude_code(path, permissions={"allow": ["Bash(make *)", "Bash(new grant)"],
                                                   "deny": ["Bash(rm -rf*)"]})

    result = gi.write_settings(path, before_replace=concurrent)
    assert result.status == "failed"
    assert result.backup is not None and result.backup.read_bytes() == pre_state
    assert "a write landed after the backup" in result.detail
    assert "permissions" in result.detail
    kept = [n for n in _files(account) if ".vnx-displaced-" in n]
    assert len(kept) == 1
    displaced = json.loads((account / kept[0]).read_text(encoding="utf-8"))
    assert "Bash(new grant)" in displaced["permissions"]["allow"]
    # our entries did land, and the lost grant is not in the file
    final = _settings(account)
    assert len(list(_guard_hooks(final))) == 3
    assert "Bash(new grant)" not in final["permissions"]["allow"]


def test_a_rename_style_write_right_before_the_replace_is_kept_and_reported(home, monkeypatch):
    """codex on #2078: another writer renamed a file with a new deny rule onto the target after
    the hard link and before `os.replace`; the install said `written` and kept nothing of it.
    The write lands right before the call that puts the scratch file in place. Whatever sat at
    the target then is the file that gets overwritten: it must be kept and reported."""
    account = _account(home)
    path = account / "settings.json"
    pre_state = path.read_bytes()
    theirs = {"allow": ["Bash(make *)"], "deny": ["Bash(rm -rf*)", "Bash(dangerous-command)"]}
    fired = _just_before_the_replace(
        monkeypatch, path, lambda: _write_like_claude_code(path, permissions=theirs))

    result = gi.write_settings(path)

    assert fired, "the other writer must land in the window"
    assert result.status == "failed", result.detail
    assert "a write landed after the backup" in result.detail and "permissions" in result.detail
    kept = [account / n for n in _files(account) if ".vnx-displaced-" in n]
    assert len(kept) == 1 and str(kept[0]) in result.detail
    assert json.loads(kept[0].read_text(encoding="utf-8"))["permissions"] == theirs
    assert result.backup is not None and result.backup.read_bytes() == pre_state
    assert len(list(_guard_hooks(_settings(account)))) == 3
    assert not [n for n in _files(account) if n.endswith(".vnx-tmp")]


def test_an_in_place_write_right_before_the_replace_is_kept_and_reported(home, monkeypatch):
    account = _account(home)
    path = account / "settings.json"
    fired = _just_before_the_replace(
        monkeypatch, path, lambda: _write_in_place(path, extraKnownMarketplaces={"b": 2}))
    result = gi.write_settings(path)
    assert fired and result.status == "failed"
    (kept,) = [account / n for n in _files(account) if ".vnx-displaced-" in n]
    assert json.loads(kept.read_text(encoding="utf-8"))["extraKnownMarketplaces"] == {"b": 2}
    assert "extraKnownMarketplaces" in result.detail


def test_a_stale_write_after_the_replace_is_reported_by_the_read_back(home, monkeypatch):
    """Not covered, and said so: a writer that read the file before the exchange and writes
    after it removes the three entries again. The read-back reports it; nothing is repaired."""
    account = _account(home)
    path = account / "settings.json"
    stale = path.read_bytes()
    real_exchange = atomic_swap.exchange

    def exchange_then_stale_write(first, second):
        real_exchange(first, second)
        other = path.with_name("settings.json.other-writer")
        other.write_bytes(stale)
        _REAL_REPLACE(other, path)

    monkeypatch.setattr(atomic_swap, "exchange", exchange_then_stale_write)
    result = gi.write_settings(path)
    assert result.status == "failed"
    assert all(f"after the write, {event} is missing" in result.detail
               for event in ("UserPromptSubmit", "PreToolUse", "Stop"))
    assert (account / "settings.json").read_bytes() == stale


def test_a_restart_removes_the_backup_of_the_abandoned_attempt(home):
    """deepseek on #2078: the first attempt's backup stayed next to settings.json when a change
    made the command start over, and only the second was reported."""
    account = _account(home)
    path = account / "settings.json"
    calls = []

    def concurrent():
        calls.append(1)
        if len(calls) == 1:
            _write_like_claude_code(path, extraKnownMarketplaces={"a": 1})

    result = gi.write_settings(path, on_compare=concurrent)
    assert result.status == "written" and len(calls) == 2
    backups = [account / n for n in _files(account) if ".vnx-backup-" in n]
    assert backups == [result.backup]
    restored = json.loads(result.backup.read_text(encoding="utf-8"))
    assert restored["extraKnownMarketplaces"] == {"a": 1}, "the backup is the state the write used"


def test_a_refusal_after_two_changes_leaves_no_backup(home):
    account = _account(home)
    path = account / "settings.json"
    counter = iter(range(100))
    result = gi.write_settings(
        path, on_compare=lambda: _write_like_claude_code(path, bump=next(counter)))
    assert result.status == "refused"
    assert _files(account) == ["settings.json"]


def test_install_refuses_on_a_platform_without_an_atomic_exchange(home, monkeypatch):
    account = _account(home)
    before = (account / "settings.json").read_bytes()
    monkeypatch.setattr(atomic_swap, "_bound",
                        (None, "no atomic exchange call is known for platform 'plan9'"))
    for dry_run in (False, True):
        code, out = _install(home, dry_run=dry_run)
        assert code == 1 and "refused" in out and "plan9" in out
        assert "without a copy" in out
    assert (account / "settings.json").read_bytes() == before
    assert _files(account) == ["settings.json"]


def test_a_filesystem_that_refuses_the_exchange_refuses_the_file_and_leaves_nothing(
        home, monkeypatch):
    account = _account(home)
    before = (account / "settings.json").read_bytes()

    def refuse(first, second):
        raise atomic_swap.ExchangeUnsupported(
            f"the filesystem holding {second} refuses an atomic exchange (ENOTSUP: not supported)")

    monkeypatch.setattr(atomic_swap, "exchange", refuse)
    result = gi.write_settings(account / "settings.json")
    assert result.status == "refused"
    assert "refuses an atomic exchange" in result.detail and "nothing was written" in result.detail
    assert (account / "settings.json").read_bytes() == before
    assert _files(account) == ["settings.json"]


def test_a_clean_write_leaves_no_displaced_file_and_no_scratch(home):
    account = _account(home)
    result = gi.write_settings(account / "settings.json")
    assert result.status == "written" and result.problems == []
    names = _files(account)
    assert not [n for n in names if ".vnx-displaced-" in n or n.endswith(".vnx-tmp")]


def test_a_file_that_appears_while_the_install_prepares_is_not_overwritten(home):
    account = _account(home, ".claude-new", settings=None)
    (account / ".claude.json").write_text("{}", encoding="utf-8")
    path = account / "settings.json"

    def appears():
        if not path.exists():
            path.write_text(json.dumps({"theirs": True}), encoding="utf-8")

    result = gi.write_settings(path, before_replace=appears)
    assert result.status == "written", result.detail
    final = _settings(account)
    assert final["theirs"] is True and len(list(_guard_hooks(final))) == 3


def test_the_scratch_name_is_per_writer(home):
    source = (REPO_ROOT / "scripts" / "lib" / "guard_install.py").read_text(encoding="utf-8")
    assert "tempfile.mkstemp" in source


def test_top_level_keys_of_the_backup_all_survive(home):
    rich = {f"key{i}": {"n": i} for i in range(12)}
    rich["hooks"] = {}
    account = _account(home, settings=rich)
    assert _install(home)[0] == 0
    final = _settings(account)
    assert set(rich) <= set(final)
    assert {k: v for k, v in final.items() if k != "hooks"} == \
        {k: v for k, v in rich.items() if k != "hooks"}


# ── the registered command, run ──────────────────────────────────────────────


def _shells():
    return [s for s in ("bash", "dash") if shutil.which(s)]


def _run_registered(shell: str, home: Path, cwd: Path) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    return subprocess.run([shell, "-c", EXPECTED_COMMAND], input="{}", capture_output=True,
                          text=True, env=env, cwd=cwd, timeout=30)


@pytest.mark.parametrize("shell", _shells())
def test_rollback_simulation_current_without_a_shim_exits_1_on_every_event(
        home, tmp_path, shell):
    """Plan § "Release delivery and rollback", step 2: `current` flips to a version without the
    shim. The command written by the install exits 1 with a named path on every event, and
    `check` reports the shim missing."""
    import guard_check

    account = _account(home)
    assert _install(home)[0] == 0
    settings = _settings(account)
    # the machine rolls back: `current` becomes a tree without the shim
    shutil.rmtree(home / ".vnx-system" / "current")
    (home / ".vnx-system" / "current" / "scripts" / "hooks").mkdir(parents=True)

    workdir = tmp_path / "anywhere"
    workdir.mkdir()
    for event, _, hook in _guard_hooks(settings):
        proc = subprocess.run(
            [shell, "-c", hook["command"]], input="{}", capture_output=True, text=True,
            env={"HOME": str(home), "PATH": os.environ["PATH"]}, cwd=workdir, timeout=30)
        assert proc.returncode == 1, (event, shell, proc.stderr)
        assert "guard shim missing" in proc.stderr and hc.SHIM_NAME in proc.stderr

    report = guard_check.run_check(home=home)
    assert not report.ok
    assert any(hc.SHIM_NAME in problem for problem in report.problems)
    assert "MISSING" in guard_check.render_report(report)


@pytest.mark.parametrize("shell", _shells())
def test_the_written_command_is_silent_outside_a_vnx_project(home, tmp_path, shell):
    workdir = tmp_path / "plain"
    workdir.mkdir()
    proc = _run_registered(shell, home, workdir)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""


# ── both entrances ───────────────────────────────────────────────────────────


def test_scripts_vnx_guard_runs_install_and_check_end_to_end(home):
    _account(home)
    env = {**os.environ, "HOME": str(home)}

    def run(*argv):
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "vnx_guard.py"), *argv],
            capture_output=True, text=True, env=env, timeout=60)

    dry = run("install", "--dry-run")
    assert dry.returncode == 0 and "nothing was written" in dry.stdout
    done = run("install")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Concurrent writes" in done.stdout and "atomic exchange" in done.stdout
    checked = run("check")
    assert checked.returncode == 0, checked.stdout
    assert "user entry Stop: ok" in checked.stdout


def test_bin_vnx_routes_guard_to_the_same_script(home):
    _account(home)
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(["bash", str(REPO_ROOT / "bin" / "vnx"), "guard", "install", "--dry-run"],
                          capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "nothing was written" in proc.stdout


def test_the_pip_cli_routes_guard_to_the_same_script(home):
    _account(home)
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; from vnx_cli.main import main; "
         "sys.argv = ['vnx', 'guard', 'install', '--dry-run']; sys.exit(main())"],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "nothing was written" in proc.stdout


def test_guard_without_a_subcommand_prints_usage_and_fails(home):
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "vnx_guard.py")],
                          capture_output=True, text=True, env={**os.environ, "HOME": str(home)},
                          timeout=60)
    assert proc.returncode == 1 and "usage" in proc.stderr.lower()
