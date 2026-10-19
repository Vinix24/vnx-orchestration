"""tests/test_guard_templates_and_release.py — the templates carry no guard, the release carries the shim.

Plan t0-guard-gebruikersniveau, deliverable G2 (templates, release check) and OI-2117 item (e)
(the rendered VNX home is validated at render time). The templates are rendered by the real
renderers: ``vnx_settings_merge.load_template`` for ``settings_vnx_keys.json.tmpl`` and
``vnx_cli.commands.init_cmd._render_template`` for the two ``settings.json.j2``.
"""
from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import hook_coverage as hc
import t0_subagent_block
import vnx_settings_merge
from vnx_cli.commands import init_cmd

GUARD = hc.GUARD_ENTRY
NO_SUBAGENTS = "pretooluse_t0_no_subagents.py"
SHIM_REL = "scripts/hooks/vnx_hook_shim.sh"
TEMPLATES = REPO_ROOT / "templates"
RAW_TEMPLATES = [
    TEMPLATES / "settings_vnx_keys.json.tmpl",
    TEMPLATES / "init" / "default" / "settings.json.j2",
    TEMPLATES / "init" / "minimal" / "settings.json.j2",
]
ENGINE_CTX = {"project_name": "p", "project_id": "p", "vnx_version": "0", "engine_root": str(REPO_ROOT)}


def _render(path: Path, tmp_path: Path) -> dict:
    if path.suffix == ".tmpl":
        return vnx_settings_merge.load_template(str(REPO_ROOT), str(tmp_path / "project"))
    return json.loads(init_cmd._render_template(path, ENGINE_CTX))


def _hooks(settings: dict):
    for event, groups in settings.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                yield event, group, hook


@pytest.fixture(params=RAW_TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def template(request):
    return request.param


# ── templates ────────────────────────────────────────────────────────────────


def test_no_template_registers_the_guard(template, tmp_path):
    settings = _render(template, tmp_path)
    assert [e for e, _, h in _hooks(settings) if GUARD in h.get("command", "")] == []
    assert GUARD not in template.read_text(encoding="utf-8")


def test_the_subagent_block_is_the_command_g2_renders_without_the_shim(template, tmp_path):
    """The shim's rule 1 would undo the hook's own T0 detection (a T0 cwd drifts out of the
    project), so the block starts the entry itself, in the command ``t0_subagent_block``
    renders and nothing typed by hand."""
    settings = _render(template, tmp_path)
    commands = [h["command"] for _, g, h in _hooks(settings) if NO_SUBAGENTS in h.get("command", "")]
    assert commands == [t0_subagent_block.registered_command(str(REPO_ROOT))]
    assert hc.SHIM_NAME not in commands[0]


def test_the_subagent_block_keeps_its_matcher(template, tmp_path):
    settings = _render(template, tmp_path)
    (group,) = [g for _, g, h in _hooks(settings) if NO_SUBAGENTS in h.get("command", "")]
    assert set(group["matcher"].split("|")) == {"Agent", "Task"}


def test_every_timeout_is_in_seconds(template, tmp_path):
    settings = _render(template, tmp_path)
    timeouts = [h["timeout"] for _, _, h in _hooks(settings) if "timeout" in h]
    assert timeouts, "the template sets timeouts, so the test has something to judge"
    assert all(isinstance(t, int) and 0 < t <= 600 for t in timeouts), timeouts
    assert not re.search(r'"timeout":\s*\d{4,}', template.read_text(encoding="utf-8"))


def test_the_subagent_hook_still_exists_in_the_scripts_tree():
    assert (REPO_ROOT / "scripts" / "hooks" / NO_SUBAGENTS).is_file()


def test_the_other_hooks_of_the_templates_survive(tmp_path):
    settings = _render(TEMPLATES / "settings_vnx_keys.json.tmpl", tmp_path)
    joined = " ".join(h.get("command", "") for _, _, h in _hooks(settings))
    for kept in ("pretooluse_block_raw_claude_spawn.sh", "build_t0_state_hook.sh", "hookpin_check.sh"):
        assert kept in joined


# ── timeouts ─────────────────────────────────────────────────────────────────

# Worst wall time in seconds over 10 runs on a fixture project (G2 round 3, report table),
# keyed by the script a template registers with a timeout.
MEASURED_MAX_SECONDS = {
    "pretooluse_block_raw_claude_spawn.sh": 0.066,
    "sessionstart.sh": 0.354,
    "hookpin_check.sh": 0.077,
    "userpromptsubmit_intelligence_inject.sh": 0.189,
}


def test_every_timeout_is_at_least_three_times_the_measured_maximum(template, tmp_path):
    settings = _render(template, tmp_path)
    seen = set()
    for _, _, hook in _hooks(settings):
        if "timeout" not in hook:
            continue
        scripts = [name for name in MEASURED_MAX_SECONDS if name in hook["command"]]
        assert scripts, f"a timeout on a hook that was not measured: {hook['command']}"
        seen.update(scripts)
        for name in scripts:
            assert hook["timeout"] >= math.ceil(3 * MEASURED_MAX_SECONDS[name]), (name, hook)
    assert seen, "the template sets timeouts, so the test has something to judge"


# ── the missing-guard notice ─────────────────────────────────────────────────


@pytest.fixture()
def machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_init_prints_one_line_when_no_account_has_the_guard(machine, tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    init_cmd._scaffold_claude_dir(project, init_cmd._templates_root("default"), ENGINE_CTX, False)
    lines = [l for l in capsys.readouterr().out.splitlines() if "vnx guard install" in l]
    assert len(lines) == 1


def test_init_prints_nothing_extra_once_an_account_has_the_guard(machine, tmp_path, capsys):
    import guard_install

    hooks = machine / ".vnx-system" / "current" / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    assert guard_install.write_settings(machine / ".claude" / "settings.json").status == "written"
    project = tmp_path / "project"
    project.mkdir()
    init_cmd._scaffold_claude_dir(project, init_cmd._templates_root("default"), ENGINE_CTX, False)
    assert "vnx guard install" not in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["--full", "--merge"])
def test_regen_settings_prints_one_line_when_no_account_has_the_guard(machine, tmp_path, mode):
    project = tmp_path / "project"
    project.mkdir()
    env = {**os.environ, "HOME": str(machine)}
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "vnx_settings_merge.py"),
                           mode, "--project-root", str(project), "--vnx-home", str(REPO_ROOT)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert len([l for l in proc.stdout.splitlines() if "vnx guard install" in l]) == 1


