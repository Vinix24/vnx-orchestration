#!/usr/bin/env python3
"""hook_coverage — does a project's own settings already run the T0 context guard?

The framework registers the T0 context guard once at user level (plan t0-guard-gebruikersniveau,
operator decision 1 of 2026-10-15). A project that still registers it itself would then run it
twice. This module holds the one answer to "does the project cover the guard here", used by the
user-level guard (``scripts/hooks/t0_context_guard.py`` with ``VNX_HOOK_SCOPE=user``) and by
``vnx guard check``, so the two cannot disagree. Stdlib only: it runs inside a hook.

COVERAGE. ``coverage(project_dir, event, tool_name)`` reads the settings Claude Code loaded for
the session: ``<project_dir>/.claude/settings.json`` and ``settings.local.json``, where
``project_dir`` is ``CLAUDE_PROJECT_DIR``, not the git toplevel (a session started in a
subdirectory has no project hooks). The ``hooks`` tree is parsed as JSON. A ``command`` hook on
``event`` covers when it references the entry

  - through the shim (``vnx_hook_shim.sh <entry>``), with any scope but ``user``: a project entry
    in the user form would skip itself and the user-level call alike; or
  - directly (``python3 <path>``, inside any wrapper), with a path that exists. ``$NAME`` and
    ``${NAME}`` expand from ``env`` and ``CLAUDE_PROJECT_DIR`` from ``project_dir``, ``~`` is
    ``HOME``. A leading variable that ``env`` does not carry (vnx-orchestration's ``$ROOT``,
    computed by ``git rev-parse``) and a relative path are read from ``project_dir``. A direct
    entry to a path that does not exist does not cover: that hook cannot run.

For ``PreToolUse`` and ``PostToolUse`` the group's ``matcher`` must match the tool: absent,
``""`` and ``"*"`` match all, a plain name or ``A|B`` list matches exactly (Claude Code: "simple
strings match exactly"), anything else is a regex searched in the tool name. An invalid regex
matches nothing. Other events ignore the matcher, as Claude Code does.

A settings file that is unreadable, is not JSON, or whose ``hooks`` tree is not the documented
shape anywhere counts as NOT covering. Claude Code does not run hooks from a file it cannot load
either, so the user-level guard runs, and a double run is the smaller fault than no run.

WHO THE GUARD IS FOR. ``t0_project_root(cwd)`` is rule 1 of the guard: the git toplevel of
``cwd`` carries a ``.vnx-project-id`` with a non-empty first line and a
``.claude/terminals/T0/CLAUDE.md``, and ``cwd`` is not under ``.vnx-data/worktrees/``.
``scripts/hooks/vnx_hook_shim.sh`` applies the same rule in sh before Python starts.

THE REGISTERED COMMAND. ``registered_command(entry, scope, vnx_home)`` renders the inline
command a settings file registers. It tests the shim before it starts it and maps every
non-zero exit to 1, so no case exits 2: the shim missing, the entry missing, or either one
removed between the test and the start (bash exits 127 for a vanished script, dash exits 2).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Mapping, Optional, Tuple, Union

GUARD_ENTRY = "t0_context_guard.py"
SHIM_NAME = "vnx_hook_shim.sh"
# Entries started through the shim. Each decides through JSON on stdout and never exits 2.
SHIM_ENTRIES: Tuple[str, ...] = (GUARD_ENTRY, "pretooluse_t0_no_subagents.py")
SCOPES: Tuple[str, ...] = ("user", "project")
DEFAULT_VNX_HOME = "$HOME/.vnx-system/current"
SETTINGS_FILES: Tuple[str, ...] = ("settings.json", "settings.local.json")

COVERED = "covered"
MISSING = "missing"
UNKNOWN = "unknown"
FORM_DIRECT = "direct"
FORM_SHIM = "shim"

_TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse"})
_ENTRY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
_UNQUOTABLE = frozenset("'\"\\`\n\r")
_SIMPLE_MATCHER_RE = re.compile(r"[A-Za-z0-9_|]+")
_TOKEN_RE = re.compile(r"[^\s'\";&|()<>`]+")
_VAR_RE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
_LEADING_VAR_RE = re.compile(r"\$(?:\{(\w+)\}|(\w+))/")
_WORKTREE_RE = re.compile(r"(?:^|/)\.vnx-data/worktrees(?:/|$)")


class _Malformed(Exception):
    """The hooks tree is not the documented shape."""


@dataclass(frozen=True)
class Coverage:
    """``state`` is COVERED, MISSING (the files load and nothing covers) or UNKNOWN (no project
    dir, or a settings file that could not be loaded, and nothing covers). ``form`` and
    ``source`` name the covering registration."""

    state: str
    form: Optional[str] = None
    source: Optional[Path] = None

    @property
    def covered(self) -> bool:
        return self.state == COVERED


# ── the registered command ───────────────────────────────────────────────────


def registered_command(entry: str, scope: str = "user", vnx_home: str = DEFAULT_VNX_HOME) -> str:
    """The inline command a settings file registers for ``entry``.

    ``vnx_home`` is the directory holding ``scripts/hooks/vnx_hook_shim.sh``; it may contain
    variables the hook's shell expands (``$HOME``, ``$CLAUDE_PROJECT_DIR``) or a template
    placeholder (``{{VNX_HOME}}``). Raises ValueError for anything that cannot be quoted safely
    inside the command.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, not {scope!r}")
    if not _ENTRY_RE.fullmatch(entry or ""):
        raise ValueError(f"entry must be a file name under scripts/hooks/, not {entry!r}")
    if not vnx_home or _UNQUOTABLE.intersection(vnx_home):
        raise ValueError(f"vnx_home cannot be quoted in a hook command: {vnx_home!r}")
    shim = f"{vnx_home.rstrip('/')}/scripts/hooks/{SHIM_NAME}"
    return (
        f"sh -c 'f=\"{shim}\"; "
        "if [ ! -f \"$f\" ]; then echo \"vnx: guard shim missing: $f\" >&2; exit 1; fi; "
        f"sh \"$f\" --scope {scope} {entry}; rc=$?; [ \"$rc\" -eq 0 ] || rc=1; exit \"$rc\"'"
    )


