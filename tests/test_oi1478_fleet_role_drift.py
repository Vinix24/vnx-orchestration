"""Regression tests for the fleet role-drift meter (OI-1478 follow-up).

OI-1478 was closed by propagating nine role files by hand across three
consumer repos. What stayed open is the reason that took a hand: nothing
measured whether the fleet was behind. ``scripts/fleet_role_drift.py`` is that
meter, and these tests pin the three properties that make it worth having
rather than a second thing to keep in sync.

  1. It measures the DIFFERENCE with the canon, never the presence of a known
     sentence. The sweep that closed OI-1478 grepped for "NEVER claude -p" and
     would have walked past the stale ``_FAKE_DEFAULT_ROLE`` sentinel rule the
     same nine files also carried. ``test_drift_is_found_without_knowing_the_
     string`` uses a divergence with no sentinel in it at all.

  2. A file that is up to date is not thereby a file that is READ (OI-1480,
     SEOcrawler_v2) and not thereby a startup that can COMPLETE (OI-1481,
     sales-copilot). Both are independent axes with their own tests.

  3. The meter does not write to the live store unless asked. A measurement
     that writes is a write, and an unasked-for write is how gate evidence got
     destroyed on 2026-08-26.

Everything runs against tmp_path — never the registry, never the real repos.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import fleet_role_drift as frd

CANON = """# T0 - VNX Master Orchestrator

You are T0. You orchestrate work and governance.

- Provider->lane (hard): claude/Opus/Sonnet default to the headless lane.
- identity_unresolved is the sentinel default for "no role resolved".
"""

# A stale copy whose ONLY divergence is a line carrying neither the string the
# OI-1478 sweep searched for nor any other sentinel. If the meter finds this,
# it is diffing, not grepping.
STALE_ROLE = CANON.replace(
    "- identity_unresolved is the sentinel default for \"no role resolved\".",
    "- backend-developer is the sentinel default for \"no role resolved\".",
)


def _make_repo(
    root: Path,
    *,
    role: str | None = CANON,
    claude_md: str | None = "@role-orchestrator.md\n",
    agents_block: str | None = None,
    gemini_block: str | None = None,
    skill_md: str | None = None,
    hook: str | None = None,
    settings: str | None = None,
) -> Path:
    t0 = root / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    if role is not None:
        (t0 / "role-orchestrator.md").write_text(role, encoding="utf-8")
    if claude_md is not None:
        (t0 / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    for name, block in (("AGENTS.md", agents_block), ("GEMINI.md", gemini_block)):
        if block is not None:
            (t0 / name).write_text(
                f"# provider file\n\n{frd.ROLE_MARKER_BEGIN}\n{block}\n{frd.ROLE_MARKER_END}\n",
                encoding="utf-8",
            )
    if skill_md is not None:
        skill = root / ".claude" / "skills" / "t0-orchestrator"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if hook is not None:
        hooks = root / ".claude" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "sessionstart.sh").write_text(hook, encoding="utf-8")
    if settings is not None:
        (root / ".claude").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "settings.json").write_text(settings, encoding="utf-8")
    return root


WIRED_SETTINGS = json.dumps({"hooks": {"SessionStart": [{"command": "hooks/sessionstart.sh"}]}})
WIRED_HOOK = 'cat "$ROOT/.claude/skills/t0-orchestrator/SKILL.md"\n'


# ---------------------------------------------------------------------------
# 1. Content — difference, not string search
# ---------------------------------------------------------------------------


def test_drift_is_found_without_knowing_the_string(tmp_path):
    repo = _make_repo(tmp_path / "consumer", role=STALE_ROLE)

    result = frd.measure_project(repo, CANON)
    surface = result["content"]["surfaces"][frd.ROLE_BASENAME]

    assert surface["status"] == frd.STALE
    assert surface["drift_lines"] == 2, "one changed line = one removal + one addition"
    assert result["behind"] is True
    # The sweep that closed OI-1478 searched for this and would have found
    # nothing in this file, yet the file is demonstrably behind the canon.
    assert "NEVER claude -p" not in STALE_ROLE
    joined = " ".join(surface["sample"])
    assert "backend-developer" in joined and "identity_unresolved" in joined, (
        "the sample must hand back WHAT drifted — the string the meter refuses "
        "to search for in advance is the one it should report"
    )


def test_identical_role_is_current(tmp_path):
    repo = _make_repo(tmp_path / "consumer", hook=WIRED_HOOK, settings=WIRED_SETTINGS)

    result = frd.measure_project(repo, CANON)

    assert result["content"]["surfaces"][frd.ROLE_BASENAME]["status"] == frd.CURRENT
    assert result["behind"] is False


def test_absent_role_file_is_behind(tmp_path):
    repo = _make_repo(tmp_path / "consumer", role=None)

    result = frd.measure_project(repo, CANON)

    assert result["content"]["surfaces"][frd.ROLE_BASENAME]["status"] == frd.ABSENT
    assert result["behind"] is True


# ---------------------------------------------------------------------------
# 2. Provider mirrors — absence is a warning, drift is drift
# ---------------------------------------------------------------------------


def test_absent_provider_mirrors_warn_but_do_not_read_as_drift(tmp_path):
    """AGENTS.md/GEMINI.md are gitignored, locally generated by role sync. A
    fresh checkout has neither and is not thereby behind — measured on this
    branch's own worktree, where the first version of the meter went red."""
    repo = _make_repo(tmp_path / "consumer", hook=WIRED_HOOK, settings=WIRED_SETTINGS)

    result = frd.measure_project(repo, CANON)

    assert [s["status"] for s in result["content"]["surfaces"].values()].count(frd.ABSENT) == 2
    assert result["behind"] is False
    assert len(result["warnings"]) == 2
    assert any("Codex" in w for w in result["warnings"])


