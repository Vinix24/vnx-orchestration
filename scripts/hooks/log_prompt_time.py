#!/usr/bin/env python3
"""UserPromptSubmit hook: record each prompt with its derived class and, when unambiguous, its delivery key.

Never interferes (ADR-056, zelfst PR-8b): prints nothing on stdout (for UserPromptSubmit stdout joins
the session context), always exits 0, does no network, git or DB work, and stores the prompt's length
and sha256 but never its text. The rules live in ``scripts/lib/prompt_attribution.py``.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))


def run(raw: str) -> bool:
    """Attribute and store one hook payload; ``False`` when nothing was written."""
    import prompt_attribution as pa

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return False
    prompt = payload.get("prompt")
    prompt = prompt if isinstance(prompt, str) else ""
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    env = dict(os.environ)
    attribution = pa.attribute(prompt, env, cwd)
    state = pa.central_state_dir(attribution.project_id)
    if state is None:
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    record = pa.build_record(attribution, session_id=payload.get("session_id"), prompt=prompt, ts_utc=stamp)
    return pa.append_record(state, record)


def main() -> int:
    try:
        run(sys.stdin.read())
    except Exception as exc:  # vnx-silent-except: a hook must never block the prompt; the failure goes to stderr
        sys.stderr.write(f"log_prompt_time: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
