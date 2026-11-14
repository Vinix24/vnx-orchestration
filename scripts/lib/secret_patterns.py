"""Shared secret patterns (ADR-075 decision 3, ADR-076 decision 9): the one ordered list
in the repo; add a pattern here with a test, never in a second list. ``find`` returns
pattern names, never values; ``mask`` turns every match into ``<secret>``."""

from __future__ import annotations

import re
from typing import List, NamedTuple

SECRET = "<secret>"
_CHARS = r"[A-Za-z0-9_+/=-]"

class SecretPattern(NamedTuple):
    name: str
    regex: "re.Pattern[str]"
    repl: str

def _p(name: str, pattern: str, repl: str = SECRET, flags: int = 0) -> SecretPattern:
    return SecretPattern(name, re.compile(pattern, flags), repl)


PATTERNS: List[SecretPattern] = [
    _p("sk_key", r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{15,}"),
    _p("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    _p("aws_key_id", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    _p("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    _p("bearer", r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    # The name stays, only the value is masked.
    _p(
        "secret_assignment",
        r"([\w-]*(?:KEY|TOKEN|SECRET|PASSWORD)[\w-]*=)(?!<secret>)(\"[^\"]*\"|'[^']*'|\S+)",
        r"\1" + SECRET,
        re.IGNORECASE,
    ),
    _p("authorization_header", r"\bAuthorization:[^\r\n\"']*", flags=re.IGNORECASE),
    # An unterminated block is masked to the end: fail-closed.
    _p("pem_block", r"-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\Z)", flags=re.DOTALL),
    _p("jwt", r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    # Only in an '=' position or as a header value: a loose sha or id stays.
    _p("long_value", r"(=|\b[A-Za-z][\w-]*:[ \t]+)(?!<secret>)" + _CHARS + r"{40,}", r"\1" + SECRET),
]

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
        text = p.regex.sub(p.repl, text)
    return text
