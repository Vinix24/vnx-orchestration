"""Gate profile P6: every consumer reads the profile through ``gate_profile.resolve`` and nothing parses the key.

A spy on ``gate_profile.resolve`` shows that ``review_seats``, ``review_rounds``,
``review_seat_evaluation`` and ``planning_cli`` each reach it, and a grep over the four modules
shows none of them reads ``VNX_GOVERNANCE_PROFILE`` itself.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import isolate_config_registry

import gate_profile
import planning_cli
import review_rounds
import review_seat_evaluation as rse
import review_seats

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
MODULES = ("lib/review_seats.py", "lib/review_rounds.py", "lib/review_seat_evaluation.py", "planning_cli.py")


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def spy(monkeypatch):
    calls = []
    real = gate_profile.resolve

    def resolve(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(gate_profile, "resolve", resolve)
    return calls


def test_review_seats_reads_through_resolve(spy):
    review_seats.resolve_review_seats()
    assert len(spy) == 1


def test_review_seat_evaluation_reads_through_resolve(spy, tmp_path):
    rse.required_seats(tmp_path, "1", "e" * 40)
    assert spy


def test_review_rounds_reads_through_resolve(spy):
    review_rounds._profile_round_cap(None, None)
    assert len(spy) == 1


def test_planning_cli_reads_through_resolve(spy):
    planning_cli._profile_panel_inputs(None)
    assert len(spy) == 1


def _reads_the_key(tree: ast.AST) -> list:
    """The places that name the profile key: the literal itself or ``PROFILE_KEY``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "VNX_GOVERNANCE_PROFILE":
            found.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "PROFILE_KEY":
            found.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == "PROFILE_KEY":
            found.append(node.lineno)
    return found


@pytest.mark.parametrize("relative", MODULES)
def test_no_module_parses_the_profile_key_itself(relative):
    tree = ast.parse((SCRIPTS / relative).read_text(encoding="utf-8"))
    assert _reads_the_key(tree) == []


def test_the_grep_sees_the_one_module_that_does_read_the_key():
    tree = ast.parse((SCRIPTS / "lib/gate_profile.py").read_text(encoding="utf-8"))
    assert _reads_the_key(tree), "the check must be able to find the key, or it proves nothing"
