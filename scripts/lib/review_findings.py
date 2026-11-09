#!/usr/bin/env python3
"""review_findings.py — stable finding identity for the delta review.

The delta review (plan ``deltareview-met-ouderketen``, Besluit 3) needs an id
that survives a line shift. Pure: no file, git or logging; callers pass the
source text; DR-3 writes these ids into the ledger, DR-4a puts them in the
prompt.

Identity is ``sha256(seat, path, symbolic context, normalised text)[:16]``, no
line number in it (research question 4). The symbolic context is the dotted
qualified name of the innermost ``def``/``async def``/``class`` around the
line, found through compound statements as well, so two functions with the
same finding text never share one id. The fail-closed status model
(``next_status``, ``blocks``) follows in the second half of this split.
"""

from __future__ import annotations

import ast
import hashlib
import re
import unicodedata
from typing import Any, Mapping, Optional

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

#: File-like tokens: a name with a code/data extension. Only these carry a
#: ``:line`` (and optional ``:column``) suffix that refers to a line, so only
#: they are stripped. ``exit:3`` or ``ratio:3`` are left alone.
_FILE_EXTENSION = (
    r"(?:py|pyi|js|jsx|ts|tsx|go|rs|java|kt|rb|sh|bash|zsh|yml|yaml|json|"
    r"toml|ini|cfg|md|rst|sql|css|html|xml|c|h|cc|cpp|hpp)"
)
#: ``path.py:123`` and ``path.py:123:7`` -> ``path.py`` (the number is dropped,
#: the path stays, so a moved finding normalises identically).
_PATH_LINE_RE = re.compile(
    r"(?P<path>[\w./\\-]+\.%s):\d+(?::\d+)?" % _FILE_EXTENSION
)
#: ``line 123`` and ``lines 12-14``.
_LINE_WORD_RE = re.compile(r"\blines?\s+\d+(?:\s*[-–—]\s*\d+)?")
#: ``L123`` shorthand.
_L_SHORTHAND_RE = re.compile(r"\bl\d+\b")
#: A standalone colon-delimited line group, ``:123:`` or ``:123:7:``,
#: preceded by whitespace or a bracket. The lookbehind keeps clock times such
#: as ``12:34:56`` intact (``:34:`` there follows a digit, not whitespace).
_COLON_LINE_RE = re.compile(r"(?<=[\s(\[{]):\d+(?::\d+)?:")
#: Separator for the id payload. Replaced inside fields so it cannot occur.
_FIELD_SEP = "\x1f"

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _statement_lists(node: ast.AST) -> list[list[ast.AST]]:
    """Statement lists ``node`` may hold; expression attributes are skipped.

    Covers ``body``/``orelse``/``finalbody``, every ``handlers[i].body`` (also
    ``TryStar``) and every ``cases[i].body`` of a ``match``; lambda bodies are
    expressions and are not returned.
    """
    blocks: list[list[ast.AST]] = []
    for name in ("body", "orelse", "finalbody"):
        value = getattr(node, name, None)
        if isinstance(value, list):
            blocks.append(value)
    for handler in getattr(node, "handlers", None) or []:
        blocks.append(handler.body)
    for case in getattr(node, "cases", None) or []:
        blocks.append(case.body)
    return blocks


