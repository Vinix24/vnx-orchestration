#!/usr/bin/env python3
"""d3filter D1 (dlv-81c4378b32c8): a failure-prevention proposal gets one
unambiguous id, a readable scope and no longer collapses silently.

Measured on 94ecddd1f: two agents on the same failure and terminal collapsed into
one pending rule, a receipt without ``terminal`` gave ``terminal_constraint ""``,
and ``provider="unknown"`` with ``model="sonnet"`` gave agent ``unknown``.
"""
from __future__ import annotations

import itertools
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402


class _FakePaths:
    def __init__(self, state_dir: Path, vnx_home: Path):
        self._d = {
            "VNX_STATE_DIR": str(state_dir),
            "VNX_HOME": str(vnx_home),
            "VNX_DATA_DIR": str(state_dir.parent),
        }

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


def _make_loop(tmp_path: Path, persist: bool):
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    vnx_home = tmp_path / "repo"
    vnx_home.mkdir()
    fake = _FakePaths(state_dir, vnx_home)
    with patch.object(ll, "ensure_env", return_value=fake):
        loop = ll.LearningLoop(persist=persist)
    return loop, fake, state_dir


@pytest.fixture
def persisting(tmp_path, approve_learning_persist):
    approve_learning_persist(tmp_path / "vnx-data" / "vnx-dev" / "state")
    loop, fake, state_dir = _make_loop(tmp_path, persist=True)
    yield loop, fake, state_dir
    loop.conn.close()


@pytest.fixture
def shadow(tmp_path):
    loop, fake, state_dir = _make_loop(tmp_path, persist=False)
    yield loop
    loop.conn.close()


