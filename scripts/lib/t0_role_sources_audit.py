#!/usr/bin/env python3
"""Audit the sources the T0 role names against the repo.

`t0_role_audit.sh --static` already checks role<->skill invocability. This
module adds the second half: every script, subcommand and state file the role
(and DISPATCH_RULES) name must still exist in the fabric. Without it a PR can
delete a `receipt_query.py` subcommand or a t0_state section and the role keeps
instructing T0 to use it.

Findings (one per line on stdout, exit 1 when any):

  SCRIPT-MISSING       a `*.py` / `*.sh` the text names does not resolve
  SCRIPT-OUTSIDE-FABRIC  the name is an absolute path, or it only resolves
                       (via `..` or a symlink) to a file outside the fabric
  SUBCOMMAND-MISSING   `vnx <cmd> [<sub>]` / `bin/vnx <cmd> [<sub>]` (inline, or
                       a command line inside a fenced block) or
                       `<script>.py <sub>` names a word the code that handles
                       that level does not dispatch on (see "Where a command
                       is really dispatched" below)
  STATE-UNWRITTEN      the role or DISPATCH_RULES names a path in the project
                       state directory that is not in the writers manifest
  STATE-WRITER-GONE    a manifest entry whose writer no longer holds its
                       marker, or whose section phrase left the role
  MANIFEST-BAD-LINE    a manifest line that is not `target | writer | marker | phrase`

Usage: t0_role_sources_audit.py <project_root> <fabric_root>

`fabric_root` is where scripts/, bin/vnx and the manifest live. For the fabric
repo itself that is the project root; a consumer project passes the fabric it
runs on, because its own tree carries no scripts/.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

MANIFEST_REL = "scripts/lib/t0_role_state_writers.txt"
ROLE_REL = ".claude/terminals/T0/role-orchestrator.md"
DOC_SOURCES = ("docs/core/DISPATCH_RULES.md",)

# `vnx <group> <sub>`: the second word is a real subcommand only for these.
# Elsewhere it is prose ("vnx dispatch the work").
VNX_GROUPS = frozenset({"objective", "horizon", "deliverable", "role", "pool", "skills", "runtime"})

SCRIPT_TOKEN = re.compile(r"(?<![\w$/.~-])([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh))\b")
# An absolute path; the `:` keeps the `//host/...` of a URL out.
ABS_SCRIPT_TOKEN = re.compile(r"(?<![\w$/.~:-])(/[A-Za-z0-9_./-]+\.(?:py|sh))\b")
PY_SUBCOMMAND = re.compile(r"(scripts/[A-Za-z0-9_./-]+\.py)[ \t]+([a-z][a-z0-9_-]*)\b")
VNX_COMMAND = re.compile(r"`vnx ([a-z][a-z0-9_-]*)(?: ([a-z][a-z0-9_-]*))?")
VNX_FENCED_LINE = re.compile(r"^[ \t]*(?:\$[ \t]+)?vnx ([a-z][a-z0-9_-]*)(?: ([a-z][a-z0-9_-]*))?")
BIN_VNX_COMMAND = re.compile(r"`(?:\./)?bin/vnx ([a-z][a-z0-9_-]*)(?: ([a-z][a-z0-9_-]*))?")
BIN_VNX_FENCED_LINE = re.compile(r"^[ \t]*(?:\$[ \t]+)?(?:\./)?bin/vnx ([a-z][a-z0-9_-]*)(?: ([a-z][a-z0-9_-]*))?")
FENCE = re.compile(r"^[ \t]*(```|~~~)")
# Built from parts: the CI legacy-path gate greps the scripts tree for the
# literal spelling of this directory.
STATE_DIR = ".vnx-data" + "/state/"
STATE_PATH = re.compile(re.escape(STATE_DIR) + r"([A-Za-z0-9_./-]+)")
SCRIPT_DIRS = ("scripts", "hooks", ".claude/hooks")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _inside(fabric: Path, cand: Path) -> bool:
    """`cand` resolves (symlinks and `..` followed) to a file under `fabric`."""
    try:
        return cand.resolve().is_relative_to(fabric.resolve())
    except (OSError, RuntimeError):
        return False


def _fabric_text(fabric: Path, rel: str) -> str:
    """Text of `fabric/rel`, or "" when that is no file inside the fabric."""
    path = fabric / rel
    return _read(path) if path.is_file() and _inside(fabric, path) else ""


def _script_index(fabric: Path) -> Tuple[Dict[str, Path], Set[str]]:
    """Return ({basename: first path inside the fabric}, basenames that only
    exist as a link out of the fabric) for every script under SCRIPT_DIRS."""
    inside: Dict[str, Path] = {}
    outside: Set[str] = set()
    for d in SCRIPT_DIRS:
        base = fabric / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.suffix not in (".py", ".sh") or not p.is_file():
                continue
            if _inside(fabric, p):
                inside.setdefault(p.name, p)
            else:
                outside.add(p.name)
    return inside, outside - set(inside)


def _resolve_script(fabric: Path, token: str, index: Tuple[Dict[str, Path], Set[str]]) -> Tuple[Optional[Path], bool]:
    """(the file `token` names inside the fabric, whether it only resolves outside).

    A token is joined to the fabric root, so `..` or a symlink could land on a
    file elsewhere; that file never counts as the fabric's own. An absolute
    token is outside by definition: it is bound to one machine.
    """
    if token.startswith("/"):
        return None, True
    outside = False
    for prefix in ("", "scripts", ".claude"):
        cand = fabric / prefix / token
        if cand.is_file():
            if _inside(fabric, cand):
                return cand, False
            outside = True
    if "/" not in token:
        names, links_out = index
        if token in names:
            return names[token], False
        outside = outside or token in links_out
    return None, outside


def _script_finding(source: str, token: str, outside: bool, fabric: Path) -> str:
    if outside:
        return f"SCRIPT-OUTSIDE-FABRIC: {source} names '{token}' but it resolves outside {fabric}"
    return f"SCRIPT-MISSING: {source} names '{token}' but it does not exist under {fabric}"


# ---------------------------------------------------------------------------
# Where a command is really dispatched.
#
# A word counts as a command or subcommand only at the place that decides
# which code runs for it, never because it occurs somewhere else in the same
# file. Three such places exist:
#
#   shell   a `case` statement on the argument the function received first
#           (or a `[ "$arg" = "word" ]` test on it), in exactly the function
#           that is called. For bin/vnx that is `main()`: the last line runs
#           `main "$@"`, and its `case "$cmd"` is what executes a command or
#           prints "Unknown command". The usage() text is prose that can drift;
#           a `scripts/commands/<name>.sh` file is only sourced by
#           `_load_command` and still needs that case branch to run.
#   python  the argparse tree: `add_parser()` on the subparsers of the parser
#           that handles the level above, followed through local helper
#           calls. A script without argparse dispatches on `argv[0|1] == "w"`.
#   entry   there are two `vnx` entrances (the role says so itself): bin/vnx
#           and the pip CLI `vnx_cli/main.py`. `vnx <cmd>` is real when one of
#           them dispatches it; `bin/vnx <cmd>` only when bin/vnx does.
# ---------------------------------------------------------------------------

PIP_CLI_REL = "vnx_cli/main.py"
BIN_VNX_REL = "bin/vnx"

_HEREDOC = re.compile(r"<<-?[ \t]*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
_CASE_OPEN = re.compile(r"^[ \t]*case[ \t]+(\S+)[ \t]+in\b(.*)$")
_CASE_LABEL = re.compile(r"^[ \t]*\(?[ \t]*([^()#\s][^()]*?)[ \t]*\)(.*)$")
_FIRST_ARG_VAR = re.compile(r"(?m)^[ \t]*(?:local[ \t]+)?([A-Za-z_]\w*)=[\"']?\$\{?1\b")
_BRANCH_END = re.compile(r";;&?[ \t]*$|;&[ \t]*$")


def _strip_heredocs(text: str) -> List[str]:
    """Lines of `text` with heredoc bodies left out: their `case`/`esac`/`;;`
    are text for a reader, not shell structure."""
    out: List[str] = []
    delim: Optional[str] = None
    for line in text.splitlines():
        if delim is not None:
            if line.strip() == delim:
                delim = None
            continue
        out.append(line)
        m = _HEREDOC.search(line)
        if m and not line.lstrip().startswith("#"):
            delim = m.group(1)
    return out


def _parse_case(lines: List[str], i: int) -> Tuple[List[Tuple[List[str], str]], int]:
    """Branches of the case statement whose `case ... in` line is `lines[i-1]`.

    Returns ([(labels, body)], index after its `esac`). A nested case inside a
    branch is part of that branch's body, never a branch of this case.
    """
    branches: List[Tuple[List[str], str]] = []
    labels: Optional[List[str]] = None
    body: List[str] = []

    def close() -> None:
        nonlocal labels, body
        if labels is not None:
            branches.append((labels, "\n".join(body)))
        labels, body = None, []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        if labels is None:
            if re.match(r"esac\b", stripped):
                return branches, i
            m = _CASE_LABEL.match(line)
            if not m:
                continue
            labels = [w.strip().strip("\"'") for w in m.group(1).split("|")]
            rest = m.group(2)
            if _BRANCH_END.search(rest.strip()):
                body = [_BRANCH_END.sub("", rest)]
                close()
            else:
                body = [rest]
            continue
        nested = _CASE_OPEN.match(line)
        if nested and not re.search(r"\besac\b", nested.group(2)):
            _, j = _parse_case(lines, i)
            body.extend(lines[i - 1: j])
            i = j
            continue
        if _BRANCH_END.search(stripped):
            body.append(_BRANCH_END.sub("", line))
            close()
            continue
        if re.match(r"esac\b", stripped):
            close()
            return branches, i
        body.append(line)
    close()
    return branches, i


def _case_statements(text: str) -> List[Tuple[str, List[Tuple[List[str], str]]]]:
    """(subject, branches) for every case statement not nested in another."""
    lines = _strip_heredocs(text)
    out: List[Tuple[str, List[Tuple[List[str], str]]]] = []
    i = 0
    while i < len(lines):
        m = _CASE_OPEN.match(lines[i])
        i += 1
        if m and not re.search(r"\besac\b", m.group(2)):
            branches, i = _parse_case(lines, i)
            out.append((m.group(1), branches))
    return out


def _shell_function(text: str, name: str) -> Optional[str]:
    """Body of the shell function `name` in `text`, or None."""
    m = re.search(
        r"(?m)^([ \t]*)(?:function[ \t]+)?%s[ \t]*\(\)[ \t]*\{(.*)$" % re.escape(name), text
    )
    if not m:
        return None
    same_line = m.group(2).rstrip()
    if same_line.endswith("}"):
        return same_line[:-1]
    end = re.search(r"(?m)^%s\}[ \t]*$" % re.escape(m.group(1)), text[m.end():])
    return text[m.end(): m.end() + end.start()] if end else text[m.end():]


def _arg_names(func_body: str) -> Set[str]:
    """Names under which a function reads the argument it dispatches on.

    When it copies `$1` into a variable, only that variable: a later `$1`
    comes after a `shift` and is the next word, not this one.
    """
    named = set(_FIRST_ARG_VAR.findall(func_body))
    return named or {"1"}


def _subject_name(subject: str) -> str:
    s = subject.strip("\"'")
    m = re.fullmatch(r"\$\{?([A-Za-z_]\w*|\d)(?::?[-=?+][^}]*)?\}?", s)
    return m.group(1) if m else ""


def _shell_dispatch(func_body: str) -> Dict[str, str]:
    """{word: branch body} the function dispatches on its first argument."""
    names = _arg_names(func_body)
    words: Dict[str, str] = {}
    for subject, branches in _case_statements(func_body):
        if _subject_name(subject) not in names:
            continue
        for labels, body in branches:
            for label in labels:
                if re.fullmatch(r"[A-Za-z0-9][\w-]*", label):
                    words.setdefault(label, body)
    for name in names:
        ref = r"\$\{?%s(?::-[^}]*)?\}?" % re.escape(name)
        for m in re.finditer(r"""["']?%s["']?[ \t]*==?[ \t]*["']([A-Za-z0-9][\w-]*)["']""" % ref, func_body):
            words.setdefault(m.group(1), "")
    return words


