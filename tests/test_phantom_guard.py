"""test_phantom_guard.py — the phantom-guard decision (§10b, the SEOcrawler-unblock linchpin)."""
from __future__ import annotations

import phantom_guard as pg


def test_review_role_is_never_phantom():
    # a reviewer's deliverable is a verdict, not a diff
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=None, role="plan-reviewer")
    assert not v.is_phantom


def test_non_completion_status_is_not_phantom():
    v = pg.phantom_guard(status="failed", worktree_diff="", token_usage=None, role="backend-developer")
    assert not v.is_phantom


def test_tokens_spent_is_not_phantom():
    # an LLM measurably ran → real work, even if the diff arg is empty here
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=1234, role="backend-developer")
    assert not v.is_phantom


def test_non_empty_diff_is_not_phantom():
    v = pg.phantom_guard(status="done", worktree_diff="diff --git a/x b/x\n+hi\n",
                         token_usage=None, role="backend-developer")
    assert not v.is_phantom


def test_seocrawler_fabrication_is_phantom():
    # the live case: kimi-like delivery worker, status=done, no tokens reported, empty diff
    v = pg.phantom_guard(status="done", worktree_diff="   \n  ", token_usage=None, role="backend-developer")
    assert v.is_phantom
    assert bool(v) is True
    assert "PHANTOM" in v.reason


def test_codex_zero_token_empty_diff_is_phantom():
    v = pg.phantom_guard(status="success", worktree_diff="", token_usage=0, role="backend-developer")
    assert v.is_phantom


def test_guard_receipt_extracts_status_role_tokens():
    # token_usage as a mapping (codex/claude shape) with real work
    receipt = {"status": "done", "role": "backend-developer",
               "token_usage": {"input": 10, "output": 5}}
    v = pg.guard_receipt(receipt, worktree_diff="")
    assert not v.is_phantom  # tokens>0 → real work

    # kimi-shaped receipt: no token_usage, empty diff, delivery role → phantom
    phantom = {"status": "done", "agent": "backend-developer"}
    v2 = pg.guard_receipt(phantom, worktree_diff="")
    assert v2.is_phantom


def test_unmeasured_tokens_mapping_is_none():
    assert pg._extract_token_usage({"token_usage": {"input": 0, "output": 0}}) is None
    assert pg._extract_token_usage({"token_usage": 42}) == 42
    assert pg._extract_token_usage({}) is None