def test_stale_provider_block_is_drift(tmp_path):
    repo = _make_repo(tmp_path / "consumer", agents_block=STALE_ROLE, gemini_block=CANON)

    result = frd.measure_project(repo, CANON)

    assert result["content"]["surfaces"]["AGENTS.md"]["status"] == frd.STALE
    assert result["content"]["surfaces"]["GEMINI.md"]["status"] == frd.CURRENT
    assert result["behind"] is True


def test_provider_file_without_a_marker_block_says_so(tmp_path):
    repo = _make_repo(tmp_path / "consumer")
    (repo / ".claude" / "terminals" / "T0" / "AGENTS.md").write_text(
        "# hand-written, never synced\n", encoding="utf-8",
    )

    result = frd.measure_project(repo, CANON)
    surface = result["content"]["surfaces"]["AGENTS.md"]

    assert surface["status"] == frd.ABSENT
    assert "no <!-- VNX:BEGIN T0-ROLE --> block" in surface["reason"]


# ---------------------------------------------------------------------------
# 3. Reach — an updated file is not a read file (OI-1480)
# ---------------------------------------------------------------------------


def test_role_current_but_never_imported_is_behind(tmp_path):
    """SEOcrawler_v2's exact shape: sync writes a perfectly current role and no
    session ever loads it."""
    repo = _make_repo(
        tmp_path / "consumer", claude_md="# project context, no import\n",
        skill_md="---\nname: t0-orchestrator\n---\n",
    )

    result = frd.measure_project(repo, CANON)

    assert result["content"]["ok"] is True, "content is current — that is the trap"
    assert result["reach"]["ok"] is False
    assert "OI-1480" in result["reach"]["reason"]
    assert result["behind"] is True


def test_missing_claude_md_breaks_reach(tmp_path):
    repo = _make_repo(tmp_path / "consumer", claude_md=None)

    result = frd.measure_project(repo, CANON)

    assert result["reach"]["ok"] is False
    assert result["behind"] is True


# ---------------------------------------------------------------------------
# 4. Startup — the role prescribes STOP when every route is dead (OI-1481)
# ---------------------------------------------------------------------------


def test_every_delivery_route_dead_is_behind(tmp_path):
    """sales-copilot's exact shape."""
    repo = _make_repo(tmp_path / "consumer")

    result = frd.measure_project(repo, CANON)

    assert result["startup"]["ok"] is False
    assert "OI-1481" in result["startup"]["reason"]
    assert result["behind"] is True
    assert result["startup"]["routes"]["skill_tool_fallback"]["ok"] is False


def test_wired_sessionstart_hook_satisfies_startup(tmp_path):
    repo = _make_repo(tmp_path / "consumer", hook=WIRED_HOOK, settings=WIRED_SETTINGS)

    result = frd.measure_project(repo, CANON)

    assert result["startup"]["routes"]["sessionstart_hook"]["ok"] is True
    assert result["startup"]["ok"] is True
    assert result["behind"] is False


def test_settings_wired_but_hook_does_not_inject_is_not_enough(tmp_path):
    """The measured sales-copilot state: SessionStart exists, the script does
    not mention the skill. Wired is not injecting."""
    repo = _make_repo(
        tmp_path / "consumer", hook="echo hello\n", settings=WIRED_SETTINGS,
    )

    result = frd.measure_project(repo, CANON)

    assert result["startup"]["routes"]["sessionstart_hook"]["ok"] is False
    assert "no hook script references" in result["startup"]["routes"]["sessionstart_hook"]["reason"]


def test_model_invocable_skill_satisfies_startup(tmp_path):
    repo = _make_repo(
        tmp_path / "consumer",
        skill_md="---\nname: t0-orchestrator\nuser-invocable: true\n---\n\n# T0 Orchestrator\n",
    )

    result = frd.measure_project(repo, CANON)

    assert result["startup"]["routes"]["skill_tool_fallback"]["ok"] is True
    assert result["startup"]["ok"] is True


def test_disabled_skill_is_not_a_fallback(tmp_path):
    repo = _make_repo(
        tmp_path / "consumer",
        skill_md="---\nname: t0-orchestrator\ndisable-model-invocation: true\n---\n",
    )

    result = frd.measure_project(repo, CANON)

    assert result["startup"]["routes"]["skill_tool_fallback"]["ok"] is False


