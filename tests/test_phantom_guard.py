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


# ---------------------------------------------------------------------------
# post10-phantom-review-role: task_class / read_only exemption keys
# ---------------------------------------------------------------------------

def test_review_task_class_empty_diff_is_not_phantom():
    # exempt case: a review/analysis dispatch with real tokens and no diff is expected, not phantom
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=4321,
                         role="architect", task_class="research_structured")
    assert not v.is_phantom
    assert "research_structured" in v.reason


def test_code_review_task_class_variants_are_exempt():
    for tc in ("02_code_review", "code_review", "review", "analysis"):
        v = pg.phantom_guard(status="done", worktree_diff="", token_usage=100,
                             role="backend-developer", task_class=tc)
        assert not v.is_phantom, f"task_class={tc!r} should be exempt"


def test_task_class_is_case_and_whitespace_insensitive():
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=100,
                         role=None, task_class="  Research_Structured  ")
    assert not v.is_phantom


def test_read_only_flag_empty_diff_is_not_phantom():
    # exempt case: an explicit read_only=True on the dispatch spec exempts regardless of role
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=999,
                         role="backend-developer", read_only=True)
    assert not v.is_phantom
    assert "read_only" in v.reason


def test_read_only_false_does_not_exempt_a_delivery():
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=999,
                         role="backend-developer", read_only=False)
    assert v.is_phantom


def test_delivery_task_class_empty_diff_is_still_phantom():
    # non-exempt case: a delivery task_class with an empty diff is rejected as before
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=1234,
                         role="backend-developer", task_class="coding_interactive")
    assert v.is_phantom
    assert "PHANTOM" in v.reason


def test_unrelated_task_class_does_not_exempt():
    v = pg.phantom_guard(status="done", worktree_diff="", token_usage=None,
                         role="backend-developer", task_class="docs_synthesis")
    assert v.is_phantom


def test_guard_receipt_exempts_on_task_class():
    receipt = {"status": "done", "role": "architect", "task_class": "research_structured",
               "token_usage": {"input": 500, "output": 200}}
    v = pg.guard_receipt(receipt, worktree_diff="")
    assert not v.is_phantom


def test_guard_receipt_exempts_on_read_only_flag():
    receipt = {"status": "done", "role": "backend-developer", "read_only": True,
               "token_usage": 42}
    v = pg.guard_receipt(receipt, worktree_diff="")
    assert not v.is_phantom


def test_guard_receipt_delivery_with_task_class_still_phantom():
    # non-exempt: task_class present but NOT a review/analysis bucket -> delivery rule applies
    receipt = {"status": "done", "role": "backend-developer", "task_class": "coding_interactive"}
    v = pg.guard_receipt(receipt, worktree_diff="")
    assert v.is_phantom
