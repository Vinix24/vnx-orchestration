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
        if any(kw in msg_lower for kw in ("authentication", "auth", "credentials", "apikey", "api key", "unauthorized", "forbidden")):
            _emit({"error_type": "credentials_missing", "message": msg})
            return _EXIT_CREDS
        if any(kw in msg_lower for kw in ("unavailable", "connection", "timeout", "unreachable", "refused")):
            _emit({"error_type": "service_unavailable", "message": msg})
            return _EXIT_ERR
        _emit({"error_type": "completion_error", "message": msg})
        return _EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