def test_description_mentioning_the_flag_is_not_a_disabled_skill(tmp_path):
    """codex finding 3 (2026-07-16): an unanchored grep read a description that
    merely DOCUMENTS the flag as the flag itself. t0_role_audit.sh carries the
    anchored check; this one must not diverge from it."""
    repo = _make_repo(
        tmp_path / "consumer",
        skill_md=(
            "---\n"
            "name: t0-orchestrator\n"
            "description: explains disable-model-invocation: true and when to set it\n"
            "---\n"
        ),
    )

    result = frd.measure_project(repo, CANON)

    assert result["startup"]["routes"]["skill_tool_fallback"]["ok"] is True


# ---------------------------------------------------------------------------
# 5. The meter's own input — a stale canon propagates yesterday's role
# ---------------------------------------------------------------------------


def test_canon_behind_the_fabric_source_is_reported(tmp_path):
    """Vincent's 2026-08-28 decision (canon on main first, propagate second)
    exists because a stale install pushes an old role to the whole fleet in one
    command, and every consumer then reports current against it."""
    source = _make_repo(tmp_path / "fabric", role=CANON)
    install = _make_repo(tmp_path / "install", role=STALE_ROLE)
    canon_path = install / ".claude" / "terminals" / "T0" / "role-orchestrator.md"

    freshness = frd._measure_canon_freshness(
        canon_path, STALE_ROLE,
        [{"project_id": "vnx-dev", "path": str(source), "name": "vnx-orchestration"}],
    )

    assert freshness["ok"] is False
    assert "stale role" in freshness["reason"]


def test_canon_that_is_the_source_is_fresh(tmp_path):
    source = _make_repo(tmp_path / "fabric", role=CANON)
    canon_path = source / ".claude" / "terminals" / "T0" / "role-orchestrator.md"

    freshness = frd._measure_canon_freshness(
        canon_path, CANON, [{"project_id": "vnx-dev", "path": str(source)}],
    )

    assert freshness["ok"] is True


# ---------------------------------------------------------------------------
# 6. Exit codes and the no-write default
# ---------------------------------------------------------------------------


def _canon_arg(tmp_path) -> list[str]:
    canon_repo = _make_repo(tmp_path / "canonrepo", role=CANON)
    return ["--canon", str(canon_repo / ".claude" / "terminals" / "T0" / "role-orchestrator.md")]


def test_exit_zero_when_the_fleet_is_current(tmp_path, capsys):
    clean = _make_repo(tmp_path / "clean", hook=WIRED_HOOK, settings=WIRED_SETTINGS)

    rc = frd.main([*_canon_arg(tmp_path), "--project-dir", str(clean)])

    assert rc == 0
    assert "0 of 1 measured projects behind" in capsys.readouterr().out


def test_exit_one_when_a_project_is_behind(tmp_path):
    stale = _make_repo(tmp_path / "stale", role=STALE_ROLE, hook=WIRED_HOOK, settings=WIRED_SETTINGS)

    rc = frd.main([*_canon_arg(tmp_path), "--project-dir", str(stale)])

    assert rc == 1


def test_exit_two_when_the_canon_cannot_be_read(tmp_path, capsys):
    rc = frd.main(["--canon", str(tmp_path / "nope.md"), "--project-dir", str(tmp_path)])

    assert rc == 2
    assert "canonical role not readable" in capsys.readouterr().err


def test_exit_two_on_an_unreadable_registry(tmp_path, capsys):
    bad = tmp_path / "projects.json"
    bad.write_text("{not json", encoding="utf-8")

    rc = frd.main([*_canon_arg(tmp_path), "--registry", str(bad)])

    assert rc == 2


def test_measuring_writes_nothing_without_write_state(tmp_path):
    """A measurement that writes is a write. The gate-evidence losses of
    2026-08-26 all came from a tool that wrote while it was 'just checking'."""
    clean = _make_repo(tmp_path / "clean", hook=WIRED_HOOK, settings=WIRED_SETTINGS)
    state = tmp_path / "state"
    state.mkdir()

    frd.main([*_canon_arg(tmp_path), "--project-dir", str(clean), "--state-dir", str(state)])

    assert list(state.rglob("*")) == [], "the default run must leave the store untouched"


def test_write_state_is_opt_in_and_lands(tmp_path):
    clean = _make_repo(tmp_path / "clean", hook=WIRED_HOOK, settings=WIRED_SETTINGS)
    state = tmp_path / "state"
    state.mkdir()

    frd.main([*_canon_arg(tmp_path), "--project-dir", str(clean),
              "--state-dir", str(state), "--write-state"])

    beacon = state / "health" / "fleet_role_drift.json"
    assert beacon.exists()
    payload = json.loads(beacon.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["details"]["projects_behind"] == 0


def test_json_output_is_machine_readable(tmp_path, capsys):
    stale = _make_repo(tmp_path / "stale", role=STALE_ROLE)

    frd.main([*_canon_arg(tmp_path), "--project-dir", str(stale), "--json"])

    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["projects_behind"] == 1
    assert report["summary"]["behind"] == ["stale"]
