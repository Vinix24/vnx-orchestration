#!/usr/bin/env python3
"""Tests for scripts/lib/architecture_components.py and
scripts/generate_architecture_doc.py (D6a).

docs/core/00_VNX_ARCHITECTURE.md used to hand-list 15 "Active Components".
Two measured drift examples this dispatch fixes: "Smart Tap V7" / "Unified
State Manager V2" named launcher-era aliases start_all() no longer runs
(smart_tap_json_translator.sh / unified_state_manager.py -- no v7/v2 in
either filename), and "Worker Intelligence Injection" claimed a hook that
.claude/settings.json never wires. These tests pin: the generated daemon
table is sourced from start_all() (via daemon_register.read_daemon_register,
D2) with no stale aliases; the generated hooks table is sourced from
.claude/settings.json and correctly excludes the unwired script; a daemon
that the registry and the doc's description map disagree about fails
generation instead of silently drifting; the committed doc's generated
sections match the live registry right now; and no hand-typed checkmark
survives outside those generated sections.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

import architecture_components as ac  # noqa: E402
import generate_architecture_doc as gen  # noqa: E402
import check_docs_file_line_refs as line_refs_check  # noqa: E402

DOC_PATH = REPO_ROOT / "docs" / "core" / "00_VNX_ARCHITECTURE.md"


# ---------------------------------------------------------------------------
# Daemon rows sourced from start_all() (via daemon_register, D2)
# ---------------------------------------------------------------------------

def test_daemon_rows_cover_every_start_all_daemon():
    rows = ac.build_daemon_rows()
    names = {row["name"] for row in rows}
    assert names == {
        "dispatcher", "smart_tap", "receipt_processor", "heartbeat_ack_monitor",
        "queue_watcher", "dashboard", "state_manager", "intelligence_daemon",
        "recommendations_engine",
    }


def test_daemon_rows_carry_a_start_all_citation_line():
    rows = ac.build_daemon_rows()
    for row in rows:
        assert row["line"], f"{row['name']} has no citation line into start_all()"


def test_rendered_daemon_md_has_no_stale_v7_v2_aliases():
    """Regression: the old doc's "Smart Tap V7" / "Unified State Manager V2"
    named scripts start_all() does not run. The real scripts carry no
    version suffix (smart_tap_json_translator.sh, unified_state_manager.py)."""
    rendered = ac.render_daemon_md(ac.build_daemon_rows())
    assert "V7" not in rendered
    assert "V2" not in rendered
    assert "smart_tap_json_translator.sh" in rendered
    assert "unified_state_manager.py" in rendered


def test_stale_daemon_description_fails_generation():
    """A DAEMON_DESCRIPTIONS entry for a daemon start_all() no longer
    declares must fail generation, not silently vanish from the doc."""
    original = dict(ac.DAEMON_DESCRIPTIONS)
    try:
        ac.DAEMON_DESCRIPTIONS["a_daemon_that_does_not_exist"] = "ghost"
        with pytest.raises(ValueError, match="no longer in start_all"):
            ac.build_daemon_rows()
    finally:
        ac.DAEMON_DESCRIPTIONS.clear()
        ac.DAEMON_DESCRIPTIONS.update(original)


def test_undescribed_live_daemon_fails_generation():
    """A daemon start_all() declares with no DAEMON_DESCRIPTIONS entry must
    fail generation, not render with a missing description."""
    original = dict(ac.DAEMON_DESCRIPTIONS)
    try:
        del ac.DAEMON_DESCRIPTIONS["dispatcher"]
        with pytest.raises(ValueError, match="no description"):
            ac.build_daemon_rows()
    finally:
        ac.DAEMON_DESCRIPTIONS.clear()
        ac.DAEMON_DESCRIPTIONS.update(original)


# ---------------------------------------------------------------------------
# Hook rows sourced from .claude/settings.json
# ---------------------------------------------------------------------------

def test_hook_rows_exclude_unwired_worker_intelligence_injection():
    """Regression: userpromptsubmit_worker_intelligence_inject.sh is not
    referenced by any hook event in .claude/settings.json (measured 30-08:
    grep -c on the file is 0)."""
    rows = ac.build_hook_rows()
    all_scripts = {name for row in rows for name in row["scripts"]}
    assert "userpromptsubmit_worker_intelligence_inject.sh" not in all_scripts


def test_hook_rows_include_the_real_userpromptsubmit_hook():
    rows = ac.build_hook_rows()
    by_event = {row["event"]: row["scripts"] for row in rows}
    assert "tmux_signal_prompt_received.sh" in by_event.get("UserPromptSubmit", [])


# ---------------------------------------------------------------------------
# The committed docs/core/00_VNX_ARCHITECTURE.md matches the live registry
# ---------------------------------------------------------------------------

def test_committed_doc_generated_sections_match_live_registry():
    committed_text = DOC_PATH.read_text(encoding="utf-8")
    assert gen.render(committed_text) == committed_text


def test_committed_doc_no_longer_claims_a_static_active_components_list():
    """The hand-maintained '### Active Components ✅' checklist (the section
    this dispatch replaced) must not reappear."""
    committed_text = DOC_PATH.read_text(encoding="utf-8")
    assert "### Active Components" not in committed_text
    assert "### Supervised Components" in committed_text


# ---------------------------------------------------------------------------
# No hand-typed checkmark outside the generated sections
# ---------------------------------------------------------------------------

# A checkmark is a status claim. Inside a generated section it resolves to a
# measurement; anywhere else it is typed by hand and nothing re-checks it. The
# old "Active Components" list and the four staging bullets ("Batch init",
# "Staging review", "Dependency-aware", "Popup trigger") were exactly that.
# The set is the checkmark family, not just U+2705, so swapping the glyph is
# not a way around the guard.
_CHECKMARKS = ("✅", "✔", "✓", "☑")


def _checkmarks_outside_generated(text: str) -> list[tuple[int, str]]:
    """Return (1-based line, line text) for every checkmark outside the
    generated sections. Each generated block is blanked to the same number of
    newlines so the reported line numbers stay those of the real file."""
    for name in gen._MARKERS:
        text = gen._marker_re(name).sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if any(mark in line for mark in _CHECKMARKS)
    ]


def test_committed_doc_has_no_checkmark_outside_generated_sections():
    committed_text = DOC_PATH.read_text(encoding="utf-8")
    offenders = _checkmarks_outside_generated(committed_text)
    assert offenders == [], (
        "a hand-typed checkmark is a status claim nothing measures; put it in a "
        "generated section or remove it: "
        + "; ".join(f"{DOC_PATH.name}:{number}: {line.strip()}" for number, line in offenders)
    )


@pytest.mark.parametrize("mark", _CHECKMARKS)
def test_checkmark_guard_flags_every_glyph_outside_a_generated_block(mark):
    text = f"intro\n- {mark} **Batch init**: claim\nouter\n"
    assert _checkmarks_outside_generated(text) == [(2, f"- {mark} **Batch init**: claim")]


def test_checkmark_guard_allows_a_checkmark_inside_a_generated_block_only():
    """The blanking must not shift line numbers: the offender after the
    generated block is reported at its real line."""
    text = (
        "intro\n"
        "<!-- BEGIN GENERATED: supervised-components -->\n"
        "- measured ✅\n"
        "<!-- END GENERATED: supervised-components -->\n"
        "- typed ✅\n"
    )
    assert _checkmarks_outside_generated(text) == [(5, "- typed ✅")]


def test_checkmark_guard_passes_a_doc_without_checkmarks():
    assert _checkmarks_outside_generated("plain prose\n- a bullet\n❌ not a checkmark\n") == []


def test_splice_block_raises_on_missing_markers():
    with pytest.raises(ValueError, match="marker pair"):
        gen.splice_block("no markers here", "supervised-components", "x")


def test_docs_file_line_refs_check_passes_on_the_generated_citations():
    """The generated daemon rows embed scripts/vnx_supervisor_simple.sh:NN
    citations; the existing docs file:line-ref CI check must accept them
    (D6a deliberately reuses that check instead of building a second one)."""
    paths = line_refs_check.tracked_rel_paths(REPO_ROOT)
    violations = line_refs_check.check_docs(REPO_ROOT / "docs", REPO_ROOT, paths)
    assert violations == []
