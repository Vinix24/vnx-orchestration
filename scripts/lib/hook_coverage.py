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

  - through the shim, when that call can run: the inline form ``registered_command`` renders, or
    a plain ``[sh|bash|dash] <path>/vnx_hook_shim.sh [--scope project] <entry>``. The arguments
    must be ones the shim accepts (``--scope`` first, ``user`` or ``project``, then exactly one
    entry name), the scope must not be ``user`` (a project entry in the user form would skip
    itself and the user-level call alike), and the shim path must be an existing file
    (executable, when no shell starts it). Only ``$HOME``/``${HOME}`` (from ``env``) and
    ``$CLAUDE_PROJECT_DIR``/``${CLAUDE_PROJECT_DIR}`` (``project_dir``) are expanded, as a
    prefix, and never through a shell. Any other variable, a substitution, a tilde, an
    unrendered ``{{VNX_HOME}}`` and a relative path leave the shim unresolved; or
  - directly (``python3 <path>``, inside any wrapper), with a path that exists. ``$NAME`` and
    ``${NAME}`` expand from ``env`` and ``CLAUDE_PROJECT_DIR`` from ``project_dir``, ``~`` is
    ``HOME``. A leading variable that ``env`` does not carry (vnx-orchestration's ``$ROOT``,
    computed by ``git rev-parse``) and a relative path are read from ``project_dir``. A direct
    entry to a path that does not exist does not cover: that hook cannot run.

