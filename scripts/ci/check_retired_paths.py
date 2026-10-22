#!/usr/bin/env python3
"""CI check: a retired path, symbol or env var stays retired.

Golf 1 of track ``golf-1-opruimen`` removes dead code in clusters (C1 to C5,
``docs/investigations/2026-10-20-golf-1-hermeting.md`` §7).  Every cluster
adds what it removed as its own fragment under
``scripts/ci/retired_paths.d/`` (one ``*.txt`` per cluster, named after the
dispatch, so parallel clusters never conflict); this check fails CI when a
retired file comes back or when anything in the tree still calls a retired
entry in an executable way.

The fragments together are one list, read in sorted file-name order.  The
list has three kinds of line (``#`` starts a comment)::

    scripts/foo.py              a path: the file is gone and nothing calls it
    scripts/lib/bar.py::baz     a symbol: the module stays, ``baz`` (a function,
                                class or ``Class.method``) is gone from it and
                                nothing imports it (for ``Class.method``:
                                nothing uses the method; the class stays)
    env:VNX_OLD_FLAG            an env var: nothing reads it any more

What counts as a reference.  Executable references only:

* Python, read through ``ast``, so a comment never counts and neither does a
  string that is a bare expression statement (a docstring): ``import`` and
  ``from ... import``, ``importlib.import_module``/``runpy.run_module``/
  ``__import__`` strings, ``-m <module>`` (inside one string, or as the
  element after ``"-m"`` in a list), the file name in any other string literal
  (a subprocess command, ``Path(...) / "foo.py"``), a symbol as
  ``module.symbol``, ``from module import symbol``, a ``patch("module.symbol")``
  target or a ``setattr``/``patch.object`` on the module, and an env var read
  through ``os.environ``, ``getenv`` or any ``*env*`` helper, or ``$NAME`` in
  a string.
* A ``K.method`` symbol: the class ``K`` stays, so importing it is no
  reference.  What counts is the retired attribute on the class or on an
  instance of it: ``K.method``, ``K().method``, ``module.K.method`` (also
  through an import alias), a name bound only to ``K(...)`` in a scope, or in
  an enclosing function or the module, followed by ``.method``,
  ``getattr``/``setattr``/``hasattr``/``delattr``/``patch.object`` on the
  class or such an instance with ``"method"``, and a ``patch("module.K.method")``
  target.  In a string or a non-Python file only ``module.K.method``, and
  ``K.method`` or ``K(...).method`` after ``from module import K`` in the same
  text, count.  Not detected: ``self.method`` inside the class or a subclass,
  an instance that arrives as a parameter, a return value, an attribute
  (``self.k = K()``) or a factory (``K.create()``), a class alias made by
  assignment (``Alias = K``), and in a string or non-Python file an import
  alias or an instance bound to a name.
* Shell, yaml, toml, ini, the ``.txt`` lists, ``Makefile`` and extensionless
  scripts with a shell shebang: the text without its ``#`` comments.  An env
  var counts as ``${NAME``, ``$NAME`` or ``env.NAME``.
* JSON (the settings templates included): the whole text.  plist and xml:
  the text without ``<!-- -->`` comments.  JS and TS: the text without ``//``
  and ``/* */`` comments.
* A template (``.j2``, ``.tmpl``, ``.template``, ``.tpl``, ``.example``) is
  read as the file type under its template suffix.
* A tracked symlink counts when its target, resolved against the directory
  of the link, is a retired path or lies under one.  The resolution is
  lexical: ``..`` and ``.`` collapse and nothing on disk is followed, so a
  dangling link resolves like a live one.  An absolute target inside the repo
  counts the same way; a target outside the repo is read like any other path
  string, by its directory tail.  A link that itself sits on a retired path
  fails as still existing.

Markdown and other prose never fail the check, and history is never
rewritten: the frozen artefacts of ``check_docs_file_line_refs.py``
(``_EXEMPT_DOC_SUBDIRS``, imported, not copied), ``docs/manifesto/``,
``tests/fixtures/`` (also nested under a package), ``CHANGELOG.md``,
``changelog.d/`` and the fragments themselves (the list directory, or a
single list passed with ``--list``) are skipped.

A file name is matched with its literal directory tail: ``$VNX_HOME/scripts/
foo.py`` and ``runners/foo.py`` both name ``scripts/.../runners/foo.py``
(the shorter tail is a suffix of the longer one); ``lib/foo.py`` does not
name ``scripts/foo.py``.  A module is matched the same way on its dotted
name.  When another tracked file has the same name and a reference cannot
tell the two apart, the reference is not counted: the check reports what it
can attribute and does not guess.  An extensionless file name needs at least
one directory component, because a bare word is not a path.

Usage::

    check_retired_paths.py [--root DIR] [--list FILE | --list-dir DIR]

``--root`` is the tree to scan (default: the git toplevel of the working
directory); ``--list`` reads one list file and ``--list-dir`` every ``*.txt``
fragment in a directory.  The default is the ``retired_paths.d`` directory
next to this script.  An empty or missing directory is an error, and an entry
that appears in two fragments is an error that names both files.

The red run of a cluster scans the PR's base with the head's fragments:
``--root <worktree on the base> --list-dir <head>/scripts/ci/retired_paths.d``.

Exit 0 clean, 1 violations, 2 a malformed or unusable list or root.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The frozen docs subdirectories are imported from the docs check, not copied:
# a second copy would drift the first time one of the two lists changes.
_EXEMPT_DOC_SUBDIRS = importlib.import_module("check_docs_file_line_refs")._EXEMPT_DOC_SUBDIRS

LIST_DIR_REL = "scripts/ci/retired_paths.d"
DEFAULT_LIST_DIR = Path(__file__).resolve().parent / "retired_paths.d"

_EXEMPT_PREFIXES = tuple(f"docs/{sub}/" for sub in sorted(_EXEMPT_DOC_SUBDIRS)) + (
    "docs/manifesto/",
    "tests/fixtures/",
    "changelog.d/",
)
_EXEMPT_FILES = frozenset({"CHANGELOG.md"})
_FIXTURE_SEGMENT = "/tests/fixtures/"

_TEMPLATE_SUFFIXES = (".j2", ".tmpl", ".template", ".tpl", ".example")
_HASH_EXTS = frozenset({
    ".sh", ".bash", ".zsh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".txt", ".env",
})
_XML_EXTS = frozenset({".plist", ".xml"})
_C_EXTS = frozenset({".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"})
_HASH_NAMES = frozenset({"Makefile"})
_SHELL_SHEBANG = re.compile(r"\b(?:ba|z|da|k)?sh\b")

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_ENV_ENTRY = re.compile(rf"env:({_IDENT})")
_SYMBOL_ENTRY = re.compile(rf"{_IDENT}(?:\.{_IDENT})*")

_LEFT_COMPONENTS = re.compile(r"((?:[\w.\-]+/)*)$")
_DASH_M = re.compile(r"(?<![\w-])-m\s+([\w.]+)")
_FROM_IMPORT = re.compile(r"\bfrom\s+([\w.]+)\s+import\s+\(?\s*(\w+(?:\s*,\s*\w+)*)")
_IMPORT = re.compile(r"\bimport\s+([\w.]+(?:\s*,\s*[\w.]+)*)")
_XML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_C_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\w\"'])//[^\n]*", re.S)

_IMPORT_CALLS = frozenset({"import_module", "run_module", "__import__"})
_ATTR_CALLS = frozenset({"setattr", "getattr", "hasattr", "delattr", "object"})
_ENV_WRITERS = frozenset({"setenv", "delenv", "putenv", "unsetenv"})


class ListError(ValueError):
    """The retired-paths list is malformed."""


@dataclass(frozen=True)
class Entry:
    """One line of the list."""

    raw: str
    kind: str  # "path", "symbol" or "env"
    path: str = ""
    symbol: str = ""
    name: str = ""

    @property
    def owner(self) -> str:
        """``K`` for a ``K.method`` symbol, empty for a plain one."""
        return self.symbol.rpartition(".")[0]

    @property
    def attr(self) -> str:
        """The retired name itself: ``method`` for ``K.method``."""
        return self.symbol.rpartition(".")[2]


@dataclass
class Target:
    """An entry with what the scan needs to attribute a reference to it."""

    entry: Entry
    parts: tuple[str, ...] = ()
    module: tuple[str, ...] = ()
    siblings: list[tuple[str, ...]] = field(default_factory=list)
    sibling_modules: list[tuple[str, ...]] = field(default_factory=list)
    bound: set[str] = field(default_factory=set)  # module aliases, per file being scanned
    classes: set[str] = field(default_factory=set)  # names bound to the class K of K.method, per file


@dataclass(frozen=True)
class Violation:
    entry: str
    where: str
    detail: str

    def render(self) -> str:
        return f"  {self.where} — {self.detail}"


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def _entries_in(text: str) -> Iterator[tuple[int, str]]:
    """The entry lines of a list (comments and blank lines dropped)."""
    for line_no, line in enumerate(text.splitlines(), start=1):
        raw = re.sub(r"(^|\s)#.*$", "", line).strip()
        if raw:
            yield line_no, raw


def parse_list(text: str, source: str = "") -> list[Entry]:
    """Parse one list text; raise ListError on a malformed or duplicate line."""
    entries: list[Entry] = []
    seen: dict[str, str] = {}
    for line_no, raw in _entries_in(text):
        where = f"{source}: line {line_no}" if source else f"line {line_no}"
        if raw in seen:
            raise ListError(f"{where}: duplicate entry {raw!r} (also at {seen[raw]})")
        seen[raw] = where
        entries.append(_parse_entry(raw, where))
    return entries


def list_dir_files(directory: Path) -> list[Path]:
    """The ``*.txt`` fragments in *directory*, sorted by name; error when empty."""
    if not directory.is_dir():
        raise ListError(f"no such directory: {directory}")
    files = sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix == ".txt"),
        key=lambda path: path.name,
    )
    if not files:
        raise ListError(f"no *.txt fragment in {directory}")
    return files


def parse_list_dir(directory: Path) -> list[Entry]:
    """Parse every ``*.txt`` fragment in *directory*, sorted, as one list."""
    entries: list[Entry] = []
    seen: dict[str, str] = {}
    for path in list_dir_files(directory):
        for line_no, raw in _entries_in(path.read_text(encoding="utf-8")):
            where = f"{path.name}:{line_no}"
            if raw in seen:
                raise ListError(f"duplicate entry {raw!r} in {where}, also in {seen[raw]}")
            seen[raw] = where
            entries.append(_parse_entry(raw, where))
    return entries


def _parse_entry(raw: str, where: str) -> Entry:
    if raw.startswith("env:"):
        match = _ENV_ENTRY.fullmatch(raw)
        if match is None:
            raise ListError(f"{where}: {raw!r}: an env entry is env:NAME")
        return Entry(raw, "env", name=match.group(1))
    path, sep, symbol = raw.partition("::")
    parts = path.split("/")
    if path.startswith("/") or re.search(r"\s", path) or any(p in ("", ".", "..") for p in parts):
        raise ListError(f"{where}: {raw!r}: the path must be repo-relative and plain")
    if not sep:
        return Entry(raw, "path", path=path)
    if not path.endswith(".py") or _SYMBOL_ENTRY.fullmatch(symbol) is None:
        raise ListError(f"{where}: {raw!r}: a symbol entry is <module>.py::<name>[.<name>]")
    return Entry(raw, "symbol", path=path, symbol=symbol)


def module_parts(path: str) -> tuple[str, ...]:
    """Dotted-module components of a repo-relative ``.py`` path."""
    if not path.endswith(".py"):
        return ()
    parts = path[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return tuple(parts)


def build_targets(entries: list[Entry], tracked: list[str]) -> list[Target]:
    """Attach path tails and same-name siblings to every entry."""
    targets: list[Target] = []
    for entry in entries:
        target = Target(entry)
        if entry.kind != "env":
            target.parts = tuple(entry.path.split("/"))
            target.module = module_parts(entry.path)
            base = target.parts[-1]
            for other in tracked:
                if other == entry.path:
                    continue
                if other.rsplit("/", 1)[-1] == base:
                    target.siblings.append(tuple(other.split("/")))
                other_mod = module_parts(other)
                if target.module and other_mod and other_mod[-1] == target.module[-1]:
                    target.sibling_modules.append(other_mod)
        targets.append(target)
    return targets


def _tails_agree(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    n = min(len(a), len(b))
    return n > 0 and a[-n:] == b[-n:]


def names_path(target: Target, ref: tuple[str, ...]) -> bool:
    """True when the literal path tail *ref* names *target* and no sibling."""
    if not _tails_agree(ref, target.parts):
        return False
    if "." not in target.parts[-1] and len(ref) < 2:
        return False
    return not any(_tails_agree(ref, sib) for sib in target.siblings)


def names_module(target: Target, mod: tuple[str, ...]) -> bool:
    """True when the dotted module *mod* names *target*'s module and no sibling."""
    if not target.module or not _tails_agree(mod, target.module):
        return False
    return not any(_tails_agree(mod, sib) for sib in target.sibling_modules)


