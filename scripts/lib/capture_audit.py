"""The capture audit engine (ADR-067, capture PR-3, split 1 of 2).

Pure functions over the records the ``capture_sources`` readers return: the
window, the candidate pairs, the disposition and the anchor rule. No I/O.
Fail-closed: a bin that is not ``ok``, or a window without a new record, makes
``open_pairs`` null. A null is no measurement and never reads as zero.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, FrozenSet, List, Optional, Tuple

import capture_sources as cs

TOKEN_MIN_LEN = 4
JACCARD_MIN = 0.34
SHARED_MIN = 2

SOURCE_EXPLICIT, SOURCE_CHAIN, SOURCE_FIRST24H = "explicit", "chain", "first24h"
FIRST_WINDOW = timedelta(hours=24)

OI, MEM, NUG = "open_items", "memory", "bucket"
BINS = (OI, MEM, NUG)
# Pair kind -> (home bin, other bin). Home order: open item, note, nugget (decision 3).
KINDS: Dict[str, Tuple[str, str]] = {"oi_mem": (OI, MEM), "oi_nug": (OI, NUG), "mem_nug": (MEM, NUG)}

_TOKEN_RE = re.compile(r"[^\W\d_]+")


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime
    source: str


@dataclass(frozen=True)
class Pair:
    pair_id: str
    kind: str
    home: dict
    other: dict
    score: float
    shared: int


def _aware(value: object, label: str) -> datetime:
    """An ISO-8601 string that names its zone, as UTC. Naive input is refused."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} is not an ISO-8601 time") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must name its zone (Z or an offset)")
    return parsed.astimezone(timezone.utc)


def parse_window(since: Optional[str], until: Optional[str], *,
                 chain_until: Optional[datetime], now: datetime) -> Window:
    """``explicit`` from since/until, else ``chain`` from the previous audit's until, else ``first24h``.

    A record is new in the window when ``since <= stamp < until``, so a chain
    of windows has neither a gap nor an overlap.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if since is not None or until is not None:
        if since is None or until is None:
            raise ValueError("--since and --until go together")
        start, end, source = _aware(since, "since"), _aware(until, "until"), SOURCE_EXPLICIT
    elif chain_until is not None:
        start, end, source = _aware(chain_until.isoformat(), "chain_until"), now, SOURCE_CHAIN
    else:
        start, end, source = now - FIRST_WINDOW, now, SOURCE_FIRST24H
    if start >= end:
        raise ValueError("the window is empty or reversed: since must lie before until")
    return Window(start, end, source)


def tokens(text: str) -> FrozenSet[str]:
    """Letters-only, lower-case tokens of at least TOKEN_MIN_LEN letters."""
    return frozenset(t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= TOKEN_MIN_LEN)


def jaccard_pair(a: FrozenSet[str], b: FrozenSet[str]) -> Tuple[float, int]:
    """``(jaccard, shared)``; two empty sets are ``(0.0, 0)``."""
    shared, union = len(a & b), len(a | b)
    return (shared / union if union else 0.0), shared


def pair_id(project_id: str, kind: str, key_a: str, key_b: str) -> str:
    return hashlib.sha256(f"{project_id}|{kind}|{key_a}|{key_b}".encode("utf-8")).hexdigest()[:16]


def _key(bin_name: str, rec: dict) -> object:
    return rec.get("id") if bin_name == OI else rec.get("key") if bin_name == MEM else rec.get("locator")


def _text(bin_name: str, rec: dict) -> str:
    if bin_name == MEM:
        return f"{rec.get('name') or ''} {rec.get('description') or ''}"
    return rec.get("title_tokens_input") or ""


def find_pairs(oi: List[dict], mem: List[dict], nug: List[dict], *, project_id: str) -> List[Pair]:
    """Candidate pairs of the three kinds; never oi_oi, mem_mem or nug_nug."""
    groups = {OI: oi, MEM: mem, NUG: nug}
    toks = {b: [tokens(_text(b, r)) for r in recs] for b, recs in groups.items()}
    pairs: List[Pair] = []
    for kind, (home_bin, other_bin) in KINDS.items():
        for home, home_toks in zip(groups[home_bin], toks[home_bin]):
            for other, other_toks in zip(groups[other_bin], toks[other_bin]):
                score, shared = jaccard_pair(home_toks, other_toks)
                if shared >= SHARED_MIN and score >= JACCARD_MIN:
                    pid = pair_id(project_id, kind, str(_key(home_bin, home)), str(_key(other_bin, other)))
                    pairs.append(Pair(pid, kind, home, other, score, shared))
    return sorted(pairs, key=lambda p: p.pair_id)


def disposition(pair: Pair, dismissed_ids: FrozenSet[str]) -> str:
    """``linked`` only when the NON-home record carries the home key; a reference
    on the home side does not link. Missing refs are no link: the pair stays open."""
    home_bin = KINDS[pair.kind][0]
    refs, kind = pair.other.get("body_refs"), ("oi" if home_bin == OI else "memory")
    # Only a list of refs counts; anything else (None, a string, a number) is no reference.
    carried = set(refs[kind]) if isinstance(refs, dict) and isinstance(refs.get(kind), list) else set()
    home_keys = {pair.home.get("id")} if home_bin == OI else {pair.home.get("key"), pair.home.get("name")}
    if carried & (home_keys - {None, ""}):
        return "linked"
    return "dismissed" if pair.pair_id in dismissed_ids else "open"


def _stamp(bin_name: str, rec: dict):
    """The record's own time (a date for a nugget), or None when it cannot be read."""
    if not isinstance(_key(bin_name, rec), str) or not _key(bin_name, rec):
        return None
    try:
        if bin_name == NUG:
            return date.fromisoformat(rec.get("date"))
        raw = rec.get("created_at" if bin_name == OI else "modified")
        # astimezone reads a naive time as host-local time (decision 13), then converts to UTC.
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def _is_new(stamp, window: Window) -> bool:
    """A time is new when ``since <= stamp < until``, a date (a nugget) when ``since.date() <= day <
    until.date()``: half-open on days too, so a chain counts every day in exactly one window. The cost:
    a nugget dated today is counted by the first window whose ``until`` is a later day, not one ending today."""
    if isinstance(stamp, datetime):
        return window.since <= stamp < window.until
    return window.since.date() <= stamp < window.until.date()


