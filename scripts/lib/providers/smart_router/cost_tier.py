"""cost_tier.py — Cost-tier classifier for smart router dispatches.

classify_dispatch(task_spec, file_paths, loc_estimate) -> str
Returns one of: 'tier-zero', 'tier-low', 'tier-mid', 'tier-high'.

Tier definitions:
  tier-zero  Trivial reformat; ≤30 LOC, single file, no elevated keywords.
  tier-low   Script edit; >30–150 LOC, no arch/schema changes.
  tier-mid   Multi-file or schema/design choices; 150–300 LOC.
  tier-high  Architectural, cross-component, or security-touching.
"""
from __future__ import annotations

TIER_ZERO = "tier-zero"
TIER_LOW = "tier-low"
TIER_MID = "tier-mid"
TIER_HIGH = "tier-high"

_LOC_ZERO_MAX = 30
_LOC_LOW_MAX = 150
_LOC_MID_MAX = 300

_SECURITY_KEYWORDS: frozenset[str] = frozenset({
    "auth", "authentication", "authorization", "oauth", "jwt", "token",
    "password", "credential", "secret", "encryption", "decrypt",
    "vulnerability", "cve", "exploit", "injection", "xss", "csrf",
    "rbac", "acl", "permission", "privilege", "security",
})

_SCHEMA_KEYWORDS: frozenset[str] = frozenset({
    "schema", "migration", "database", "table", "column", "index",
    "constraint", "adr", "architecture", "design", "interface",
    "event stream", "ndjson",
})

_ARCH_KEYWORDS: frozenset[str] = frozenset({
    "orchestrat", "cross-component", "refactor", "restructure", "rewrite",
    "rearchitect", "breaking change", "backward compat", "deprecat",
})


def _has_keyword(text: str, keywords: frozenset) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def classify_dispatch(
    task_spec: dict,
    file_paths: list,
    loc_estimate: int,
) -> str:
    """Classify a dispatch into one of four cost tiers.

    Rules applied in priority order (first match wins):
    1. Security-touching keywords or 'security' tag → tier-high
    2. Architectural/cross-component keywords → tier-high
    3. LOC > 300 → tier-high
    4. Schema/design keywords → tier-mid
    5. Multi-file (>1) AND LOC > 30 → tier-mid
    6. LOC > 150 → tier-mid
    7. LOC > 30 → tier-low
    8. Default (≤30 LOC, single file, no elevated keywords) → tier-zero
    """
    instruction = (task_spec.get("instruction") or task_spec.get("prompt") or "")
    tags = [t.lower() for t in (task_spec.get("tags") or [])]
    full_text = f"{instruction} {' '.join(tags)}"
    n_files = len(file_paths) if file_paths else 0

    if "security" in tags or _has_keyword(full_text, _SECURITY_KEYWORDS):
        return TIER_HIGH
    if _has_keyword(full_text, _ARCH_KEYWORDS):
        return TIER_HIGH
    if loc_estimate > _LOC_MID_MAX:
        return TIER_HIGH
    if _has_keyword(full_text, _SCHEMA_KEYWORDS):
        return TIER_MID
    if n_files > 1 and loc_estimate > _LOC_ZERO_MAX:
        return TIER_MID
    if loc_estimate > _LOC_LOW_MAX:
        return TIER_MID
    if loc_estimate > _LOC_ZERO_MAX:
        return TIER_LOW
    return TIER_ZERO
