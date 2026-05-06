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
import sys

_EXIT_OK = 0
_EXIT_CREDS = 1
_EXIT_ERR = 2


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


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

    try:
        import litellm  # noqa: PLC0415
    except ImportError as exc:
        _emit({"error_type": "runner_error", "message": f"litellm not installed: {exc}"})
        return _EXIT_ERR

    # Silence litellm's own logging to avoid polluting stdout
    import logging
    logging.getLogger("litellm").setLevel(logging.CRITICAL)
    litellm.suppress_debug_info = True

    try:
        response = litellm.completion(model=model, messages=messages, stream=True)
        for chunk in response:
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
