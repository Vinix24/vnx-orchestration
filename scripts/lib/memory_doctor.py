"""ADR-078 §9 (M5a): ``run`` proposes structural memory fixes and writes only
``state/memory_doctor.json`` (atomic). Never a memory folder, binding or ledger; names only, no note text."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import atomic_io
import lesson_refs
import memory_ledger
import operator_memory_indexer as omi

SCHEMA, RESULT_FILE = "memory_doctor/1", "memory_doctor.json"
INDEX_LINES_MAX, INDEX_BYTES_MAX = 200, 25 * 1024  # plan point 4
OTHER_BINDINGS = ("capture_sources.json", "steering_sources.json")
_MD_LINK = re.compile(r"\]\(([^)\s]+\.md)\)")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
_PR_REF = re.compile(r"(?<![\w#&])#(\d{3,})\b")
RECEIPTS_FILE = "t0_receipts.ndjson"


def _proposal(kind: str, source: str, note: str, evidence: Any) -> dict:
    key = json.dumps([kind, source, note, evidence], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {"id": hashlib.sha256(key.encode("utf-8")).hexdigest(), "kind": kind, "source": source, "note": note, "evidence": evidence}


def _wikilinks(text: str) -> List[str]:
    return [m.group(1).strip().removesuffix(".md") for m in _WIKILINK.finditer(text)]


def _targets(text: str) -> List[str]:
    """File names a text points to: index links and wikilinks, no URLs."""
    return [t for t in _MD_LINK.findall(text) if "://" not in t] + [n + ".md" for n in _wikilinks(text)]


def _read_notes(folder: Path, source: str, errors: List[dict]) -> Optional[Dict[str, str]]:
    try:
        names = sorted(p.name for p in folder.iterdir() if p.suffix == ".md" and p.is_file())
    except OSError as exc:
        errors.append({"source": source, "note": None, "reason": "os_error:" + type(exc).__name__})
        return None
    texts: Dict[str, str] = {}
    for name in names:
        try:
            texts[name] = (folder / name).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append({"source": source, "note": name, "reason": "unreadable:" + type(exc).__name__})
    return texts


def _covered(index_text: str, texts: Dict[str, str]) -> set:
    """Notes the index names, following [[naam]] and links through ``*-index`` notes."""
    seen, queue = set(), _targets(index_text)
    while queue:
        name = queue.pop()
        if name not in seen:
            seen.add(name)
            queue.extend(_targets(texts[name]) if name.endswith("-index.md") and name in texts else [])
    return seen


def _index_over_budget(index_text: str) -> str:
    over = ["lines>%d" % INDEX_LINES_MAX] if len(index_text.splitlines()) > INDEX_LINES_MAX else []
    over += ["bytes>%d" % INDEX_BYTES_MAX] if len(index_text.encode("utf-8")) > INDEX_BYTES_MAX else []
    return ",".join(over)


def _oi_states(state_dir: Path, project_id: str) -> Optional[Dict[str, str]]:
    """OI id -> status from the bound project's own store (``read_items``); ``None`` when unreadable."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # open_items_manager lives in scripts/
    from open_items_manager import read_items
    if not (state_dir / "open_items.json").is_file():  # read_items would hand back an empty store
        return None
    try:
        items = read_items(state_dir).get("items")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(items, list):
        return None
    return {i["id"]: i.get("status") for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)
            and i.get("project_id") in (None, project_id)}