def _dotted(text: str) -> tuple[str, ...]:
    return tuple(p for p in text.split(".") if p)


# ---------------------------------------------------------------------------
# Text references (non-Python files, and string literals inside Python)
# ---------------------------------------------------------------------------


def _path_refs(text: str, target: Target) -> list[tuple[int, str]]:
    base = target.parts[-1]
    pattern = re.compile(r"(?<![\w.\-])" + re.escape(base) + r"(?![\w\-])")
    hits = []
    for match in pattern.finditer(text):
        left = text[max(0, match.start() - 256): match.start()]
        comp = _LEFT_COMPONENTS.search(left)
        dirs = [c for c in comp.group(1).split("/") if c]
        if dirs and comp.start(1) > 0 and left[comp.start(1) - 1] in "${":
            dirs = dirs[1:]
        if names_path(target, tuple(dirs) + (base,)):
            hits.append((match.start(), f"names {base}"))
    return hits


def _module_refs(text: str, target: Target) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    from_spans: list[tuple[int, int]] = []
    for match in _FROM_IMPORT.finditer(text):
        from_spans.append(match.span())
        mod = _dotted(match.group(1))
        names = [n.strip() for n in match.group(2).split(",")]
        if names_module(target, mod) or any(names_module(target, mod + (n,)) for n in names):
            hits.append((match.start(), f"imports {'.'.join(target.module)}"))
    for match in _IMPORT.finditer(text):
        if any(start <= match.start() < end for start, end in from_spans):
            continue
        if any(names_module(target, _dotted(m.strip())) for m in match.group(1).split(",")):
            hits.append((match.start(), f"imports {'.'.join(target.module)}"))
    for match in _DASH_M.finditer(text):
        if names_module(target, _dotted(match.group(1))):
            hits.append((match.start(), f"runs -m {match.group(1)}"))
    return hits