def _bin_vnx_main(fabric: Path) -> Dict[str, str]:
    """{command: branch body} of the case in bin/vnx `main()` on its first argument."""
    body = _shell_function(_fabric_text(fabric, BIN_VNX_REL), "main")
    return _shell_dispatch(body) if body else {}


# --- argparse tree --------------------------------------------------------


class _Parser:
    """One argparse level: the names it dispatches to the next level."""

    def __init__(self, origin: Tuple[str, str]) -> None:
        self.origin = origin  # (module, function) that built it
        self.children: Dict[str, "_Parser"] = {}
        self.parsed = False  # parse_args() runs on it


class _ArgparseWalk:
    """Static walk of a module's argparse construction, via `ast`.

    Follows `add_subparsers()` / `add_parser(name, aliases=[...])` on the
    objects they return, through assignments and calls into functions: of the
    same module, or imported (`from x import f`) from a module inside the
    fabric (the `_register_x(subparsers)` pattern). Nothing is executed.
    """

    MAX_DEPTH = 12
    MAIN = "<main>"
    PARSE_CALLS = frozenset({"parse_args", "parse_known_args", "parse_intermixed_args"})

    def __init__(self, tree: "ast.Module", resolve: Callable[[str], Optional["ast.Module"]]) -> None:
        self.roots: List[_Parser] = []
        self.argv_words: Set[str] = set()
        self._resolve = resolve
        self._mods: Dict[str, Tuple[Dict[str, "ast.FunctionDef"], Dict[str, Tuple[str, str]]]] = {}
        self._stack: List[Tuple[str, str]] = []
        self._register(self.MAIN, tree)
        self._stmts(tree.body, {}, (self.MAIN, "<module>"))
        for name, fn in self._mods[self.MAIN][0].items():
            self._call((self.MAIN, name), fn, [], {})

    def _register(self, key: str, tree: Optional["ast.Module"]) -> None:
        funcs: Dict[str, "ast.FunctionDef"] = {}
        imports: Dict[str, Tuple[str, str]] = {}
        if tree is not None:
            funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.module and not n.level:
                    for a in n.names:
                        imports[a.asname or a.name] = (n.module, a.name)
        self._mods[key] = (funcs, imports)

    def _lookup(self, key: str, name: str) -> Optional[Tuple[str, "ast.FunctionDef"]]:
        funcs, imports = self._mods[key]
        if name in funcs:
            return key, funcs[name]
        if name not in imports:
            return None
        mod, attr = imports[name]
        if mod not in self._mods:
            self._register(mod, self._resolve(mod))
        fn = self._mods[mod][0].get(attr)
        return (mod, fn) if fn is not None else None

    def _call(self, where: Tuple[str, str], fn: "ast.FunctionDef", args: List[object], kwargs: Dict[str, object]) -> object:
        if where in self._stack or len(self._stack) >= self.MAX_DEPTH:
            return None
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        env: Dict[str, object] = {}
        for p, v in zip(params, args):
            env[p] = v
        for k, v in kwargs.items():
            if k in params:
                env[k] = v
        self._stack.append(where)
        try:
            return self._stmts(fn.body, env, where)
        finally:
            self._stack.pop()

    def _stmts(self, stmts: List["ast.stmt"], env: Dict[str, object], where: Tuple[str, str]) -> object:
        returned: object = None
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(st, ast.Assign):
                val = self._expr(st.value, env, where)
                for t in st.targets:
                    if isinstance(t, ast.Name):
                        env[t.id] = val
            elif isinstance(st, ast.AnnAssign) and st.value is not None:
                val = self._expr(st.value, env, where)
                if isinstance(st.target, ast.Name):
                    env[st.target.id] = val
            elif isinstance(st, ast.Return) and st.value is not None:
                val = self._expr(st.value, env, where)
                returned = returned if returned is not None else val
            else:
                for child in ast.iter_child_nodes(st):
                    if isinstance(child, ast.expr):
                        self._expr(child, env, where)
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(st, field, None)
                    if isinstance(sub, list):
                        r = self._stmts(sub, env, where)
                        returned = returned if returned is not None else r
                for handler in getattr(st, "handlers", []) or []:
                    self._stmts(handler.body, env, where)
                for case in getattr(st, "cases", []) or []:
                    self._stmts(case.body, env, where)
        return returned

    def _expr(self, node: "ast.expr", env: Dict[str, object], where: Tuple[str, str]) -> object:
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.Compare):
            self._argv_compare(node)
        if not isinstance(node, ast.Call):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    self._expr(child, env, where)
            return None
        func = node.func
        args = [self._expr(a, env, where) for a in node.args]
        kwargs = {k.arg: self._expr(k.value, env, where) for k in node.keywords if k.arg}
        if isinstance(func, ast.Attribute):
            recv = self._expr(func.value, env, where)
            if func.attr == "add_subparsers" and isinstance(recv, _Parser):
                return ("subs", recv)
            if func.attr == "add_parser" and isinstance(recv, tuple) and recv[0] == "subs":
                names = [a.value for a in node.args[:1] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                for k in node.keywords:
                    if k.arg == "aliases" and isinstance(k.value, (ast.List, ast.Tuple)):
                        names += [e.value for e in k.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if not names:
                    return None
                child = recv[1].children.get(names[0]) or _Parser(where)
                for n in names:
                    recv[1].children.setdefault(n, child)
                return child
            if func.attr in self.PARSE_CALLS and isinstance(recv, _Parser):
                recv.parsed = True
                return None
            if func.attr.endswith("ArgumentParser"):
                return self._root(where)
            return None
        if isinstance(func, ast.Name):
            if func.id.endswith("ArgumentParser"):
                return self._root(where)
            target = self._lookup(where[0], func.id)
            if target is not None:
                return self._call((target[0], func.id), target[1], args, kwargs)
        return None

    def _root(self, where: Tuple[str, str]) -> _Parser:
        p = _Parser(where)
        self.roots.append(p)
        return p

    def _argv_compare(self, node: "ast.Compare") -> None:
        """`argv[0] == "w"` / `sys.argv[1] in ("a", "b")`: a hand-rolled dispatch."""
        left = node.left
        if not isinstance(left, ast.Subscript):
            return
        base = left.value
        base_name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
        idx = left.slice
        if "argv" not in base_name or not (isinstance(idx, ast.Constant) and idx.value in (0, 1)):
            return
        for op, comp in zip(node.ops, node.comparators):
            if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                self.argv_words.add(comp.value)
            elif isinstance(op, ast.In) and isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                self.argv_words.update(
                    e.value for e in comp.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )


def _parse_py(path: Optional[Path]) -> Optional["ast.Module"]:
    if path is None:
        return None
    try:
        return ast.parse(_read(path))
    except (SyntaxError, ValueError):
        return None


@functools.lru_cache(maxsize=None)
def _argparse_walk(fabric: Path, path: Path) -> Optional[_ArgparseWalk]:
    tree = _parse_py(path)
    if tree is None:
        return None
    return _ArgparseWalk(tree, lambda mod: _parse_py(_module_file(fabric, mod)))


def _py_words(fabric: Path, path: Path, prefix: List[str], roots_from: Optional[str] = None) -> Set[str]:
    """Words the script at `path` dispatches on after the literal args `prefix`.

    Only a parser that `parse_args()` runs on reads the command line; a second
    parser in the same file (a helper, a test harness) does not, unless the
    walk sees no parse call at all. `roots_from` limits the tree further to
    the parsers built in that function of the script itself (the pip CLI
    builds its command parser in `main`).
    """
    walk = _argparse_walk(fabric, path)
    if walk is None:
        return set()
    wanted = (_ArgparseWalk.MAIN, roots_from)
    level = [r for r in walk.roots if roots_from is None or r.origin == wanted]
    level = [r for r in level if r.parsed] or level
    for word in prefix:
        level = [r.children[word] for r in level if word in r.children]
    words = {w for r in level for w in r.children}
    if not prefix:
        words |= walk.argv_words
    return words


# --- the handlers of one `vnx` command ------------------------------------

_BRANCH_PY = re.compile(r"(scripts/[A-Za-z0-9_./-]+\.py)[\"']?((?:[ \t]+[A-Za-z0-9][\w-]*)*)")
_BRANCH_SH = re.compile(r"(scripts/[A-Za-z0-9_./-]+\.sh)[\"']?((?:[ \t]+[A-Za-z0-9][\w-]*)*)")
_BRANCH_MOD = re.compile(r"-m[ \t]+([A-Za-z0-9_.]+)((?:[ \t]+[A-Za-z0-9][\w-]*)*)")
_BRANCH_FUNC = re.compile(r"(?m)(?:^|[;&|][ \t]*|\bthen[ \t]+)[ \t]*(cmd_\w+)\b")


def _module_file(fabric: Path, mod: str) -> Optional[Path]:
    rel = mod.replace(".", "/")
    for cand in (f"{rel}.py", f"{rel}/__main__.py", f"scripts/lib/{rel}.py", f"scripts/{rel}.py"):
        path = fabric / cand
        if path.is_file() and _inside(fabric, path):
            return path
    return None


def _shell_func_body(fabric: Path, cmd: str, func: str) -> Optional[str]:
    """Body of `func` as bin/vnx runs it for `vnx <cmd>`.

    `_load_command "$cmd"` sources `scripts/commands/<cmd>.sh` (or its
    underscore spelling) before the case runs, so a definition there wins
    over the inline one. No other command file is loaded for `cmd`.
    """
    for name in (cmd, cmd.replace("-", "_")):
        body = _shell_function(_fabric_text(fabric, f"scripts/commands/{name}.sh"), func)
        if body is not None:
            return body
    return _shell_function(_fabric_text(fabric, BIN_VNX_REL), func)


def _bin_vnx_sub_words(fabric: Path, cmd: str, branch: str) -> Set[str]:
    """Words the code in bin/vnx's `<cmd>)` branch dispatches on next."""
    words: Set[str] = set()
    for rel, lits in _BRANCH_PY.findall(branch):
        path = fabric / rel
        if path.is_file() and _inside(fabric, path):
            words |= _py_words(fabric, path, lits.split())
    for mod, lits in _BRANCH_MOD.findall(branch):
        path = _module_file(fabric, mod)
        if path is not None:
            words |= _py_words(fabric, path, lits.split())
    for rel, lits in _BRANCH_SH.findall(branch):
        if not lits.split():
            words |= set(_shell_dispatch(_fabric_text(fabric, rel)))
    for func in dict.fromkeys(_BRANCH_FUNC.findall(branch)):
        body = _shell_func_body(fabric, cmd, func)
        if body is not None:
            words |= set(_shell_dispatch(body))
    return words


def _vnx_command_exists(fabric: Path, cmd: str, entrances: Tuple[str, ...]) -> bool:
    if "bin" in entrances and cmd in _bin_vnx_main(fabric):
        return True
    pip = fabric / PIP_CLI_REL
    if "pip" in entrances and pip.is_file() and _inside(fabric, pip):
        return cmd in _py_words(fabric, pip, [], roots_from="main")
    return False


def _vnx_sub_exists(fabric: Path, cmd: str, sub: str, entrances: Tuple[str, ...]) -> bool:
    if "bin" in entrances:
        branch = _bin_vnx_main(fabric).get(cmd)
        if branch is not None and sub in _bin_vnx_sub_words(fabric, cmd, branch):
            return True
    pip = fabric / PIP_CLI_REL
    if "pip" in entrances and pip.is_file() and _inside(fabric, pip):
        return sub in _py_words(fabric, pip, [cmd], roots_from="main")
    return False


def _fenced_commands(text: str, pattern: "re.Pattern[str]") -> List[Tuple[str, str]]:
    """`<pattern>` matches at the start of a line inside a fenced block."""
    found: List[Tuple[str, str]] = []
    fence: Optional[str] = None
    for line in text.splitlines():
        m = FENCE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence is None:
            continue
        c = pattern.match(line)
        if c:
            found.append((c.group(1), c.group(2) or ""))
    return found


def check_text(text: str, source: str, fabric: Path) -> List[str]:
    findings: List[str] = []
    index = _script_index(fabric)

    for token in sorted(set(SCRIPT_TOKEN.findall(text)) | set(ABS_SCRIPT_TOKEN.findall(text))):
        script, outside = _resolve_script(fabric, token, index)
        if script is None:
            findings.append(_script_finding(source, token, outside, fabric))

    for script_rel, sub in sorted(set(PY_SUBCOMMAND.findall(text))):
        script, outside = _resolve_script(fabric, script_rel, index)
        if script is None:
            findings.append(_script_finding(source, script_rel, outside, fabric))
            continue
        if sub not in _py_words(fabric, script, []):
            findings.append(
                f"SUBCOMMAND-MISSING: {source} runs '{script_rel} {sub}' but {script_rel} defines no '{sub}'"
            )

    for label, entrances, inline, fenced in (
        ("vnx", ("bin", "pip"), VNX_COMMAND, VNX_FENCED_LINE),
        ("bin/vnx", ("bin",), BIN_VNX_COMMAND, BIN_VNX_FENCED_LINE),
    ):
        where = "bin/vnx and vnx_cli" if len(entrances) == 2 else "bin/vnx"
        for cmd, sub in sorted(set(inline.findall(text)) | set(_fenced_commands(text, fenced))):
            if not _vnx_command_exists(fabric, cmd, entrances):
                findings.append(f"SUBCOMMAND-MISSING: {source} runs '{label} {cmd}' but {where} dispatches no '{cmd}'")
                continue
            if sub and cmd in VNX_GROUPS and not _vnx_sub_exists(fabric, cmd, sub, entrances):
                findings.append(
                    f"SUBCOMMAND-MISSING: {source} runs '{label} {cmd} {sub}' but the code handling '{cmd}' defines no '{sub}'"
                )
    return findings


def _manifest(fabric: Path) -> Tuple[List[Tuple[str, str, str, str]], List[str]]:
    rows: List[Tuple[str, str, str, str]] = []
    bad: List[str] = []
    for lineno, raw in enumerate(_read(fabric / MANIFEST_REL).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) != 4 or not cols[0] or not cols[1] or not cols[2]:
            bad.append(f"MANIFEST-BAD-LINE: {MANIFEST_REL}:{lineno} is not 'target | writer | marker | phrase'")
            continue
        rows.append((cols[0], cols[1], cols[2], cols[3]))
    return rows, bad


def check_state(text: str, source: str, fabric: Path, phrases: bool = True) -> List[str]:
    findings: List[str] = []
    rows, bad = _manifest(fabric)
    findings += bad
    if not (fabric / MANIFEST_REL).is_file():
        return findings + [f"MANIFEST-BAD-LINE: {MANIFEST_REL} does not exist"]

    known = {target.split("#", 1)[0] for target, _, _, _ in rows}
    named = {p.rstrip(".,;:)") for p in STATE_PATH.findall(text)}
    for path in sorted(named):
        if path not in known:
            findings.append(
                f"STATE-UNWRITTEN: {source} names {STATE_DIR}{path} but {MANIFEST_REL} lists no writer for it"
            )

    for target, writer, marker, phrase in rows:
        if target.split("#", 1)[0] not in named:
            continue  # this source does not rely on it
        writer_path = fabric / writer
        if not writer_path.is_file() or not _inside(fabric, writer_path):
            findings.append(f"STATE-WRITER-GONE: {target}: writer {writer} does not exist inside {fabric}")
        elif marker not in _read(writer_path):
            findings.append(f"STATE-WRITER-GONE: {target}: {writer} no longer contains {marker!r}")
        if phrases and phrase and phrase.lower() not in text.lower():
            findings.append(f"STATE-WRITER-GONE: {target}: {source} no longer mentions {phrase!r}")
    return findings


def audit(project: Path, fabric: Path) -> List[str]:
    role = project / ROLE_REL
    findings: List[str] = []
    role_text = _read(role)
    if role_text:
        findings += check_text(role_text, "role-orchestrator.md", fabric)
        findings += check_state(role_text, "role-orchestrator.md", fabric)
    for rel in DOC_SOURCES:
        doc = fabric / rel
        if doc.is_file():
            doc_text = _read(doc)
            findings += check_text(doc_text, Path(rel).name, fabric)
            findings += check_state(doc_text, Path(rel).name, fabric, phrases=False)
    return list(dict.fromkeys(findings))


def main(argv: Iterable[str]) -> int:
    args = list(argv)
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    findings = audit(Path(args[0]), Path(args[1]))
    for f in findings:
        print(f)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