# ── rule 1: a VNX project with a T0 ──────────────────────────────────────────


def project_id(root: Union[str, Path]) -> Optional[str]:
    """First line of ``<root>/.vnx-project-id``, stripped; None when absent, empty or unreadable."""
    try:
        with open(Path(root) / ".vnx-project-id", encoding="utf-8") as fh:
            first = fh.readline()
    except (OSError, UnicodeDecodeError):
        return None
    return first.strip() or None


def _git_toplevel(cwd: str) -> Optional[Path]:
    try:
        proc = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return Path(out).resolve() if proc.returncode == 0 and out else None


def t0_project_root(cwd: Optional[str]) -> Optional[Path]:
    """The project root when ``cwd`` lies in a VNX project with a T0 folder, else None."""
    if not cwd or _WORKTREE_RE.search(cwd.replace("\\", "/")):
        return None
    root = _git_toplevel(cwd)
    if root is None or _WORKTREE_RE.search(root.as_posix()):
        return None
    if not (root / ".claude" / "terminals" / "T0" / "CLAUDE.md").is_file():
        return None
    return root if project_id(root) else None


# ── coverage ─────────────────────────────────────────────────────────────────


def matcher_matches(matcher: object, tool_name: str) -> bool:
    """Claude Code's matcher rule for a tool event (see the module docstring)."""
    if matcher is None:
        return True
    if not isinstance(matcher, str):
        return False
    pattern = matcher.strip()
    if pattern in ("", "*"):
        return True
    if _SIMPLE_MATCHER_RE.fullmatch(pattern):
        return tool_name in {part for part in pattern.split("|") if part}
    try:
        return re.search(pattern, tool_name or "") is not None
    except re.error:
        return False


def _basename(token: str) -> str:
    return token.rstrip("/").rsplit("/", 1)[-1]