def _symbol_refs(text: str, target: Target) -> list[tuple[int, str]]:
    entry = target.entry
    head = entry.symbol.split(".")[0]
    uses = f"uses {target.module[-1]}.{entry.symbol}"
    # For K.method the class may be called first: module.K().method.
    member = re.escape(entry.owner) + r"(?:\([^()]*\))?\." if entry.owner else ""
    hits = []
    chain = re.compile(r"((?:\w+\.)*\w+)\." + member + re.escape(entry.attr) + r"(?![\w])")
    for match in chain.finditer(text):
        if match.start() > 0 and (text[match.start() - 1].isalnum() or text[match.start() - 1] in "_."):
            continue
        if names_module(target, _dotted(match.group(1))):
            hits.append((match.start(), uses))
    imported = False
    for match in _FROM_IMPORT.finditer(text):
        names = [n.strip() for n in match.group(2).split(",")]
        if head in names and names_module(target, _dotted(match.group(1))):
            imported = True
            if not entry.owner:
                hits.append((match.start(), f"imports {head} from {target.module[-1]}"))
    if entry.owner and imported:
        # The class itself survives: only the retired attribute on it counts.
        bare = re.compile(r"(?<![\w.])" + member + re.escape(entry.attr) + r"(?![\w])")
        hits.extend((match.start(), uses) for match in bare.finditer(text))
    return hits


