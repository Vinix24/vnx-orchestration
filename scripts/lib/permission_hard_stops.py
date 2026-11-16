"""Hard stops on the raw form (ADR-075 decision 9, amended by ADR-082): a frozen table, no model, no I/O.

``classify`` reads the raw, unmasked, unnormalised command. Only its closed category reaches the ledger
(field ``hard_stop``); the raw text never does. A recognised form is ``alleen_melding`` for good.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from secret_patterns import SECRET, mask

EVIDENCE_UNAVAILABLE = "evidence_unavailable"
PROMOTE_REASON = ("hard_stop:promote: ADR-056 route: metadata.autonomous_promotion_eligible on the track "
                  "plus a plan-gate grant that the verifier reads as VERIFIED")
_CMD = (r"(?:^|[;&|(`\n]|\$\()\s*(?:sudo(?:\s+-[\w-]+(?:=\S*|\s+(?!-)\S+)?)*\s+|env\s+|[A-Za-z_]\w*=\S*\s+)*"
        r"(?:[^\s;&|()`]*/)?")  # command position: sudo with its options, env, VAR=, then an optional path
_I = re.IGNORECASE
TABLE: Tuple[Tuple[str, Tuple["re.Pattern[str]", ...]], ...] = (  # (category, patterns), checked in order
    ("secrets", (
        re.compile(r"[A-Z0-9_]*(?:TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|PRIVATE_KEY)[A-Z0-9_]*"),
        re.compile(r"\bsecurity\s+(?:find|add|delete)-(?:generic|internet)-password\b"),
        re.compile(r"\bgh\s+secret\b"),
        re.compile(_CMD + r"op\s+\w"))),
    ("verwijderen", (
        re.compile(r"\bgit\b[^\n;&|]*\bpush\b[^\n;&|]*(?:\s--delete\b|\s-d\b|\s\+?:\S)"),
        re.compile(r"\bgh\b[^\n;&|]*\bdelete\b"))),
    ("supabase", (
        re.compile(r"\bsupabase\b[^\n;&|]*\b(?:db|migrations?|link|secrets|roles?|projects|branches|functions)\b", _I),
        re.compile(r"supabase/migrations"),
        re.compile(r"\b(?:GRANT|REVOKE)\b|\b(?:ALTER|CREATE|DROP)\s+(?:ROLE|POLICY|USER)\b", _I))),
    ("server", (
        re.compile(r"\b(?:gcloud|hcloud)\b"),
        re.compile(_CMD + r"(?:ssh|scp|sftp|mosh)(?![\w-])"))),
    ("units", (re.compile(r"\b(?:launchctl|systemctl)\b"),)),
    ("deploy_herstart", (
        re.compile(r"\b(?:re)?deploy\w*|\brestart\w*|\breboot\b|\bshutdown\b|\brollout\b", _I),
        re.compile(r"\b(?:kubectl|terraform)\s+apply\b|\bpulumi\s+up\b|\bvercel\b[^\n;&|]*--prod\b"))),
    ("promote", (re.compile(r"\bdeliverable\s+promote\b"),)),
)
CATEGORIES = frozenset(c for c, _ in TABLE)
HARD_STOP_VALUES = frozenset(CATEGORIES | {EVIDENCE_UNAVAILABLE, ""})  # "" = not recognised, falls through
_RM = re.compile(r"\brm\s+([^\n;&|)`]*)")
_SCRATCH = re.compile(r"/(?:private/)?tmp/claude-\d+/(?:[\w.@%+,:=-]+/)*scratchpad(?:/[\w.@%+,:=-]+)*/?")  # absolute only


def _rm_outside_scratchpad(raw: str) -> bool:
    """True for a recursive ``rm`` with any target outside a scratchpad; an unparseable one is True (fail-closed)."""
    for m in _RM.finditer(raw):
        try:
            words = shlex.split(m.group(1))
        except ValueError:
            return True
        end = words.index("--") if "--" in words else len(words)  # after the terminator every word is a target
        flags = [w for w in words[:end] if w.startswith("-")]
        if not any(w in ("--recursive", "-r", "-R") or (not w.startswith("--") and set(w[1:]) & {"r", "R"})
                   for w in flags):
            continue
        targets = [w for w in words[:end] if not w.startswith("-")] + words[end + 1:]
        if not targets or any(".." in t or not _SCRATCH.fullmatch(t) for t in targets):
            return True
    return False


def classify(raw_text: Any) -> Tuple[str, str]:
    """(category, reason) for the raw form: a category of ``TABLE``, ``evidence_unavailable`` when there is no
    raw text, or ("", "") for a form outside every category."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        return EVIDENCE_UNAVAILABLE, f"{EVIDENCE_UNAVAILABLE}: no raw source form to classify"
    try:
        masked = mask(raw_text)
    except Exception:  # vnx-silent-except: a mask failure is reported as unavailable evidence, never a pass
        return EVIDENCE_UNAVAILABLE, f"{EVIDENCE_UNAVAILABLE}: the raw form could not be masked"
    if masked != raw_text or SECRET in masked:
        return "secrets", reason("secrets")
    if _rm_outside_scratchpad(raw_text):
        return "verwijderen", reason("verwijderen")
    for category, patterns in TABLE:
        if any(p.search(raw_text) for p in patterns):
            return category, reason(category)
    return "", ""


def reason(category: str) -> str:
    """The fixed ``alleen_melding`` reason for a category; promote names the ADR-056 route."""
    if category == "promote":
        return PROMOTE_REASON
    if category in CATEGORIES:
        return f"hard_stop:{category}"
    return f"{EVIDENCE_UNAVAILABLE}: hard_stop {category!r} is not a known category"


def melding(event: Mapping[str, Any]) -> Optional[str]:
    """The ``alleen_melding`` reason an event carries, or None when it falls through to the audience rule.
    A missing, non-str or unknown ``hard_stop`` (an older ledger line, a damaged one) is evidence_unavailable."""
    value = event.get("hard_stop")
    if value == "":
        return None
    if value == EVIDENCE_UNAVAILABLE:
        return f"{EVIDENCE_UNAVAILABLE}: the event carries no classification of its raw form"
    return reason(value) if isinstance(value, str) else reason(repr(value))
