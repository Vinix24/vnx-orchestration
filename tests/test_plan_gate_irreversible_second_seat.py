"""PR-B of track gate-weight-and-worker-posture-cluster: an irreversible path gets
the plan-gate's second seat from the start (ADR-030, amendment 2026-10-25).

The three path-derivable irreversible categories of ``smart_router`` (schema
migration, fleet default, receipt format) join schema, security and outside-repo
as reasons to run both seats in the first round. The path is normalised first,
so ``.claude/skills/`` keeps its dot. Everything below drives the real
classifier and the real ``compose_panel`` over the shipped panel config; the
public classifier this PR adds is reached through a stand-in when it is absent,
so a run on the old code fails on an assertion, never on an import.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
for _p in (str(_REPO / "scripts" / "lib"), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import plan_gate_panel as pgp
import smart_router

_SHIPPED = _REPO / "configs" / "plan_gate_panel.yaml"


def _missing(*_args, **_kwargs):
    return "<not on this code>"


irreversible_path_category = getattr(smart_router, "irreversible_path_category", _missing)


@pytest.mark.parametrize("path, category, named", [
    ("scripts/lib/receipt_schema.py", "receipt-format", "scripts/lib/receipt_schema.py"),
    ("scripts/lib/append_receipt_internals/validation.py", "receipt-format",
     "scripts/lib/append_receipt_internals/validation.py"),
    ("agents/backend-developer/CLAUDE.md", "fleet-default", "agents/backend-developer/CLAUDE.md"),
    (".claude/skills/x/SKILL.md", "fleet-default", ".claude/skills/x/SKILL.md"),
    ("./skills/x/SKILL.md", "fleet-default", "skills/x/SKILL.md"),
    (".claude/terminals/T1/CLAUDE.md", "fleet-default", ".claude/terminals/T1/CLAUDE.md"),
])
def test_an_irreversible_path_brings_the_second_seat_in_from_the_start(path, category, named):
    reason = pgp.second_seat_from_start([path])
    assert reason == f"irreversible {category} path {named}"

    composition = pgp.compose_panel(config_path=_SHIPPED, dispatch_paths=[path])
    assert [s["label"] for s in composition.seats] == ["codex", "opus"]
    assert (composition.quorum, composition.escalation, composition.chosen_by) == (2, None, "default")
    assert composition.second_seat == reason


def test_a_docs_only_change_keeps_one_seat():
    assert pgp.second_seat_from_start(["docs/README.md"]) is None
    composition = pgp.compose_panel(config_path=_SHIPPED, dispatch_paths=["docs/README.md"])
    assert [s["label"] for s in composition.seats] == ["codex"]
    assert composition.escalation["label"] == "opus"
    assert (composition.quorum, composition.second_seat) == (1, None)


@pytest.mark.parametrize("paths, reason", [
    (["agents/x/CLAUDE.md", "scripts/migrations/0001.sql"], "schema path scripts/migrations/0001.sql"),
    (["skills/x/SKILL.md", "hooks/x.sh"], "security path hooks/x.sh"),
    (["agents/x/CLAUDE.md", "scripts/lib/receipt_schema.py"],
     "irreversible receipt-format path scripts/lib/receipt_schema.py"),
], ids=["schema-before-fleet-default", "security-before-fleet-default", "receipt-format-before-fleet-default"])
def test_the_reason_follows_the_fixed_precedence_not_the_path_order(paths, reason):
    assert pgp.second_seat_from_start(paths) == reason


def test_an_explicit_operator_seat_choice_wins_over_the_derived_second_seat():
    composition = pgp.compose_panel(
        ["kimi"], config_path=_SHIPPED, dispatch_paths=["scripts/lib/receipt_schema.py"],
    )
    assert [s["label"] for s in composition.seats] == ["kimi"]
    assert (composition.chosen_by, composition.quorum) == ("operator", 1)
    assert (composition.escalation, composition.second_seat) == (None, None)


@pytest.mark.parametrize("path", [
    "skills//x/SKILL.md", ".claude//skills/x/SKILL.md", "./.claude/skills/x/SKILL.md",
    "skills\\x\\SKILL.md", "agents/./x/CLAUDE.md",
])
def test_the_classifier_normalises_before_it_matches(path):
    assert irreversible_path_category(path) == "fleet-default"


@pytest.mark.parametrize("path", ["docs/README.md", "scripts/lib/dispatch_cli.py", "", "   ", "../agents/x.md"])
def test_a_path_outside_the_three_categories_has_none(path):
    assert irreversible_path_category(path) is None


def test_an_absolute_path_is_read_relative_to_the_repository(tmp_path):
    inside = str(tmp_path / "agents" / "a" / "CLAUDE.md")
    assert irreversible_path_category(inside, repo_root=tmp_path) == "fleet-default"
    assert irreversible_path_category("/elsewhere/agents/a/CLAUDE.md", repo_root=tmp_path) is None
    assert irreversible_path_category(str(tmp_path), repo_root=tmp_path) is None


def test_without_a_root_an_absolute_path_is_read_against_the_project_root(monkeypatch):
    monkeypatch.chdir(_REPO)
    assert irreversible_path_category(str(_REPO / "scripts" / "lib" / "receipt_schema.py")) == "receipt-format"


def test_a_symlinked_skills_dir_classifies_by_its_own_name(tmp_path):
    target = tmp_path / "vnx-skills"
    target.mkdir()
    (tmp_path / "repo" / ".claude").mkdir(parents=True)
    (tmp_path / "repo" / ".claude" / "skills").symlink_to(target)
    path = str(tmp_path / "repo" / ".claude" / "skills" / "x" / "SKILL.md")
    assert irreversible_path_category(path, repo_root=tmp_path / "repo") == "fleet-default"


def test_one_path_under_several_prefixes_takes_the_category_the_precedence_names(monkeypatch):
    # The table the function reads, not the tests/smart_router shim's copy of it.
    table_home = sys.modules[irreversible_path_category.__module__]
    monkeypatch.setattr(table_home, "_IRREVERSIBLE_PREFIXES", (
        ("x/", "fleet-default"), ("x/", "receipt-format"), ("x/", "schema-migration"),
    ), raising=False)
    assert irreversible_path_category("x/a.py") == "schema-migration"
    monkeypatch.setattr(table_home, "_IRREVERSIBLE_PREFIXES", (
        ("x/", "fleet-default"), ("x/", "receipt-format"),
    ), raising=False)
    assert irreversible_path_category("x/a.py") == "receipt-format"


@pytest.mark.parametrize("path, plan_scope", [
    ("scripts/lib/receipt_schema.py", None),
    ("agents/backend-developer/CLAUDE.md", None),
    (".claude/skills/x/SKILL.md", None),
    ("scripts/lib/append_receipt_internals/validation.py", smart_router.PLAN_SCOPE_GATE_MERGE),
])
def test_the_review_risk_class_reading_of_these_paths_is_unchanged(path, plan_scope):
    assert smart_router.plan_scope_path_category(path) == plan_scope
