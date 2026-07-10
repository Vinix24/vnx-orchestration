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


def test_tokens_spent_but_empty_diff_is_phantom():
    # token>0 does NOT exempt a delivery role: an LLM thought but produced no deliverable
    # (empty diff) → phantom. (The old token>0 short-circuit made the guard inert on
    # token-reporting lanes like claude — panel P0.2 finding.)
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=1234, role="backend-developer")
    assert v.is_phantom


def test_tokens_spent_with_real_diff_is_not_phantom():
    v = pg.phantom_guard(status="done", worktree_diff="diff --git a/x b/x\n+y\n",
                         token_usage=1234, role="backend-developer")
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
    # real work: a non-empty diff → not phantom (and status/role/token extraction works)
    receipt = {"status": "done", "role": "backend-developer",
               "token_usage": {"input": 10, "output": 5}}
    v = pg.guard_receipt(receipt, worktree_diff="diff --git a/x b/x\n+y\n")
    assert not v.is_phantom

    # kimi-shaped receipt: no token_usage, empty diff, delivery role → phantom
    phantom = {"status": "done", "agent": "backend-developer"}
    v2 = pg.guard_receipt(phantom, worktree_diff="")
    assert v2.is_phantom


def test_unmeasured_tokens_mapping_is_none():
    assert pg._extract_token_usage({"token_usage": {"input": 0, "output": 0}}) is None
    assert pg._extract_token_usage({"token_usage": 42}) == 42
    assert pg._extract_token_usage({}) is None


def test_review_task_class_is_never_phantom():
    # a review keyed by task_class (not a REVIEW_ROLES role string) with real tokens spent and an
    # empty diff — the genuine read-only review case this PR fixes.
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=987,
                         role="backend-developer", task_class="review")
    assert not v.is_phantom
    assert "task_class" in v.reason


def test_review_task_class_variants_are_exempt():
    for tc in ("code-review", "code_review", "plan-review", "plan_review",
               "security-review", "security_review", "REVIEW", " review "):
        v = pg.phantom_guard(status="done", worktree_diff="", token_usage=None,
                             role="backend-developer", task_class=tc)
        assert not v.is_phantom, f"task_class={tc!r} should be exempt"


def test_unrelated_task_class_is_still_phantom():
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=None,
                         role="backend-developer", task_class="implementation")
    assert v.is_phantom


def test_read_only_flag_is_never_phantom():
    # explicit read_only=True exempts regardless of role/task_class — the escape hatch for a
    # review dispatch that isn't captured by either known-role or known-task_class matching.
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=555,
                         role="backend-developer", task_class=None, read_only=True)
    assert not v.is_phantom
    assert "read_only" in v.reason


def test_read_only_false_does_not_exempt():
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=None,
                         role="backend-developer", read_only=False)
    assert v.is_phantom


def test_guard_receipt_extracts_task_class_and_read_only():
    # task_class-keyed review: real tokens, empty diff, delivery-shaped role -> not phantom
    receipt = {"status": "done", "role": "backend-developer", "task_class": "review",
               "token_usage": {"input": 100, "output": 50}}
    v = pg.guard_receipt(receipt, worktree_diff="")
    assert not v.is_phantom

    # read_only flag on the receipt itself, no recognized role/task_class
    receipt2 = {"status": "done", "role": "backend-developer", "read_only": True}
    v2 = pg.guard_receipt(receipt2, worktree_diff="")
    assert not v2.is_phantom

    # neither signal present, empty diff, delivery role -> still phantom
    receipt3 = {"status": "done", "role": "backend-developer"}
    v3 = pg.guard_receipt(receipt3, worktree_diff="")
    assert v3.is_phantom
