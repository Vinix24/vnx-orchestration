#!/usr/bin/env python3
"""hook_coverage — does a project's own settings already run the T0 context guard?

The framework registers the T0 context guard once at user level (plan t0-guard-gebruikersniveau,
operator decision 1 of 2026-10-15). A project that still registers it itself would then run it
twice. This module holds the one answer to "does the project cover the guard here", used by the
user-level guard (``scripts/hooks/t0_context_guard.py`` with ``VNX_HOOK_SCOPE=user``) and by
``vnx guard check``, so the two cannot disagree. Stdlib only: it runs inside a hook.

A registration counts as covering only when it DECIDES: it runs the entry, in the foreground,
at project scope, where Claude Code runs the project's hooks. Covering where nothing decides is
the worse fault, because the user-level guard then skips and no guard runs at all. Whatever
this module cannot establish therefore does not cover: a double run is the smaller fault.

COVERAGE. ``coverage(project_dir, event, tool_name)`` reads the settings Claude Code loaded for
the session: ``<project_dir>/.claude/settings.json`` and ``settings.local.json``, where
``project_dir`` is ``CLAUDE_PROJECT_DIR``, not the git toplevel (a session started in a
subdirectory has no project hooks). The ``hooks`` tree is parsed as JSON. A hook on ``event``
can cover only when it is a ``command`` hook that runs in the foreground through bash on every
matching event: not ``async`` or ``asyncRewake`` (background, it cannot block), not ``once``
(removed after one run), no ``if`` condition, no exec-form ``args`` and no other ``shell`` (the
fields of the hook schema in Claude Code 2.1.301). Its command covers when it starts the entry

  - through the shim, when that call can run: the inline form ``registered_command`` renders, or
    a plain ``[sh|bash|dash] <path>/vnx_hook_shim.sh [--scope project] <entry>`` with nothing
    around it. The arguments must be ones the shim accepts (``--scope`` first, ``user`` or
    ``project``, then exactly one entry name), the scope must not be ``user`` (a project entry in
    the user form would skip itself and the user-level call alike), and the shim path must be an
    existing file (executable, when no shell starts it). Only ``$HOME``/``${HOME}`` (from
    ``env``) and ``$CLAUDE_PROJECT_DIR``/``${CLAUDE_PROJECT_DIR}`` (``project_dir``) are
    expanded, as a prefix, never through a shell, and only where the shell expands them: a
    variable in single quotes or behind a backslash stays literal, and a bare one whose value the
    shell would split does not resolve. Any other variable, a substitution, a tilde, an
    unrendered ``{{VNX_HOME}}`` and a relative path leave the shim unresolved. The entry the shim
    would start (``shim_entry_file``) must exist as well; or
  - directly: the entry is the script operand of a python interpreter (``python3 <path>``, after
    ``exec``, ``command``, ``env`` and interpreter flags that take no value), or the entry itself
    stands in command position and is executable. It may sit inside ``if``/``then``/``else``,
    ``( )``, a ``;``/``&&``/``||`` list and ``sh -c '<source>'`` (read recursively); a condition
    around it is not evaluated, the path's existence is. The call does not decide when it is part
    of a pipe, runs in the background, is negated with ``!``, or redirects anything but stderr:
    its JSON decision must reach stdout and its payload come from stdin. A command that mentions
    ``VNX_HOOK_SCOPE`` other than as ``VNX_HOOK_SCOPE=project``, or that runs ``.``, ``source``
    or ``eval``, may run the entry at user scope and does not cover. The path expands like the
    shim path, but from every variable ``env`` carries, plus a variable the command assigns
    before the call: a plain value as written, ``$(git rev-parse --show-toplevel ...)`` as
    ``project_dir`` (vnx-orchestration's ``$ROOT``), anything else unresolved. A prefix
    assignment applies to the inner shell of ``sh -c`` but not to its own command's words, as in
    the shell. ``~`` is ``HOME``. A relative path is read from ``project_dir``, unless a ``cd``
    came before it. A direct entry to a path that does not exist does not cover.

The command reader (``_read_commands``) knows just enough of the POSIX grammar for this: quotes,
backslashes, ``$( )``, backticks, ``${ }``, comments, ``;``, ``&``, ``&&``, ``|``, ``||``,
``( )`` and redirections with their fd. A heredoc, ``case``, an unterminated quote or
substitution make the command unreadable, and an unreadable command does not cover.

For ``PreToolUse`` and ``PostToolUse`` the group's ``matcher`` must match the tool: absent,
``""`` and ``"*"`` match all, a plain name or ``A|B`` list matches exactly (Claude Code: "simple
strings match exactly"), anything else is a regex searched in the tool name. An invalid regex
matches nothing. Other events ignore the matcher, as Claude Code does.

A registration that cannot run (a direct entry to a missing path, a shim or entry that is
missing or cannot be resolved, a shim call the shim itself refuses) reports MISSING, not
UNKNOWN: the files loaded, and nothing in them runs the guard. ``vnx guard check`` reads this
function and shows such a project as ``missing``, so the operator sees that the user-level guard
is the one running there.

A settings file that is unreadable, is not JSON, or whose ``hooks`` tree is not the documented
shape anywhere counts as NOT covering. Claude Code does not run hooks from a file it cannot load
either, so the user-level guard runs, and a double run is the smaller fault than no run.

TRUST. Claude Code runs a project's hooks only in a folder the operator has trusted. Measured
in the 2.1.301 binary: the decision is recorded in the global config file (``trust_file``: a
non-empty legacy ``<config home>/.config.json``, else ``.claude.json`` in ``CLAUDE_CONFIG_DIR``
or ``HOME``, so per account) as ``projects[<folder>].hasTrustDialogAccepted: true``. The folder
is the session folder or any parent up to and including its git root (outside a repository: up
to ``/``), looked up as written and in NFC. In an interactive session without trust 2.1.301
skips every hook, the user-level ones included; with trust accepted for the session only (the
home directory) it runs them all while nothing is recorded. A covering registration therefore
counts only where trust is recorded: in a folder with none it reports MISSING, and when the
trust file cannot be read (missing, unreadable, not JSON, ``projects`` not an object) UNKNOWN.
Both are "not covered" to the guard, so the cost of a wrong reading is a double run. Trust is
read only once a registration covers, so a project without one never opens the file.

WHO THE GUARD IS FOR. ``t0_project_root(cwd)`` is rule 1 of the guard: the root of ``cwd``
carries a ``.vnx-project-id`` with a non-empty first line and a
``.claude/terminals/T0/CLAUDE.md``, and ``cwd`` is not under ``.vnx-data/worktrees/``. The root
is the nearest directory, from the real path of ``cwd`` up, holding a ``.git`` entry (directory
or file): the folder ``git rev-parse --show-toplevel`` names, found without starting git. A hook
has 10 s; ``git`` waited up to 10 s of its own, and a walk starts no subprocess at all.
``scripts/hooks/vnx_hook_shim.sh`` applies the same rule in sh before Python starts.

``session_project_root(cwd, env)`` is rule 1 for a session, with one order both sides take: the
cwd first, and only when that yields no rule-1 root ``CLAUDE_PROJECT_DIR`` from ``env``. A
candidate under ``.vnx-data/worktrees/`` yields nothing and stops the resolution there, so a
dispatch worktree stays silent even when ``CLAUDE_PROJECT_DIR`` names the main checkout. An empty
or unset ``CLAUDE_PROJECT_DIR`` yields nothing, and so does a session with no project on either
step. There is no transcript fallback: the shim cannot read the payload without consuming stdin,
so a step only Python could take would let the shim stop a run the guard would have made. The
shim's ``rule1_root`` takes the same two steps in sh.

WHICH CODE THE SHIM RUNS. ``shim_entry_file(root, entry, env)`` is the shim's step 2. The
fabric repo runs its own ``<root>/scripts/hooks/<entry>``. Every other root runs, for a
user-scope call, the entry beside the shim its command names (``user_shim``), and for a
project-scope call ``${VNX_HOME:-$HOME/.vnx-system/current}/scripts/hooks/<entry>``. A
user-scope call ignores ``VNX_HOME`` because nobody it trusts chose that value: ``bin/vnx`` and
``vnx_paths.sh`` export it as the location of whichever copy launched the session (a version
directory, a dispatch worktree), and a project's settings ``env`` block can set it. Either would
select the code a user-level registration starts, possibly a release whose guard does not know
the user scope and runs twice. A repository writes its own ``.vnx-project-id``, so that id never
selects repo code on its own: a user-level hook would then run whatever a cloned repo ships.
``is_fabric_root`` also requires the root's real path to be the real path of the first
``vnx-dev`` entry in the user-owned registry ``$HOME/.vnx/projects.json``. A missing or
unreadable registry, one that is not a regular file, or an entry whose path is not an absolute
string, means the released code. The shim reads the registry the same way, with
``python3 -I -S``, so a module the repo plants in the cwd is never imported, and stops that read
after 2 s.

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
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Tuple, Union

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
# Hook fields that keep a registration from deciding every matching event in the foreground.
_DEFERRING_FIELDS: Tuple[str, ...] = ("async", "asyncRewake", "once")
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
# Shells a plain shim call may name, and shells whose `-c` source is read for a direct entry.
_SHIM_SHELLS = frozenset({"sh", "bash", "dash"})
_SHELLS = _SHIM_SHELLS | {"zsh"}
_SIMPLE_MATCHER_RE = re.compile(r"[A-Za-z0-9_|]+")
_TOKEN_RE = re.compile(r"[^\s'\";&|()<>`]+")
_VAR_RE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
_WORKTREE_RE = re.compile(r"(?:^|/)\.vnx-data/worktrees(?:/|$)")


class _Malformed(Exception):
    """The hooks tree is not the documented shape."""


@dataclass(frozen=True)
class Coverage:
    """``state`` is COVERED, MISSING (the files load and nothing that runs covers) or UNKNOWN (no
    project dir, a settings file or the trust file that could not be read, and nothing covers).
    ``form`` and ``source`` name the covering registration."""

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


def _repo_root(cwd: str) -> Optional[Path]:
    """The nearest directory from the real path of ``cwd`` up holding a ``.git`` entry."""
    try:
        start = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not start.is_dir():
        return None
    for level in (start, *start.parents):
        if (level / ".git").exists():
            return level
    return None


def t0_project_root(cwd: Optional[str]) -> Optional[Path]:
    """The project root when ``cwd`` lies in a VNX project with a T0 folder, else None."""
    if not cwd or _WORKTREE_RE.search(cwd.replace("\\", "/")):
        return None
    root = _repo_root(cwd)
    if root is None or _WORKTREE_RE.search(root.as_posix()):
        return None
    if not (root / ".claude" / "terminals" / "T0" / "CLAUDE.md").is_file():
        return None
    return root if project_id(root) else None


def _under_worktree(path: Optional[str]) -> bool:
    """``path`` is, or physically resolves to, a dispatch worktree."""
    if not path:
        return False
    if _WORKTREE_RE.search(path.replace("\\", "/")):
        return True
    return bool(_WORKTREE_RE.search(os.path.realpath(path).replace("\\", "/")))


def session_project_root(cwd: Optional[str], env: Mapping[str, str]) -> Optional[Path]:
    """Rule 1's root for the session: (a) the cwd's root, else (b) ``CLAUDE_PROJECT_DIR``'s.

    One order and one set of exclusions, shared with ``scripts/hooks/vnx_hook_shim.sh``
    (``rule1_root``). A candidate under ``.vnx-data/worktrees/`` yields nothing and stops the
    resolution, so a dispatch worktree stays silent even when ``CLAUDE_PROJECT_DIR`` names the
    main checkout. An empty or unset ``CLAUDE_PROJECT_DIR`` yields nothing, and so does a session
    with no project on either step. There is no transcript fallback (module docstring).
    """
    root = t0_project_root(cwd)
    if root is not None:
        return root
    if _under_worktree(cwd):
        return None
    project_dir = env.get("CLAUDE_PROJECT_DIR") or ""
    if not project_dir or _under_worktree(project_dir):
        return None
    return t0_project_root(project_dir)


# ── shim step 2: which code runs ─────────────────────────────────────────────


def registered_fabric_root(env: Mapping[str, str]) -> Optional[str]:
    """The real path of the first ``vnx-dev`` entry in ``$HOME/.vnx/projects.json``.

    None when ``HOME`` is unset or empty, the registry is missing, not a regular file (a FIFO
    would block the hook), unreadable or not the documented shape, or that entry's ``path`` is
    not an absolute string. The shim's inline ``python3 -I -S`` reader decides the same way.
    """
    home = env.get("HOME") or ""
    if not home:
        return None
    registry = os.path.join(home, ".vnx", "projects.json")
    if not os.path.isfile(registry):
        return None
    try:
        with open(registry, encoding="utf-8") as fh:
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


def shim_entry_file(root: Union[str, Path], entry: str, env: Mapping[str, str], *,
                    user_shim: Union[str, Path, None] = None) -> Path:
    """The file the shim runs for ``entry`` at a guarded ``root`` (the shim's step 2).

    ``user_shim`` is the shim path a user-scope call was started as (its ``$0``): such a call
    runs the entry beside it and ignores ``VNX_HOME``. Without it the call is project scope."""
    own = Path(root) / "scripts" / "hooks" / entry
    if own.is_file() and is_fabric_root(root, env):
        return own
    if user_shim is not None:
        return Path(user_shim).parent / entry
    vnx_home = env.get("VNX_HOME") or f"{env.get('HOME', '')}/.vnx-system/current"
    return Path(vnx_home) / "scripts" / "hooks" / entry


# ── trust: does Claude Code run this project's hooks at all ─────────────────


def trust_file(env: Mapping[str, str]) -> Optional[Path]:
    """The global config file Claude Code records trust decisions in (measured on 2.1.301,
    ``v_``/``b``): ``<config home>/.config.json`` when it is a non-empty file (or a writable
    empty one while the other is absent), else ``.claude.json`` in ``CLAUDE_CONFIG_DIR`` or
    ``HOME``, named ``.claude-custom-oauth.json`` when ``CLAUDE_CODE_CUSTOM_OAUTH_URL`` is set.
    The config home is ``CLAUDE_CONFIG_DIR`` or ``$HOME/.claude``. None without either."""
    config_dir = env.get("CLAUDE_CONFIG_DIR") or ""
    home = env.get("HOME") or ""
    base = config_dir or home
    if not base:
        return None
    suffix = "-custom-oauth" if env.get("CLAUDE_CODE_CUSTOM_OAUTH_URL") else ""
    current = Path(base) / f".claude{suffix}.json"
    legacy = Path(config_dir or os.path.join(home, ".claude")) / ".config.json"
    try:
        info = legacy.stat()
    except OSError:
        return current
    writable_and_alone = bool(info.st_mode & 0o222) and not current.exists()
    if stat.S_ISREG(info.st_mode) and (info.st_size > 0 or writable_and_alone):
        return legacy
    return current


def _trust_levels(project_dir: str) -> Iterator[str]:
    """The folders whose trust covers ``project_dir``: itself and its parents, up to and
    including the nearest one holding a ``.git`` entry, or up to ``/`` outside a repository."""
    level = os.path.normpath(os.path.abspath(project_dir))
    while True:
        yield level
        if os.path.exists(os.path.join(level, ".git")):
            return
        parent = os.path.dirname(level)
        if parent == level:
            return
        level = parent


def project_trusted(project_dir: Union[str, Path], env: Mapping[str, str]) -> Optional[bool]:
    """Whether Claude Code recorded trust for ``project_dir`` (see TRUST in the module
    docstring); None when the trust file cannot be read."""
    path = trust_file(env)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    projects = data.get("projects", {}) if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return None
    for level in _trust_levels(str(project_dir)):
        for key in {level, unicodedata.normalize("NFC", level)}:
            record = projects.get(key)
            if isinstance(record, dict) and record.get("hasTrustDialogAccepted") is True:
                return True
    return False


# ── reading a hook command the way its shell does ───────────────────────────

# How a piece of a word was written: single quotes or a backslash escape (literal), double
# quotes (expanded, not split), or bare (expanded and split).
_LITERAL, _DQUOTE, _BARE = "'", '"', ""
_MAX_DEPTH = 3
_CLOSERS = frozenset({"}", "fi", "done", "esac"})
# What the shell splits a bare expansion on, or expands as a pathname pattern.
_SPLIT_RE = re.compile(r"[\s*?\[]")
_GLOB_RE = re.compile(r"[*?\[]")


class _Unreadable(Exception):
    """The command uses shell syntax this reader does not model."""


@dataclass(frozen=True)
class _Word:
    pieces: Tuple[Tuple[str, str], ...]

    @property
    def text(self) -> str:
        return "".join(text for text, _ in self.pieces)

    @property
    def expands(self) -> bool:
        """The shell would act on a ``$`` or backtick in it."""
        return any(kind != _LITERAL and ("$" in text or "`" in text)
                   for text, kind in self.pieces)

    def literal(self) -> Optional[str]:
        """The word's text when the shell would take it as written, else None."""
        return None if self.expands else self.text


@dataclass
class _Simple:
    """One simple command: its words, its redirections (operator with fd, e.g. ``2>``), and
    whether it is part of a pipe, runs in the background or has its status negated."""

    words: List[_Word] = field(default_factory=list)
    redirects: List[str] = field(default_factory=list)
    piped: bool = False
    background: bool = False


class _Reader:
    """Splits shell source into simple commands (see the module docstring for what it models)."""

    def __init__(self, source: str) -> None:
        self.src = source
        self.i = 0
        self.commands: List[_Simple] = []
        self.current = _Simple()
        self.pieces: List[Tuple[str, str]] = []
        self.bare = ""
        self.started = False
        self.redirect: Optional[str] = None
        self.pipe_next = False

    def read(self) -> List[_Simple]:
        while self.i < len(self.src):
            self._step(self.src[self.i])
        self._end_command()
        return self.commands

    def _step(self, ch: str) -> None:
        if ch in " \t":
            self._end_word()
            self.i += 1
        elif ch == "#" and not self.started:
            end = self.src.find("\n", self.i)
            self.i = len(self.src) if end == -1 else end
        elif ch == "'":
            end = self.src.find("'", self.i + 1)
            if end == -1:
                raise _Unreadable("unterminated single quote")
            self._piece(self.src[self.i + 1:end], _LITERAL)
            self.i = end + 1
        elif ch == '"':
            self._double_quoted()
        elif ch == "\\":
            self._escaped()
        elif ch in "$`":
            self.bare += self._expansion()
            self.started = True
        elif ch in ";&|()\n<>":
            self._operator(ch)
        else:
            self.bare += ch
            self.started = True
            self.i += 1

    def _piece(self, text: str, kind: str) -> None:
        if self.bare:
            self.pieces.append((self.bare, _BARE))
            self.bare = ""
        self.pieces.append((text, kind))
        self.started = True

    def _escaped(self) -> None:
        nxt = self.src[self.i + 1:self.i + 2]
        if nxt != "\n":
            self._piece(nxt, _LITERAL)
        self.i += 2

    def _double_quoted(self) -> None:
        self.i += 1
        text = ""
        while True:
            if self.i >= len(self.src):
                raise _Unreadable("unterminated double quote")
            ch = self.src[self.i]
            if ch == '"':
                self.i += 1
                break
            if ch == "\\" and self.src[self.i + 1:self.i + 2] in ('$', '`', '"', "\\", "\n"):
                if text:
                    self._piece(text, _DQUOTE)
                    text = ""
                if self.src[self.i + 1] != "\n":
                    self._piece(self.src[self.i + 1], _LITERAL)
                self.i += 2
            elif ch in "$`":
                text += self._expansion()
            else:
                text += ch
                self.i += 1
        self._piece(text, _DQUOTE)

    def _expansion(self) -> str:
        """``$( )``, ``${ }``, a backtick span or a plain ``$``, as written."""
        start = self.i
        if self.src.startswith("$(", start):
            self.i = _skip_parens(self.src, start + 1)
        elif self.src.startswith("${", start):
            end = self.src.find("}", start)
            if end == -1:
                raise _Unreadable("unterminated ${")
            self.i = end + 1
        elif self.src[start] == "`":
            end = self.src.find("`", start + 1)
            if end == -1:
                raise _Unreadable("unterminated backtick")
            self.i = end + 1
        else:
            self.i = start + 1
        return self.src[start:self.i]

    def _operator(self, ch: str) -> None:
        two = self.src[self.i:self.i + 2]
        if ch in "<>":
            self._redirection()
            return
        self._end_word()
        if two == ";;":
            raise _Unreadable("case is not modelled")
        if two == "&>":
            self.i += 1
            self._redirection(prefix="&")
            return
        if two in ("&&", "||"):
            self.i += 2
        elif ch in "&|":
            if not self.current.words:
                raise _Unreadable("a pipe or background job around a group is not modelled")
            if ch == "&":
                self.current.background = True
            else:
                self.current.piped = self.pipe_next = True
            self.i += 2 if two == "|&" else 1
        else:
            if ch == "(" and (self.current.words or self.current.piped):
                raise _Unreadable("a function definition or a piped group is not modelled")
            self.i += 1
        self._end_command()

    def _redirection(self, prefix: str = "") -> None:
        fd = ""
        if not prefix and self.started and not self.pieces and self.bare.isdigit():
            fd, self.bare, self.started = self.bare, "", False
        self._end_word()
        match = re.match(r"<<-?|<<<|<>|<&|>>|>&|>\||[<>]", self.src[self.i:])
        op = match.group(0) if match else self.src[self.i]
        if op.startswith("<<") and op != "<<<":
            raise _Unreadable("heredoc is not modelled")
        self.i += len(op)
        self.redirect = prefix + fd + op

    def _end_word(self) -> None:
        if self.bare:
            self.pieces.append((self.bare, _BARE))
        if self.started:
            word = _Word(tuple(self.pieces))
            if self.redirect is not None:
                self.current.redirects.append(self.redirect)
                self.redirect = None
            else:
                self.current.words.append(word)
        self.pieces, self.bare, self.started = [], "", False

    def _end_command(self) -> None:
        """Close the simple command. A redirection, pipe or ``&`` that applies to a whole group
        (after ``)``, ``}``, ``fi``, ``done`` or ``esac``) would reach the commands inside it,
        which this reader does not track, so it makes the command unreadable."""
        self._end_word()
        cmd = self.current
        if self.redirect is not None:
            raise _Unreadable("redirection without a target")
        if cmd.redirects and not cmd.words:
            raise _Unreadable("a redirection of a group is not modelled")
        closes_group = bool(cmd.words) and cmd.words[0].literal() in _CLOSERS
        if closes_group and (cmd.redirects or cmd.piped or cmd.background):
            raise _Unreadable("a redirection of a group is not modelled")
        if cmd.words:
            self.commands.append(cmd)
        self.current = _Simple(piped=self.pipe_next)
        self.pipe_next = False


def _skip_parens(src: str, open_at: int) -> int:
    """The index after the ``)`` matching the ``(`` at ``open_at``, across nested quotes."""
    depth, i = 0, open_at
    while i < len(src):
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            end = src.find("'", i + 1)
            if end == -1:
                break
            i = end + 1
            continue
        if ch == '"':
            i = _skip_double(src, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise _Unreadable("unterminated $(")


def _skip_double(src: str, open_at: int) -> int:
    i = open_at + 1
    while i < len(src):
        if src[i] == "\\":
            i += 2
        elif src[i] == '"':
            return i + 1
        elif src.startswith("$(", i):
            i = _skip_parens(src, i + 1)
        else:
            i += 1
    raise _Unreadable("unterminated double quote")


def _read_commands(source: str) -> List[_Simple]:
    return _Reader(source).read()


# ── the direct form: does the command run the entry at project scope ────────

_KEYWORDS = frozenset({"if", "then", "else", "elif", "{"})
_ENV_CHANGERS = frozenset({".", "source", "eval"})
_DIR_CHANGERS = frozenset({"cd", "pushd", "popd"})
_WRAPPERS = frozenset({"exec", "command"})
_KEPT_REDIRECTS = frozenset({"2>", "2>>"})
_PYTHON_RE = re.compile(r"python(?:3(?:\.\d+)?)?")
_PYTHON_FLAGS_RE = re.compile(r"-[BEIPSOqsu]+")
_SHELL_FLAGS_RE = re.compile(r"-[cefluvx]+")
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SCOPE_RE = re.compile(r"VNX_HOOK_SCOPE(?!=project(?![\w.-]))")
_GIT_TOPLEVEL_RE = re.compile(r"\$\(\s*git\s+rev-parse\s+--show-toplevel\b[^()]*\)")


@dataclass
class _Scope:
    """What the reader knows at one level of the command: assigned variables (None when the
    value cannot be known), the subset an inner ``sh -c`` inherits, and whether a ``cd`` ran."""

    known: Dict[str, Optional[str]] = field(default_factory=dict)
    exported: Dict[str, Optional[str]] = field(default_factory=dict)
    moved: bool = False


def _direct_covers(command: str, entry: str, project_dir: Path, env: Mapping[str, str]) -> bool:
    if _SCOPE_RE.search(command):
        return False
    return _runs_entry(command, entry, project_dir, env, {}, 0)


def _runs_entry(source: str, entry: str, project_dir: Path, env: Mapping[str, str],
                inherited: Dict[str, Optional[str]], depth: int) -> bool:
    if depth > _MAX_DEPTH:
        return False
    try:
        commands = _read_commands(source)
    except _Unreadable:
        return False
    scope = _Scope(known=dict(inherited))
    for cmd in commands:
        words = _drop_keywords(cmd.words)
        head = words[0].literal() if words else None
        if head in _ENV_CHANGERS:
            return False
        if not words or _records(words, head, scope, project_dir):
            continue
        if cmd.piped or cmd.background or not set(cmd.redirects) <= _KEPT_REDIRECTS:
            continue
        if _invokes(words, entry, project_dir, env, scope, depth):
            return True
    return False


def _drop_keywords(words: List[_Word]) -> List[_Word]:
    index = 0
    while index < len(words) and words[index].literal() in _KEYWORDS:
        index += 1
    return words[index:]


def _assignment(word: _Word, project_dir: Path) -> Optional[Tuple[str, Optional[str]]]:
    """(name, value) when ``word`` is ``NAME=value``: a plain value as written, the git
    toplevel form as ``project_dir``, None for any other computed value."""
    first, kind = word.pieces[0] if word.pieces else ("", _LITERAL)
    match = _ASSIGN_RE.match(first) if kind == _BARE else None
    if match is None:
        return None
    name = match.group(0)[:-1]
    value = _Word(((first[match.end():], _BARE),) + word.pieces[1:])
    if not value.expands:
        return name, value.text
    return name, str(project_dir) if _GIT_TOPLEVEL_RE.fullmatch(value.text) else None


def _records(words: List[_Word], head: Optional[str], scope: _Scope, project_dir: Path) -> bool:
    """Apply an assignment-only, ``export`` or ``cd`` statement to ``scope``; True when the
    statement was one of them and starts nothing."""
    if head in _DIR_CHANGERS:
        scope.moved = True
        return True
    exporting = head in ("export", "readonly")
    pairs = [_assignment(word, project_dir) for word in (words[1:] if exporting else words)]
    if not exporting and None in pairs:
        return False
    for pair in filter(None, pairs):
        scope.known[pair[0]] = pair[1]
        if exporting:
            scope.exported[pair[0]] = pair[1]
    return True


def _invokes(words: List[_Word], entry: str, project_dir: Path, env: Mapping[str, str],
             scope: _Scope, depth: int) -> bool:
    prefix: Dict[str, Optional[str]] = {}
    while words and (pair := _assignment(words[0], project_dir)) is not None:
        prefix[pair[0]] = pair[1]
        words = words[1:]
    words = _unwrap(words, project_dir)
    head = words[0].literal() if words else None
    if head is None:
        return False
    name = _basename(head)
    if _PYTHON_RE.fullmatch(name):
        script = _script_operand(words[1:])
        return script is not None and _entry_path(script, entry, project_dir, env, scope)
    if name in _SHELLS:
        source = _shell_source(words[1:])
        inner = {**scope.exported, **prefix}
        return source is not None and _runs_entry(source, entry, project_dir, env, inner,
                                                  depth + 1)
    return name == entry and _entry_path(words[0], entry, project_dir, env, scope,
                                         executable=True)


def _unwrap(words: List[_Word], project_dir: Path) -> List[_Word]:
    """Drop ``exec``, ``command`` and ``env`` with its assignments; ``env`` with an option
    leaves nothing to run."""
    while words:
        head = words[0].literal()
        if head in _WRAPPERS:
            words = words[1:]
        elif head is not None and _basename(head) == "env":
            words = words[1:]
            while words and _assignment(words[0], project_dir) is not None:
                words = words[1:]
            if words and (words[0].literal() or "").startswith("-"):
                return []
        else:
            return words
    return words


def _script_operand(args: List[_Word]) -> Optional[_Word]:
    """The script python runs, after flags that take no value; None for ``-c``, ``-m`` and
    anything else that is not a script."""
    for index, word in enumerate(args):
        text = word.literal()
        if text == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if text is not None and text.startswith("-"):
            if not _PYTHON_FLAGS_RE.fullmatch(text):
                return None
            continue
        return word
    return None


def _shell_source(args: List[_Word]) -> Optional[str]:
    """The source of ``sh -c '<source>'``; None for a script file or an unmodelled option."""
    with_c = False
    for word in args:
        text = word.literal()
        if text is None:
            return None
        if text.startswith("-"):
            if not _SHELL_FLAGS_RE.fullmatch(text):
                return None
            with_c = with_c or "c" in text
            continue
        return text if with_c else None
    return None


def _entry_path(word: _Word, entry: str, project_dir: Path, env: Mapping[str, str],
                scope: _Scope, *, executable: bool = False) -> bool:
    if _basename(word.text) != entry:
        return False
    text = _expanded(word, project_dir, env, scope.known)
    if text is None or (scope.moved and not text.startswith("/")):
        return False
    path = Path(text) if text.startswith("/") else project_dir / text
    return path.is_file() and (not executable or os.access(path, os.X_OK))


def _expanded(word: _Word, project_dir: Path, env: Mapping[str, str],
              known: Mapping[str, Optional[str]]) -> Optional[str]:
    """The word after the expansions the shell applies, or None when that cannot be known
    without running a shell."""
    out = []
    for index, (text, kind) in enumerate(word.pieces):
        if kind == _LITERAL:
            if "$" in text or "`" in text:
                return None
            out.append(text)
            continue
        if "`" in text or "$(" in text:
            return None
        if index == 0 and kind == _BARE and text.startswith("~"):
            if not (text == "~" or text.startswith("~/")) or not env.get("HOME"):
                return None
            text = env["HOME"] + text[1:]
        value = _substitute(text, project_dir, env, known)
        if value is None or "$" in value or (kind == _BARE and _SPLIT_RE.search(value)):
            return None
        out.append(value)
    return "".join(out)


def _substitute(text: str, project_dir: Path, env: Mapping[str, str],
                known: Mapping[str, Optional[str]]) -> Optional[str]:
    def value(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        if name == "CLAUDE_PROJECT_DIR":
            return str(project_dir)
        found = known.get(name) if name in known else env.get(name)
        if found is None:
            raise KeyError(name)
        return found

    try:
        return _VAR_RE.sub(value, text)
    except KeyError:
        return None


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


@dataclass(frozen=True)
class _ShimCall:
    shim: _Word
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
    """The call in exactly the form ``registered_command`` renders, else None. The shim path
    sits in double quotes inside ``sh -c '...'``, where the inner shell expands it."""
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
    if rendered != command:
        return None
    return _ShimCall(_Word(((match["shim"], _DQUOTE),)), scope, entry, False)


def _plain_shim_call(command: str) -> Optional[_ShimCall]:
    """``[sh|bash|dash] <path>/vnx_hook_shim.sh <args>`` and nothing else, else None."""
    try:
        commands = _read_commands(command)
    except _Unreadable:
        return None
    if len(commands) != 1:
        return None
    cmd = commands[0]
    if cmd.redirects or cmd.piped or cmd.background or not cmd.words:
        return None
    words = cmd.words
    needs_exec = not (words[0].literal() is not None
                      and _basename(words[0].text) in _SHIM_SHELLS)
    if not needs_exec:
        words = words[1:]
    if not words or _basename(words[0].text) != SHIM_NAME:
        return None
    args = [word.literal() for word in words[1:]]
    parsed = None if None in args else _shim_args(args)
    return None if parsed is None else _ShimCall(words[0], parsed[0], parsed[1], needs_exec)


def _shim_path(word: _Word, project_dir: Path, env: Mapping[str, str]) -> Optional[Path]:
    """The shim path with a leading $HOME or $CLAUDE_PROJECT_DIR expanded where the shell
    expands it, never through a shell; None for anything else a shell would still have to
    expand, or a bare expansion it would split."""
    text = word.text
    first, kind = word.pieces[0] if word.pieces else ("", _LITERAL)
    lead = _SHIM_VAR_RE.match(first) if kind != _LITERAL else None
    if lead:
        name = lead.group(1) or lead.group(2)
        value = str(project_dir) if name == "CLAUDE_PROJECT_DIR" else env.get("HOME") or ""
        if not value or (kind == _BARE and _SPLIT_RE.search(value)):
            return None
        text = value + text[lead.end():]
    if "$" in text or "`" in text or not text.startswith("/"):
        return None
    if any(kind == _BARE and _GLOB_RE.search(piece) for piece, kind in word.pieces):
        return None
    return Path(text)


def _shim_covers(command: str, entry: str, project_dir: Path, env: Mapping[str, str]) -> bool:
    """The registration starts ``entry`` at project scope through a shim that can run, and the
    entry that shim would start exists."""
    stripped = command.strip()
    call = _inline_shim_call(stripped) or _plain_shim_call(stripped)
    if call is None or call.entry != entry or call.scope != "project":
        return False
    path = _shim_path(call.shim, project_dir, env)
    if path is None or not path.is_file():
        return False
    if call.needs_exec and not os.access(path, os.X_OK):
        return False
    return shim_entry_file(project_dir, entry, env).is_file()


def command_form(command: str, entry: str, project_dir: Path,
                 env: Mapping[str, str]) -> Optional[str]:
    """FORM_SHIM or FORM_DIRECT when ``command`` covers ``entry``, else None. A command that
    names the shim is judged as a shim registration only."""
    if any(_basename(token) == SHIM_NAME for token in _TOKEN_RE.findall(command)):
        return FORM_SHIM if _shim_covers(command, entry, project_dir, env) else None
    return FORM_DIRECT if _direct_covers(command, entry, project_dir, env) else None


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


def _decides_every_time(hook: dict) -> bool:
    """A command hook that runs in the foreground through bash on every matching event."""
    if hook.get("type", "command") != "command" or not hook.get("command"):
        return False
    if any(hook.get(name, False) is not False for name in _DEFERRING_FIELDS):
        return False
    return hook.get("if") in (None, "") and "args" not in hook and (
        hook.get("shell", "bash") == "bash")


def _commands(hooks: dict, event: str, tool_name: Optional[str]) -> Iterator[str]:
    for group in hooks.get(event, []):
        if event in _TOOL_EVENTS and not matcher_matches(group.get("matcher"), tool_name or ""):
            continue
        for hook in group.get("hooks", []):
            if _decides_every_time(hook):
                yield hook["command"]


def _covering_registration(base: Path, event: str, tool_name: Optional[str], entry: str,
                           env: Mapping[str, str]) -> Tuple[Optional[Tuple[str, Path]], bool]:
    """((form, source) of the first covering registration or None, whether a file failed to
    load)."""
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
            form = command_form(command, entry, base, env)
            if form is not None:
                return (form, path), unloadable
    return None, unloadable


def coverage(project_dir: Union[str, Path, None], event: str, tool_name: Optional[str] = None,
             *, entry: str = GUARD_ENTRY, env: Optional[Mapping[str, str]] = None) -> Coverage:
    """Whether the project at ``project_dir`` runs ``entry`` itself for ``event`` (and, for a
    tool event, ``tool_name``). See the module docstring for the rules."""
    if not project_dir:
        return Coverage(UNKNOWN)
    base = Path(project_dir)
    mapping = os.environ if env is None else env
    found, unloadable = _covering_registration(base, event, tool_name, entry, mapping)
    if found is None:
        return Coverage(UNKNOWN if unloadable else MISSING)
    trusted = project_trusted(base, mapping)
    if trusted is None:
        return Coverage(UNKNOWN)
    return Coverage(COVERED, *found) if trusted else Coverage(MISSING)
