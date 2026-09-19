#!/usr/bin/env python3
"""Pin the canonical T0 role's lane rule to the lane the door actually resolves.

Dispatch-ID: 20260919-082000-rolelane-overname (takes over D-rolelane-202226, PR #1867)

The canonical orchestrator role (``.claude/terminals/T0/role-orchestrator.md``)
is synced fleet-wide by ``vnx role sync``. Its "Provider→lane (hard)" rule kept
forbidding ``claude -p`` in the fleet copies long after A2 (2026-08-26) made
``claude_headless`` the default, and the canon itself kept offering
``--force-tmux`` as the opt-out after 2026-09-12, when the door started refusing
it. The lane was removed on 2026-09-18. Nothing failed at any of those points:
the rule was prose, and prose does not run.

The rule cannot be generated from the code. The role is static markdown that
Claude Code loads verbatim through ``@role-orchestrator.md`` and that
``vnx role sync`` copies verbatim, with no render step in between. So every lane
fact the rule states is computed here from the code that decides it, and then
looked up in the rule. Change the default lane, the audit line or the severity of
the headless constraint, and this file goes red on the sentence that has to be
rewritten.

Sources of truth: ``dispatch_plan.resolve_claude_lane`` (which lane a spec gets),
``dispatch_plan.compile_plan`` (what the plan carries) and the ``claude-headless``
entry of ``provider_constraints.yaml`` (what the door warns about). A content
pin, same shape as ``test_role_orchestrator_sentinel.py``.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path, PurePosixPath

import pytest
import yaml

import dispatch_cli
from dispatch_plan import (
    ExecutionPlan,
    RuntimeSnapshot,
    compile_plan,
    resolve_claude_lane,
)
from dispatch_spec import (
    DispatchPath,
    DispatchSpec,
    Provider,
    ValidatedSpec,
    validate,
)
from vnx_cli.main import _register_dispatch_agent_subparser

REPO = Path(__file__).resolve().parent.parent
ROLE_FILE = REPO / ".claude" / "terminals" / "T0" / "role-orchestrator.md"
CONSTRAINTS_FILE = REPO / "scripts" / "lib" / "providers" / "provider_constraints.yaml"
TOMBSTONE_FILE = REPO / "docs" / "operations" / "TMUX_SPAWN_LANE.md"

RULE_HEAD = "- Provider→lane (hard):"
TMUX_LANE_REMOVED_ON = "2026-09-18"
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


def _plan(tmp_path: Path, **overrides) -> ExecutionPlan:
    """The plan the door would execute for a claude spec, no I/O involved."""
    vspec = _validate(_spec(tmp_path, **overrides))
    assert isinstance(vspec, ValidatedSpec), vspec
    plan = compile_plan(vspec, RuntimeSnapshot(staging_promoted=True))
    assert isinstance(plan, ExecutionPlan), plan
    return plan


def _dispatch_agent_parser() -> argparse.ArgumentParser:
    subparsers = argparse.ArgumentParser(prog="vnx").add_subparsers(dest="command")
    _register_dispatch_agent_subparser(subparsers)
    return subparsers.choices["dispatch-agent"]


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


# ---------------------------------------------------------------------------
# The opt-in that adds an audit line and nothing else
# ---------------------------------------------------------------------------


def test_allow_headless_changes_nothing_but_the_audit_line(tmp_path):
    """The damage seen on 17/18-09: dispatches whose spec said allow_headless=False
    were read as 'not headless', so the lane looked like another one. The rule must
    say what the code does: same lane as without the flag, plus one audit line."""
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


def test_a_spec_without_the_flag_lands_on_the_plan_of_one_with_it(tmp_path):
    """The same claim one level up, on the plan the door executes. The helper test
    above compares the lane string only and drops the adapter; it never builds a
    plan, so a regression in compile_plan that made the flag change billing,
    serialization or the adapter would pass it. Here everything the permit
    fingerprints (canonical_dict, hence digest) must be identical with and without
    the flag, and the only difference left is the one audit line in warnings.

    Scale, from the dispatch that assigned this test (measured by the operator on
    the mission-control store, 17/18-09, not re-measured here): six of the forty
    most recent dispatches carried allow_headless=False and force_tmux=False."""
    plain = _plan(tmp_path)
    flagged = _plan(
        tmp_path, allow_headless=True, headless_reason="stated on the record"
    )
    _, _, audit_line = resolve_claude_lane(
        _spec(tmp_path, allow_headless=True, headless_reason="stated on the record")
    )

    assert (plain.lane, plain.adapter) == (flagged.lane, flagged.adapter)
    assert plain.canonical_dict() == flagged.canonical_dict(), (
        "the flag changed a field the permit fingerprints: it no longer only adds "
        "an audit line, so the role's 'does not change the lane' sentence is stale"
    )
    assert plain.digest() == flagged.digest()
    assert [w for w in flagged.warnings if w not in plain.warnings] == [audit_line]
    assert [w for w in plain.warnings if w not in flagged.warnings] == []


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
    """OPENED and DEFAULT each cite a dispatch id. A cited id must be on record
    where the decision lives: the claude-headless constraint. An id that is not in
    it is a misremembered citation. Whitespace is dropped because the
    constraint reason is a folded YAML scalar that wraps one of the ids across a
    line.

    The removal of the tmux lane cites no dispatch id. Its record was
    dispatch_spec's Rule 12b, which went with the lane, and the tombstone note that
    replaced it names a date and a last commit, not a dispatch. So the rule points
    at the tombstone instead, and the tombstone has to exist and carry the date the
    rule gives."""
    rule = _rule()
    cited = set(re.findall(r"dispatch-2026\d{4}[a-z0-9-]*[a-z0-9]", rule))
    constraints = yaml.safe_load(CONSTRAINTS_FILE.read_text(encoding="utf-8"))[
        "constraints"
    ]
    headless = next(c for c in constraints if c["id"] == "claude-headless")
    record = re.sub(r"\s+", "", headless["reason"])

    assert len(cited) >= 2, (
        f"the rule should date OPENED and DEFAULT with a dispatch id; found {sorted(cited)}"
    )
    assert not sorted(d for d in cited if d not in record)

    assert TOMBSTONE_FILE.relative_to(REPO).as_posix() in rule
    assert TOMBSTONE_FILE.is_file(), "the rule cites a tombstone note that does not exist"
    assert TMUX_LANE_REMOVED_ON in rule
    assert TMUX_LANE_REMOVED_ON in TOMBSTONE_FILE.read_text(encoding="utf-8"), (
        "the rule and the tombstone note disagree on the day the tmux lane was removed"
    )


def test_worker_claude_override_is_cited_by_symbol_not_line_number():
    """The role used to cite `dispatch_cli.py:691-705`, which drifted to
    unrelated code while the gate moved. A symbol does not drift with a line."""
    text = ROLE_FILE.read_text(encoding="utf-8")

    assert f"`{dispatch_cli.WORKER_CLAUDE_OVERRIDE_ENV}=1`" in text
    assert f"`{dispatch_cli.WORKER_CLAUDE_OVERRIDE_REASON_ENV}`" in text
    assert "`dispatch_cli.WORKER_CLAUDE_OVERRIDE_ENV`" in text
    assert not re.search(r"dispatch_cli\.py:\d", text)
