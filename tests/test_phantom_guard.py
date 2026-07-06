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
# TOKEN_BLIND_PROVIDERS — per-provider-billing-phantom PR
# ---------------------------------------------------------------------------

def test_kimi_zero_tokens_with_evidence_is_not_phantom():
    # kimi-cli reports token_usage=0 on a real push; a non-token evidence reference
    # (commit/PR) must stand in for the (still empty, e.g. post-merge) diff.
    v = pg.phantom_guard(
        status="done", worktree_diff="", token_usage=0, role="backend-developer",
        provider="kimi", non_token_evidence="pr#1234",
    )
    assert not v.is_phantom
    assert "kimi" in v.reason


def test_kimi_zero_tokens_no_evidence_is_still_phantom():
    # the genuine phantom case: kimi, no diff, no evidence at all — still rejected.
    v = pg.phantom_guard(
        status="done", worktree_diff="", token_usage=0, role="backend-developer",
        provider="kimi", non_token_evidence=None,
    )
    assert v.is_phantom


def test_non_kimi_provider_with_evidence_is_still_phantom():
    # the evidence escape hatch is provider-scoped: a token-reporting provider (claude)
    # gets no such exemption — empty diff is still a phantom regardless of "evidence".
    v = pg.phantom_guard(
        status="done", worktree_diff="", token_usage=0, role="backend-developer",
        provider="claude", non_token_evidence="pr#1234",
    )
    assert v.is_phantom


def test_kimi_with_real_diff_and_no_evidence_is_not_phantom_as_before():
    # non-empty diff still short-circuits before the evidence check is ever consulted.
    v = pg.phantom_guard(
        status="done", worktree_diff="diff --git a/x b/x\n+y\n", token_usage=0,
        role="backend-developer", provider="kimi", non_token_evidence=None,
    )
    assert not v.is_phantom


def test_guard_receipt_kimi_pr_id_evidence_not_phantom():
    receipt = {
        "status": "done", "role": "backend-developer", "provider": "kimi",
        "token_usage": 0, "pr_id": "1234",
    }
    v = pg.guard_receipt(receipt, worktree_diff="")
    assert not v.is_phantom


def test_guard_receipt_kimi_no_evidence_is_phantom():
    receipt = {
        "status": "done", "role": "backend-developer", "provider": "kimi",
        "token_usage": 0,
    }
    v = pg.guard_receipt(receipt, worktree_diff="")
    assert v.is_phantom


def test_extract_non_token_evidence_checks_known_keys_in_order():
    assert pg._extract_non_token_evidence({"pr_id": "42"}) == "42"
    assert pg._extract_non_token_evidence({"pr_id": "none", "commit_sha": "abc123"}) == "abc123"
    assert pg._extract_non_token_evidence({"branch": "dispatch/foo"}) == "dispatch/foo"
    assert pg._extract_non_token_evidence({}) is None
    assert pg._extract_non_token_evidence({"pr_id": None, "commit_sha": ""}) is None
