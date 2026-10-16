"""tests/test_guard_templates_and_release.py — the templates carry no guard, the release carries the shim.

Plan t0-guard-gebruikersniveau, deliverable G2 (templates, release check) and OI-2117 item (e)
(the rendered VNX home is validated at render time). The templates are rendered by the real
renderers: ``vnx_settings_merge.load_template`` for ``settings_vnx_keys.json.tmpl`` and
``vnx_cli.commands.init_cmd._render_template`` for the two ``settings.json.j2``.
"""
from __future__ import annotations

import fnmatch
import json
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


def test_the_subagent_block_goes_through_the_shim_via_registered_command(template, tmp_path):
    settings = _render(template, tmp_path)
    commands = [h["command"] for _, g, h in _hooks(settings) if NO_SUBAGENTS in h.get("command", "")]
    assert commands == [hc.registered_command(NO_SUBAGENTS, "project", str(REPO_ROOT))]
    assert "--scope project" in commands[0]


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
def test_the_init_templates_literal_is_what_registered_command_renders(variant):
    path = TEMPLATES / "init" / variant / "settings.json.j2"
    rendered = json.loads(init_cmd._render_template(path, {**ENGINE_CTX, "engine_root": "/opt/vnx"}))
    commands = [h["command"] for _, _, h in _hooks(rendered) if NO_SUBAGENTS in h.get("command", "")]
    assert commands == [hc.registered_command(NO_SUBAGENTS, "project", "/opt/vnx")]


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
                "scripts/lib/guard_install.py", "scripts/lib/guard_check.py"):
        proc = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=REPO_ROOT,
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{rel} is not tracked by git"
