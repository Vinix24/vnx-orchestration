"""scout_prepass — sidecar contract for the cheap-model scout pre-pass.

Build-step 5 (consumption side). The scout pre-pass is a cheap-model recon that
ranks the deterministic code-anchor candidates for a dispatch into
INCLUDE/MAYBE/EXCLUDE verdicts plus a short plan sketch, written to a per-dispatch
``scout_context.json`` sidecar BEFORE the permit is issued. The producer (the
door call + provider invocation) lands in a follow-up; this module owns the
sidecar location, the read path, and the rendering of the injected sketch so the
intelligence layer can consume a sidecar the moment one exists.

Design contract:
  - The sidecar is a SEPARATE file keyed by dispatch_id — it never mutates the
    instruction, so the permit / instruction_sha256 TOCTOU is untouched.
  - Reading is best-effort and fail-open: a missing / malformed sidecar yields
    None and the worker falls back to the deterministic code_anchor injection.
  - The rendered sketch is bounded (SCOUT_SKETCH_MAX_CHARS) and pointer-only —
    file:line ranges, never code bodies.

Sidecar schema (v1):
    {
      "schema_version": 1,
      "dispatch_id": "<id>",
      "generated_at": "<ISO8601>",
      "provider": "deepseek",
      "model": "deepseek-v4-flash",
      "include": [{"ref": "scripts/lib/foo.py:10-20", "why": "..."}],
      "maybe":   [{"ref": "scripts/lib/foo.py:50-60", "why": "..."}],
      "exclude": [{"ref": "...", "why": "..."}],
      "tests":   ["tests/test_foo.py"],
      "docs":    ["docs/foo.md"],
      "plan_sketch": "one or two line sketch"
    }
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCOUT_SUBDIR = "scout"
SCOUT_SIDECAR_SUFFIX = ".json"
SCHEMA_VERSION = 1

# Path-containment contract: a dispatch_id becomes a filesystem path segment, so
# it must not carry separators, '..', or absolute-path markers. Mirrors
# worker_permission_relay._safe_dispatch_id so the scout sidecar can never escape
# <state_dir>/scout/ via a malformed / hostile id (kimi-gate finding).
_SAFE_DISPATCH_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_dispatch_id(dispatch_id: str) -> str:
    """Return *dispatch_id* unchanged if safe as a path segment, else raise ValueError."""
    if not isinstance(dispatch_id, str) or not dispatch_id:
        raise ValueError("dispatch_id must be a non-empty string")
    if dispatch_id in (".", ".."):
        raise ValueError(f"unsafe dispatch_id {dispatch_id!r}: '.'/'..' are path-traversal segments")
    if not _SAFE_DISPATCH_ID.match(dispatch_id):
        raise ValueError(
            f"unsafe dispatch_id {dispatch_id!r}: must match [A-Za-z0-9._-]+ "
            "(no path separators, '..', or absolute paths)"
        )
    return dispatch_id

# The rendered sketch competes for the 2000-char direct-injection payload budget,
# so keep it well under that. Pointers + a one-line plan fit comfortably.
SCOUT_SKETCH_MAX_CHARS = 1200

# Defensive caps so a malformed / oversized sidecar can never blow the budget.
_MAX_REFS_PER_BUCKET = 8
_MAX_AUX_ITEMS = 5
_MAX_WHY_CHARS = 120
_MAX_PLAN_CHARS = 300


def scout_sidecar_path(state_dir: "Path | str", dispatch_id: str) -> Path:
    """Return the per-dispatch sidecar path: ``<state_dir>/scout/<dispatch_id>.json``.

    Raises ValueError when dispatch_id is not a safe path segment (traversal
    guard). Callers on the read path catch this and fail open.
    """
    return Path(state_dir) / SCOUT_SUBDIR / f"{_safe_dispatch_id(dispatch_id)}{SCOUT_SIDECAR_SUFFIX}"


def read_scout_sidecar(state_dir: "Path | str | None", dispatch_id: str) -> Optional[Dict[str, Any]]:
    """Best-effort read of a scout sidecar. Returns a dict, or None on any miss.

    Fail-open: a missing file, unreadable file, malformed JSON, or non-object
    payload all return None so the caller degrades to the deterministic anchors.
    """
    if state_dir is None or not dispatch_id:
        return None
    try:
        path = scout_sidecar_path(state_dir, dispatch_id)
    except ValueError as exc:
        # Hostile / malformed dispatch_id — fail open, never read outside scope.
        logger.debug("read_scout_sidecar: rejected unsafe dispatch_id %r: %s", dispatch_id, exc)
        return None
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("read_scout_sidecar: unreadable sidecar %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("read_scout_sidecar: malformed sidecar %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    # Defense-in-depth (kimi-gate advisory): ignore a sidecar from a future
    # schema or a different dispatch — a misplaced / stale file must never be
    # misread as this dispatch's context.
    sv = data.get("schema_version")
    if sv is not None and sv != SCHEMA_VERSION:
        logger.debug("read_scout_sidecar: unsupported schema_version %r for %s", sv, dispatch_id)
        return None
    sid = data.get("dispatch_id")
    if sid is not None and str(sid) != str(dispatch_id):
        logger.debug("read_scout_sidecar: sidecar dispatch_id %r != requested %r", sid, dispatch_id)
        return None
    return data


def _coerce_ref_list(value: Any) -> List[Dict[str, str]]:
    """Normalize an include/maybe/exclude bucket to a list of {ref, why} dicts."""
    out: List[Dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for entry in value[:_MAX_REFS_PER_BUCKET]:
        if isinstance(entry, dict):
            ref = str(entry.get("ref") or "").strip()
            why = str(entry.get("why") or "").strip()
        elif isinstance(entry, str):
            ref, why = entry.strip(), ""
        else:
            continue
        if not ref:
            continue
        out.append({"ref": ref, "why": why[:_MAX_WHY_CHARS]})
    return out


def _coerce_str_list(value: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(value, list):
        return out
    for entry in value[:_MAX_AUX_ITEMS]:
        s = str(entry).strip()
        if s:
            out.append(s)
    return out


def normalize_sidecar(sidecar: Dict[str, Any]) -> Dict[str, Any]:
    """Return a defensively-normalized view of the sidecar buckets.

    Coerces shapes, caps list lengths and string sizes, and drops junk — so the
    renderer (and any other consumer) works against a known-bounded structure
    regardless of what the producing model emitted.
    """
    return {
        "provider": str(sidecar.get("provider") or "").strip(),
        "include": _coerce_ref_list(sidecar.get("include")),
        "maybe": _coerce_ref_list(sidecar.get("maybe")),
        "exclude": _coerce_ref_list(sidecar.get("exclude")),
        "tests": _coerce_str_list(sidecar.get("tests")),
        "docs": _coerce_str_list(sidecar.get("docs")),
        "plan_sketch": str(sidecar.get("plan_sketch") or "").strip()[:_MAX_PLAN_CHARS],
    }


def sidecar_evidence_count(sidecar: Dict[str, Any]) -> int:
    """Number of ranked pointers the sketch carries (include + maybe)."""
    norm = normalize_sidecar(sidecar)
    return len(norm["include"]) + len(norm["maybe"])


def format_scout_sketch(sidecar: Dict[str, Any]) -> str:
    """Render the injected scout sketch markdown (pointer-only, bounded).

    EXCLUDE verdicts are intentionally NOT rendered — they only shrink the
    INCLUDE set and would add noise the worker should not anchor on. Returns ""
    when there is nothing useful to inject.
    """
    norm = normalize_sidecar(sidecar)
    if not (norm["include"] or norm["maybe"] or norm["plan_sketch"]
            or norm["tests"] or norm["docs"]):
        return ""

    provider = norm["provider"] or "cheap recon model"
    lines: List[str] = [
        "## SCOUT PRE-PASS (cheap-model recon — ranked grounding)",
        "",
        f"> A {provider} pre-pass ranked the candidate ranges below. Start with "
        "INCLUDE, skim MAYBE. These are pointers to live code — open each range "
        "and re-read if it looks stale.",
        "",
    ]
    if norm["include"]:
        lines.append("**Start here (INCLUDE):**")
        for it in norm["include"]:
            lines.append(_ref_line(it))
        lines.append("")
    if norm["maybe"]:
        lines.append("**Maybe relevant:**")
        for it in norm["maybe"]:
            lines.append(_ref_line(it))
        lines.append("")
    if norm["tests"]:
        lines.append("**Relevant tests:** " + ", ".join(f"`{t}`" for t in norm["tests"]))
    if norm["docs"]:
        lines.append("**Relevant docs:** " + ", ".join(f"`{d}`" for d in norm["docs"]))
    if norm["plan_sketch"]:
        lines.append("")
        lines.append(f"**Plan sketch:** {norm['plan_sketch']}")

    rendered = "\n".join(lines).rstrip()
    if len(rendered) <= SCOUT_SKETCH_MAX_CHARS:
        return rendered
    # Hard cap: truncate on a line boundary so we never emit a half-pointer.
    clipped = rendered[:SCOUT_SKETCH_MAX_CHARS]
    nl = clipped.rfind("\n")
    if nl > 0:
        clipped = clipped[:nl]
    return clipped.rstrip()


def _ref_line(item: Dict[str, str]) -> str:
    ref = item.get("ref", "")
    why = item.get("why", "")
    return f"- `{ref}` — {why}" if why else f"- `{ref}`"
