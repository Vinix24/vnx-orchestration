"""ADR-078 §9 (M5a): ``run`` proposes structural memory fixes and writes only
``state/memory_doctor.json`` (atomic). Never a memory folder, binding or ledger; names only, no note text."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import atomic_io
import memory_ledger

SCHEMA, RESULT_FILE = "memory_doctor/1", "memory_doctor.json"
INDEX_LINES_MAX, INDEX_BYTES_MAX = 200, 25 * 1024  # plan point 4
OTHER_BINDINGS = ("capture_sources.json", "steering_sources.json")
_MD_LINK = re.compile(r"\]\(([^)\s]+\.md)\)")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")


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


def _check_folder(folder: Path, source: str, errors: List[dict]) -> List[dict]:
    texts = _read_notes(folder, source, errors)
    if texts is None:
        return []
    out: List[dict] = []
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
        for directory in binding["memory_dirs"]:
            proposals.extend(_check_folder(Path(directory), memory_ledger.source_id(directory), errors))
    proposals.extend(_divergence(binding, _other_binding_dirs(state_dir, errors)))
    if binding is not None:
        status = "degraded" if errors else "ok"
    doc = {"schema": SCHEMA, "generated_at": stamp, "status": status,
           "project_id": binding["project_id"] if binding else None, "reason": reason,
           "sources": [memory_ledger.source_id(d) for d in (binding or {}).get("memory_dirs", [])], "errors": errors, "proposals": proposals}
    atomic_io.atomic_write_json(state_dir / RESULT_FILE, doc)
    return doc
