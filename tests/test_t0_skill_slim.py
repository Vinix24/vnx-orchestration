"""test_t0_skill_slim.py — keep the t0-orchestrator skill slim + delegating (PR-11).

The skill holds JUDGMENT; the mechanics (lane/provider routing, failure modes,
gate detail) live in docs/core/DISPATCH_RULES.md. These guards stop routing prose
from creeping back into the always-loaded skill.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SKILL = _ROOT / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md"
_RULES = _ROOT / "docs" / "core" / "DISPATCH_RULES.md"

# Line-count guard: the slim skill must stay under this ceiling.
_SKILL_MAX_LINES = 160


def test_skill_exists():
    assert _SKILL.is_file(), f"t0 skill missing at {_SKILL}"


def test_dispatch_rules_doc_exists():
    """The extracted ruleset the skill delegates to must exist."""
    assert _RULES.is_file(), "docs/core/DISPATCH_RULES.md (the enforced ruleset) is missing"


def test_skill_under_line_cap():
    n = len(_SKILL.read_text(encoding="utf-8").splitlines())
    assert n <= _SKILL_MAX_LINES, (
        f"t0 SKILL.md is {n} lines (cap {_SKILL_MAX_LINES}). Move mechanics/routing prose "
        f"into docs/core/DISPATCH_RULES.md and reference it; keep the skill judgment-only."
    )


def test_skill_delegates_to_dispatch_rules():
    """The skill must point to the ruleset, not restate it."""
    assert "DISPATCH_RULES.md" in _SKILL.read_text(encoding="utf-8"), (
        "t0 SKILL.md must reference docs/core/DISPATCH_RULES.md for the dispatch mechanics."
    )


def test_skill_has_no_provider_routing_table():
    """The provider-string cheat-sheet / lane tables belong in DISPATCH_RULES.md, not the skill.

    Heuristic: a markdown table row that names a concrete lane script or provider string
    (provider_dispatch.py / tmux_interactive_dispatch.py / litellm:) is routing prose.
    """
    offenders = [
        line.strip()
        for line in _SKILL.read_text(encoding="utf-8").splitlines()
        if "|" in line
        and re.search(r"provider_dispatch\.py|tmux_interactive_dispatch\.py|litellm:", line)
    ]
    assert not offenders, (
        "Provider/lane routing TABLE found in the skill — move it to DISPATCH_RULES.md. "
        f"Offending lines: {offenders}"
    )


def test_dispatch_rules_carries_the_routing():
    """Sanity: the ruleset actually contains the routing the skill no longer inlines."""
    rules = _RULES.read_text(encoding="utf-8")
    for token in ("provider_dispatch.py", "tmux_interactive_dispatch.py", "litellm:zai", "claude-tmux"):
        assert token in rules, f"DISPATCH_RULES.md is missing expected routing content: {token!r}"


# --- CLAUDE.md / snippet prune guards (PR-11) -------------------------------
# The community CLAUDE.md is regenerated from templates/snippets/CLAUDE_SNIPPET.md
# (vnx_marked_blocks.sh replaces the marked block from the snippet). So the snippet
# is the source of truth: prune the snippet, not just the rendered file, or re-init
# clobbers the prune. These guards keep both lean + delegating.

_CLAUDE_MD = _ROOT / "CLAUDE.md"
_SNIPPET = _ROOT / "templates" / "snippets" / "CLAUDE_SNIPPET.md"

# Verbose lane prose that was collapsed into a DISPATCH_RULES.md pointer (PR-11).
_PRUNED_LANE_PROSE = "leaseless ephemeral lane the README"


def test_claude_snippet_delegates_lane_detail():
    """The bootstrap snippet must point to DISPATCH_RULES.md, not re-inline lane prose."""
    snippet = _SNIPPET.read_text(encoding="utf-8")
    assert "docs/core/DISPATCH_RULES.md" in snippet, (
        "CLAUDE_SNIPPET.md must reference docs/core/DISPATCH_RULES.md for dispatch mechanics."
    )
    assert _PRUNED_LANE_PROSE not in snippet, (
        "Verbose tmux-spawn lane prose crept back into CLAUDE_SNIPPET.md — keep it in "
        "docs/core/DISPATCH_RULES.md / docs/operations/TMUX_SPAWN_LANE.md and link instead."
    )


def test_claude_md_in_sync_with_snippet():
    """The rendered CLAUDE.md bootstrap block must equal the snippet (no drift).

    Drift means the next `vnx patch-agent-files` / init silently rewrites CLAUDE.md
    from the snippet, dropping whatever was hand-added to the rendered file.
    """
    begin, end = "<!-- VNX:BEGIN BOOTSTRAP -->", "<!-- VNX:END BOOTSTRAP -->"
    md = _CLAUDE_MD.read_text(encoding="utf-8")
    s, e = md.find(begin), md.find(end)
    assert s != -1 and e != -1, "CLAUDE.md is missing the VNX bootstrap markers."
    block = md[s + len(begin):e].strip()
    snippet = _SNIPPET.read_text(encoding="utf-8").strip()
    assert block == snippet, (
        "CLAUDE.md bootstrap block has drifted from CLAUDE_SNIPPET.md. Regenerate the block "
        "from the snippet so re-init does not clobber rendered content."
    )


def test_claude_md_keeps_report_contract_and_local_override():
    """CLAUDE.md must keep the mandatory report contract and the local-override hook."""
    md = _CLAUDE_MD.read_text(encoding="utf-8")
    assert "Mandatory Report Contract" in md, "CLAUDE.md lost the mandatory report contract."
    assert "@~/.claude/vnx-local.md" in md, "CLAUDE.md lost the local-override import hook."