def _env_refs(text: str, name: str) -> list[tuple[int, str]]:
    patterns = (
        r"\$\{" + name + r"(?![\w])",
        r"\$" + name + r"(?![\w])",
        r"(?<![\w])env\." + name + r"(?![\w])",
        r"env\[\s*[\"']" + name + r"[\"']",
    )
    hits = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            hits.append((match.start(), f"reads env {name}"))
    return hits


def text_refs(text: str, target: Target) -> list[tuple[int, str]]:
    """Return ``(offset, detail)`` for every reference to *target* in *text*."""
    kind = target.entry.kind
    if kind == "env":
        return _env_refs(text, target.entry.name)
    if kind == "symbol":
        return _symbol_refs(text, target)
    hits = _path_refs(text, target)
    if target.module:
        hits += _module_refs(text, target)
    return hits


# ---------------------------------------------------------------------------
# Comment stripping, file classification
# ---------------------------------------------------------------------------


def _hash_comment_start(line: str) -> int:
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return i
        i += 1
    return len(line)


def strip_hash_comments(text: str) -> str:
    """Drop ``#`` comments (at line start or after whitespace, outside quotes)."""
    return "\n".join(line[: _hash_comment_start(line)] for line in text.split("\n"))


def _blank_keep_lines(match: re.Match[str]) -> str:
    return "\n" * match.group(0).count("\n")


def classify(rel: str, first_line: str) -> str | None:
    """Return the reader for a file: python, hash, plain, xml, c, or None (skip)."""
    name = rel.rsplit("/", 1)[-1]
    for suffix in _TEMPLATE_SUFFIXES:
        if name.endswith(suffix) and name != suffix:
            name = name[: -len(suffix)]
            break
    ext = name[name.rfind("."):] if "." in name else ""
    if ext == ".py":
        return "python"
    if ext in _HASH_EXTS or name in _HASH_NAMES:
        return "hash"
    if ext == ".json":
        return "plain"
    if ext in _XML_EXTS:
        return "xml"
    if ext in _C_EXTS:
        return "c"
    if not ext and first_line.startswith("#!"):
        if "python" in first_line:
            return "python"
        if _SHELL_SHEBANG.search(first_line):
            return "hash"
    return None


