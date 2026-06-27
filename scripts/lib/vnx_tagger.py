"""Model-agnostic LLM tagger over the VNX closed tag vocabulary (build-step 3b).

Layers an optional LLM tagging pass on top of the deterministic
vnx_tag_vocabulary.derive_tags() floor. The model is selected via env
(VNX_TAGGER_PROVIDER, default "deepseek" — the cheap key-auth DeepSeek-Flash
harness lane), so the tagging/review model is swappable without code changes.
Output is validated against the closed vocabulary (snap-to-vocab) so an
off-vocabulary value can never enter the matching layer.

Opt-in via VNX_TAGGER_ENABLED=1 (it makes an LLM call). Fail-silent: any error
returns [], so callers degrade to the deterministic floor and never break.

NOTE: this is a CAPABILITY, intentionally NOT wired into the per-dispatch hot
selection path (that would add LLM latency to every dispatch). It is meant for
persist-time pattern tagging or a scout pre-pass, where one call enriches many
later matches.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

try:
    from vnx_tag_vocabulary import (
        VNX_COMPONENTS,
        VNX_DOMAINS,
        VNX_INTENTS,
        derive_tags,
        validate_tags,
    )
except ImportError:  # pragma: no cover - path fallback
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from vnx_tag_vocabulary import (
        VNX_COMPONENTS,
        VNX_DOMAINS,
        VNX_INTENTS,
        derive_tags,
        validate_tags,
    )

ENV_ENABLED = "VNX_TAGGER_ENABLED"
ENV_PROVIDER = "VNX_TAGGER_PROVIDER"
_DEFAULT_PROVIDER = "deepseek"
_MAX_TAGS = 6
_TAGGABLE_TABLES = frozenset({"success_patterns", "antipatterns"})


def is_enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "0") == "1"


def get_tagger_provider_name() -> str:
    return (os.environ.get(ENV_PROVIDER) or _DEFAULT_PROVIDER).strip().lower()


def _build_prompt(text: str, paths: Optional[List[str]]) -> str:
    domains = ", ".join(sorted(VNX_DOMAINS))
    intents = ", ".join(sorted(VNX_INTENTS))
    components = ", ".join(sorted(VNX_COMPONENTS))
    paths_str = ", ".join(paths or []) or "(none)"
    return (
        "You are a strict classifier for the VNX Orchestration codebase. Assign "
        "tags to the work described below, choosing ONLY from the closed vocabulary. "
        "Do not invent tags. Pick the most relevant (at most "
        f"{_MAX_TAGS}).\n\n"
        f"DOMAIN (the subsystem): {domains}\n"
        f"INTENT (what the work is): {intents}\n"
        f"COMPONENT (cross-cutting concern): {components}\n\n"
        f"FILES: {paths_str}\n"
        f"WORK: {text}\n\n"
        'Return ONLY a JSON object: {"tags": ["...", "..."]} with values drawn '
        "from the vocabulary above. No prose."
    )


def llm_tags(text: str, paths: Optional[List[str]] = None) -> List[str]:
    """Return validated VNX tags from the configured LLM, or [] on any failure.

    Honours VNX_TAGGER_ENABLED (opt-in) and VNX_TAGGER_PROVIDER (model-agnostic).
    """
    if not is_enabled() or not (text or paths):
        return []
    try:
        from classifier_providers import get_provider
        provider = get_provider(get_tagger_provider_name())
        if not provider.is_available():
            return []
        result = provider.classify(_build_prompt(text or "", paths), _max_tokens=200)
        if result.error:
            return []
        data = result.parsed_json
        if data is None and result.raw_response:
            try:
                data = json.loads(result.raw_response)
            except (json.JSONDecodeError, TypeError):
                data = None
        if not isinstance(data, dict):
            return []
        return validate_tags(list(data.get("tags", []))[: _MAX_TAGS * 2])
    except Exception:
        # Fail-silent: callers fall back to the deterministic floor.
        return []


def enrich_tags(text: str, paths: Optional[List[str]] = None) -> List[str]:
    """Deterministic tags + (when enabled) validated LLM tags, deduplicated.

    The deterministic derive_tags() is always the floor; the LLM only adds.
    """
    tags = list(derive_tags(text, paths))
    for t in llm_tags(text, paths):
        if t not in tags:
            tags.append(t)
    return tags


def enrich_pattern_tags(
    db_path: "object",
    *,
    limit: int = 200,
    only_untagged: bool = True,
    tables: "Optional[List[str]]" = None,
) -> dict:
    """Persist enriched tags onto success_patterns/antipatterns (`tags` JSON column).

    Serves both the one-time BACKFILL and ongoing enrichment: for each pattern
    (by default only those with no stored tags) it computes ``enrich_tags`` and
    writes the result as a JSON array. NO-OP when the tagger is disabled — storing
    the deterministic floor alone adds nothing the selector doesn't already derive
    on the fly, so the stored column is only worth populating when the LLM enriches.
    Best-effort + never raises; missing ``tags`` column → skipped.

    Returns a per-table count dict, e.g. ``{"success_patterns": 12, "antipatterns": 7}``.
    """
    if not is_enabled():
        return {"_skipped": "tagger_disabled"}
    import json as _json
    import sqlite3 as _sqlite3

    # Whitelist: only these identifiers are ever interpolated into SQL, so the
    # public ``tables`` arg can never reach an arbitrary table name (no injection).
    requested = tables or ["success_patterns", "antipatterns"]
    tbls = [t for t in requested if t in _TAGGABLE_TABLES]
    # Clamp into [1, 1_000_000]: the upper bound keeps the value SQLite-bindable
    # (a too-large int raises on bind) while staying far above any real table size.
    try:
        safe_limit = max(1, min(int(limit), 1_000_000))
    except (TypeError, ValueError, OverflowError):
        safe_limit = 200
    out: dict = {}
    try:
        conn = _sqlite3.connect(str(db_path))
    except _sqlite3.Error:
        return {"_skipped": "db_open_failed"}
    try:
        for tbl in tbls:
            try:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
            except _sqlite3.Error:
                continue
            if "tags" not in cols or "title" not in cols:
                continue
            where = "WHERE tags IS NULL OR tags = '' OR tags = '[]'" if only_untagged else ""
            desc_col = "COALESCE(description,'')" if "description" in cols else "''"
            try:
                rows = conn.execute(
                    f"SELECT id, title, {desc_col} FROM {tbl} {where} ORDER BY id DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
            except _sqlite3.Error:
                continue
            n = 0
            for pid, title, desc in rows:
                tags = enrich_tags(f"{title or ''} {desc or ''}".strip())
                try:
                    conn.execute(f"UPDATE {tbl} SET tags = ? WHERE id = ?", (_json.dumps(tags), pid))
                    n += 1
                except _sqlite3.Error:
                    continue
            conn.commit()
            out[tbl] = n
    finally:
        conn.close()
    return out
