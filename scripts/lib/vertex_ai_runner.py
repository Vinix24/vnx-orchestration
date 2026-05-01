"""Vertex AI and prompt-building helpers for gate_runner.

Extracted from gate_runner.py. Callers must pass subprocess_run and urlopen
explicitly so that tests can patch gate_runner.subprocess.run without
needing to change their patch targets.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List

VERTEX_DEFAULT_REGION = "us-central1"
VERTEX_DEFAULT_MODEL = "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# Vertex AI REST helpers
# ---------------------------------------------------------------------------


def _get_vertex_project(subprocess_run: Callable) -> str:
    """Return GCP project from env or via gcloud config get-value."""
    project = os.environ.get("VNX_VERTEX_PROJECT", "").strip()
    if not project:
        result = subprocess_run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True, text=True, timeout=10,
        )
        project = result.stdout.strip()
    if not project:
        raise RuntimeError("VNX_VERTEX_PROJECT not set and gcloud has no default project")
    return project


def _get_gcloud_token(subprocess_run: Callable) -> str:
    """Return a valid gcloud access token or raise RuntimeError."""
    result = subprocess_run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True, text=True, timeout=10,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("Failed to get gcloud access token")
    return token


def _build_vertex_url(project: str, region: str, model: str) -> str:
    """Construct the Vertex AI generateContent endpoint URL."""
    return (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{region}/publishers/google/models/{model}:generateContent"
    )


def run_vertex_ai(
    prompt: str,
    *,
    subprocess_run: Callable,
    urlopen: Callable,
) -> str:
    """Call Vertex AI REST API using gcloud token. Returns raw text response."""
    project = _get_vertex_project(subprocess_run)
    region = os.environ.get("VNX_VERTEX_REGION", VERTEX_DEFAULT_REGION)
    model = os.environ.get("VNX_VERTEX_MODEL", VERTEX_DEFAULT_MODEL)
    token = _get_gcloud_token(subprocess_run)

    url = _build_vertex_url(project, region, model)
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192},
    }
    import urllib.request as _urllib_request
    data = json.dumps(body).encode("utf-8")
    req = _urllib_request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=120) as resp:
        response_data = json.loads(resp.read().decode("utf-8"))
    return response_data["candidates"][0]["content"]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _gather_changed_files(files: List[str], subprocess_run: Callable) -> List[str]:
    """Return file list, falling back to git diff --name-only when empty."""
    if files:
        return files
    try:
        result = subprocess_run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        return [f for f in result.stdout.splitlines() if f.strip()]
    except Exception:
        return []


def _read_file_from_branch(
    path: str,
    branch: str,
    subprocess_run: Callable,
) -> str | None:
    """Return file content as committed on `branch` via `git show branch:path`.

    Returns None when the file is not present on the branch, the path is
    absolute (and therefore not a tracked path), or git is unavailable.
    """
    if not branch or os.path.isabs(path):
        return None
    try:
        result = subprocess_run(
            ["git", "show", f"{branch}:{path}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, Exception):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _inline_file_contents(
    files: List[str],
    max_bytes: int,
    *,
    branch: str = "",
    subprocess_run: Callable | None = None,
) -> str:
    """Read and inline file contents up to max_bytes total.

    When `branch` is provided, file content is resolved via `git show branch:path`
    so that gates review the PR-branch version regardless of which worktree the
    gate runs in. Falls back to filesystem read when the file is absent on the
    branch (e.g., uncommitted local edits) or when no branch is supplied.
    """
    content = ""
    bytes_used = 0
    for f in files:
        remaining = max_bytes - bytes_used
        if remaining <= 0:
            break

        chunk: str | None = None
        if branch and subprocess_run is not None:
            branch_content = _read_file_from_branch(f, branch, subprocess_run)
            if branch_content is not None:
                chunk = branch_content[:remaining]

        if chunk is None:
            if not os.path.exists(f):
                continue
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    chunk = fh.read(remaining)
            except OSError:
                continue

        content += f"\n--- FILE: {f} ---\n{chunk}"
        bytes_used += len(chunk.encode("utf-8"))
    return content


def build_gemini_prompt(
    request_payload: Dict[str, Any],
    *,
    subprocess_run: Callable,
) -> str:
    """Build an enriched prompt with inline file contents for Vertex AI routing."""
    files = _gather_changed_files(
        request_payload.get("changed_files", []), subprocess_run
    )
    branch = request_payload.get("branch", "")
    risk = request_payload.get("risk_class", "medium")
    pr = request_payload.get("pr_number", "")
    max_bytes = int(os.environ.get("VNX_GEMINI_MAX_PROMPT_BYTES", "100000"))

    review_instructions = (
        f"Review PR #{pr} on branch {branch} (risk: {risk}).\n"
        f"Changed files: {', '.join(files)}\n\n"
        "Perform a thorough code review of the file contents below.\n\n"
        "Respond with a structured JSON verdict only:\n"
        "```json\n"
        "{\n"
        '  "verdict": "pass|fail|blocked",\n'
        '  "findings": [{"severity": "error|warning|info", "message": "..."}],\n'
        '  "residual_risk": "description of remaining risks or null",\n'
        '  "rerun_required": false,\n'
        '  "rerun_reason": null\n'
        "}\n"
        "```\n"
    )
    file_content = _inline_file_contents(
        files, max_bytes, branch=branch, subprocess_run=subprocess_run,
    )
    return f"{review_instructions}\n{file_content}"


def collect_file_contents(
    request_payload: Dict[str, Any],
    *,
    subprocess_run: Callable,
) -> str:
    """Return inline file content string for Vertex AI prompt enrichment.

    Uses changed_files from payload, falling back to git diff when empty.
    Respects VNX_GEMINI_MAX_PROMPT_BYTES (default 100000).
    """
    files = _gather_changed_files(
        request_payload.get("changed_files", []), subprocess_run
    )
    branch = request_payload.get("branch", "")
    max_bytes = int(os.environ.get("VNX_GEMINI_MAX_PROMPT_BYTES", "100000"))
    return _inline_file_contents(
        files, max_bytes, branch=branch, subprocess_run=subprocess_run,
    )


def build_codex_prompt(
    request_payload: Dict[str, Any],
    *,
    subprocess_run: Callable,
) -> str:
    """Build a headless review prompt for codex gate with inline file contents.

    Inlines file contents instead of asking Codex to fetch the PR via GitHub API.
    Falls back to git diff main..HEAD when changed_files list is empty.
    """
    branch = request_payload.get("branch", "")
    risk = request_payload.get("risk_class", "medium")
    max_bytes = int(os.environ.get("VNX_GEMINI_MAX_PROMPT_BYTES", "100000"))

    files = _gather_changed_files(
        request_payload.get("changed_files", []), subprocess_run
    )
    file_contents = _inline_file_contents(
        files, max_bytes, branch=branch, subprocess_run=subprocess_run,
    )

    return (
        f"Review the following code changes on branch {branch} (risk: {risk}).\n\n"
        f"{file_contents}\n\n"
        "Perform a thorough code review of the file contents above.\n\n"
        "## Severity rules (strict)\n\n"
        "Default `severity` is `warning`. Promote to `error` ONLY when the finding's impact includes one of:\n"
        "- Data loss or corruption (database, files, append-only logs)\n"
        "- False-positive PR closure (closure_verifier passing when it should block)\n"
        "- False-negative PR rejection (closure_verifier blocking when it should pass)\n"
        "- Security boundary breach (auth bypass, secret leak, privilege escalation)\n"
        "- Cross-dispatch state corruption (one dispatch's data leaking into another's audit trail)\n\n"
        "Use `info` for advisory-only observations.\n\n"
        "Findings about the following are NOT `error`-severity by default:\n"
        "- Style, formatting, log shape (stderr vs stdout, plain vs JSON)\n"
        "- Truncated-but-named hash fields (unless a caller compares to a real full SHA)\n"
        "- Hardcoded test fixtures (only when tests run elsewhere, mark out-of-scope)\n"
        "- Operator-toggled surfaces (when toggling resolves the issue)\n\n"
        "Findings about lines NOT in this PR's diff: mark as `severity: info` AND set `\"out_of_scope\": true`.\n"
        "Findings introduced by a previous fix-round commit: mark as `severity: warning` AND set `\"introduced_by_prior_fix\": true`.\n\n"
        "Respond with a structured JSON verdict only:\n"
        "```json\n"
        "{\n"
        '  "verdict": "pass|fail|blocked",\n'
        '  "findings": [{"severity": "error|warning|info", "message": "...", "out_of_scope": false, "introduced_by_prior_fix": false}],\n'
        '  "residual_risk": "description of remaining risks or null",\n'
        '  "rerun_required": false,\n'
        '  "rerun_reason": null\n'
        "}\n"
        "```\n"
    )