def normalize_text(text: str) -> str:
    """Canonical form of finding text: identical wording, identical output.

    In order: Unicode NFC, ``str.casefold()``, then line references removed so
    a finding copied from a moved line normalises the same:

    - ``path.py:123`` / ``path.py:123:7`` lose the number but keep ``path.py``
      (:data:`_PATH_LINE_RE`; only tokens with a code/data extension);
    - ``line 123`` and ``lines 12-14`` are dropped (:data:`_LINE_WORD_RE`);
    - ``L123`` is dropped (:data:`_L_SHORTHAND_RE`);
    - a standalone ``:123:`` / ``:123:7:`` after whitespace or a bracket is
      dropped (:data:`_COLON_LINE_RE`); a clock time ``12:34:56`` stays.

    Numbers that are not line references stay: ``exit 3``, ``0o444``,
    ``450 lines``, a bare ``123``. Whitespace runs collapse to one space and
    the result is stripped.
    """
    if not isinstance(text, str):
        raise TypeError(f"normalize_text expects str, got {type(text).__name__}")
    out = unicodedata.normalize("NFC", text).casefold()
    out = _PATH_LINE_RE.sub(lambda m: m.group("path"), out)
    out = _LINE_WORD_RE.sub(" ", out)
    out = _L_SHORTHAND_RE.sub(" ", out)
    out = _COLON_LINE_RE.sub(" ", out)
    return " ".join(out.split())


def symbolic_context(source: Optional[str], line: Optional[int]) -> str:
    """Qualified name of the innermost ``def``/``async def``/``class`` scope.

    Nesting is dotted (``Outer.method``); spans are read as
    ``gate_reanchor.changed_symbols`` reads them, ``lineno``..``end_lineno``.
    Compound statements are descended through with the same prefix (``if``/
    ``elif`` chains arrive as nested ``If`` nodes in ``orelse``), so a scope
    inside ``if``/``try``/``with``/``for``/``match`` is found. Returns ``""``
    for ``source=None``, unparseable or non-Python source, ``line=None``, a
    non-integer line and module-level code. Pure: no IO.
    """
    if source is None or line is None or isinstance(line, bool) or not isinstance(line, int):
        return ""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, TypeError):
        return ""
    best: Optional[tuple[int, str]] = None
    stack: list[tuple[list[ast.AST], str]] = [(tree.body, "")]
    while stack:
        nodes, prefix = stack.pop()
        for node in nodes:
            inner_prefix = prefix
            if isinstance(node, _SCOPE_NODES):
                lo = node.lineno
                hi = getattr(node, "end_lineno", None) or lo
                if not (lo <= line <= hi):
                    continue
                qualified = prefix + node.name
                span = hi - lo
                if best is None or span < best[0]:
                    best = (span, qualified)
                inner_prefix = qualified + "."
            for block in _statement_lists(node):
                stack.append((block, inner_prefix))
    return "" if best is None else best[1]


# Identity


def _posix_path(path: str) -> str:
    """POSIX form of ``path`` with no leading ``./`` (backslashes become ``/``)."""
    out = path.replace("\\", "/")
    while out.startswith("./"):
        out = out[2:]
    return out


def _field(value: str) -> str:
    """A payload field with the separator removed so it cannot forge a split."""
    return value.replace(_FIELD_SEP, " ")


def finding_id(seat: str, path: str, context: str, text: str) -> str:
    """Stable 16-hex-character id for one finding. No line number goes in.

    Payload: ``seat``, POSIX ``path`` without a leading ``./``, symbolic
    ``context`` and ``normalize_text(text)``, joined with ``\\x1f`` and
    hashed with ``sha256``. Missing pieces are empty strings.
    """
    payload = _FIELD_SEP.join(
        _field(part)
        for part in (
            seat or "",
            _posix_path(path or ""),
            context or "",
            normalize_text(text or ""),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def id_for_finding(seat: str, finding: Mapping[str, Any], source: Optional[str]) -> str:
    """Id of a finding dict as the gate records write it.

    Reads ``file_path``, ``line`` and ``message``. A finding without a message
    raises ``ValueError`` (distinct blockers must not collapse onto one id); a
    non-mapping raises ``TypeError``.
    """
    if not isinstance(finding, Mapping):
        raise TypeError(f"finding must be a mapping, got {type(finding).__name__}")
    message = finding.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("finding has no message; identity would collide across findings")
    path = finding.get("file_path") or ""
    line = finding.get("line")
    return finding_id(seat, str(path), symbolic_context(source, line), message)
