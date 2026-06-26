"""Tests for the VNX tag vocabulary (build-step 3a, deterministic tagging)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

from vnx_tag_vocabulary import (  # noqa: E402
    VNX_TAG_VOCABULARY,
    VNX_DOMAINS,
    VNX_INTENTS,
    VNX_COMPONENTS,
    derive_tags,
    validate_tags,
)


def test_facets_are_disjoint_and_unioned():
    assert VNX_TAG_VOCABULARY == VNX_DOMAINS | VNX_INTENTS | VNX_COMPONENTS
    assert VNX_DOMAINS and VNX_INTENTS and VNX_COMPONENTS


def test_derive_maps_dispatch_bug():
    tags = derive_tags("fix a bug in the dispatch lane routing", ["scripts/lib/dispatch_cli.py"])
    assert "dispatch" in tags
    assert "fix_bug" in tags
    # everything returned is in the closed vocabulary
    assert all(t in VNX_TAG_VOCABULARY for t in tags)


def test_derive_maps_schema_migration():
    tags = derive_tags("migrate the schema, add composite project_id key", ["schemas/x.sql"])
    assert "schema_migrations" in tags
    assert "migrate_schema" in tags
    assert "project_id_stamping" in tags


def test_derive_empty_on_no_match():
    assert derive_tags("zzz qqq", []) == []
    assert derive_tags("", None) == []


def test_derive_dedups_and_is_facet_ordered():
    tags = derive_tags("dispatch dispatch fix fix", [])
    assert tags.count("dispatch") == 1
    # domain tag appears before intent tag (facet order)
    assert tags.index("dispatch") < tags.index("fix_bug")


def test_validate_snaps_to_vocabulary():
    assert validate_tags(["dispatch", "bogus_tag", "fix_bug", ""]) == ["dispatch", "fix_bug"]
    assert validate_tags(None) == []


def test_tag_overlap_boosts_relevance_score():
    """The selector's rank score must rise when query tags match the item's
    derived tags (intent/subsystem matching, not just file paths)."""
    from intelligence_selector import _relevance_score
    from intelligence_sources._common import IntelligenceItem
    item = IntelligenceItem(
        item_id="i", item_class="proven_pattern", title="dispatch lane fix",
        content="how to fix dispatch routing", confidence=0.8, evidence_count=2,
        last_seen="2026-06-26T00:00:00Z", scope_tags=[],
    )
    matched = _relevance_score(item, ["dispatch", "fix_bug"])
    unmatched = _relevance_score(item, ["benchmark"])
    assert matched > unmatched