def is_exempt(rel: str) -> bool:
    """True for frozen history, prose and the fragments themselves."""
    if rel in _EXEMPT_FILES or rel.endswith((".md", ".markdown", ".rst")):
        return True
    if rel.startswith(LIST_DIR_REL + "/"):
        return True
    return rel.startswith(_EXEMPT_PREFIXES) or _FIXTURE_SEGMENT in "/" + rel


# ---------------------------------------------------------------------------
# Python references, through ast
# ---------------------------------------------------------------------------


def _dotted_expr(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _statement_values(tree: ast.AST) -> set[int]:
    """ids of expressions that are statements on their own.

    A string among them is a docstring or a bare string, which never counts;
    a call among them has its result thrown away.
    """
    return {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}


def _bind_module_aliases(tree: ast.AST, targets: list[Target]) -> None:
    """Record, per symbol target, the names this file binds to its module.

    For a ``K.method`` target also the names bound to its class ``K`` by
    ``from module import K [as name]``.
    """
    for target in targets:
        target.bound = set()
        target.classes = set()
    symbol_targets = [t for t in targets if t.entry.kind == "symbol"]
    if not symbol_targets:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for target in symbol_targets:
                    if names_module(target, _dotted(alias.name)):
                        target.bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _dotted(node.module or "")
            for alias in node.names:
                for target in symbol_targets:
                    if names_module(target, base + (alias.name,)):
                        target.bound.add(alias.asname or alias.name)
                    elif (
                        target.entry.owner
                        and alias.name == target.entry.symbol.split(".")[0]
                        and base
                        and names_module(target, base)
                    ):
                        target.classes.add(alias.asname or alias.name)


def _import_hits(node: ast.AST, target: Target) -> str | None:
    entry = target.entry
    if isinstance(node, ast.Import):
        if entry.kind == "path" and any(names_module(target, _dotted(a.name)) for a in node.names):
            return f"imports {'.'.join(target.module)}"
        return None
    if not isinstance(node, ast.ImportFrom):
        return None
    base = _dotted(node.module or "")
    if entry.kind == "path":
        if (base and names_module(target, base)) or any(
            names_module(target, base + (a.name,)) for a in node.names
        ):
            return f"imports {'.'.join(target.module)}"
        return None
    # Importing the class of a K.method is no reference: K stays.
    if entry.kind == "symbol" and not entry.owner and base and names_module(target, base):
        if any(a.name == entry.symbol for a in node.names):
            return f"imports {entry.symbol} from {target.module[-1]}"
    return None


def _call_hits(node: ast.Call, target: Target, discarded: bool) -> str | None:
    func = _dotted_expr(node.func)
    last = func.rsplit(".", 1)[-1]
    first = node.args[0] if node.args else None
    first_str = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
    entry = target.entry
    if entry.kind == "path" and target.module and last in _IMPORT_CALLS and first_str:
        if names_module(target, _dotted(first_str)):
            return f"imports {first_str}"
    if entry.kind == "env" and first_str == entry.name:
        # A pop whose result is thrown away scrubs the variable; it reads nothing.
        scrub = last == "pop" and discarded
        if "env" in func.lower() and last not in _ENV_WRITERS and not scrub:
            return f"reads env {entry.name} via {func}()"
    if entry.kind == "symbol" and not entry.owner and _attr_call_on(node, entry.symbol):
        if _dotted_expr(node.args[0]) in target.bound:
            return f"{last}() on {target.module[-1]}.{entry.symbol}"
    return None


def _attr_call_on(node: ast.Call, attr: str) -> bool:
    """True for ``getattr(x, "attr")``, ``patch.object(x, "attr")`` and the like."""
    second = node.args[1] if len(node.args) >= 2 else None
    return (
        _dotted_expr(node.func).rsplit(".", 1)[-1] in _ATTR_CALLS
        and isinstance(second, ast.Constant)
        and second.value == attr
    )


def _env_node_hits(node: ast.AST, name: str) -> str | None:
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        key = node.slice
        if (
            _dotted_expr(node.value).endswith("environ")
            and isinstance(key, ast.Constant)
            and key.value == name
        ):
            return f"reads env {name} via environ[]"
    if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) and node.left.value == name:
        if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops) and any(
            _dotted_expr(c).endswith("environ") for c in node.comparators
        ):
            return f"tests env {name} in environ"
    return None