def _receipt(**fields) -> dict:
    base = {
        "event_type": "task_failed",
        "status": "failed",
        "dispatch_id": "d-1",
        "task_id": "t-1",
        "error": "ImportError: boom",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    base.update(fields)
    return base


def _extract(loop, tmp_path: Path, receipts: list[dict]) -> list[dict]:
    path = tmp_path / "receipts.ndjson"
    path.write_text("\n".join(json.dumps(r) for r in receipts) + "\n", encoding="utf-8")
    loop.receipts_path = path
    loop.cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    return loop.extract_failure_patterns(datetime.now(timezone.utc) - timedelta(hours=2))


def _failure(terminal: str = "T1", agent: str = "claude", error: str = "boom") -> dict:
    return {"task": "t", "terminal": terminal, "agent": agent, "error": error, "timestamp": "x"}


def _queue(persisting, failures: list[dict]) -> tuple[list[dict], list[dict]]:
    loop, fake, state_dir = persisting
    rules = loop.generate_prevention_rules(failures)
    with patch.object(ll, "ensure_env", return_value=fake):
        loop.update_terminal_constraints(rules)
    data = json.loads((state_dir / "pending_rules.json").read_text(encoding="utf-8"))
    return rules, data["pending_rules"]


# --- projection of terminal and agent ---------------------------------------

def test_receipt_without_terminal_gives_any_in_the_proposal(persisting, tmp_path):
    loop = persisting[0]
    failures = _extract(loop, tmp_path, [_receipt(provider="claude"), _receipt(provider="claude", dispatch_id="d-2")])
    _, pending = _queue(persisting, failures)
    assert len(pending) == 1
    assert pending[0]["terminal_constraint"] == "any"


def test_sentinel_terminal_gives_any(persisting, tmp_path):
    loop = persisting[0]
    failures = _extract(loop, tmp_path, [_receipt(provider="claude", terminal="unknown"),
                                        _receipt(provider="claude", terminal="unknown", dispatch_id="d-2")])
    _, pending = _queue(persisting, failures)
    assert pending[0]["terminal_constraint"] == "any"


def test_sentinel_provider_does_not_shadow_a_real_model(persisting, tmp_path):
    loop = persisting[0]
    failures = _extract(loop, tmp_path, [_receipt(provider="unknown", model="sonnet", terminal="T1"),
                                        _receipt(provider="unknown", model="sonnet", terminal="T1", dispatch_id="d-2")])
    assert {f["agent"] for f in failures} == {"sonnet"}
    _, pending = _queue(persisting, failures)
    assert pending[0]["agent_constraint"] == "sonnet"


def test_real_provider_and_terminal_are_kept(persisting, tmp_path):
    loop = persisting[0]
    failures = _extract(loop, tmp_path, [_receipt(provider="claude", terminal="T1"),
                                        _receipt(provider="claude", terminal="T1", dispatch_id="d-2")])
    _, pending = _queue(persisting, failures)
    assert (pending[0]["terminal_constraint"], pending[0]["agent_constraint"]) == ("T1", "claude")


# --- the record --------------------------------------------------------------

def test_pending_file_record_has_both_scopes_and_status_pending(persisting):
    _, pending = _queue(persisting, [_failure(), _failure()])
    assert pending[0]["status"] == "pending"
    assert pending[0]["terminal_constraint"] == "T1"
    assert pending[0]["agent_constraint"] == "claude"


def test_two_agents_on_the_same_failure_are_two_rules_and_the_count_is_honest(persisting, capsys):
    failures = [_failure(agent="claude"), _failure(agent="claude"),
                _failure(agent="deepseek"), _failure(agent="deepseek")]
    rules, pending = _queue(persisting, failures)
    assert len(rules) == 2
    assert len(pending) == 2
    assert len({p["id"] for p in pending}) == 2
    assert {p["agent_constraint"] for p in pending} == {"claude", "deepseek"}
    assert "Queued 2 prevention rules" in capsys.readouterr().out


def test_requeueing_the_same_proposals_says_so(persisting, capsys):
    failures = [_failure(), _failure()]
    _queue(persisting, failures)
    capsys.readouterr()
    _, pending = _queue(persisting, failures)
    out = capsys.readouterr().out
    assert len(pending) == 1
    assert "Queued 0 prevention rules (1 already pending)" in out


def test_shadow_record_carries_both_scopes(shadow):
    rules = shadow.generate_prevention_rules([_failure(terminal="", agent=""), _failure(terminal="", agent="")])
    shadow.update_terminal_constraints(rules)
    proposal = shadow.shadow["proposals"][0]
    assert proposal["terminal_constraint"] == "any"
    assert "agent_constraint" in proposal and proposal["agent_constraint"] is None


# --- C1: the id is equal exactly when the three values are equal --------------

def test_rule_id_equal_iff_pattern_terminal_agent_equal():
    patterns = ["Error pattern: a", "Error pattern: b"]
    terminals = ["any", "T1"]
    agents = [None, "claude", "deepseek"]
    combos = list(itertools.product(patterns, terminals, agents))
    ids = {c: ll._rule_id({"pattern": c[0], "terminal_constraint": c[1], "agent_constraint": c[2]}) for c in combos}
    for a, b in itertools.product(combos, repeat=2):
        assert (ids[a] == ids[b]) == (a == b)


def test_rule_id_separator_prevents_boundary_collision():
    one = ll._rule_id({"pattern": "ab", "terminal_constraint": "c", "agent_constraint": None})
    two = ll._rule_id({"pattern": "a", "terminal_constraint": "bc", "agent_constraint": None})
    assert one != two


# --- fail-closed normalisation: every malformed value stays any / None --------

MALFORMED = [None, "", "  ", "unknown", "NONE", "null", 7, ["T1"], {"t": 1}]


@pytest.mark.parametrize("value", MALFORMED)
def test_terminal_malformed_value_is_any(shadow, value):
    rules = shadow.generate_prevention_rules(
        [{"error": "e", "terminal": value, "agent": "claude"}] * 2)
    assert rules[0]["terminal_constraint"] == "any"


@pytest.mark.parametrize("value", MALFORMED)
def test_agent_malformed_value_is_none(shadow, value):
    rules = shadow.generate_prevention_rules(
        [{"error": "e", "terminal": "T1", "agent": value}] * 2)
    assert rules[0]["agent_constraint"] is None


def test_missing_terminal_and_agent_keys_are_any_and_none(shadow):
    rules = shadow.generate_prevention_rules([{"error": "e"}, {"error": "e"}])
    assert (rules[0]["terminal_constraint"], rules[0]["agent_constraint"]) == ("any", None)


@pytest.mark.parametrize("receipt", [
    {}, {"provider": None, "model": None}, {"provider": 5, "model": ["x"]},
    {"provider": "unknown", "model": "none"}, {"provider": "", "model": "  "},
])
def test_agent_identity_without_real_value_is_empty(receipt):
    assert ll._agent_identity(receipt) == ""


def test_agent_identity_prefers_provider_then_model():
    assert ll._agent_identity({"provider": "claude", "model": "sonnet"}) == "claude"
    assert ll._agent_identity({"provider": "unknown", "model": " sonnet "}) == "sonnet"
