#!/usr/bin/env python3
"""Pin the canonical T0 role's lane rule to the lane the door actually resolves.

Dispatch-ID: D-rolelane-202226

The canonical orchestrator role (``.claude/terminals/T0/role-orchestrator.md``)
is synced fleet-wide by ``vnx role sync``. Its "Provider→lane (hard)" rule kept
forbidding ``claude -p`` in the fleet copies long after A2 (2026-08-26) made
``claude_headless`` the default, and the canon itself kept offering
``--force-tmux`` as the opt-out after 2026-09-12, when the door started refusing
it. Nothing failed either time: the rule was prose, and prose does not run.

The rule cannot be generated from the code. The role is static markdown that
Claude Code loads verbatim through ``@role-orchestrator.md`` and that
``vnx role sync`` copies verbatim, with no render step in between. So every lane
fact the rule states is computed here from the code that decides it, and then
looked up in the rule. Flip the default, reopen tmux, rename the brake or the
reject code, and this file goes red on the sentence that has to be rewritten.

Sources of truth: ``dispatch_plan.resolve_claude_lane`` (which lane a spec gets)
and ``dispatch_spec.validate`` Rule 12a/12b (which lane choices the door
accepts). A content pin, same shape as ``test_role_orchestrator_sentinel.py``.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path, PurePosixPath

import pytest
import yaml

import dispatch_cli
from dispatch_plan import resolve_claude_lane
from dispatch_spec import (
    DispatchPath,
    DispatchSpec,
    Provider,
    Reject,
    ValidatedSpec,
    validate,
)
from vnx_cli.main import _register_dispatch_agent_subparser

REPO = Path(__file__).resolve().parent.parent
ROLE_FILE = REPO / ".claude" / "terminals" / "T0" / "role-orchestrator.md"
CONSTRAINTS_FILE = REPO / "scripts" / "lib" / "providers" / "provider_constraints.yaml"
DISPATCH_SPEC_FILE = REPO / "scripts" / "lib" / "dispatch_spec.py"

RULE_HEAD = "- Provider→lane (hard):"
# The env var the rule names as the brake. Named here, then PROVEN to be the
# brake by test_brake_named_in_the_rule_reopens_the_tmux_lane: set it and the
# refused spec validates.
TMUX_BRAKE = "VNX_ALLOW_TMUX_LANE"
_PROJECT_ID = "vnx-dev"


def _rule() -> str:
    """The Provider→lane bullet plus its indented sub-bullets, as one string."""
    lines = ROLE_FILE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith(RULE_HEAD):
            block = [line]
            for nxt in lines[i + 1 :]:
                if not nxt.startswith("  "):
                    break
                block.append(nxt.strip())
            return "\n".join(block)
    pytest.fail(f"{ROLE_FILE.name} has no {RULE_HEAD!r} bullet: the lane rule is gone")


def _spec(tmp_path: Path, **overrides) -> DispatchSpec:
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Do the work.", encoding="utf-8")
    fields: dict = dict(
        schema_version=1,
        project_id=_PROJECT_ID,
        dispatch_id="20260918-rolelane-pin",
        staging_id="20260918-rolelane-pin-staging",
        instruction_file=instruction,
        role="system-architect",
        target_slot="T1",
        gate="codex_gate",
        dispatch_paths=(DispatchPath(PurePosixPath("scripts/lib/foo.py")),),
        provider=Provider.CLAUDE,
    )
    fields.update(overrides)
    return DispatchSpec(**fields)


def _validate(spec: DispatchSpec):
    return validate(spec, project_id=_PROJECT_ID, repo_root=Path("/fake/repo"))


def _dispatch_agent_parser() -> argparse.ArgumentParser:
    subparsers = argparse.ArgumentParser(prog="vnx").add_subparsers(dest="command")
    _register_dispatch_agent_subparser(subparsers)
    return subparsers.choices["dispatch-agent"]


@pytest.fixture(autouse=True)
def _no_ambient_brake(monkeypatch):
    # An operator shell with the brake on would make the refusal tests pass
    # vacuously; every test starts with it OFF unless it turns it on itself.
    monkeypatch.delenv(TMUX_BRAKE, raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_ALLOW_TMUX_LANE", raising=False)


# ---------------------------------------------------------------------------
# The default lane
# ---------------------------------------------------------------------------


def test_rule_names_the_lane_the_code_resolves_by_default(tmp_path):
    spec = _spec(tmp_path)
    assert isinstance(_validate(spec), ValidatedSpec)

    lane, _, warning = resolve_claude_lane(spec)

    assert warning is None, (
        "a spec without a lane flag must not carry an opt-in audit line"
    )
    assert f"runs on the default lane `{lane}`" in _rule(), (
        f"the door resolves a claude spec without a lane flag to {lane!r}; the role's "
        "lane rule must name that lane as the default"
    )


def test_allow_headless_changes_nothing_but_the_audit_line(tmp_path):
    """The concrete damage of 17/18-09: a T0 passed --allow-headless six times,
    believing it declared an exception. The rule must say what the code does:
    same lane as without the flag, plus one audit line."""
    default_lane, _, _ = resolve_claude_lane(_spec(tmp_path))
    spec = _spec(tmp_path, allow_headless=True, headless_reason="stated on the record")
    assert isinstance(_validate(spec), ValidatedSpec)

    lane, _, warning = resolve_claude_lane(spec)

    assert lane == default_lane, (
        f"--allow-headless now resolves to {lane!r}, not the default {default_lane!r}: "
        "the rule's 'does not change the lane' sentence is stale"
    )
    assert warning, (
        "--allow-headless no longer writes an audit line: rewrite that sentence"
    )
    audit_prefix = warning.split(":", 1)[0]
    rule = _rule()
    assert "`--allow-headless` + `--headless-reason` does not change the lane" in rule
    assert f"`{audit_prefix}`" in rule


# ---------------------------------------------------------------------------
# The retired tmux opt-out and its brake
# ---------------------------------------------------------------------------


def test_force_tmux_refusal_in_the_rule_is_the_code_refusal(tmp_path):
    result = _validate(
        _spec(tmp_path, force_tmux=True, force_tmux_reason="wants a live pane")
    )

    if not isinstance(result, Reject):
        pytest.fail(
            "the door accepts --force-tmux again without the brake: rewrite the "
            "'retired' half of the role's lane rule"
        )
    assert f"the door refuses it with `{result.code}`" in _rule()


def test_brake_named_in_the_rule_reopens_the_tmux_lane(tmp_path, monkeypatch):
    monkeypatch.setenv(TMUX_BRAKE, "1")
    spec = _spec(tmp_path, force_tmux=True, force_tmux_reason="wants a live pane")

    assert isinstance(_validate(spec), ValidatedSpec), (
        f"{TMUX_BRAKE}=1 no longer reopens the tmux lane: the rule names the wrong brake"
    )
    lane, _, _ = resolve_claude_lane(spec)
    assert f"`{TMUX_BRAKE}=1` is the emergency brake that reopens `{lane}`" in _rule()


def test_reason_stays_mandatory_under_the_brake(tmp_path, monkeypatch):
    monkeypatch.setenv(TMUX_BRAKE, "1")

    result = _validate(_spec(tmp_path, force_tmux=True, force_tmux_reason="   "))

    assert isinstance(result, Reject), (
        "the brake now waives the reason: rewrite that sentence"
    )
    assert "the reason stays mandatory under the brake" in _rule()


def test_force_tmux_help_says_what_the_door_does(tmp_path):
    """The door's own help is the second place a T0 reads this fact. It said
    'opt OUT ... back to the tmux lane' while the door refused exactly that."""
    result = _validate(
        _spec(tmp_path, force_tmux=True, force_tmux_reason="wants a live pane")
    )
    if not isinstance(result, Reject):
        pytest.fail(
            "the door accepts --force-tmux again: the help's 'RETIRED' is stale"
        )

    help_text = " ".join(
        _dispatch_agent_parser()._option_string_actions["--force-tmux"].help.split()
    )

    assert result.code in help_text
    assert f"{TMUX_BRAKE}=1" in help_text


# ---------------------------------------------------------------------------
# Every name and citation in the rule resolves
# ---------------------------------------------------------------------------


def test_every_lane_flag_in_the_rule_exists_on_the_door():
    flags = set(re.findall(r"`(--[a-z][a-z-]*)`", _rule()))
    assert flags, "the rule names no lane flag at all"

    known = set(_dispatch_agent_parser()._option_string_actions)

    assert not sorted(flags - known), (
        f"flags the door does not have: {sorted(flags - known)}"
    )


def test_the_dry_run_the_rule_points_to_exists(capsys):
    assert "--dry-run" in _rule()
    with pytest.raises(SystemExit):
        dispatch_cli.main(["--help"])

    assert "--dry-run" in capsys.readouterr().out


def test_the_headless_constraint_the_rule_calls_a_warn_is_a_warn():
    constraints = yaml.safe_load(CONSTRAINTS_FILE.read_text(encoding="utf-8"))[
        "constraints"
    ]
    headless = next(c for c in constraints if c["id"] == "claude-headless")

    assert headless["audit_severity"] == "warn"
    assert "The `claude-headless` constraint warn on every headless dispatch" in _rule()


def test_every_dispatch_the_rule_dates_is_on_record_in_the_code():
    """OPENED / DEFAULT / RETIRED each cite a dispatch id. A cited id must be on
    record where the decision lives: the claude-headless constraint and
    dispatch_spec's Rule 12b. An id found in neither is a misremembered
    citation. Whitespace is dropped because the constraint reason is a folded
    YAML scalar that wraps one of the ids across a line."""
    cited = set(re.findall(r"dispatch-2026\d{4}[a-z0-9-]*[a-z0-9]", _rule()))
    constraints = yaml.safe_load(CONSTRAINTS_FILE.read_text(encoding="utf-8"))[
        "constraints"
    ]
    headless = next(c for c in constraints if c["id"] == "claude-headless")
    record = re.sub(
        r"\s+", "", headless["reason"] + DISPATCH_SPEC_FILE.read_text(encoding="utf-8")
    )

    assert len(cited) >= 3, (
        f"the rule should date OPENED, DEFAULT and RETIRED; found {sorted(cited)}"
    )
    assert not sorted(d for d in cited if d not in record)


def test_worker_claude_override_is_cited_by_symbol_not_line_number():
    """The role used to cite `dispatch_cli.py:691-705`, which drifted to
    unrelated code while the gate moved. A symbol does not drift with a line."""
    text = ROLE_FILE.read_text(encoding="utf-8")

    assert f"`{dispatch_cli.WORKER_CLAUDE_OVERRIDE_ENV}=1`" in text
    assert f"`{dispatch_cli.WORKER_CLAUDE_OVERRIDE_REASON_ENV}`" in text
    assert "`dispatch_cli.WORKER_CLAUDE_OVERRIDE_ENV`" in text
    assert not re.search(r"dispatch_cli\.py:\d", text)