For ``PreToolUse`` and ``PostToolUse`` the group's ``matcher`` must match the tool: absent,
``""`` and ``"*"`` match all, a plain name or ``A|B`` list matches exactly (Claude Code: "simple
strings match exactly"), anything else is a regex searched in the tool name. An invalid regex
matches nothing. Other events ignore the matcher, as Claude Code does.

A registration that cannot run (a direct entry to a missing path, a shim that is missing or
cannot be resolved, a shim call the shim itself refuses) reports MISSING, not UNKNOWN: the
files loaded, and nothing in them runs the guard. ``vnx guard check`` reads this function and
shows such a project as ``missing``, so the operator sees that the user-level guard is the one
running there.

A settings file that is unreadable, is not JSON, or whose ``hooks`` tree is not the documented
shape anywhere counts as NOT covering. Claude Code does not run hooks from a file it cannot load
either, so the user-level guard runs, and a double run is the smaller fault than no run.

WHO THE GUARD IS FOR. ``t0_project_root(cwd)`` is rule 1 of the guard: the git toplevel of
``cwd`` carries a ``.vnx-project-id`` with a non-empty first line and a
``.claude/terminals/T0/CLAUDE.md``, and ``cwd`` is not under ``.vnx-data/worktrees/``.
``scripts/hooks/vnx_hook_shim.sh`` applies the same rule in sh before Python starts.

WHICH CODE THE SHIM RUNS. ``shim_entry_file(root, entry, env)`` is the shim's step 2. The
fabric repo runs its own ``<root>/scripts/hooks/<entry>``; every other root runs
``${VNX_HOME:-$HOME/.vnx-system/current}/scripts/hooks/<entry>``. A repository writes its own
``.vnx-project-id``, so that id never selects repo code on its own: a user-level hook would then
run whatever a cloned repo ships. ``is_fabric_root`` also requires the root's real path to be
the real path of the first ``vnx-dev`` entry in the user-owned registry
``$HOME/.vnx/projects.json``. A missing or unreadable registry, or an entry whose path is not an
absolute string, means ``current``. The shim reads the registry the same way, with
``python3 -I -S``, so a module the repo plants in the cwd is never imported.

THE REGISTERED COMMAND. ``registered_command(entry, scope, vnx_home)`` renders the inline
command a settings file registers. It tests the shim before it starts it and maps every
non-zero exit to 1, so no case exits 2: the shim missing, the entry missing, or either one
removed between the test and the start (bash exits 127 for a vanished script, dash exits 2).
``vnx_home`` is interpolated into shell source, so it is an allowlist of documented forms.
"""
from __future__ import annotations

import json
import os
import re
import shlex
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
FABRIC_PROJECT_ID = "vnx-dev"
SHIM_SUFFIX = f"/scripts/hooks/{SHIM_NAME}"

COVERED = "covered"
MISSING = "missing"
UNKNOWN = "unknown"
FORM_DIRECT = "direct"
FORM_SHIM = "shim"

_TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse"})
# The shim's own entry rule: not empty, no leading `.` or `-`, only these characters.
_ENTRY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
# The documented forms of a VNX home: an absolute path of plain path characters, or one that
# starts with exactly one of these prefixes. Nothing here means anything inside double quotes.
_HOME_PREFIX = (r"\$HOME|\$\{HOME\}|\$CLAUDE_PROJECT_DIR|\$\{CLAUDE_PROJECT_DIR\}"
                r"|\{\{VNX_HOME\}\}")
_PATH_CHARS = r"[A-Za-z0-9._+@,: -]"
_VNX_HOME_RE = re.compile(rf"(?:{_HOME_PREFIX})(?:/{_PATH_CHARS}*)*|(?:/{_PATH_CHARS}*)+")
_SHIM_VAR_RE = re.compile(r"\$(?:(HOME|CLAUDE_PROJECT_DIR)|\{(HOME|CLAUDE_PROJECT_DIR)\})(?=/|$)")
_INLINE_SHIM_RE = re.compile(
    r"sh -c 'f=\"(?P<shim>[^\"]*)\"; .*; sh \"\$f\" (?P<args>[^;']*); rc=\$\?; ")
_SHELLS = frozenset({"sh", "bash", "dash"})
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

    ``vnx_home`` is the directory holding ``scripts/hooks/vnx_hook_shim.sh``. It lands inside
    shell source, so only the documented forms are accepted: an absolute path of plain path
    characters (letters, digits, space and ``. _ + @ , : - /``), optionally starting with
    ``$HOME``, ``${HOME}``, ``$CLAUDE_PROJECT_DIR`` or ``${CLAUDE_PROJECT_DIR}`` (the hook's
    shell expands them), or with the template placeholder ``{{VNX_HOME}}``. Each prefix is
    followed by ``/`` or nothing. Anything else, a command substitution included, raises
    ValueError naming the value.

    The command ends with ``[ "$rc" -eq 0 ] || rc=1`` where the plan's line reads
    ``[ "$rc" -eq 2 ] && rc=1``: bash exits 127 for a shim removed between the test and the
    start, the G1 row requires every case to exit 0 or 1, and every non-zero code but 2 is
    non-blocking anyway (T0 decision on #2075, 2026-10-16).
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, not {scope!r}")
    if not _ENTRY_RE.fullmatch(entry or ""):
        raise ValueError(f"entry must be a file name under scripts/hooks/, not {entry!r}")
    if not _VNX_HOME_RE.fullmatch(vnx_home or ""):
        raise ValueError(
            "vnx_home must be an absolute path of plain path characters, optionally starting "
            "with $HOME, ${HOME}, $CLAUDE_PROJECT_DIR, ${CLAUDE_PROJECT_DIR} or {{VNX_HOME}}, "
            f"not {vnx_home!r}")
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


# ── shim step 2: which code runs ─────────────────────────────────────────────


def registered_fabric_root(env: Mapping[str, str]) -> Optional[str]:
    """The real path of the first ``vnx-dev`` entry in ``$HOME/.vnx/projects.json``.

    None when ``HOME`` is unset or empty, the registry is missing, unreadable or not the
    documented shape, or that entry's ``path`` is not an absolute string. The shim's inline
    ``python3 -I -S`` reader decides the same way.
    """
    home = env.get("HOME") or ""
    if not home:
        return None
    try:
        with open(os.path.join(home, ".vnx", "projects.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    projects = data.get("projects") if isinstance(data, dict) else None
    for project in projects if isinstance(projects, list) else ():
        if isinstance(project, dict) and project.get("project_id") == FABRIC_PROJECT_ID:
            path = project.get("path")
            return os.path.realpath(path) if isinstance(path, str) and os.path.isabs(path) else None
    return None


def is_fabric_root(root: Union[str, Path], env: Mapping[str, str]) -> bool:
    """The root calls itself ``vnx-dev`` AND is the root the user-owned registry names."""
    if project_id(root) != FABRIC_PROJECT_ID:
        return False
    registered = registered_fabric_root(env)
    return registered is not None and registered == os.path.realpath(root)


def shim_entry_file(root: Union[str, Path], entry: str, env: Mapping[str, str]) -> Path:
    """The file the shim runs for ``entry`` at a guarded ``root`` (the shim's step 2)."""
    own = Path(root) / "scripts" / "hooks" / entry
    if own.is_file() and is_fabric_root(root, env):
        return own
    vnx_home = env.get("VNX_HOME") or f"{env.get('HOME', '')}/.vnx-system/current"
    return Path(vnx_home) / "scripts" / "hooks" / entry


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


@dataclass(frozen=True)
class _ShimCall:
    shim: str
    scope: str
    entry: str
    needs_exec: bool


def _shim_args(words: List[str]) -> Optional[Tuple[str, str]]:
    """(scope, entry) when the shim accepts these arguments, as its own parser reads them."""
    scope = "project"
    if words and words[0] == "--scope":
        if len(words) < 2:
            return None
        scope, words = words[1], words[2:]
    if scope not in SCOPES or len(words) != 1 or not _ENTRY_RE.fullmatch(words[0]):
        return None
    return scope, words[0]


def _inline_shim_call(command: str) -> Optional[_ShimCall]:
    """The call in exactly the form ``registered_command`` renders, else None."""
    match = _INLINE_SHIM_RE.match(command)
    if match is None or not match["shim"].endswith(SHIM_SUFFIX):
        return None
    parsed = _shim_args(match["args"].split(" "))
    if parsed is None:
        return None
    scope, entry = parsed
    try:
        rendered = registered_command(entry, scope, match["shim"][:-len(SHIM_SUFFIX)] or "/")
    except ValueError:
        return None
    return _ShimCall(match["shim"], scope, entry, False) if rendered == command else None


def _plain_shim_call(command: str) -> Optional[_ShimCall]:
    """``[sh|bash|dash] <path>/vnx_hook_shim.sh <args>`` and nothing else, else None."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    needs_exec = not (words and _basename(words[0]) in _SHELLS)
    if not needs_exec:
        words = words[1:]
    if not words or _basename(words[0]) != SHIM_NAME:
        return None
    parsed = _shim_args(words[1:])
    return None if parsed is None else _ShimCall(words[0], parsed[0], parsed[1], needs_exec)


def _shim_path(text: str, project_dir: Path, env: Mapping[str, str]) -> Optional[Path]:
    """The shim path with a leading $HOME or $CLAUDE_PROJECT_DIR expanded, never through a
    shell; None for anything else that a shell would still have to expand."""
    lead = _SHIM_VAR_RE.match(text)
    if lead:
        name = lead.group(1) or lead.group(2)
        value = str(project_dir) if name == "CLAUDE_PROJECT_DIR" else env.get("HOME") or ""
        if not value:
            return None
        text = value + text[lead.end():]
    if "$" in text or "`" in text or not text.startswith("/"):
        return None
    return Path(text)


def _shim_covers(command: str, entry: str, project_dir: Path, env: Mapping[str, str]) -> bool:
    """The registration starts ``entry`` at project scope through a shim that can run."""
    stripped = command.strip()
    call = _inline_shim_call(stripped) or _plain_shim_call(stripped)
    if call is None or call.entry != entry or call.scope != "project":
        return False
    path = _shim_path(call.shim, project_dir, env)
    if path is None or not path.is_file():
        return False
    return not call.needs_exec or os.access(path, os.X_OK)


def command_form(command: str, entry: str, project_dir: Path,
                 env: Mapping[str, str]) -> Optional[str]:
    """FORM_SHIM or FORM_DIRECT when ``command`` covers ``entry``, else None. A command that
    names the shim is judged as a shim registration only."""
    tokens = _TOKEN_RE.findall(command)
    if any(_basename(token) == SHIM_NAME for token in tokens):
        return FORM_SHIM if _shim_covers(command, entry, project_dir, env) else None
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
