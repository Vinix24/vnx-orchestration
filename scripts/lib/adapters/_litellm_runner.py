#!/usr/bin/env python3
"""_litellm_runner.py — One-shot LiteLLM completion subprocess helper.

Called by LiteLLMAdapter as: python -u _litellm_runner.py
Reads JSON from stdin: {"model": "bedrock/claude-sonnet-4-6", "messages": [...]}
Emits OpenAI-shaped NDJSON chunks (one JSON object per line) to stdout.

Exit codes:
  0 — success
  1 — credentials / authentication error
  2 — other error (import failure, service unavailable, etc.)

BILLING SAFETY: No Anthropic SDK imports. Uses litellm library only.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

log = logging.getLogger(__name__)

_EXIT_OK = 0
_EXIT_CREDS = 1
_EXIT_ERR = 2

# Required env var per provider prefix (deepseek/*, moonshot/*, openrouter/*, etc.)
_PROVIDER_KEY_REQS: dict = {
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",  # z.AI via OpenRouter (PR-7.3)
}

def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _provider_prefix(model: str) -> str:
    """Return the provider prefix from a LiteLLM model string (e.g. 'deepseek' from 'deepseek/v3.2')."""
    return model.split("/")[0] if "/" in model else ""


def _validate_provider_key(model: str) -> tuple[bool, str]:
    """Return (ok, error_msg). ok=False when a required API key is absent."""
    prefix = _provider_prefix(model)
    key_env = _PROVIDER_KEY_REQS.get(prefix)
    if key_env and not os.environ.get(key_env):
        return False, f"missing required env var {key_env!r} for provider '{prefix}'"
    return True, ""


def _completion_kwargs(model: str) -> dict:
    """Extra keyword args for litellm.completion — always request usage in stream."""
    return {"stream_options": {"include_usage": True}}


def _emit_usage(usage: object) -> None:
    """Emit a usage_complete event carrying token counts."""
    if hasattr(usage, "model_dump"):
        usage_dict = usage.model_dump()
    elif hasattr(usage, "dict"):
        usage_dict = usage.dict()
    else:
        try:
            usage_dict = dict(usage)  # type: ignore[call-overload]
        except AttributeError as e:
            log.warning("_litellm_runner: usage serialization fallback: %s", e)
            usage_dict = {"input_tokens": 0, "output_tokens": 0, "usage_serialization_failed": True}
    _emit({"event_type": "usage_complete", "usage": usage_dict})


def _extract_status_code(exc: Exception) -> Optional[int]:
    """Try to extract an HTTP status code from a litellm / httpx exception.

    litellm wraps provider errors in various exception types (e.g.
    litellm.exceptions.AuthenticationError, APIError, or raw httpx.HTTPStatusError).
    The status code may be on the exception object itself (.status_code) or on a
    nested response attribute.
    """
    # Direct attribute
    for attr in ("status_code",):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and 100 <= val <= 599:
            return val

    # Nested response.status_code (httpx, requests patterns)
    response = getattr(exc, "response", None)
    if response is not None:
        val = getattr(response, "status_code", None)
        if isinstance(val, int) and 100 <= val <= 599:
            return val

    # Sometimes litellm stores it on a nested litellm_response or original_exception
    for nested_attr in ("litellm_response", "original_exception"):
        nested = getattr(exc, nested_attr, None)
        if nested is not None:
            val = getattr(nested, "status_code", None)
            if isinstance(val, int) and 100 <= val <= 599:
                return val
            resp = getattr(nested, "response", None)
            if resp is not None:
                val = getattr(resp, "status_code", None)
                if isinstance(val, int) and 100 <= val <= 599:
                    return val

    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:
        _emit({"error_type": "runner_error", "message": f"stdin parse error: {exc}"})
        return _EXIT_ERR

    model = payload.get("model", "")
    messages = payload.get("messages", [])

    if not model:
        _emit({"error_type": "runner_error", "message": "model field required"})
        return _EXIT_ERR

    ok, err_msg = _validate_provider_key(model)
    if not ok:
        _emit({"error_type": "credentials_missing", "message": err_msg})
        return _EXIT_CREDS

    try:
        import litellm  # noqa: PLC0415
    except ImportError as exc:
        _emit({"error_type": "runner_error", "message": f"litellm not installed: {exc}"})
        return _EXIT_ERR

    # Silence litellm's own logging to avoid polluting stdout
    logging.getLogger("litellm").setLevel(logging.CRITICAL)
    litellm.suppress_debug_info = True

    extra_kwargs = _completion_kwargs(model)
    usage_data = None

    try:
        response = litellm.completion(model=model, messages=messages, stream=True, **extra_kwargs)
        for chunk in response:
            if hasattr(chunk, "usage") and chunk.usage:
                usage_data = chunk.usage
            try:
                if hasattr(chunk, "model_dump"):
                    obj = chunk.model_dump()
                elif hasattr(chunk, "dict"):
                    obj = chunk.dict()
                else:
                    obj = dict(chunk)
                _emit(obj)
            except Exception as exc:
                _emit({"error_type": "serialize_error", "message": str(exc)})
        if usage_data is not None:
            _emit_usage(usage_data)
        return _EXIT_OK

    except Exception as exc:
        msg = str(exc)
        msg_lower = msg.lower()

        # Extract HTTP status code from litellm/httpx exceptions when available.
        # litellm wraps provider errors in exceptions that often carry a
        # status_code attribute (int) or embed it in the message string.
        status_code = _extract_status_code(exc)

        # Determine error_type from keywords + status code.
        if (
            any(kw in msg_lower for kw in ("authentication", "auth", "credentials",
                                             "apikey", "api key", "unauthorized", "forbidden"))
            or status_code in (401, 403)
        ):
            payload: dict = {"error_type": "credentials_missing", "message": msg}
            if status_code is not None:
                payload["status_code"] = status_code
            _emit(payload)
            return _EXIT_CREDS
        if any(kw in msg_lower for kw in ("unavailable", "connection", "timeout", "unreachable", "refused")):
            payload = {"error_type": "service_unavailable", "message": msg}
            if status_code is not None:
                payload["status_code"] = status_code
            _emit(payload)
            return _EXIT_ERR
        payload = {"error_type": "completion_error", "message": msg}
        if status_code is not None:
            payload["status_code"] = status_code
        _emit(payload)
        return _EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
