"""Characterisation tests for golf-schoon R05:
T0IntelligenceGatherer.extract_tags_from_description.

Pins observable behaviour (return value, stdout, exit code) so the
extract-method refactor can be shown behaviour-preserving.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from gather_intelligence import T0IntelligenceGatherer


def _gatherer(agents=("alpha", "beta")):
    obj = object.__new__(T0IntelligenceGatherer)
    obj.quality_db = None
    obj.quality_db_path = Path("/nonexistent/quality.db")
    obj.tag_engine = None
    obj.agent_directory = list(agents)
    obj.vnx_path = Path("/tmp/vnx")
    obj.project_root = Path("/tmp/project")
    return obj


# ---- extract_tags_from_description ----------------------------------------

TAG_CASES = [
    ("", ["normal-priority"]),
    ("design the system api", ["design-phase", "system-architecture", "api-design",
                               "api-component", "normal-priority"]),
    ("implement feature fix", ["implementation-phase", "feature-implementation",
                               "bug-fix-implementation", "normal-priority"]),
    ("unit integration e2e production test", [
        "testing-phase", "unit-testing", "integration-testing", "e2e-testing",
        "production-validation", "production-phase", "normal-priority"]),
    ("deploy with rollback", ["production-phase", "production-rollback", "normal-priority"]),
    ("vnx hybrid crawler with chromium browser", [
        "crawler-component", "vnx-crawler", "hybrid-crawler", "browser-pool",
        "normal-priority"]),
    ("database query optimization", [
        "storage-component", "storage-query", "storage-optimization",
        "normal-priority"]),
    ("quickscan endpoint sse", [
        "api-component", "quickscan-api", "sse-pipeline", "sse-streaming", "quickscan",
        "normal-priority"]),
    ("streaming pipeline performance", [
        "sse-streaming", "sse-pipeline", "sse-performance", "performance",
        "normal-priority"]),
    ("quickscan preview extended", ["quickscan", "quickscan-preview", "quickscan-extended",
                                    "normal-priority"]),
    ("b2b test kvk", ["sme-b2b", "sme-b2b-testing", "dutch-compliance", "testing-phase",
                      "dutch-market", "normal-priority"]),
    ("schema validation error pipeline", [
        "validation", "validation-error", "validation-pipeline",
        "normal-priority"]),
    ("slow memory problem optimization", [
        "performance", "performance-issue", "performance-optimization",
        "memory-optimization", "memory", "normal-priority"]),
    ("memory leak chromium", ["memory", "memory-leak", "browser-memory", "normal-priority"]),
    ("race deadlock thread", ["concurrency", "race-condition", "deadlock", "normal-priority"]),
    ("refactor cleanup technical debt", ["refactoring", "technical-debt", "code-cleanup",
                                         "normal-priority"]),
    ("jwt vulnerability", ["security", "authentication", "security-vulnerability",
                           "normal-priority"]),
    ("netherlands decimal format btw", ["dutch-market", "dutch-compliance",
                                        "dutch-formatting", "normal-priority"]),
    ("critical blocker", ["critical-severity"]),
    ("important major", ["high-priority"]),
    ("moderate", ["medium-priority"]),
    ("critical high medium", ["critical-severity"]),
    ("high medium", ["high-priority"]),
]


@pytest.mark.parametrize("desc,expected", TAG_CASES)
def test_extract_tags_contains_expected(desc, expected):
    tags = _gatherer().extract_tags_from_description(desc)
    assert isinstance(tags, list)
    assert len(tags) == len(set(tags))
    assert set(expected) <= set(tags)
    severity = {"critical-severity", "high-priority", "medium-priority", "normal-priority"}
    assert len(severity & set(tags)) == 1


def test_extract_tags_exact_set_simple():
    tags = _gatherer().extract_tags_from_description("Design System API")
    assert sorted(tags) == sorted(["design-phase", "system-architecture", "api-design",
                                   "api-component", "normal-priority"])


def test_extract_tags_case_insensitive():
    g = _gatherer()
    assert sorted(g.extract_tags_from_description("CRITICAL JWT")) == sorted(
        g.extract_tags_from_description("critical jwt"))


def test_extract_tags_none_description_raises():
    with pytest.raises(AttributeError):
        _gatherer().extract_tags_from_description(None)