def _classify(raw: dict, window: Window):
    """Bin rows, the records of the ok bins and the keys of the new ones."""
    bins, records, new_keys = {}, {}, {}
    for name in BINS:
        recs, state, extra = raw.get(name) or ([], cs.STATE_ABSENT, {})
        stamps = [_stamp(name, r) for r in recs]
        if state == cs.STATE_OK and any(s is None for s in stamps):
            state = cs.STATE_ERROR  # a record without a readable key or time: the bin cannot answer
        records[name] = recs if state == cs.STATE_OK else []
        new_keys[name] = {_key(name, r) for r, s in zip(records[name], stamps) if _is_new(s, window)}
        bins[name] = ({"state": state, "count": len(recs), "new": len(new_keys[name]), **extra}
                      if state == cs.STATE_OK else {"state": state, "count": None, "new": None})
    return bins, records, new_keys


def _pair_entry(pair: Pair, project_id: str, new_keys: dict, dismissed: FrozenSet[str]) -> dict:
    home_bin, other_bin = KINDS[pair.kind]
    sides = ((home_bin, pair.home), (other_bin, pair.other))
    anchor = next((b for b, r in sides if _key(b, r) in new_keys[b]), None)
    note = pair.home if home_bin == MEM else pair.other if other_bin == MEM else {}
    return {"pair_id": pair.pair_id, "kind": pair.kind, "oi_id": pair.home.get("id") if home_bin == OI else None,
            "locators": [cs._locator(project_id, "memory", r["key"]) if b == MEM else r["locator"] for b, r in sides],
            "anchor_bin": anchor, "disposition": disposition(pair, dismissed),
            "in_window": anchor is not None, "modified_source": note.get("modified_source")}


def audit_model(raw: Dict[str, tuple], window: Window, *, project_id: str,
                dismissed_ids: FrozenSet[str]) -> dict:
    """The read model of one window. ``raw`` maps a bin to ``(records, state, extra_dict)``, already
    normalized and not the reader output (``read_memory_notes`` is a 2-tuple, ``read_open_items`` and
    ``read_bucket_entries`` a 3-tuple ending in an int foreign count); ``build_audit`` (split 2)
    normalizes them. A missing bin is ``absent``. A pair counts for the window when one of its records
    is new in it (``open_pairs``); open pairs without a new record are ``backlog_open_pairs``.
    ``anchors_total == 0`` makes ``open_pairs`` null, and a bin that is not ``ok`` makes all three null."""
    bins, records, new_keys = _classify(raw, window)
    pairs = find_pairs(records[OI], records[MEM], records[NUG], project_id=project_id)
    entries = [_pair_entry(p, project_id, new_keys, dismissed_ids) for p in pairs]
    all_ok = all(row["state"] == cs.STATE_OK for row in bins.values())
    anchors = sum(row["new"] for row in bins.values()) if all_ok else None
    open_in = sum(1 for e in entries if e["in_window"] and e["disposition"] == "open")
    backlog = sum(1 for e in entries if not e["in_window"] and e["disposition"] == "open")
    return {"bins": bins, "anchors_total": anchors,
            "open_pairs": open_in if anchors is not None and anchors > 0 else None,
            "backlog_open_pairs": backlog if all_ok else None, "pairs": entries}
