"""The capture audit engine and read model (ADR-067, capture PR-3).

Pure functions over the records the ``capture_sources`` readers return (the
window, the candidate pairs, the disposition and the anchor rule) and
``build_audit``, which reads the bins and the ledger and writes
``state/capture_audit.json`` atomically with hashes, ids and counts only.
Fail-closed: a bin that is not ``ok``, or a window without a new record, makes
``open_pairs`` null. A null is no measurement and never reads as zero.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

import capture_sources as cs
from atomic_io import atomic_write_json
from vnx_paths import project_id_from_state_dir

TOKEN_MIN_LEN = 4
JACCARD_MIN = 0.34
SHARED_MIN = 2

AUDIT_FILENAME = "capture_audit.json"
AUDIT_SCHEMA = "capture_audit/1"
LEDGER_FILENAME = "capture_dispositions.ndjson"
SOURCE_EXPLICIT, SOURCE_CHAIN, SOURCE_FIRST24H = "explicit", "chain", "first24h"
FIRST_WINDOW = timedelta(hours=24)

OI, MEM, NUG = "open_items", "memory", "bucket"
BINS = (OI, MEM, NUG)
# Pair kind -> (home bin, other bin). Home order: open item, note, nugget (decision 3).
KINDS: Dict[str, Tuple[str, str]] = {"oi_mem": (OI, MEM), "oi_nug": (OI, NUG), "mem_nug": (MEM, NUG)}

_TOKEN_RE = re.compile(r"[^\W\d_]+")
_PAIR_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_MONTH_FILE_RE = re.compile(r"^(\d{4}-(?:0[1-9]|1[0-2]))\.md$")


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


def previous_chain_until(state_dir: Path) -> Optional[datetime]:
    """Start of the next chain run: the until of the last successful chain or
    first24h audit. None only when the file does not exist; a file that cannot
    be read or has another schema raises ValueError, it is never a first run."""
    try:
        data = json.loads((Path(state_dir) / AUDIT_FILENAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError("the previous capture_audit.json cannot be read") from exc
    window = data.get("window") if isinstance(data, dict) else None
    if not isinstance(window, dict) or data.get("schema") != AUDIT_SCHEMA or "chain_until" not in window:
        raise ValueError("the previous capture_audit.json has an unknown schema")
    return None if window["chain_until"] is None else _aware(window["chain_until"], "previous chain_until")


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


def _valid_dismissal(row: object) -> bool:
    fields = ("pair_id", "reason", "at", "by")
    if not isinstance(row, dict) or not all(isinstance(row.get(f), str) and row[f].strip() for f in fields):
        return False
    try:
        _aware(row["at"], "at")
    except ValueError:
        return False
    return bool(_PAIR_ID_RE.match(row["pair_id"]))


def read_dismissed(state_dir: Path) -> FrozenSet[str]:
    """Pair ids with a valid ledger line. Read only; ``dismiss`` (PR-4) writes."""
    try:
        text = (Path(state_dir) / LEDGER_FILENAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # vnx-silent-except: no readable ledger dismisses nothing, every pair stays open
        return frozenset()
    ids = set()
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:  # vnx-silent-except: a broken line is no dismissal, its pair stays open
            continue
        if _valid_dismissal(row):
            ids.add(row["pair_id"])
    return frozenset(ids)


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


def _bucket_months(binding: dict, window: Window) -> List[str]:
    months = {window.until.strftime("%Y-%m")}
    try:
        names = os.listdir(Path(binding["bucket_dir"]).expanduser())
    except OSError:  # vnx-silent-except: read_bucket_entries lists the dir again and reports error
        names = []
    months.update(m.group(1) for m in map(_MONTH_FILE_RE.match, names) if m)
    return sorted(months)


# Reader output per bin: (tuple length, key of the foreign count). read_memory_notes returns
# (records, status); read_open_items and read_bucket_entries add an int count of foreign records.
_READER_SHAPES: Dict[str, Tuple[int, Optional[str]]] = {
    OI: (3, "foreign_project_skipped"), MEM: (2, None), NUG: (3, "foreign_slug_skipped")}


def _normalize(bin_name: str, result: object) -> tuple:
    """A reader result as ``(records, state, extra_dict)``. Any other shape is ``error``, so the
    measurement goes null: an unexpected shape is never read as an ``ok`` bin."""
    size, foreign_key = _READER_SHAPES[bin_name]
    if not (isinstance(result, tuple) and len(result) == size and isinstance(result[0], list)
            and all(isinstance(r, dict) for r in result[0]) and isinstance(result[1], str)):
        return [], cs.STATE_ERROR, {}
    if foreign_key is None:
        return result[0], cs.state_of(result[1]), {}
    if type(result[2]) is not int or result[2] < 0:  # a bool, None or a negative number is no count
        return [], cs.STATE_ERROR, {}
    return result[0], cs.state_of(result[1]), {foreign_key: result[2]}


def _read_bins(state_dir: Path, window: Window) -> dict:
    """Per bin ``(records, state, extra_dict)``; all unbound without a binding."""
    binding, status = cs.load_binding(state_dir)
    if binding is None:
        return {b: ([], cs.state_of(status), {}) for b in BINS}
    return {OI: _normalize(OI, cs.read_open_items(state_dir)),
            MEM: _normalize(MEM, cs.read_memory_notes(binding)),
            NUG: _normalize(NUG, cs.read_bucket_entries(binding, _bucket_months(binding, window)))}


def build_audit(state_dir: Path, window: Window) -> dict:
    """Compute the read model for one window and write ``state/capture_audit.json`` atomically.

    Only a run with every bin ``ok`` on a ``chain`` or ``first24h`` window moves ``chain_until`` to
    its ``until``. An explicit or failed run carries the previous value on unchanged, so the chain
    stays gap-free; when that value cannot be read it raises ValueError and writes nothing, as it
    does for a state dir that does not exist."""
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        raise ValueError("the state dir is not an existing directory")
    project_id = project_id_from_state_dir(state_dir) or None
    model = audit_model(_read_bins(state_dir, window), window, project_id=project_id or "",
                        dismissed_ids=read_dismissed(state_dir))
    all_ok = all(row["state"] == cs.STATE_OK for row in model["bins"].values())
    chain = window.until if all_ok and window.source != SOURCE_EXPLICIT else previous_chain_until(state_dir)
    audit = {"schema": AUDIT_SCHEMA, "project_id": project_id,
             "window": {"since": window.since.isoformat(), "until": window.until.isoformat(),
                        "source": window.source, "chain_until": chain.isoformat() if chain else None},
             **model}
    atomic_write_json(state_dir / AUDIT_FILENAME, audit)
    return audit