def _merged_prs(state_dir: Path) -> Optional[set]:
    """PR numbers with a ``pr_merged`` receipt in the own ledger (never ``gh``); ``None`` when unreadable."""
    try:
        lines = (state_dir / RECEIPTS_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    merged = set()
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # vnx-silent-except: a torn line is no evidence; the PR stays unmerged
        number = rec.get("pr_number") if isinstance(rec, dict) and rec.get("event_type") == "pr_merged" else None
        if isinstance(number, (int, str)) and str(number).isdigit():
            merged.add(int(number))
    return merged


def _candidate(text: str, oi: Dict[str, str], merged: set) -> Optional[dict]:
    """ADR-078 §8: at least one OI, every OI known and ``done``/``wontfix``, every cited PR merged."""
    refs, prs = lesson_refs.find_oi_refs(text), sorted({int(n) for n in _PR_REF.findall(text)})
    if not refs or any(oi.get(r) not in lesson_refs.CLOSED_STATUSES for r in refs) or any(n not in merged for n in prs):
        return None
    return {"oi": {r: oi[r] for r in refs}, "pr_merged": prs}


def _validity(texts: Dict[str, str], folder: Path, source: str, now: datetime) -> List[dict]:
    """Warnings of §6 as proposals, from M3's own parser and chain rule (no second copy)."""
    mems, out = [], []
    for name, text in texts.items():
        state, _ = memory_ledger._frontmatter(text.encode("utf-8"))
        if state == "unparseable":
            out.append(_proposal("frontmatter_unparseable", source, name, state))
        mems.append(omi._parse_memory_file(folder / name, text))
    warnings: List[dict] = []
    omi.filter_valid_memories([m for m in mems if m is not None], now, warnings)
    for w in warnings:
        evidence = {k: v for k, v in w.items() if k != "note"}
        out.append(_proposal("validity_warning", source, w["note"], evidence))
    return out


def _check_content(texts: Dict[str, str], folder: Path, source: str, ctx: dict) -> List[dict]:
    out = _validity(texts, folder, source, ctx["now"])
    for name, text in texts.items():
        found = _candidate(text, ctx["oi"] or {}, ctx["merged"] or set())
        if found:
            out.append(_proposal("invalid_at_candidate", source, name, found))
    return out


def _check_folder(folder: Path, source: str, errors: List[dict], ctx: dict) -> List[dict]:
    texts = _read_notes(folder, source, errors)
    if texts is None:
        return []
    if ctx["oi"] is None and any(lesson_refs.find_oi_refs(t) for t in texts.values()):
        # no store to answer: every cited OI stays unknown, and the run says so (never zero findings in silence)
        errors.append({"source": source, "note": "open_items.json", "reason": "store_unreadable_or_absent"})
    out: List[dict] = _check_content(texts, folder, source, ctx)
    for name, text in texts.items():
        for target in sorted(set(_wikilinks(text))):
            if target + ".md" not in texts:
                out.append(_proposal("dead_wikilink", source, name, target))
    index = texts.get(memory_ledger.INDEX_NOTE)
    if index is None:
        if not any(e["note"] == memory_ledger.INDEX_NOTE for e in errors if e["source"] == source):
            errors.append({"source": source, "note": memory_ledger.INDEX_NOTE, "reason": "index_missing"})
        return out
    for target in sorted({t for t in _MD_LINK.findall(index) if "://" not in t}):
        if not (folder / target).is_file():
            out.append(_proposal("index_missing_file", source, memory_ledger.INDEX_NOTE, target))
    covered = _covered(index, texts)
    for name in texts:
        if name != memory_ledger.INDEX_NOTE and name not in covered:
            out.append(_proposal("file_not_in_index", source, name, name))
    over = _index_over_budget(index)
    if over:  # "" when within budget
        out.append(_proposal("index_over_budget", source, memory_ledger.INDEX_NOTE, over))
    return out


def _other_binding_dirs(state_dir: Path, errors: List[dict]) -> Dict[str, Optional[str]]:
    found: Dict[str, Optional[str]] = {}
    for fname in OTHER_BINDINGS:
        path = state_dir / fname
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))["memory_dir"]
            found[fname] = str(Path(value).expanduser().resolve()) if isinstance(value, str) and value else None
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            found[fname] = None
        if found[fname] is None:
            errors.append({"source": "project", "note": fname, "reason": "binding_unreadable_or_no_memory_dir"})
    return found


def _divergence(binding: Optional[dict], others: Dict[str, Optional[str]]) -> List[dict]:
    dirs = binding["memory_dirs"] if binding else None
    bad = [f for f, d in others.items() if d is not None and (dirs is None or d not in dirs)]
    if not bad:
        return []
    values = {"memory_sources": dirs, "capture_sources": others.get(OTHER_BINDINGS[0]), "steering_sources": others.get(OTHER_BINDINGS[1])}
    return [_proposal("binding_divergence", "project", memory_ledger.SOURCES_FILE, values)]


def run(state_dir: Path, *, now: Optional[datetime] = None) -> dict:
    """Propose structural fixes for the bound folders and write ``state/memory_doctor.json``."""
    state_dir = Path(state_dir)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    binding, reason = memory_ledger.load_sources(state_dir)
    errors: List[dict] = []
    proposals: List[dict] = []
    status = "unbound"
    if binding is None:
        proposals.append(_proposal("unbound", "project", memory_ledger.SOURCES_FILE, reason))
    else:
        ctx = {"now": now or datetime.now(timezone.utc), "oi": _oi_states(state_dir, binding["project_id"]),
               "merged": _merged_prs(state_dir)}
        for directory in binding["memory_dirs"]:
            proposals.extend(_check_folder(Path(directory), memory_ledger.source_id(directory), errors, ctx))
    proposals.extend(_divergence(binding, _other_binding_dirs(state_dir, errors)))
    if binding is not None:
        status = "degraded" if errors else "ok"
    doc = {"schema": SCHEMA, "generated_at": stamp, "status": status,
           "project_id": binding["project_id"] if binding else None, "reason": reason,
           "sources": [memory_ledger.source_id(d) for d in (binding or {}).get("memory_dirs", [])], "errors": errors, "proposals": proposals}
    atomic_io.atomic_write_json(state_dir / RESULT_FILE, doc)
    return doc
