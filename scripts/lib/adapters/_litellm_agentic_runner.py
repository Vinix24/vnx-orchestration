#!/usr/bin/env python3
"""_litellm_agentic_runner.py — Agentic tool-use loop for OpenAI-compatible models.

The one-shot sibling (`_litellm_runner.py`) sends a single chat completion and
streams the text back — it gives the model NO tools, so it can never write a
file, run a test, or iterate. That makes it structurally incapable of agentic
coding tasks (observed 2026-06-18: GLM lanes scored correctness 0 across every
cell because the harness, not the model, could not produce a deliverable).

This runner closes that gap. It drives a real agentic loop:

    completion(tools=[read_file, write_file, list_dir, run_command])
      -> execute the model's tool calls against the working directory
      -> feed results back -> repeat until the model stops or max_turns

It is the OpenAI-protocol counterpart of the claude-CLI harness that the
deepseek-harness lane reuses: same idea (let the model use tools and iterate),
different transport. Any litellm-reachable model (OpenRouter / z.AI GLM,
DeepSeek bare, etc.) gets a fair agentic lane through it.

Called by LiteLLMAgenticSpawn as: python -u _litellm_agentic_runner.py
Reads JSON from stdin:
    {"model": "openrouter/z-ai/glm-5.2", "prompt": "<task>", "cwd": "/abs/cell",
     "max_turns": 30, "max_tokens": 8192, "command_timeout": 120}
Emits NDJSON events (one JSON object per line) to stdout:
    {"event_type": "text", "content": "..."}
    {"event_type": "tool_use", "name": "write_file", "arguments": {...}}
    {"event_type": "tool_result", "name": "...", "is_error": bool, "content": "..."}
    {"event_type": "usage_complete", "usage": {"prompt_tokens": N, "completion_tokens": M}}
    {"event_type": "complete", "turns": N, "stop_reason": "..."}
    {"error_type": "...", "message": "..."}                       (on failure)

Exit codes:
  0 — success (model finished or hit max_turns with work done)
  1 — credentials / authentication error
  2 — other error (import failure, service unavailable, bad input, etc.)

SECURITY: all tool paths resolve INSIDE cwd; traversal escapes are refused.
run_command executes in cwd with a bounded timeout.

BILLING SAFETY: No Anthropic SDK imports. Uses the litellm library only.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_EXIT_OK = 0
_EXIT_CREDS = 1
_EXIT_ERR = 2

# Required env var per provider prefix (mirrors _litellm_runner.py).
_PROVIDER_KEY_REQS: dict = {
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

_DEFAULT_MAX_TURNS = 30
_DEFAULT_MAX_TOKENS = 8192
_DEFAULT_COMMAND_TIMEOUT = 120
_MAX_TOOL_RESULT_CHARS = 16000  # cap fed-back tool output so context stays bounded
_MAX_API_RETRIES = 4            # transient completion() errors (rate-limit / 5xx / credits) get backed-off, not fatal
_RETRY_BASE_DELAY = 2.0         # seconds; exponential backoff base
_MAX_TOOL_NUDGES = 2            # times to nudge a model that stops WITHOUT using the tools (GLM-5.2 explains instead of writing)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file. Path is relative to the working directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a file with the given content. Path is relative "
                "to the working directory; parent directories are created as needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries of a directory. Path is relative to the working directory (default '.').",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command in the working directory and return its stdout, "
                "stderr and exit code. Use this to run tests or inspect the project."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]

_SYSTEM_PROMPT = (
    "You are an autonomous coding agent operating inside a single working "
    "directory. You complete the task by USING THE TOOLS — do not just describe "
    "what to do, actually do it. Read the files you need, write the deliverable "
    "files, and run the tests or commands required to verify your work. When the "
    "task is fully done and verified, reply with a short final summary and STOP "
    "calling tools. Never ask the user questions; make reasonable assumptions and "
    "proceed."
)


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _provider_prefix(model: str) -> str:
    return model.split("/")[0] if "/" in model else ""


def _validate_provider_key(model: str) -> tuple[bool, str]:
    prefix = _provider_prefix(model)
    key_env = _PROVIDER_KEY_REQS.get(prefix)
    if key_env and not os.environ.get(key_env):
        return False, f"missing required env var {key_env!r} for provider '{prefix}'"
    return True, ""


def _safe_path(cwd: Path, rel: str) -> Path:
    """Resolve rel under cwd; raise ValueError on traversal escape or absolute path."""
    candidate = (cwd / rel).resolve()
    try:
        candidate.relative_to(cwd)
    except ValueError as exc:
        raise ValueError(f"path escapes working directory: {rel!r}") from exc
    return candidate


def _tool_read_file(cwd: Path, args: dict) -> str:
    path = _safe_path(cwd, str(args.get("path", "")))
    if not path.is_file():
        return f"ERROR: file not found: {args.get('path')!r}"
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        return text[:_MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"
    return text


def _tool_write_file(cwd: Path, args: dict) -> str:
    path = _safe_path(cwd, str(args.get("path", "")))
    content = args.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} bytes to {args.get('path')}"


def _tool_list_dir(cwd: Path, args: dict) -> str:
    path = _safe_path(cwd, str(args.get("path", ".") or "."))
    if not path.is_dir():
        return f"ERROR: not a directory: {args.get('path')!r}"
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
    return "\n".join(entries) if entries else "(empty)"


def _tool_run_command(cwd: Path, args: dict, timeout: int) -> str:
    command = str(args.get("command", ""))
    if not command.strip():
        return "ERROR: empty command"
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    out = (proc.stdout or "")[-_MAX_TOOL_RESULT_CHARS:]
    err = (proc.stderr or "")[-_MAX_TOOL_RESULT_CHARS:]
    return f"exit_code={proc.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


def _execute_tool(name: str, args: dict, cwd: Path, command_timeout: int) -> tuple[str, bool]:
    """Return (result_text, is_error)."""
    try:
        if name == "read_file":
            r = _tool_read_file(cwd, args)
        elif name == "write_file":
            r = _tool_write_file(cwd, args)
        elif name == "list_dir":
            r = _tool_list_dir(cwd, args)
        elif name == "run_command":
            r = _tool_run_command(cwd, args, command_timeout)
        else:
            return f"ERROR: unknown tool {name!r}", True
        return r, r.startswith("ERROR:")
    except Exception as exc:  # noqa: BLE001 — surface any tool failure to the model
        return f"ERROR: {type(exc).__name__}: {exc}", True


def _accumulate_usage(usage: Any, totals: dict) -> None:
    if usage is None:
        return
    if hasattr(usage, "model_dump"):
        u = usage.model_dump()
    elif hasattr(usage, "dict"):
        u = usage.dict()
    elif isinstance(usage, dict):
        u = usage
    else:
        return
    totals["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
    totals["completion_tokens"] += int(u.get("completion_tokens") or 0)


def _parse_tool_arguments(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _classify_error(msg: str) -> tuple[str, int]:
    low = msg.lower()
    if any(k in low for k in ("authentication", "auth", "credentials", "apikey", "api key", "unauthorized", "forbidden")):
        return "credentials_missing", _EXIT_CREDS
    if any(k in low for k in ("unavailable", "connection", "timeout", "unreachable", "refused")):
        return "service_unavailable", _EXIT_ERR
    return "completion_error", _EXIT_ERR


def _is_retryable(msg: str) -> bool:
    """A transient completion() error worth a backoff-retry (NOT an auth/credential failure).

    The single biggest cause of GLM-lane immediate-exits was a one-shot OpenRouter
    402 ("requested N tokens, can only afford M — adjust the key's daily limit") or a
    429/5xx killing the whole run on turn 1 with zero retries. Those are transient;
    auth failures are not.
    """
    low = msg.lower()
    if any(k in low for k in ("authentication", "unauthorized", "forbidden", "api key", "apikey")):
        return False
    return any(k in low for k in (
        "rate limit", "rate_limit", "429", " 500", "502", "503", "504", "overloaded",
        "timeout", "timed out", "connection", "unavailable", "temporarily",
        "more credits", "fewer max_tokens", "try again", "please retry", "retry",
    ))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:  # noqa: BLE001
        _emit({"error_type": "runner_error", "message": f"stdin parse error: {exc}"})
        return _EXIT_ERR

    model = payload.get("model", "")
    prompt = payload.get("prompt", "")
    cwd_raw = payload.get("cwd", "")
    max_turns = int(payload.get("max_turns") or _DEFAULT_MAX_TURNS)
    max_tokens = int(payload.get("max_tokens") or _DEFAULT_MAX_TOKENS)
    command_timeout = int(payload.get("command_timeout") or _DEFAULT_COMMAND_TIMEOUT)

    if not model:
        _emit({"error_type": "runner_error", "message": "model field required"})
        return _EXIT_ERR
    if not prompt:
        _emit({"error_type": "runner_error", "message": "prompt field required"})
        return _EXIT_ERR
    if not cwd_raw or not Path(cwd_raw).is_dir():
        _emit({"error_type": "runner_error", "message": f"cwd must be an existing directory: {cwd_raw!r}"})
        return _EXIT_ERR
    cwd = Path(cwd_raw).resolve()

    ok, err_msg = _validate_provider_key(model)
    if not ok:
        _emit({"error_type": "credentials_missing", "message": err_msg})
        return _EXIT_CREDS

    try:
        import litellm  # noqa: PLC0415
    except ImportError as exc:
        _emit({"error_type": "runner_error", "message": f"litellm not installed: {exc}"})
        return _EXIT_ERR

    logging.getLogger("litellm").setLevel(logging.CRITICAL)
    litellm.suppress_debug_info = True
    api_key = os.environ.get(_PROVIDER_KEY_REQS.get(_provider_prefix(model), ""), None)

    messages: list = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    totals = {"prompt_tokens": 0, "completion_tokens": 0}
    stop_reason = "max_turns"
    turns = 0
    nudges_left = _MAX_TOOL_NUDGES

    for turn in range(max_turns):
        turns = turn + 1
        resp = None
        for attempt in range(_MAX_API_RETRIES + 1):
            try:
                resp = litellm.completion(
                    model=model, messages=messages, tools=_TOOLS, tool_choice="auto",
                    max_tokens=max_tokens, api_key=api_key,
                )
                break
            except Exception as exc:  # noqa: BLE001 — surface/retry any completion failure
                err_type, code = _classify_error(str(exc))
                if not _is_retryable(str(exc)) or attempt >= _MAX_API_RETRIES:
                    _emit({"error_type": err_type, "message": str(exc)})
                    return code
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                _emit({"event_type": "retry", "attempt": attempt + 1,
                       "delay_s": round(delay, 1), "message": str(exc)[:200]})
                time.sleep(delay)

        _accumulate_usage(getattr(resp, "usage", None), totals)
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, "tool_calls", None) or []
        content = msg.content or ""

        if content:
            _emit({"event_type": "text", "content": content})

        # Record the assistant turn (with any tool calls) so the model has history.
        assistant_entry: dict = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
        messages.append(assistant_entry)

        if not tool_calls:
            # Model stopped without calling tools. GLM-5.2 especially tends to EXPLAIN
            # the solution instead of writing it, which left cells with no deliverable
            # (correctness 0 by construction). Nudge it to actually USE the tools before
            # accepting a no-op exit; bounded so a genuinely-finished model still stops.
            if nudges_left > 0 and turn < max_turns - 1:
                nudges_left -= 1
                messages.append({"role": "user", "content": (
                    "You stopped without using the tools. Do not just describe the "
                    "solution — use write_file to create the required deliverable file(s) "
                    "now, then run the tests to verify your work. Continue."
                )})
                _emit({"event_type": "nudge", "remaining": nudges_left})
                continue
            stop_reason = choice.finish_reason or "stop"
            break

        for tc in tool_calls:
            name = tc.function.name
            args = _parse_tool_arguments(tc.function.arguments)
            _emit({"event_type": "tool_use", "name": name, "arguments": args})
            result, is_error = _execute_tool(name, args, cwd, command_timeout)
            _emit({"event_type": "tool_result", "name": name, "is_error": is_error,
                   "content": result[:2000]})
            messages.append({
                "role": "tool", "tool_call_id": tc.id, "name": name,
                "content": result,
            })

    _emit({"event_type": "usage_complete", "usage": totals})
    _emit({"event_type": "complete", "turns": turns, "stop_reason": stop_reason})
    return _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