def _expand(text: str, project_dir: Path, env: Mapping[str, str]) -> Optional[str]:
    def value(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        if name == "CLAUDE_PROJECT_DIR":
            return str(project_dir)
        if name not in env:
            raise KeyError(name)
        return env[name]

    try:
        return _VAR_RE.sub(value, text)
    except KeyError:
        return None


def _direct_path(token: str, project_dir: Path, env: Mapping[str, str]) -> Optional[Path]:
    text = token.rsplit("=", 1)[-1]
    if text.startswith("~/"):
        if "HOME" not in env:
            return None
        text = env["HOME"] + text[1:]
    lead = _LEADING_VAR_RE.match(text)
    if lead:
        name = lead.group(1) or lead.group(2)
        if name != "CLAUDE_PROJECT_DIR" and name not in env:
            text = text[lead.end():]
    expanded = _expand(text, project_dir, env)
    if expanded is None:
        return None
    path = Path(expanded)
    return path if path.is_absolute() else project_dir / path


def _shim_scope(tokens: List[str]) -> str:
    scope = "project"
    for index, token in enumerate(tokens):
        if token == "--scope" and index + 1 < len(tokens):
            scope = tokens[index + 1]
        elif token.startswith("--scope="):
            scope = token.split("=", 1)[1]
    return scope


def command_form(command: str, entry: str, project_dir: Path,
                 env: Mapping[str, str]) -> Optional[str]:
    """FORM_SHIM or FORM_DIRECT when ``command`` covers ``entry``, else None."""
    tokens = _TOKEN_RE.findall(command)
    if any(_basename(token) == SHIM_NAME for token in tokens):
        if entry in tokens and _shim_scope(tokens) != "user":
            return FORM_SHIM
        return None
    for token in tokens:
        if _basename(token) != entry:
            continue
        path = _direct_path(token, project_dir, env)
        if path is not None and path.is_file():
            return FORM_DIRECT
    return None


def _load_hooks(path: Path) -> Optional[dict]:
    """The file's ``hooks`` tree ({} when it has none); None when it cannot be loaded."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    hooks = data.get("hooks", {})
    try:
        _validate(hooks)
    except _Malformed:
        return None
    return hooks


def _validate(hooks: object) -> None:
    if not isinstance(hooks, dict):
        raise _Malformed("hooks")
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise _Malformed("event")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                raise _Malformed("group")
            if "matcher" in group and not isinstance(group["matcher"], (str, type(None))):
                raise _Malformed("matcher")
            for hook in group.get("hooks", []):
                if not isinstance(hook, dict):
                    raise _Malformed("hook")
                if hook.get("type", "command") == "command" and not isinstance(
                        hook.get("command", ""), str):
                    raise _Malformed("command")


def _commands(hooks: dict, event: str, tool_name: Optional[str]) -> Iterator[str]:
    for group in hooks.get(event, []):
        if event in _TOOL_EVENTS and not matcher_matches(group.get("matcher"), tool_name or ""):
            continue
        for hook in group.get("hooks", []):
            if hook.get("type", "command") == "command" and hook.get("command"):
                yield hook["command"]


def coverage(project_dir: Union[str, Path, None], event: str, tool_name: Optional[str] = None,
             *, entry: str = GUARD_ENTRY, env: Optional[Mapping[str, str]] = None) -> Coverage:
    """Whether the project at ``project_dir`` runs ``entry`` itself for ``event`` (and, for a
    tool event, ``tool_name``). See the module docstring for the rules."""
    if not project_dir:
        return Coverage(UNKNOWN)
    base = Path(project_dir)
    mapping = os.environ if env is None else env
    unloadable = False
    for name in SETTINGS_FILES:
        path = base / ".claude" / name
        if not path.exists():
            continue
        hooks = _load_hooks(path)
        if hooks is None:
            unloadable = True
            continue
        for command in _commands(hooks, event, tool_name):
            form = command_form(command, entry, base, mapping)
            if form is not None:
                return Coverage(COVERED, form, path)
    return Coverage(UNKNOWN if unloadable else MISSING)
