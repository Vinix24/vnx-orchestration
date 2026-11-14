"""Shared secret patterns (ADR-075 decision 3, ADR-076 decision 9): the one ordered list
in the repo; add a pattern here with a test, never in a second list. ``find`` returns
pattern names, never values; ``mask`` turns every match into ``<secret>``.

``shell_value_end`` is the one public reader of a shell value (bash grammar); every place
that recognises a value, here or in a caller, goes through it."""

from __future__ import annotations

import re
from typing import Callable, List, NamedTuple, Optional, Union

SECRET = "<secret>"
_CHARS = r"[A-Za-z0-9_+/=-]"

# The only quote grammar in the module. A quote that never closes runs to the end of the
# text (fail-closed): the second alternative of each quoted form takes the rest.
#   $'...'  bash escapes, backslash takes any next char (so \' stays inside)
#   "..."   backslash escapes
#   '...'   no escapes
# A value is one or more adjacent segments (bash glues them): a"b c"d is one word.
# A command substitution ($(...) with nesting, or `...`) is one more segment, read by
# ``_substitution_end``; one that never closes runs to the end of the line.
_SEGMENT_SRC = (
    r"(?:"
    r"\$'(?:\\[\s\S]|[^'\\])*'|\$'[\s\S]*"
    r"|\"(?:\\[\s\S]|[^\"\\])*\"|\"[\s\S]*"
    r"|'[^']*'|'[\s\S]*"
    r"|[^\s\"'\\$`]|\\[\s\S]|\$(?![('])"
    r")"
)
_SEGMENT = re.compile(_SEGMENT_SRC)
_QUOTED = re.compile(r"'[^']*'|\"(?:\\[\s\S]|[^\"\\])*\"")

def _line_end(text: str, i: int) -> int:
    n = text.find("\n", i)
    return len(text) if n < 0 else n


def _substitution_end(text: str, i: int) -> int:
    """End of the substitution opening at ``i`` (``$(`` or a backtick); an unbalanced one
    ends at the end of the line (fail-closed)."""
    if text[i] == "`":
        j = i + 1
        while j < len(text) and text[j] != "\n":
            if text[j] == "\\":
                j += 2
            elif text[j] == "`":
                return j + 1
            else:
                j += 1
        return _line_end(text, i)
    depth, j = 0, i + 1  # text[i:i+2] == "$("; the "(" is at i+1
    while j < len(text) and text[j] != "\n":
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c in "'\"":
            q = _QUOTED.match(text, j)
            j = q.end() if q else _line_end(text, j)
            continue
        if c == "`":
            j = _substitution_end(text, j)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return _line_end(text, i)


def shell_value_end(text: str, start: int = 0) -> Optional[int]:
    """End index of the shell value that begins at ``start``, or None when no value
    starts there. An unclosed quote ends at ``len(text)``, an unbalanced substitution at
    the end of its line; never a tail is left over."""
    if not isinstance(text, str):
        raise TypeError("shell_value_end() needs str")
    i = start
    while i < len(text):
        if text.startswith("$(", i) or text[i] == "`":
            i = max(_substitution_end(text, i), i + 1)
            continue
        m = _SEGMENT.match(text, i)
        if not m:
            break
        i = m.end()
    return i if i > start else None


_Repl = Union[str, Callable[["re.Match[str]"], str]]


class SecretPattern(NamedTuple):
    # A regex that matches only a value prefix (lastindex 1) is extended by ``mask``.
    name: str
    regex: "re.Pattern[str]"
    repl: _Repl


def _p(name: str, pattern: str, repl: _Repl = SECRET, flags: int = 0) -> SecretPattern:
    return SecretPattern(name, re.compile(pattern, flags), repl)


PATTERNS: List[SecretPattern] = [
    # First: the whole assignment value goes before a token pattern can cut it short.
    # The name stays, only the value is masked.
    _p(
        "secret_assignment",
        r"([\w-]*(?:KEY|TOKEN|SECRET|PASSWORD)[\w-]*=)(?!<secret>)(?=\S)",
        r"\1" + SECRET,
        re.IGNORECASE,
    ),
    _p("sk_key", r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{15,}"),
    _p("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    _p("aws_key_id", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    _p("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    _p("bearer", r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    # To the end of the line: a quote inside a header value must not end the masking.
    _p("authorization_header", r"\bAuthorization:[^\r\n]*", flags=re.IGNORECASE),
    # An unterminated block is masked to the end: fail-closed.
    _p("pem_block", r"-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\Z)", flags=re.DOTALL),
    _p("jwt", r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    # Only in an '=' position or as a header value: a loose sha or id stays. After '='
    # the whole shell value goes (also when quoted); a header value goes to the next space.
    _p(
        "long_value",
        r"(=)(?!<secret>)(?=(?:\$?[\"'])?" + _CHARS + r"{40,})"
        + r"|(\b[A-Za-z][\w-]*:[ \t]+)(?!<secret>)(?=" + _CHARS + r"{40,})\S+",
        lambda m: (m.group(1) or m.group(2)) + SECRET,
    ),
]


_VALUE_AFTER_PREFIX = {"secret_assignment", "long_value"}


def find(text: str) -> List[str]:
    """Names of the patterns that match ``text``; never the matched value."""
    if not isinstance(text, str):
        raise TypeError("find() needs str")
    return [p.name for p in PATTERNS if p.regex.search(text)]


def mask(text: str) -> str:
    """Replace every match with ``<secret>``; raises TypeError on a non-str."""
    if not isinstance(text, str):
        raise TypeError("mask() needs str")
    for p in PATTERNS:
        text = _mask_one(p, text)
    return text


def _mask_one(p: SecretPattern, text: str) -> str:
    out, pos = [], 0
    for m in p.regex.finditer(text):
        if m.start() < pos:
            continue
        end = m.end()
        if m.lastindex == 1 and p.name in _VALUE_AFTER_PREFIX:
            end = shell_value_end(text, end) or end
        out.append(text[pos:m.start()])
        out.append(m.expand(p.repl) if isinstance(p.repl, str) else p.repl(m))
        pos = end
    out.append(text[pos:])
    return "".join(out)