def test_regen_settings_dry_run_stays_quiet(machine, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    env = {**os.environ, "HOME": str(machine)}
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "vnx_settings_merge.py"),
                           "--merge", "--dry-run", "--json", "--project-root", str(project),
                           "--vnx-home", str(REPO_ROOT)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "vnx guard install" not in proc.stdout


# ── OI-2117 item (e): the rendered value is validated ────────────────────────


@pytest.mark.parametrize("name", [
    "a$(touch pwned)b", "a`touch pwned`b", 'a"b', "a'b", "a\\b", "a;b", "a$HOME_x$(id)",
])
def test_a_vnx_home_with_shell_syntax_is_refused_at_render_time(tmp_path, capsys, name):
    """The directory exists and carries a real template, so only the render-time check can
    refuse it. Before the check the substitution put the value into shell source: `$(...)` kept
    the JSON valid and ended up inside the registered command."""
    install = tmp_path / name
    (install / "templates").mkdir(parents=True)
    (install / "templates" / "settings_vnx_keys.json.tmpl").write_text(
        (TEMPLATES / "settings_vnx_keys.json.tmpl").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        vnx_settings_merge.load_template(str(install), str(tmp_path / "project"))
    assert raised.value.code == 1
    err = capsys.readouterr().err
    # the message quotes the value with repr(), so a backslash shows doubled
    assert "cannot be rendered into hook commands" in err and repr(str(install))[1:-1] in err


def test_a_plain_vnx_home_still_renders(tmp_path):
    install = tmp_path / "install root with space"
    (install / "templates").mkdir(parents=True)
    (install / "templates" / "settings_vnx_keys.json.tmpl").write_text(
        (TEMPLATES / "settings_vnx_keys.json.tmpl").read_text(encoding="utf-8"), encoding="utf-8")
    settings = vnx_settings_merge.load_template(str(install), str(tmp_path / "project"))
    assert any(NO_SUBAGENTS in h.get("command", "") for _, _, h in _hooks(settings))


@pytest.mark.parametrize("engine_root", ['/x/a"b', "/x/$(id)", "/x/`id`", "relative/root", ""])
def test_the_init_renderer_refuses_an_engine_root_with_shell_syntax(engine_root):
    with pytest.raises(ValueError):
        init_cmd._assert_engine_root_renderable(engine_root)


def test_init_writes_no_settings_file_for_a_refused_engine_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    ctx = {**ENGINE_CTX, "engine_root": "/x/$(touch pwned)"}
    with pytest.raises(ValueError):
        init_cmd._scaffold_claude_dir(project, init_cmd._templates_root("default"), ctx, False)
    assert not (project / ".claude" / "settings.json").exists()


@pytest.mark.parametrize("variant", ["default", "minimal"])
def test_the_init_templates_literal_is_what_g2_renders(variant):
    path = TEMPLATES / "init" / variant / "settings.json.j2"
    rendered = json.loads(init_cmd._render_template(path, {**ENGINE_CTX, "engine_root": "/opt/vnx"}))
    commands = [h["command"] for _, _, h in _hooks(rendered) if NO_SUBAGENTS in h.get("command", "")]
    assert commands == [t0_subagent_block.registered_command("/opt/vnx")]


def test_the_merge_templates_literal_is_what_g2_renders_for_the_placeholder():
    raw = json.loads((TEMPLATES / "settings_vnx_keys.json.tmpl").read_text(encoding="utf-8"))
    commands = [h["command"] for _, _, h in _hooks(raw) if NO_SUBAGENTS in h.get("command", "")]
    assert commands == [t0_subagent_block.registered_command("{{" + "VNX_HOME" + "}}")]


@pytest.mark.parametrize("vnx_home", ["/x/a\"b", "/x/$(id)", "/x/`id`", "relative/root", "",
                                      "~/vnx", "$PWD/vnx"])
def test_the_renderer_uses_the_registered_command_allowlist(vnx_home):
    with pytest.raises(ValueError) as raised:
        t0_subagent_block.registered_command(vnx_home)
    with pytest.raises(ValueError) as reference:
        hc.registered_command(NO_SUBAGENTS, "project", vnx_home)
    assert str(raised.value) == str(reference.value)


# ── release check ────────────────────────────────────────────────────────────


def test_the_shim_is_in_the_release_tree():
    """A release is `git clone --depth 1 --branch <tag>` of this repository
    (install-central.sh `clone_version`), so a file belongs to it when git tracks it. The wheel
    is built from the package-data globs of pyproject.toml."""
    tracked = subprocess.run(["git", "ls-files", "-s", "--error-unmatch", SHIM_REL],
                             cwd=REPO_ROOT, capture_output=True, text=True)
    assert tracked.returncode == 0, f"{SHIM_REL} is not tracked by git: {tracked.stderr}"
    assert tracked.stdout.startswith("100755"), "the shim must be committed executable"
    ignored = subprocess.run(["git", "check-ignore", "-q", SHIM_REL], cwd=REPO_ROOT)
    assert ignored.returncode == 1, f"{SHIM_REL} is git-ignored"
    assert not (REPO_ROOT / ".gitattributes").exists() or \
        "export-ignore" not in (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")


def test_the_shim_is_in_the_wheel_globs():
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data = config["tool"]["setuptools"]
    included = data["package-data"]["vnx_orchestration"]
    excluded = data["exclude-package-data"]["vnx_orchestration"]

    def hit(pattern: str) -> bool:
        return fnmatch.fnmatch(SHIM_REL, pattern.replace("**/", "*/").replace("**", "*"))

    assert any(hit(p) for p in included), included
    assert not any(hit(p) for p in excluded), [p for p in excluded if hit(p)]


def test_the_guard_it_starts_is_in_the_release_tree_too():
    for rel in (f"scripts/hooks/{GUARD}", f"scripts/hooks/{NO_SUBAGENTS}",
                "scripts/lib/hook_coverage.py", "scripts/vnx_guard.py",
                "scripts/lib/guard_install.py", "scripts/lib/guard_check.py",
                "scripts/lib/atomic_swap.py", "scripts/lib/t0_subagent_block.py"):
        proc = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=REPO_ROOT,
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{rel} is not tracked by git"
