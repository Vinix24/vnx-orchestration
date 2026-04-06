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


def _inline_file_contents(files: List[str], max_bytes: int) -> str:
    """Read and inline file contents up to max_bytes total."""
    content = ""
    bytes_used = 0
    for f in files:
        if not os.path.exists(f):
            continue
        remaining = max_bytes - bytes_used
        if remaining <= 0:
            break
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                chunk = fh.read(remaining)
            content += f"\n--- FILE: {f} ---\n{chunk}"
            bytes_used += len(chunk.encode("utf-8"))
        except OSError:
            continue
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
    file_content = _inline_file_contents(files, max_bytes)
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
    max_bytes = int(os.environ.get("VNX_GEMINI_MAX_PROMPT_BYTES", "100000"))
    return _inline_file_contents(files, max_bytes)


def build_codex_prompt(request_payload: Dict[str, Any]) -> str:
    """Build a review prompt for codex gate when no prompt is present."""
    files = request_payload.get("changed_files", [])
    branch = request_payload.get("branch", "")
    risk = request_payload.get("risk_class", "medium")
    pr = request_payload.get("pr_number", "")
    return (
        f"Review PR #{pr} on branch {branch} (risk: {risk}).\n"
        f"Changed files: {', '.join(files)}\n"
        "Read each file and provide a structured code review with findings.\n\n"
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
