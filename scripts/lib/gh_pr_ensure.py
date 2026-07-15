"""gh_pr_ensure.py — single shared implementation of "find or create a GitHub PR
for a branch", used by every lane that needs an idempotent auto-PR guarantee.

Two call sites need this:
  - auto_gate_trigger.py: opens a PR once a multi-PR feature's checkboxes are
    all committed, so the review-gate stack has something to attach to.
  - pr_enforcement.py (tmux-spawn build-dispatch completion path): opens a PR
    for a worker that committed + pushed its dispatch branch but never ran
    `gh pr create` itself, so T0 does not have to salvage it by hand.

Both call sites must share exactly one `gh pr create` invocation — two independent
implementations drift (different flags, different idempotency checks) and that
drift is how duplicate PRs happen.

BILLING SAFETY: No Anthropic SDK. CLI-only (git/gh via subprocess).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def find_open_pr(branch: str, repo_root: Path, *, timeout: int = 20) -> Optional[int]:
    """Return the open PR number for *branch*, or None if none exists / lookup failed.

    Never raises: a `gh` failure (auth, network, rate limit) is treated as "unknown",
    not "no PR exists" — callers must not use None here to justify creating a
    duplicate without a corroborating create-side idempotency check of their own.
    """
    if not branch:
        return None
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--json", "number,state"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(repo_root),
        )
        if proc.returncode != 0:
            logger.debug("gh pr list failed for %s: %s", branch, (proc.stderr or "").strip()[:200])
            return None
        prs = json.loads(proc.stdout)
        open_prs = [p for p in prs if (p.get("state") or "").upper() == "OPEN"]
        return open_prs[0]["number"] if open_prs else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("find_open_pr failed for %s: %s", branch, exc)
        return None


def create_pr(
    branch: str,
    repo_root: Path,
    *,
    title: str,
    body: str,
    draft: bool = False,
    timeout: int = 60,
) -> Optional[int]:
    """Create a GitHub PR for *branch*; return the PR number, or None on failure.

    Never raises: `gh` unavailability / auth / network errors are reported via the
    None return, not propagated — a transient GitHub outage must not crash a
    dispatch lane.
    """
    cmd = ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch]
    if draft:
        cmd.append("--draft")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(repo_root),
        )
        if proc.returncode != 0:
            logger.warning("gh pr create failed for %s: %s", branch, (proc.stderr or "").strip()[:400])
            return None
        url = proc.stdout.strip()
        m = re.search(r"/pull/(\d+)", url)
        if m:
            return int(m.group(1))
        logger.warning("Could not parse PR number from gh output for %s: %s", branch, url[:200])
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_pr exception for %s: %s", branch, exc)
        return None


def ensure_pr(
    branch: str,
    repo_root: Path,
    *,
    title: str,
    body: str,
    draft: bool = False,
) -> Dict[str, Any]:
    """Idempotently ensure an open PR exists for *branch*.

    Re-checks for an open PR immediately before creating one and again treats a
    "gh pr create" failure that reports the PR already exists as success (a race
    between the initial lookup and the create call), so concurrent callers never
    produce two PRs for the same branch.

    Returns {"pr_number": int|None, "created": bool, "reason": str|None}.
    "reason" is populated only when pr_number is None (a genuine failure).
    """
    pr_number = find_open_pr(branch, repo_root)
    if pr_number is not None:
        return {"pr_number": pr_number, "created": False, "reason": None}

    pr_number = create_pr(branch, repo_root, title=title, body=body, draft=draft)
    if pr_number is not None:
        return {"pr_number": pr_number, "created": True, "reason": None}

    # create_pr failed — re-check once for a PR that appeared concurrently (a
    # racing caller, or `gh pr create` itself failing only because one already
    # exists) before reporting a genuine failure.
    pr_number = find_open_pr(branch, repo_root)
    if pr_number is not None:
        return {"pr_number": pr_number, "created": False, "reason": None}

    return {
        "pr_number": None,
        "created": False,
        "reason": f"gh pr create failed for branch {branch!r} (no open PR found on retry)",
    }