def _node_hits(node: ast.AST, target: Target, rel: str, discarded: set[int]) -> str | None:
    """A reference to *target* made by one AST node (strings excluded)."""
    entry = target.entry
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return _import_hits(node, target)
    if isinstance(node, ast.Call):
        return _call_hits(node, target, id(node) in discarded)
    if entry.kind == "env":
        return _env_node_hits(node, entry.name)
    if entry.kind == "symbol" and not entry.owner:
        if isinstance(node, ast.Attribute) and any(
            _dotted_expr(node) == f"{bound}.{entry.symbol}" for bound in target.bound
        ):
            return f"uses {target.module[-1]}.{entry.symbol}"
        if rel == entry.path and isinstance(node, ast.Name) and node.id == entry.symbol:
            return f"uses {node.id} inside its own module"
    if entry.kind == "path" and target.module and isinstance(node, (ast.List, ast.Tuple)):
        values = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
        for flag, mod in zip(values, values[1:]):
            if flag == "-m" and isinstance(mod, str) and names_module(target, _dotted(mod)):
                return f"runs -m {mod}"
    return None


_FUNC_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_SCOPES = _FUNC_SCOPES + (ast.ClassDef,)


def _body(scope: ast.AST) -> list[ast.AST]:
    body = scope.body
    return body if isinstance(body, list) else [body]


def _scope_parts(scope: ast.AST) -> tuple[list[ast.AST], list[ast.AST]]:
    """The nodes *scope* evaluates itself, and the scopes nested in it.

    A nested function or class belongs to its own scope, but its decorators,
    defaults, annotations and bases are evaluated by the enclosing one.  A
    comprehension is read as part of the enclosing scope.
    """
    own: list[ast.AST] = []
    nested: list[ast.AST] = []
    stack = list(_body(scope))
    while stack:
        node = stack.pop()
        own.append(node)
        if isinstance(node, _SCOPES):
            nested.append(node)
            inner = {id(stmt) for stmt in _body(node)}
            stack.extend(c for c in ast.iter_child_nodes(node) if id(c) not in inner)
        else:
            stack.extend(ast.iter_child_nodes(node))
    return own, nested


def _stored_names(node: ast.AST) -> list[str]:
    """Names one node binds (or unbinds) in its scope."""
    if isinstance(node, ast.Name):
        return [node.id] if isinstance(node.ctx, (ast.Store, ast.Del)) else []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(alias.asname or alias.name).split(".")[0] for alias in node.names]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _param_names(scope: ast.AST) -> list[str]:
    if not isinstance(scope, _FUNC_SCOPES):
        return []
    args = scope.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return [p.arg for p in params if p is not None]


def _scope_bindings(
    scope: ast.AST, own: list[ast.AST], creates: Callable[[ast.AST], bool]
) -> tuple[set[str], set[str]]:
    """Names *scope* binds, and the ones it binds only to a new instance.

    Flow-insensitive: a name counts as an instance when every binding of it in
    the scope is ``name = K(...)``; one other binding makes it ambiguous, and
    an ambiguous name is not followed.
    """
    stores = Counter(_param_names(scope))
    created: Counter[str] = Counter()
    for node in own:
        stores.update(_stored_names(node))
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            if creates(node.value):
                tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
                created.update(t.id for t in tgts if isinstance(t, ast.Name))
    return set(stores), {name for name, count in created.items() if stores[name] == count}


def _scope_walk(
    scope: ast.AST, inherited: frozenset[str], creates: Callable[[ast.AST], bool]
) -> Iterator[tuple[ast.AST, frozenset[str]]]:
    """Yield every node with the names that hold an instance where it runs.

    A function sees the instances of the scopes around it unless it rebinds
    the name; a class body does not pass its own names on to its methods.
    """
    own, nested = _scope_parts(scope)
    rebound, local = _scope_bindings(scope, own, creates)
    visible = (inherited - rebound) | local
    for node in own:
        yield node, visible
    passed = inherited if isinstance(scope, ast.ClassDef) else visible
    for child in nested:
        yield from _scope_walk(child, passed, creates)


