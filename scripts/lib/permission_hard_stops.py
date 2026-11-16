"""Hard stops on the raw form (ADR-075 decision 9, amended by ADR-082): a frozen table, no model, no I/O.
Secrets are checked on the untouched raw text, every other rule is a word anywhere in the de-quoted projection
(never a command position), and shell indirection is ``indirection`` as a whole. Only the closed value reaches
the ledger (field ``hard_stop``), never the raw text. A recognised form is ``alleen_melding``."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from secret_patterns import SECRET, mask

EVIDENCE_UNAVAILABLE = "evidence_unavailable"
INDIRECTION = "indirection"
PROMOTE_REASON = ("hard_stop:promote: ADR-056 route: metadata.autonomous_promotion_eligible on the track "
                  "plus a plan-gate grant that the verifier reads as VERIFIED")
_I = re.IGNORECASE

def _w(names: str, before: str = r"\w-") -> "re.Pattern[str]":
    """A word anywhere, in any case: no letter, digit, ``_`` or ``-`` (plus ``before``) on either side."""
    return re.compile(rf"(?<![{before}])(?:{names})(?![\w-])", _I)

_OP_SUBS = "read|run|inject|signin|item|vault|document|whoami|account|connect|user|group|service-account"
_MARKERS = re.compile(r"[A-Z0-9_]*(?:TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|PRIVATE_KEY)[A-Z0-9_]*")  # raw, upper
_INDIRECT = (re.compile(r"\$[\w{(@*#?!$'\"-]|`"), _w("eval"),
             re.compile(r"\|\s*(?:\S*/)?(?:ba|z|da|k)?sh(?![\w-])", _I))  # $X ${X} $( ) $'…' `…`, eval, | sh
_GLOB_NAMES = ("rm ssh scp sftp mosh gcloud hcloud launchctl systemctl op gh git supabase eval base64 security "
               "sh bash zsh dash ksh kubectl terraform pulumi vercel fly").split()
_SPLIT = re.compile(r"[\s;&|()<>]+")
# (category, rules): a rule is a tuple of patterns that must all match somewhere; checked in this order.
TABLE: Tuple[Tuple[str, Tuple[Tuple["re.Pattern[str]", ...], ...]], ...] = (
    ("secrets", (
        (_w("(?:find|add|delete)-(?:generic|internet)-password"),),
        (_w("gh"), _w("secrets?")),
        (re.compile(rf"(?<![\w-])op(?:\s+-\S*(?:\s+[^\s-]\S*)?)*?\s+(?:{_OP_SUBS})(?![\w-])", _I),),
        (re.compile(r"(?<![\w-])op://", _I),))),
    ("verwijderen", (
        (_w("rm"), re.compile(r"(?<![\w-])(?:-[a-z]*r[a-z]*|--recursive)(?![\w-])", _I)),
        (_w("git"), _w("push"), re.compile(r"(?<![\w-])(?:-d|--delete)(?![\w-])|(?<!\S)\+?:\S", _I)),
        (_w("gh"), re.compile("delete", _I)))),
    ("supabase", (
        (_w("supabase"),),
        (_w("grant|revoke"),),
        (re.compile(r"(?<![\w-])(?:alter|create|drop)\s+(?:role|policy|user)(?![\w-])", _I),))),
    ("server", ((_w("gcloud|hcloud"),), (_w("ssh|scp|sftp|mosh", r"\w.-"),))),
    ("units", ((_w("launchctl|systemctl"),),)),
    ("deploy_herstart", (
        (re.compile(r"(?<![a-z0-9_])(?:(?:re)?deploy|restart|reboot|shutdown|rollout)", _I),),
        (_w("kubectl|terraform"), _w("apply")), (_w("pulumi"), _w("up")), (_w("vercel"), _w("--prod")))),
    ("promote", ((_w("deliverable"), _w("promote")),)),
)
CATEGORIES = frozenset(c for c, _ in TABLE)
HARD_STOP_VALUES = frozenset(CATEGORIES | {INDIRECTION, EVIDENCE_UNAVAILABLE, ""})  # "" = falls through

def _glob_hides_a_name(text: str) -> bool:
    """A word with a glob or brace (``ss?``, ``{r,}m``) that can expand to a table name; unparseable is True."""
    for word in _SPLIT.split(text):
        base = word.rsplit("/", 1)[-1]
        if not any(c in base for c in "*?[{") or not base.strip("*"):  # a bare * names no command
            continue
        rx = "".join({"*": ".*", "?": ".", "{": "(?:", "}": ")", ",": "|"}.get(c, re.escape(c)) for c in
                     re.sub(r"\[[^\]]*\]?", "?", base))  # a [...] class counts as any one character
        try:
            pattern = re.compile(rx, _I)
        except re.error:
            return True
        if any(pattern.fullmatch(name) for name in _GLOB_NAMES):
            return True
    return False

def _match(text: str, rules: Tuple[Tuple["re.Pattern[str]", ...], ...]) -> bool:
    return any(all(p.search(text) for p in rule) for rule in rules)

def classify(raw_text: Any) -> Tuple[str, str]:
    """(value, reason): a category of ``TABLE``, ``indirection``, ``evidence_unavailable`` when there is no raw
    text, or ("", "") for a form outside every category (it falls through to the audience rule)."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        return EVIDENCE_UNAVAILABLE, f"{EVIDENCE_UNAVAILABLE}: no raw source form to classify"
    try:
        masked = mask(raw_text)
    except Exception:  # vnx-silent-except: a mask failure is reported as unavailable evidence, never a pass
        return EVIDENCE_UNAVAILABLE, f"{EVIDENCE_UNAVAILABLE}: the raw form could not be masked"
    text = raw_text.translate({ord("'"): None, ord('"'): None, ord("\\"): None})  # rule 1: de-quote first
    if masked != raw_text or SECRET in masked or _MARKERS.search(raw_text) or _match(text, TABLE[0][1]):
        return "secrets", reason("secrets")
    if (any(p.search(raw_text + "\n" + text) for p in _INDIRECT) or _glob_hides_a_name(text)
            or _match(text, ((_w("base64"), _w("(?:ba|z|da|k)?sh", r"\w.-")),))):
        return INDIRECTION, reason(INDIRECTION)
    for category, rules in TABLE[1:]:
        if _match(text, rules):
            return category, reason(category)
    return "", ""

def reason(category: str) -> str:
    """The fixed ``alleen_melding`` reason for a value; promote names the ADR-056 route."""
    if category == "promote":
        return PROMOTE_REASON
    if category == INDIRECTION:
        return f"{INDIRECTION}: shell indirection hides the command, the form is not parsed"
    if category in CATEGORIES:
        return f"hard_stop:{category}"
    return f"{EVIDENCE_UNAVAILABLE}: hard_stop {category!r} is not a known value"

def melding(event: Mapping[str, Any]) -> Optional[str]:
    """The ``alleen_melding`` reason an event carries, or None when it falls through to the audience rule.
    A missing, non-str or unknown ``hard_stop`` (a line from before L5a, a damaged one) is evidence_unavailable."""
    value = event.get("hard_stop")
    if value == "":
        return None
    if value == EVIDENCE_UNAVAILABLE:
        return f"{EVIDENCE_UNAVAILABLE}: the event carries no classification of its raw form"
    return reason(value) if isinstance(value, str) else reason(repr(value))
