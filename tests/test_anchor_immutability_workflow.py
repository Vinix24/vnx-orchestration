"""Structural regression tests for
.github/workflows/anchor-immutability-check.yml (ADR-034 §3).

These are not a real GitHub Actions run (dispatch item 5: "workflow-gedrag
via de check-functie, niet via een echte GitHub-run" — the check-function
behavior itself is exercised directly against
chain_origin_anchor.check_anchor_immutability in
tests/test_chain_origin_anchor.py's T6/T7). This file guards the properties
the ADR requires of the WORKFLOW WIRING specifically: the job name matches
the Python-side constant the branch-protection check looks for, the check is
NOT continue-on-error (required from commit one, unlike attestation-gate.yml's
own staged-advisory precedent — ADR §3 "C4" fix), the workflow parses as
valid YAML, and it triggers on every pull_request (no path filter — a
required status check needs the workflow to always report, ADR §3 note).

ADR-034 fix-r1 Finding 1: the trigger is `pull_request_target`, not
`pull_request` — GitHub always runs a `pull_request_target` workflow using
the JOB DEFINITION committed on the BASE branch, never the PR's own copy of
this YAML. Under the old `pull_request` trigger a PR could rewrite this file
into a no-op job with the same required-check name while also tampering
governance/chain-origin.ndjson in the same diff, and its own neutered
workflow version — not this one — is what GitHub would run. The tests below
also guard the classic `pull_request_target` footgun: this job must never
check out the PR head into the working tree or execute anything from it —
the head commit may only be read as raw blob content via `git show`/`git
diff`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
from chain_origin_anchor import ANCHOR_IMMUTABILITY_CHECK_NAME  # noqa: E402

WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "anchor-immutability-check.yml"
)


def test_workflow_file_exists():
    assert WORKFLOW_PATH.is_file()


def test_workflow_yaml_parses():
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert "jobs" in data
    assert "anchor-immutability" in data["jobs"]


def test_workflow_job_name_matches_python_constant():
    """This job's `name:` IS the GitHub required-status-check "context" that
    check_branch_protection() (ADR §6 step 2b) looks for — drift here would
    make the branch-protection precondition unsatisfiable even when the
    workflow itself is correctly required."""
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert f'name: "{ANCHOR_IMMUTABILITY_CHECK_NAME}"' in content


def test_workflow_is_not_continue_on_error():
    """ADR §3 "C4": required from commit one — NOT the staged-advisory pattern
    attestation-gate.yml uses (continue-on-error: true). An advisory-only
    write-side check is equivalent to no write-side defense for this ADR's
    threat model."""
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    job = data["jobs"]["anchor-immutability"]
    assert "continue-on-error" not in job


def test_workflow_triggers_on_pull_request_target_no_path_filter():
    """`pull_request_target`, not `pull_request` (Finding 1, ADR-034 fix-r1):
    GitHub always evaluates this workflow using the base-branch's copy of the
    job steps, so a PR cannot neutralize its own gate by editing this file.
    No `paths:` filter at the workflow level — the job must always run (and
    always report a status) so it can function as a required check; the
    file-touched classification happens INSIDE the job, not via a trigger
    filter that could make the workflow silently not run for some PRs."""
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "\non:\n  pull_request_target:" in content
    assert "paths:" not in content


def test_workflow_never_checks_out_pr_head_ref():
    """Classic `pull_request_target` footgun: checking out attacker-controlled
    PR code and running it with the base branch's token. Every
    `actions/checkout` step must resolve to the default (base-branch) ref —
    none may point `ref:` at the PR head SHA. The head commit is only ever
    read as raw blob content via `git show`/`git diff` further down, never
    checked into the working tree or executed."""
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    job = data["jobs"]["anchor-immutability"]
    for step in job["steps"]:
        if str(step.get("uses", "")).startswith("actions/checkout"):
            ref = str((step.get("with") or {}).get("ref", ""))
            assert "head" not in ref, f"checkout step must not reference the PR head: {step}"


def test_workflow_declares_minimal_read_only_permissions():
    """`pull_request_target` inherits the repo's default token permissions
    unless scoped down explicitly — this job never writes back to the repo,
    so it declares `contents: read` (defense in depth alongside the
    never-checkout-PR-head rule above)."""
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert data.get("permissions") == {"contents": "read"}


def test_workflow_extracts_checker_code_from_base_branch_not_pr_head():
    """Trust-anchor safety: the checking code must be extracted from
    origin/main, mirroring attestation-gate.yml's base-branch-trust pattern —
    a PR cannot weaken check_anchor_immutability and have that weakened copy
    used to evaluate itself."""
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert 'git show "origin/main:scripts/lib/$f"' in content
    assert "chain_origin_anchor.py" in content


def test_workflow_invokes_check_anchor_immutability():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "check_anchor_immutability" in content