def _member_violations(tree: ast.AST, rel: str, target: Target) -> list[Violation]:
    """Uses of a retired ``K.method``: the attribute on the class or an instance."""
    entry = target.entry
    tail = entry.owner[len(entry.symbol.split(".")[0]):]
    classes = {f"{bound}.{entry.owner}" for bound in target.bound}
    classes |= {name + tail for name in target.classes}
    if rel == entry.path:
        classes.add(entry.owner)
    if not classes:
        return []

    def creates(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and _dotted_expr(node.func) in classes

    def owns(node: ast.AST, instances: frozenset[str]) -> bool:
        if isinstance(node, ast.Name) and node.id in instances:
            return True
        return creates(node) or _dotted_expr(node) in classes

    found: list[Violation] = []
    for node, instances in _scope_walk(tree, frozenset(), creates):
        detail = None
        if isinstance(node, ast.Attribute) and node.attr == entry.attr and owns(node.value, instances):
            detail = f"uses {target.module[-1]}.{entry.symbol}"
        elif isinstance(node, ast.Call) and _attr_call_on(node, entry.attr) and owns(node.args[0], instances):
            detail = f"{_dotted_expr(node.func).rsplit('.', 1)[-1]}() on {target.module[-1]}.{entry.symbol}"
        if detail:
            found.append(Violation(entry.raw, f"{rel}:{node.lineno}", detail))
    return found


def python_refs(tree: ast.AST, rel: str, targets: list[Target]) -> list[Violation]:
    """Every executable reference in one parsed Python file."""
    _bind_module_aliases(tree, targets)
    standalone = _statement_values(tree)
    found: list[Violation] = []
    for target in targets:
        if target.entry.kind == "symbol" and target.entry.owner:
            found.extend(_member_violations(tree, rel, target))
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        is_string = isinstance(node, ast.Constant) and isinstance(node.value, str)
        for target in targets:
            if is_string:
                if id(node) in standalone:
                    continue
                for offset, detail in text_refs(node.value, target):
                    at = line + node.value.count("\n", 0, offset)
                    found.append(Violation(target.entry.raw, f"{rel}:{at}", detail))
                continue
            detail = _node_hits(node, target, rel, standalone)
            if detail:
                found.append(Violation(target.entry.raw, f"{rel}:{line}", detail))
    return found


# ---------------------------------------------------------------------------
# Presence of the retired thing itself
# ---------------------------------------------------------------------------


def _bound_names(stmts: list[ast.stmt]) -> dict[str, ast.stmt]:
    """Names a block binds, looking through if/try/with at the same level."""
    bound: dict[str, ast.stmt] = {}
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound[stmt.name] = stmt
        elif isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    bound[tgt.id] = stmt
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            bound[stmt.target.id] = stmt
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                bound[(alias.asname or alias.name).split(".")[0]] = stmt
        for sub in ("body", "orelse", "finalbody"):
            if isinstance(stmt, (ast.If, ast.Try, ast.With)) and hasattr(stmt, sub):
                bound.update(_bound_names(getattr(stmt, sub)))
        if isinstance(stmt, ast.Try):
            for handler in stmt.handlers:
                bound.update(_bound_names(handler.body))
    return bound


def presence(root: Path, target: Target) -> Violation | None:
    """The retired file still existing, or the retired symbol still defined."""
    entry = target.entry
    if entry.kind == "env":
        return None
    path = root / entry.path
    if entry.kind == "path":
        if path.exists() or path.is_symlink():
            return Violation(entry.raw, entry.path, "still exists")
        return None
    if not path.is_file():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return Violation(entry.raw, entry.path, f"does not parse ({exc.msg}); cannot prove the symbol is gone")
    stmts: list[ast.stmt] = tree.body
    node: ast.stmt | None = None
    for part in entry.symbol.split("."):
        node = _bound_names(stmts).get(part)
        if node is None:
            return None
        stmts = node.body if isinstance(node, ast.ClassDef) else []
    return Violation(entry.raw, f"{entry.path}:{node.lineno}", f"still defines {entry.symbol}")


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _needles(target: Target) -> tuple[str, ...]:
    entry = target.entry
    if entry.kind == "env":
        return (entry.name,)
    if entry.kind == "symbol":
        return (entry.symbol.split(".")[-1],)
    return (target.parts[-1],) + ((target.module[-1],) if target.module else ())


def _text_violations(text: str, reader: str, rel: str, targets: list[Target]) -> list[Violation]:
    if reader == "hash":
        text = strip_hash_comments(text)
    elif reader == "xml":
        text = _XML_COMMENT.sub(_blank_keep_lines, text)
    elif reader == "c":
        text = _C_COMMENT.sub(_blank_keep_lines, text)
    found = []
    for target in targets:
        for offset, detail in text_refs(text, target):
            line = text.count("\n", 0, offset) + 1
            found.append(Violation(target.entry.raw, f"{rel}:{line}", detail))
    return found


def symlink_names(root: Path, rel: str, link: str, target: Target) -> bool:
    """True when the tracked symlink *rel*, pointing at *link*, points at *target*.

    *link* is resolved lexically against the directory of *rel*: ``..`` and
    ``.`` collapse and nothing on disk is followed, so a dangling link
    resolves like a live one.  An absolute *link* is taken as it is.  Inside
    the repo (the root as given or its real path) the result must be the
    retired path or lie under it; outside the repo it is read like any other
    path string.
    """
    given = os.path.normpath(os.path.abspath(root))
    resolved = os.path.normpath(os.path.join(given, os.path.dirname(rel), link))
    for base in dict.fromkeys((given, os.path.realpath(given))):
        if resolved == base:
            return False
        if resolved.startswith(base + os.sep):
            inside = os.path.relpath(resolved, base).replace(os.sep, "/")
            return inside == target.entry.path or inside.startswith(target.entry.path + "/")
    return bool(_path_refs(resolved, target))


def scan_file(root: Path, rel: str, targets: list[Target]) -> list[Violation]:
    """Every executable reference to the *targets* in one tracked file."""
    path = root / rel
    if path.is_symlink():
        link = os.readlink(path)
        return [
            Violation(t.entry.raw, rel, f"symlink to {link}")
            for t in targets
            if t.entry.kind == "path" and symlink_names(root, rel, link, t)
        ]
    if not path.is_file():
        return []
    raw = path.read_bytes()
    if b"\0" in raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    relevant = [t for t in targets if any(n in text for n in _needles(t))]
    if not relevant:
        return []
    reader = classify(rel, text.split("\n", 1)[0])
    if reader is None:
        return []
    if reader == "python":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return _text_violations(text, "hash", rel, relevant)
        return python_refs(tree, rel, relevant)
    return _text_violations(text, reader, rel, relevant)


def tracked_rel_paths(root: Path) -> list[str]:
    """Tracked files, NUL-separated so a non-ASCII name is not quoted away."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=True,
    )
    return [rel for rel in result.stdout.split("\0") if rel]


def check(root: Path, entries: list[Entry], extra_exempt: frozenset[str] = frozenset()) -> list[Violation]:
    """All violations of *entries* in the tracked tree under *root*."""
    tracked = tracked_rel_paths(root)
    targets = build_targets(entries, tracked)
    retired_files = {e.path for e in entries if e.kind == "path"}
    violations = [v for v in (presence(root, t) for t in targets) if v is not None]
    for rel in tracked:
        if rel in retired_files or rel in extra_exempt or is_exempt(rel):
            continue
        violations.extend(scan_file(root, rel, targets))
    return violations


def _resolve_root(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip()).resolve()


def _report(entries: list[Entry], violations: list[Violation], root: Path, source_path: Path) -> int:
    label = "fragments" if source_path.is_dir() else "list"
    if not violations:
        print(
            f"OK: none of the {len(entries)} retired entries is present or referenced "
            f"(root: {root}, {label}: {source_path})."
        )
        return 0
    by_entry: dict[str, list[Violation]] = {}
    for violation in violations:
        by_entry.setdefault(violation.entry, []).append(violation)
    for entry in entries:
        hits = by_entry.get(entry.raw, [])
        if hits:
            print(f"{entry.raw} — {len(hits)} violation(s)")
            for violation in sorted(set(hits), key=lambda v: (v.where, v.detail)):
                print(violation.render())
    print(
        f"ERROR: {len(by_entry)} of {len(entries)} retired entries are present or still "
        f"referenced (root: {root}, {label}: {source_path})."
    )
    return 1


def _exempt_under(root: Path, source_path: Path) -> frozenset[str]:
    """The list's own files relative to *root*: the check never reads them."""
    if not source_path.is_relative_to(root):
        return frozenset()
    if source_path.is_dir():
        return frozenset(
            path.relative_to(root).as_posix() for path in source_path.iterdir() if path.is_file()
        )
    return frozenset({source_path.relative_to(root).as_posix()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when a retired path, symbol or env var is back.")
    parser.add_argument("--root", help="tree to scan (default: git toplevel of the working directory)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--list", dest="list_path", help="one list file (default: every *.txt fragment)")
    source.add_argument(
        "--list-dir", dest="list_dir", help=f"a directory of fragments (default: {LIST_DIR_REL} next to this script)"
    )
    args = parser.parse_args(argv)
    chosen = args.list_path or args.list_dir
    source_path = Path(chosen).resolve() if chosen else DEFAULT_LIST_DIR
    try:
        root = _resolve_root(args.root)
        if args.list_path:
            entries = parse_list(source_path.read_text(encoding="utf-8"), source=source_path.name)
        else:
            entries = parse_list_dir(source_path)
        violations = check(root, entries, _exempt_under(root, source_path))
    except (ListError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return _report(entries, violations, root, source_path)


if __name__ == "__main__":
    sys.exit(main())
