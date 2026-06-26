"""VNX tag vocabulary — a closed, faceted tag taxonomy for intelligence matching.

The existing docs/intelligence/TAG_TAXONOMY.md is SEOcrawler-flavoured
(crawler/storage/api components). This module is the VNX-specific closed
vocabulary the design research recommended: three flat, independent facets
(domain / intent / component) used to match injected intelligence to a dispatch
by intent + subsystem, not just file-path overlap.

`derive_tags()` is the DETERMINISTIC floor (keyword/path → closed-vocab tags, no
LLM). A later model-agnostic LLM enrichment (build-step 3b) layers on top: it
will use the classifier_providers.get_provider() factory with the model selected
via an env var (VNX_TAGGER_MODEL / VNX_TAGGER_PROVIDER, defaulting to a cheap
key-auth lane such as DeepSeek-Flash), so the tagging/review model is swappable
in env without code changes. The LLM output is validated against this same closed
vocabulary (reject/snap off-vocab), so the deterministic and LLM paths share one
SSOT.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional

# --- Facet A: DOMAIN — the subsystem the work touches ---------------------
_DOMAIN_KEYWORDS: Dict[str, List[str]] = {
    "dispatch": ["dispatch", "lane", "door", "tmux", "worker", "worktree"],
    "intelligence": ["intelligence", "pattern", "anchor", "inject", "selector", "doc_relevant"],
    "receipts_audit": ["receipt", "ndjson", "audit", "t0_receipts", "provenance"],
    "governance_gates": ["gate", "govern", "phantom", "permit", "first-pass"],
    "providers_routing": ["provider", "kimi", "glm", "deepseek", "codex", "gemini", "routing", "litellm", "openrouter"],
    "schema_migrations": ["schema", "migration", "migrate", "alter table", "user_version", "sqlite"],
    "tenant_project_id": ["project_id", "tenant", "adr-007", "multitenant"],
    "tests_harness": ["pytest", "fixture", "harness", "test_"],
    "docs": ["docs/", "readme", "changelog", "documentation", "manifesto"],
    "dashboard_ui": ["dashboard", ".tsx", ".html", " css", " ui"],
    "benchmark": ["benchmark", "bench"],
    "learning_loop": ["learning_loop", "self-learning", "pending_rules", "confidence"],
}

# --- Facet B: INTENT — what the work is -----------------------------------
_INTENT_KEYWORDS: Dict[str, List[str]] = {
    "fix_bug": ["fix", "bug", "crash", "broken", "regression", "no such"],
    "implement_feature": ["implement", "feature", "introduce", "add a", "add the"],
    "refactor": ["refactor", "extract", "rename", "cleanup", "simplify", "deduplicate"],
    "add_test": ["add test", "coverage", "regression test", "unit test"],
    "harden": ["harden", "fail-closed", "fail closed", "guard", "robust", "edge case"],
    "migrate_schema": ["migrat", "rebuild", "alter table", "composite key"],
    "wire_integration": ["wire", "integrate", "hook up", "plumb"],
    "review_audit": ["review", "audit", "verify", "validate"],
    "document": ["document", "write docs", "changelog", "readme"],
    "investigate_rootcause": ["investigate", "root cause", "diagnose", "debug"],
}

# --- Facet C: COMPONENT — cross-cutting concerns --------------------------
_COMPONENT_KEYWORDS: Dict[str, List[str]] = {
    "fail_closed": ["fail-closed", "fail closed", "fail_closed", "abort on"],
    "idempotency": ["idempotent", "idempotency"],
    "concurrency_lease": ["lease", "concurren", "race condition", "deadlock", "savepoint", "lock"],
    "project_id_stamping": ["project_id", "stamp", "tenant"],
    "cli_subprocess": ["subprocess", "claude -p", "popen", "console-script"],
    "ndjson_contract": ["ndjson", "receipt contract", "report contract", "report_body_contract"],
    "fts5": ["fts5", "code_snippets", "full-text"],
    "provider_constraint": ["constraint", "no-sdk", "kimi-via-cli", "headless-block", "no anthropic sdk"],
}

_FACETS = (_DOMAIN_KEYWORDS, _INTENT_KEYWORDS, _COMPONENT_KEYWORDS)

VNX_DOMAINS: FrozenSet[str] = frozenset(_DOMAIN_KEYWORDS)
VNX_INTENTS: FrozenSet[str] = frozenset(_INTENT_KEYWORDS)
VNX_COMPONENTS: FrozenSet[str] = frozenset(_COMPONENT_KEYWORDS)
VNX_TAG_VOCABULARY: FrozenSet[str] = VNX_DOMAINS | VNX_INTENTS | VNX_COMPONENTS


def derive_tags(text: Optional[str], paths: Optional[List[str]] = None) -> List[str]:
    """Deterministically derive closed-vocabulary tags from free text + file paths.

    A keyword/path scan only — no LLM. Returns canonical tags (a subset of
    VNX_TAG_VOCABULARY), deduplicated, in facet order (domain, intent, component).
    """
    haystack = (text or "").lower()
    if paths:
        haystack += " " + " ".join(str(p).lower() for p in paths)
    tags: List[str] = []
    for facet in _FACETS:
        for tag, keywords in facet.items():
            if tag not in tags and any(kw in haystack for kw in keywords):
                tags.append(tag)
    return tags


def validate_tags(tags: Optional[List[str]]) -> List[str]:
    """Keep only tags that are in the closed vocabulary (snap-to-vocab).

    Used to validate LLM-assigned tags (build-step 3b) against the SSOT so an
    off-vocabulary value can never enter the matching layer.
    """
    return [t for t in (tags or []) if t in VNX_TAG_VOCABULARY]
